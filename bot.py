import os
import asyncio
import logging
import re
from datetime import datetime
from html import escape

from pymongo import MongoClient

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.constants import (
    ChatMemberStatus,
)

from telegram.error import (
    TelegramError,
    Forbidden,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
LOGS_GROUP_ID = os.getenv("LOGS_GROUP_ID")

OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip()
}


# =========================================================
# CONFIG CHECK
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing")

if not OWNER_IDS:
    raise RuntimeError("OWNER_IDS is missing")

if not LOGS_GROUP_ID:
    raise RuntimeError("LOGS_GROUP_ID is missing")

LOGS_GROUP_ID = int(LOGS_GROUP_ID)


# =========================================================
# SETTINGS
# =========================================================

ANTISPAM_ENABLED = True

# 10 messages in 5 seconds
ANTISPAM_LIMIT = 10
ANTISPAM_WINDOW = 5

# Maximum remembered timestamps per user
ANTISPAM_MEMORY = 30


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# MONGODB
# =========================================================

mongo = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=10000,
)

db = mongo["global_ban_bot"]

chats_col = db["chats"]
users_col = db["users"]

global_bans_col = db["global_bans"]
global_mutes_col = db["global_mutes"]

admins_col = db["admins"]
ban_words_col = db["ban_words"]
logged_links_col = db["logged_links"]
settings_col = db["settings"]


# =========================================================
# INDEXES
# =========================================================

# Username lookup
users_col.create_index(
    "username",
    sparse=True,
)

# Global ban words
ban_words_col.create_index(
    "word",
    unique=True,
)

# Unique link logging
logged_links_col.create_index(
    "normalized_url",
    unique=True,
)

# IMPORTANT:
# DO NOT create an explicit unique index on "_id".
# MongoDB automatically creates the "_id_" unique index.
#
# OLD / WRONG:
# admins_col.create_index("_id", unique=True)
#
# It has intentionally been removed.


# =========================================================
# GLOBAL IN-MEMORY DATA
# =========================================================

spam_tracker = {}
ban_words_cache = set()


# =========================================================
# HELPERS
# =========================================================

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_admin(user_id: int) -> bool:

    if is_owner(user_id):
        return True

    return (
        admins_col.find_one(
            {"_id": user_id},
            {"_id": 1},
        )
        is not None
    )


def save_user(user):

    if not user:
        return

    username = (
        user.username.lower()
        if user.username
        else None
    )

    users_col.update_one(
        {"_id": user.id},
        {
            "$set": {
                "username": username,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


def save_chat(chat):

    if not chat:
        return

    chats_col.update_one(
        {"_id": chat.id},
        {
            "$set": {
                "title": chat.title or "",
                "username": chat.username or "",
                "type": chat.type,
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


def remove_chat(chat_id):

    chats_col.delete_one(
        {"_id": chat_id}
    )


def resolve_user(value: str):

    if not value:
        return None

    value = value.strip()

    try:
        return int(value)
    except ValueError:
        pass

    username = value.lstrip("@").lower()

    user = users_col.find_one(
        {"username": username},
        {"_id": 1},
    )

    if user:
        return int(user["_id"])

    return None


def get_reply_user(update: Update):

    if not update.message:
        return None

    replied = update.message.reply_to_message

    if not replied:
        return None

    return replied.from_user


def get_user_mention(
    user_id: int,
    username: str = "",
    first_name: str = "User",
):

    if username:
        return f"@{escape(username.lstrip('@'))}"

    safe_name = escape(
        first_name or "User"
    )

    return (
        f'<a href="tg://user?id={user_id}">'
        f"{safe_name}"
        f"</a>"
    )


def current_time():

    return datetime.now().strftime(
        "%d %b %Y • %H:%M:%S"
    )


async def bot_is_admin(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):

    try:

        me = await context.bot.get_me()

        member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=me.id,
        )

        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )

    except TelegramError as e:

        logger.warning(
            "Admin check failed | %s | %s",
            chat_id,
            e,
        )

        return False


def get_target_from_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if context.args:

        user_id = resolve_user(
            context.args[0]
        )

        if user_id is not None:
            return user_id

    reply_user = get_reply_user(update)

    if reply_user:
        return reply_user.id

    return None


def get_reason(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not context.args:
        return ""

    first = context.args[0]

    if resolve_user(first) is not None:

        return " ".join(
            context.args[1:]
        ).strip()

    if get_reply_user(update):

        return " ".join(
            context.args
        ).strip()

    return ""


def get_target_info(
    user_id: int,
    update: Update,
):

    reply_user = get_reply_user(update)

    target_data = users_col.find_one(
        {"_id": user_id}
    )

    if reply_user:

        save_user(reply_user)

        return (
            reply_user.username or "",
            reply_user.first_name or "User",
        )

    if target_data:

        return (
            target_data.get("username") or "",
            target_data.get("first_name") or "User",
        )

    return "", "User"


# =========================================================
# GLOBAL BAN
# =========================================================

def add_global_ban(
    user_id: int,
    username: str = "",
    first_name: str = "",
    reason: str = "",
    admin_id: int = 0,
):

    global_bans_col.update_one(
        {"_id": user_id},
        {
            "$set": {
                "username": username or "",
                "first_name": first_name or "",
                "reason": reason or "",
                "admin_id": admin_id,
                "created_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


def remove_global_ban(user_id: int):

    global_bans_col.delete_one(
        {"_id": user_id}
    )


def is_global_banned(user_id: int) -> bool:

    return (
        global_bans_col.find_one(
            {"_id": user_id},
            {"_id": 1},
        )
        is not None
    )


# =========================================================
# GLOBAL MUTE
# =========================================================

def add_global_mute(
    user_id: int,
    username: str = "",
    first_name: str = "",
    reason: str = "",
    admin_id: int = 0,
):

    global_mutes_col.update_one(
        {"_id": user_id},
        {
            "$set": {
                "username": username or "",
                "first_name": first_name or "",
                "reason": reason or "",
                "admin_id": admin_id,
                "created_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )


def remove_global_mute(user_id: int):

    global_mutes_col.delete_one(
        {"_id": user_id}
    )


def is_global_muted(user_id: int) -> bool:

    return (
        global_mutes_col.find_one(
            {"_id": user_id},
            {"_id": 1},
        )
        is not None
    )


# =========================================================
# ACTION LOG
# =========================================================

async def send_action_log(
    context,
    action,
    target_id,
    target_username,
    target_first_name,
    admin,
    reason,
    total,
    success,
    skipped,
    failed,
):

    target_mention = get_user_mention(
        target_id,
        target_username,
        target_first_name,
    )

    admin_display = (
        f"@{escape(admin.username)}"
        if admin.username
        else escape(admin.full_name or "Admin")
    )

    if action == "GBAN":

        title = "🔨 <b>GLOBAL BAN</b>"
        result_name = "Banned"

    elif action == "UNGBAN":

        title = "♻️ <b>GLOBAL UNGBAN</b>"
        result_name = "Unbanned"

    elif action == "GMUTE":

        title = "🔇 <b>GLOBAL MUTE</b>"
        result_name = "Muted"

    else:

        title = "🔊 <b>GLOBAL UNMUTE</b>"
        result_name = "Unmuted"

    text = (
        f"{title}\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>Target ID:</b> "
        f"<code>{target_id}</code>\n\n"

        f"👮 <b>Action By:</b> {admin_display}\n"
        f"🆔 <b>Admin ID:</b> "
        f"<code>{admin.id}</code>\n\n"

        f"📝 <b>Reason:</b> "
        f"{escape(reason or 'Not specified')}\n\n"

        f"📦 <b>Total Chats:</b> {total}\n"
        f"✅ <b>{result_name}:</b> {success}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}\n\n"

        f"🕐 <b>Time:</b> {current_time()}"
    )

    try:

        await context.bot.send_message(
            chat_id=LOGS_GROUP_ID,
            text=text,
            parse_mode="HTML",
        )

    except TelegramError as e:

        logger.error(
            "LOG SEND FAILED: %s",
            e,
        )


# =========================================================
# BAN WORD SYSTEM
# =========================================================

def normalize_ban_word(text: str):

    if not text:
        return ""

    text = text.lower().strip()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text


def load_ban_words():

    global ban_words_cache

    ban_words_cache = {
        normalize_ban_word(
            doc.get("word", "")
        )
        for doc in ban_words_col.find({})
        if doc.get("word")
    }

    ban_words_cache.discard("")

    logger.info(
        "BAN WORDS LOADED: %s",
        len(ban_words_cache),
    )


def add_ban_word(word: str, admin_id: int):

    normalized = normalize_ban_word(word)

    if not normalized:
        return False

    ban_words_col.update_one(
        {"word": normalized},
        {
            "$set": {
                "word": normalized,
                "added_by": admin_id,
                "created_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )

    ban_words_cache.add(normalized)

    return True


def remove_ban_word(word: str):

    normalized = normalize_ban_word(word)

    result = ban_words_col.delete_one(
        {"word": normalized}
    )

    ban_words_cache.discard(normalized)

    return result.deleted_count > 0


def message_has_ban_word(text: str):

    if not text or not ban_words_cache:
        return False

    normalized_text = text.lower()

    for word in ban_words_cache:

        pattern = (
            r"(?<!\w)"
            + re.escape(word)
            + r"(?!\w)"
        )

        if re.search(
            pattern,
            normalized_text,
            re.IGNORECASE,
        ):
            return True

    return False


# =========================================================
# LINK DETECTION
# =========================================================

URL_PATTERN = re.compile(
    r"(?i)\b("
    r"(?:https?://|www\.)"
    r"[^\s<>()]+"
    r"|"
    r"(?:t\.me|telegram\.me|telegram\.dog)"
    r"/[^\s<>()]+"
    r")"
)


DOMAIN_PATTERN = re.compile(
    r"(?i)\b"
    r"(?:[a-z0-9-]+\.)+"
    r"[a-z]{2,}"
    r"(?:/[^\s<>()]*)?"
)


EMAIL_PATTERN = re.compile(
    r"(?i)\b"
    r"[a-z0-9._%+-]+"
    r"@"
    r"[a-z0-9.-]+\."
    r"[a-z]{2,}"
    r"\b"
)


PHONE_PATTERN = re.compile(
    r"(?<!\w)"
    r"(?:\+?\d[\d\s().-]{7,}\d)"
    r"(?!\w)"
)


TELEGRAM_INVITE_PATTERN = re.compile(
    r"(?i)"
    r"(?:https?://)?"
    r"(?:www\.)?"
    r"(?:t\.me|telegram\.me)"
    r"/"
    r"(?:\+|joinchat/)"
    r"[A-Za-z0-9_-]+"
)


def extract_links(text: str):

    if not text:
        return []

    found = []

    for match in URL_PATTERN.findall(text):

        if match not in found:
            found.append(match)

    for match in DOMAIN_PATTERN.findall(text):

        if match not in found:
            found.append(match)

    return found


def normalize_url(url: str):

    if not url:
        return ""

    url = url.strip()

    url = url.rstrip(
        ".,!?;:)]}>\"'"
    )

    url = url.lower()

    if url.startswith("www."):

        url = "https://" + url

    elif not re.match(
        r"^[a-z]+://",
        url,
        re.IGNORECASE,
    ):

        if re.match(
            r"^(t\.me|telegram\.me|telegram\.dog)/",
            url,
            re.IGNORECASE,
        ):

            url = "https://" + url

    url = url.rstrip("/")

    return url


def contains_link(text: str):

    return bool(
        extract_links(text)
    )


def contains_email(text: str):

    if not text:
        return False

    return bool(
        EMAIL_PATTERN.search(text)
    )


def contains_phone(text: str):

    if not text:
        return False

    return bool(
        PHONE_PATTERN.search(text)
    )


def contains_telegram_invite(text: str):

    if not text:
        return False

    return bool(
        TELEGRAM_INVITE_PATTERN.search(text)
    )


# =========================================================
# LINK LOG
# =========================================================

async def log_deleted_link(
    context,
    link: str,
    user,
    chat,
):

    normalized = normalize_url(link)

    if not normalized:
        return

    try:

        result = logged_links_col.update_one(
            {
                "normalized_url": normalized
            },
            {
                "$setOnInsert": {
                    "normalized_url": normalized,
                    "original_url": link,
                    "first_user_id": user.id,
                    "first_username": user.username or "",
                    "first_name": user.full_name or "",
                    "first_chat_id": chat.id,
                    "first_chat_title": chat.title or "",
                    "created_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

        if result.matched_count > 0:
            return

    except Exception as e:

        if "duplicate" in str(e).lower():
            return

        logger.warning(
            "LINK LOG DB ERROR: %s",
            e,
        )

        return

    user_display = (
        f"@{escape(user.username)}"
        if user.username
        else escape(user.full_name or "User")
    )

    chat_title = escape(
        chat.title or "Unknown Group"
    )

    text = (
        "🔗 <b>LINK DETECTED</b>\n\n"

        f"🔗 <b>Link:</b>\n"
        f"<code>{escape(link)}</code>\n\n"

        f"👤 <b>User:</b> {user_display}\n"
        f"🆔 <b>User ID:</b> "
        f"<code>{user.id}</code>\n\n"

        f"👥 <b>Group:</b> {chat_title}\n"
        f"🆔 <b>Group ID:</b> "
        f"<code>{chat.id}</code>\n\n"

        f"🕐 <b>Time:</b> {current_time()}"
    )

    try:

        await context.bot.send_message(
            chat_id=LOGS_GROUP_ID,
            text=text,
            parse_mode="HTML",
        )

    except TelegramError as e:

        logger.warning(
            "LINK LOG SEND FAILED: %s",
            e,
        )


# =========================================================
# ANTI SPAM
# =========================================================

def check_antispam(
    chat_id: int,
    user_id: int,
):

    if not ANTISPAM_ENABLED:
        return False

    now = datetime.utcnow().timestamp()

    key = (
        chat_id,
        user_id,
    )

    timestamps = spam_tracker.get(
        key,
        [],
    )

    timestamps = [
        t
        for t in timestamps
        if now - t <= ANTISPAM_WINDOW
    ]

    timestamps.append(now)

    timestamps = timestamps[
        -ANTISPAM_MEMORY:
    ]

    spam_tracker[key] = timestamps

    return len(timestamps) > ANTISPAM_LIMIT


def clear_spam_tracker(
    chat_id,
    user_id,
):

    spam_tracker.pop(
        (chat_id, user_id),
        None,
    )


# =========================================================
# MODERATION MESSAGE HANDLER
# =========================================================

async def moderation_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.type not in (
        "group",
        "supergroup",
    ):
        return

    if user.is_bot:
        return

    save_user(user)
    save_chat(chat)

    # -----------------------------------------------------
    # OWNER PROTECTED
    # -----------------------------------------------------

    if is_owner(user.id):
        return

    # -----------------------------------------------------
    # TELEGRAM GROUP ADMINS PROTECTED
    # -----------------------------------------------------

    try:

        member = await context.bot.get_chat_member(
            chat.id,
            user.id,
        )

        if member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):

            return

    except TelegramError:
        pass

    # -----------------------------------------------------
    # MESSAGE TEXT / CAPTION
    # -----------------------------------------------------

    text = (
        message.text
        or message.caption
        or ""
    )

    # -----------------------------------------------------
    # ANTI SPAM
    # -----------------------------------------------------

    if check_antispam(
        chat.id,
        user.id,
    ):

        try:

            await message.delete()

        except TelegramError as e:

            logger.debug(
                "SPAM DELETE FAILED | %s",
                e,
            )

        return

    # -----------------------------------------------------
    # BAN WORD
    # -----------------------------------------------------

    if message_has_ban_word(text):

        try:

            await message.delete()

        except TelegramError as e:

            logger.debug(
                "BAN WORD DELETE FAILED | %s",
                e,
            )

        return

    # -----------------------------------------------------
    # EMAIL
    # -----------------------------------------------------

    if contains_email(text):

        try:

            await message.delete()

        except TelegramError as e:

            logger.debug(
                "EMAIL DELETE FAILED | %s",
                e,
            )

        return

    # -----------------------------------------------------
    # PHONE
    # -----------------------------------------------------

    if contains_phone(text):

        try:

            await message.delete()

        except TelegramError as e:

            logger.debug(
                "PHONE DELETE FAILED | %s",
                e,
            )

        return

    # -----------------------------------------------------
    # TELEGRAM INVITE LINK
    # -----------------------------------------------------

    if contains_telegram_invite(text):

        links = extract_links(text)

        for link in links:

            await log_deleted_link(
                context,
                link,
                user,
                chat,
            )

        try:

            await message.delete()

        except TelegramError as e:

            logger.debug(
                "TELEGRAM INVITE DELETE FAILED | %s",
                e,
            )

        return

    # -----------------------------------------------------
    # ALL OTHER LINKS
    # -----------------------------------------------------

    if contains_link(text):

        links = extract_links(text)

        for link in links:

            await log_deleted_link(
                context,
                link,
                user,
                chat,
            )

        try:

            await message.delete()

        except TelegramError as e:

            logger.debug(
                "LINK DELETE FAILED | %s",
                e,
            )

        return


# =========================================================
# BOT MEMBERSHIP UPDATE
# =========================================================

async def bot_membership_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    event = update.my_chat_member

    if not event:
        return

    chat = update.effective_chat

    if not chat:
        return

    new_status = event.new_chat_member.status

    if new_status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):

        save_chat(chat)

        logger.info(
            "CHAT REGISTERED | %s | %s",
            chat.id,
            chat.title,
        )

    elif new_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.BANNED,
    ):

        remove_chat(chat.id)

        logger.info(
            "CHAT REMOVED | %s | %s",
            chat.id,
            chat.title,
        )


# =========================================================
# USER CHAT MEMBER UPDATE
# =========================================================

async def user_membership_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    event = update.chat_member

    if not event:
        return

    chat = update.effective_chat

    if not chat:
        return

    member = event.new_chat_member
    user = member.user

    if not user:
        return

    save_user(user)

    if user.is_bot:
        return

    new_status = member.status

    if new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):

        # -------------------------------------------------
        # GLOBAL BAN CHECK
        # -------------------------------------------------

        if is_global_banned(user.id):

            try:

                if await bot_is_admin(
                    context,
                    chat.id,
                ):

                    await context.bot.ban_chat_member(
                        chat_id=chat.id,
                        user_id=user.id,
                        revoke_messages=True,
                    )

                    logger.info(
                        "AUTO GBAN | User=%s | Chat=%s",
                        user.id,
                        chat.id,
                    )

                    return

            except Forbidden:

                logger.warning(
                    "AUTO GBAN FORBIDDEN | User=%s | Chat=%s",
                    user.id,
                    chat.id,
                )

            except TelegramError as e:

                logger.warning(
                    "AUTO GBAN FAILED | User=%s | Chat=%s | %s",
                    user.id,
                    chat.id,
                    e,
                )

        # -------------------------------------------------
        # GLOBAL MUTE CHECK
        # -------------------------------------------------

        if is_global_muted(user.id):

            mute_permissions = ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_manage_topics=False,
            )

            try:

                if await bot_is_admin(
                    context,
                    chat.id,
                ):

                    await context.bot.restrict_chat_member(
                        chat_id=chat.id,
                        user_id=user.id,
                        permissions=mute_permissions,
                    )

                    logger.info(
                        "AUTO GMUTE | User=%s | Chat=%s",
                        user.id,
                        chat.id,
                    )

            except Forbidden:

                logger.warning(
                    "AUTO GMUTE FORBIDDEN | User=%s | Chat=%s",
                    user.id,
                    chat.id,
                )

            except TelegramError as e:

                logger.warning(
                    "AUTO GMUTE FAILED | User=%s | Chat=%s | %s",
                    user.id,
                    chat.id,
                    e,
                )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if update.effective_user:

        save_user(
            update.effective_user
        )

    if not update.message:
        return

    text = (
        "🛡️ <b>GLOBAL BAN BOT</b>\n\n"

        "🔨 <code>/gban 123456789 reason</code>\n"
        "🔨 <code>/gban @username reason</code>\n"
        "🔨 <code>Reply → /gban reason</code>\n\n"

        "♻️ <code>/ungban 123456789</code>\n"
        "♻️ <code>/ungban @username</code>\n"
        "♻️ <code>Reply → /ungban</code>\n\n"

        "🔇 <code>/gmute 123456789 reason</code>\n"
        "🔇 <code>/gmute @username reason</code>\n"
        "🔇 <code>Reply → /gmute reason</code>\n\n"

        "🔊 <code>/ungmute 123456789</code>\n"
        "🔊 <code>/ungmute @username</code>\n"
        "🔊 <code>Reply → /ungmute</code>\n\n"

        "📋 <code>/chats</code>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# ADMIN PANEL
# =========================================================

def admin_panel_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚫 Ban Words",
                    callback_data="panel_banwords",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔗 Link Filter",
                    callback_data="panel_links",
                ),
                InlineKeyboardButton(
                    "⚡ Anti-Spam",
                    callback_data="panel_spam",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👑 Admin Management",
                    callback_data="panel_admins",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 Statistics",
                    callback_data="panel_stats",
                ),
            ],
        ]
    )


async def panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    if not is_admin(user.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(user)

    await update.message.reply_text(
        "🛡️ <b>GBAN ADMIN PANEL</b>\n\n"
        "Select an option:",
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )


# =========================================================
# CALLBACK QUERY
# =========================================================

async def panel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    # IMPORTANT:
    # global MUST be declared before using ANTISPAM_ENABLED
    # anywhere inside this function.

    global ANTISPAM_ENABLED

    query = update.callback_query

    if not query:
        return

    user = query.from_user

    # -----------------------------------------------------
    # AUTH CHECK BEFORE ANSWERING CALLBACK
    # -----------------------------------------------------

    if not is_admin(user.id):

        await query.answer(
            "❌ Not authorized.",
            show_alert=True,
        )

        return

    # Answer exactly once
    await query.answer()

    data = query.data

    # -----------------------------------------------------
    # MAIN PANEL
    # -----------------------------------------------------

    if data == "panel_home":

        await query.edit_message_text(
            "🛡️ <b>GBAN ADMIN PANEL</b>\n\n"
            "Select an option:",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )

        return

    # -----------------------------------------------------
    # BAN WORD PANEL
    # -----------------------------------------------------

    if data == "panel_banwords":

        count = ban_words_col.count_documents({})

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Add Ban Word",
                        callback_data="banword_add",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ Remove Ban Word",
                        callback_data="banword_remove",
                    ),
                    InlineKeyboardButton(
                        "📋 Word List",
                        callback_data="banword_list",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="panel_home",
                    ),
                ],
            ]
        )

        await query.edit_message_text(
            "🚫 <b>GLOBAL BAN WORDS</b>\n\n"
            f"📊 Total Words: <b>{count}</b>\n\n"
            "Words added here are automatically active "
            "in all registered groups.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        return

    # -----------------------------------------------------
    # ADD BAN WORD
    # -----------------------------------------------------

    if data == "banword_add":

        context.user_data["panel_action"] = "add_banword"

        await query.edit_message_text(
            "➕ <b>ADD GLOBAL BAN WORD</b>\n\n"
            "Send the word now.\n\n"
            "It will automatically apply to "
            "<b>all current and future groups</b>.\n\n"
            "Send /cancel to cancel.",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # REMOVE BAN WORD
    # -----------------------------------------------------

    if data == "banword_remove":

        context.user_data["panel_action"] = "remove_banword"

        await query.edit_message_text(
            "🗑️ <b>REMOVE GLOBAL BAN WORD</b>\n\n"
            "Send the word you want to remove.\n\n"
            "Send /cancel to cancel.",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # LIST BAN WORDS
    # -----------------------------------------------------

    if data == "banword_list":

        words = list(
            ban_words_col.find(
                {},
                {"word": 1},
            ).sort(
                "word",
                1,
            )
        )

        if not words:

            text = (
                "📋 <b>BAN WORD LIST</b>\n\n"
                "No ban words added."
            )

        else:

            lines = []

            for i, doc in enumerate(
                words,
                1,
            ):

                lines.append(
                    f"{i}. <code>"
                    f"{escape(doc['word'])}"
                    f"</code>"
                )

            text = (
                "📋 <b>GLOBAL BAN WORDS</b>\n\n"
                + "\n".join(lines)
            )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="panel_banwords",
                    )
                ]
            ]
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        return

    # -----------------------------------------------------
    # LINK PANEL
    # -----------------------------------------------------

    if data == "panel_links":

        count = logged_links_col.count_documents({})

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📊 Logged Links",
                        callback_data="link_count",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="panel_home",
                    )
                ],
            ]
        )

        await query.edit_message_text(
            "🔗 <b>GLOBAL LINK FILTER</b>\n\n"
            "Status: <b>ACTIVE</b> 🟢\n\n"
            "• All links → delete\n"
            "• Telegram links → delete\n"
            "• Invite links → delete\n"
            "• Email → delete\n"
            "• Phone numbers → delete\n\n"
            "🔇 Silent moderation\n"
            "📥 Deleted links are logged\n"
            "♻️ Duplicate links are never logged twice.\n\n"
            f"📊 Unique logged links: <b>{count}</b>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        return

    # -----------------------------------------------------
    # LINK COUNT
    # -----------------------------------------------------

    if data == "link_count":

        count = logged_links_col.count_documents({})

        await query.answer(
            f"Unique logged links: {count}",
            show_alert=True,
        )

        return

    # -----------------------------------------------------
    # ANTISPAM
    # -----------------------------------------------------

    if data == "panel_spam":

        status = (
            "🟢 ON"
            if ANTISPAM_ENABLED
            else "🔴 OFF"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 Enable",
                        callback_data="spam_on",
                    ),
                    InlineKeyboardButton(
                        "🔴 Disable",
                        callback_data="spam_off",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="panel_home",
                    )
                ],
            ]
        )

        await query.edit_message_text(
            "⚡ <b>ANTI-SPAM</b>\n\n"
            f"Status: <b>{status}</b>\n\n"
            f"Limit: <b>{ANTISPAM_LIMIT}</b> messages\n"
            f"Window: <b>{ANTISPAM_WINDOW}</b> seconds\n\n"
            "Spam messages are silently deleted.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        return

    # -----------------------------------------------------
    # SPAM ON
    # -----------------------------------------------------

    if data == "spam_on":

        ANTISPAM_ENABLED = True

        settings_col.update_one(
            {"_id": "antispam"},
            {
                "$set": {
                    "enabled": True
                }
            },
            upsert=True,
        )

        await query.edit_message_text(
            "⚡ <b>ANTI-SPAM ENABLED</b>\n\n"
            f"Limit: {ANTISPAM_LIMIT} messages / "
            f"{ANTISPAM_WINDOW} seconds.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Back",
                            callback_data="panel_spam",
                        )
                    ]
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # SPAM OFF
    # -----------------------------------------------------

    if data == "spam_off":

        ANTISPAM_ENABLED = False

        settings_col.update_one(
            {"_id": "antispam"},
            {
                "$set": {
                    "enabled": False
                }
            },
            upsert=True,
        )

        await query.edit_message_text(
            "⚡ <b>ANTI-SPAM DISABLED</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Back",
                            callback_data="panel_spam",
                        )
                    ]
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # ADMIN MANAGEMENT
    # -----------------------------------------------------

    if data == "panel_admins":

        if not is_owner(user.id):

            await query.answer(
                "Only Owner can manage admins.",
                show_alert=True,
            )

            return

        count = admins_col.count_documents({})

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Add Admin",
                        callback_data="admin_add",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "➖ Remove Admin",
                        callback_data="admin_remove",
                    ),
                    InlineKeyboardButton(
                        "👥 Admin List",
                        callback_data="admin_list",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="panel_home",
                    )
                ],
            ]
        )

        await query.edit_message_text(
            "👑 <b>ADMIN MANAGEMENT</b>\n\n"
            f"Current additional admins: <b>{count}</b>\n\n"
            "Only Owner can add/remove admins.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        return

    # -----------------------------------------------------
    # ADD ADMIN
    # -----------------------------------------------------

    if data == "admin_add":

        if not is_owner(user.id):
            return

        context.user_data["panel_action"] = "add_admin"

        await query.edit_message_text(
            "➕ <b>ADD ADMIN</b>\n\n"
            "Send the Telegram User ID.\n\n"
            "Example:\n"
            "<code>123456789</code>\n\n"
            "Send /cancel to cancel.",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # REMOVE ADMIN
    # -----------------------------------------------------

    if data == "admin_remove":

        if not is_owner(user.id):
            return

        context.user_data["panel_action"] = "remove_admin"

        await query.edit_message_text(
            "➖ <b>REMOVE ADMIN</b>\n\n"
            "Send the Telegram User ID.\n\n"
            "Send /cancel to cancel.",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # ADMIN LIST
    # -----------------------------------------------------

    if data == "admin_list":

        if not is_owner(user.id):
            return

        admins = list(
            admins_col.find({})
        )

        if not admins:

            text = (
                "👥 <b>ADMIN LIST</b>\n\n"
                "No additional admins."
            )

        else:

            lines = []

            for i, doc in enumerate(
                admins,
                1,
            ):

                admin_id = doc["_id"]

                user_data = users_col.find_one(
                    {"_id": admin_id}
                )

                if user_data:

                    name = (
                        user_data.get("first_name")
                        or "Admin"
                    )

                    username = (
                        user_data.get("username")
                        or ""
                    )

                    if username:

                        display = (
                            f"@{escape(username)}"
                        )

                    else:

                        display = escape(name)

                else:

                    display = "Admin"

                lines.append(
                    f"{i}. {display}\n"
                    f"🆔 <code>{admin_id}</code>"
                )

            text = (
                "👥 <b>ADMIN LIST</b>\n\n"
                + "\n\n".join(lines)
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Back",
                            callback_data="panel_admins",
                        )
                    ]
                ]
            ),
        )

        return

    # -----------------------------------------------------
    # STATISTICS
    # -----------------------------------------------------

    if data == "panel_stats":

        chats_count = chats_col.count_documents({})
        users_count = users_col.count_documents({})
        bans_count = global_bans_col.count_documents({})
        mutes_count = global_mutes_col.count_documents({})
        words_count = ban_words_col.count_documents({})
        links_count = logged_links_col.count_documents({})

        await query.edit_message_text(
            "📊 <b>GBAN BOT STATISTICS</b>\n\n"

            f"👥 Users: <b>{users_count}</b>\n"
            f"💬 Registered Groups: <b>{chats_count}</b>\n"
            f"🔨 Global Bans: <b>{bans_count}</b>\n"
            f"🔇 Global Mutes: <b>{mutes_count}</b>\n"
            f"🚫 Ban Words: <b>{words_count}</b>\n"
            f"🔗 Unique Logged Links: <b>{links_count}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Back",
                            callback_data="panel_home",
                        )
                    ]
                ]
            ),
        )

        return


# =========================================================
# PANEL TEXT INPUT
# =========================================================

async def panel_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    action = context.user_data.get(
        "panel_action"
    )

    if not action:
        return

    if not is_admin(user.id):

        context.user_data.pop(
            "panel_action",
            None,
        )

        return

    text = (
        message.text
        or ""
    ).strip()

    if not text:
        return

    # -----------------------------------------------------
    # ADD BAN WORD
    # -----------------------------------------------------

    if action == "add_banword":

        context.user_data.pop(
            "panel_action",
            None,
        )

        if add_ban_word(
            text,
            user.id,
        ):

            await message.reply_text(
                "✅ <b>Ban Word Added</b>\n\n"
                f"🚫 Word: <code>{escape(text)}</code>\n\n"
                "🌍 It is now active in "
                "<b>all current and future registered groups</b>.\n\n"
                "🗑️ Matching messages will be "
                "silently deleted.",
                parse_mode="HTML",
            )

        return

    # -----------------------------------------------------
    # REMOVE BAN WORD
    # -----------------------------------------------------

    if action == "remove_banword":

        context.user_data.pop(
            "panel_action",
            None,
        )

        if remove_ban_word(text):

            await message.reply_text(
                "✅ <b>Ban Word Removed</b>\n\n"
                f"🚫 Word: <code>{escape(text)}</code>",
                parse_mode="HTML",
            )

        else:

            await message.reply_text(
                "❌ Ban Word not found."
            )

        return

    # -----------------------------------------------------
    # ADD ADMIN
    # -----------------------------------------------------

    if action == "add_admin":

        context.user_data.pop(
            "panel_action",
            None,
        )

        if not is_owner(user.id):

            await message.reply_text(
                "❌ Owner only."
            )

            return

        try:

            admin_id = int(text)

        except ValueError:

            await message.reply_text(
                "❌ Invalid User ID."
            )

            return

        if is_owner(admin_id):

            await message.reply_text(
                "ℹ️ This user is already an Owner."
            )

            return

        admins_col.update_one(
            {"_id": admin_id},
            {
                "$set": {
                    "added_by": user.id,
                    "created_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )

        await message.reply_text(
            "✅ <b>Admin Added</b>\n\n"
            f"🆔 User ID: <code>{admin_id}</code>",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # REMOVE ADMIN
    # -----------------------------------------------------

    if action == "remove_admin":

        context.user_data.pop(
            "panel_action",
            None,
        )

        if not is_owner(user.id):

            await message.reply_text(
                "❌ Owner only."
            )

            return

        try:

            admin_id = int(text)

        except ValueError:

            await message.reply_text(
                "❌ Invalid User ID."
            )

            return

        if is_owner(admin_id):

            await message.reply_text(
                "❌ Owner cannot be removed."
            )

            return

        result = admins_col.delete_one(
            {"_id": admin_id}
        )

        if result.deleted_count:

            await message.reply_text(
                "✅ <b>Admin Removed</b>\n\n"
                f"🆔 User ID: <code>{admin_id}</code>",
                parse_mode="HTML",
            )

        else:

            await message.reply_text(
                "❌ Admin not found."
            )

        return


# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.pop(
        "panel_action",
        None,
    )

    if update.message:

        await update.message.reply_text(
            "❌ Cancelled."
        )


# =========================================================
# GLOBAL BAN
# =========================================================

async def gban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    admin = update.effective_user

    if not admin:
        return

    if not is_admin(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(admin)

    if not context.args and not get_reply_user(update):

        await update.message.reply_text(
            "Usage:\n\n"
            "/gban 123456789 Spam\n\n"
            "/gban @username Spam\n\n"
            "Reply ചെയ്ത്:\n"
            "/gban Spam"
        )

        return

    user_id = get_target_from_command(
        update,
        context,
    )

    if user_id is None:

        await update.message.reply_text(
            "❌ User not found.\n\n"
            "User ID / @username ഉപയോഗിക്കുക.\n"
            "അല്ലെങ്കിൽ User message reply ചെയ്യുക."
        )

        return

    if is_owner(user_id):

        await update.message.reply_text(
            "❌ Owner cannot be globally banned."
        )

        return

    reason = get_reason(
        update,
        context,
    )

    username, first_name = get_target_info(
        user_id,
        update,
    )

    add_global_ban(
        user_id=user_id,
        username=username,
        first_name=first_name,
        reason=reason,
        admin_id=admin.id,
    )

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "🛡️ Global ban saved.\n"
            "📭 No registered chats currently."
        )

        return

    target_mention = get_user_mention(
        user_id,
        username,
        first_name,
    )

    status_message = await update.message.reply_text(
        "🔨 <b>GLOBAL BAN STARTED...</b>\n\n"

        f"👤 Target: {target_mention}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent global ban saved.</b>\n"
        "New joins will also be automatically banned.\n\n"

        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    total = len(chats)
    banned = 0
    skipped = 0
    failed = 0

    for chat in chats:

        chat_id = chat["_id"]

        try:

            if not await bot_is_admin(
                context,
                chat_id,
            ):

                skipped += 1
                continue

            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                revoke_messages=True,
            )

            banned += 1

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "GBAN FAILED | User=%s | Chat=%s | %s",
                user_id,
                chat_id,
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "GBAN ERROR | %s",
                e,
            )

        await asyncio.sleep(0.15)

    result = (
        "🔨 <b>GLOBAL BAN COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent Global Ban: ON</b>\n"
        "🚫 Future joins: <b>AUTO BAN</b>\n\n"

        f"📦 <b>Total:</b> {total}\n"
        f"✅ <b>Banned:</b> {banned}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}"
    )

    await status_message.edit_text(
        result,
        parse_mode="HTML",
    )

    await send_action_log(
        context,
        "GBAN",
        user_id,
        username,
        first_name,
        admin,
        reason,
        total,
        banned,
        skipped,
        failed,
    )


# =========================================================
# GLOBAL UNGBAN
# =========================================================

async def ungban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    admin = update.effective_user

    if not admin:
        return

    if not is_admin(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(admin)

    if not context.args and not get_reply_user(update):

        await update.message.reply_text(
            "Usage:\n\n"
            "/ungban 123456789\n"
            "/ungban @username\n\n"
            "അല്ലെങ്കിൽ reply ചെയ്ത് /ungban"
        )

        return

    user_id = get_target_from_command(
        update,
        context,
    )

    if user_id is None:

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    reason = get_reason(
        update,
        context,
    )

    username, first_name = get_target_info(
        user_id,
        update,
    )

    remove_global_ban(user_id)

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "♻️ Global ban removed."
        )

        return

    target_mention = get_user_mention(
        user_id,
        username,
        first_name,
    )

    status_message = await update.message.reply_text(
        "♻️ <b>GLOBAL UNGBAN STARTED...</b>\n\n"

        f"👤 Target: {target_mention}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent Global Ban: OFF</b>\n\n"

        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    total = len(chats)
    unbanned = 0
    skipped = 0
    failed = 0

    for chat in chats:

        chat_id = chat["_id"]

        try:

            if not await bot_is_admin(
                context,
                chat_id,
            ):

                skipped += 1
                continue

            await context.bot.unban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                only_if_banned=True,
            )

            unbanned += 1

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "UNGBAN FAILED | %s",
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "UNGBAN ERROR | %s",
                e,
            )

        await asyncio.sleep(0.15)

    result = (
        "♻️ <b>GLOBAL UNGBAN COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent Global Ban: OFF</b>\n"
        "✅ Future joins will NOT be auto-banned.\n\n"

        f"📦 <b>Total:</b> {total}\n"
        f"✅ <b>Unbanned:</b> {unbanned}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}"
    )

    await status_message.edit_text(
        result,
        parse_mode="HTML",
    )

    await send_action_log(
        context,
        "UNGBAN",
        user_id,
        username,
        first_name,
        admin,
        reason,
        total,
        unbanned,
        skipped,
        failed,
    )


# =========================================================
# GLOBAL MUTE
# =========================================================

async def gmute(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    admin = update.effective_user

    if not admin:
        return

    if not is_admin(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(admin)

    if not context.args and not get_reply_user(update):

        await update.message.reply_text(
            "Usage:\n\n"
            "/gmute 123456789 Spam\n"
            "/gmute @username Spam\n\n"
            "Reply ചെയ്ത്:\n"
            "/gmute Spam"
        )

        return

    user_id = get_target_from_command(
        update,
        context,
    )

    if user_id is None:

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    if is_owner(user_id):

        await update.message.reply_text(
            "❌ Owner cannot be globally muted."
        )

        return

    reason = get_reason(
        update,
        context,
    )

    username, first_name = get_target_info(
        user_id,
        update,
    )

    add_global_mute(
        user_id=user_id,
        username=username,
        first_name=first_name,
        reason=reason,
        admin_id=admin.id,
    )

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "🔇 Global mute saved.\n"
            "📭 No registered chats currently."
        )

        return

    target_mention = get_user_mention(
        user_id,
        username,
        first_name,
    )

    status_message = await update.message.reply_text(
        "🔇 <b>GLOBAL MUTE STARTED...</b>\n\n"

        f"👤 Target: {target_mention}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent global mute saved.</b>\n"
        "New joins will also be automatically muted.\n\n"

        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    mute_permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )

    total = len(chats)
    muted = 0
    skipped = 0
    failed = 0

    for chat in chats:

        chat_id = chat["_id"]

        try:

            if not await bot_is_admin(
                context,
                chat_id,
            ):

                skipped += 1
                continue

            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=mute_permissions,
            )

            muted += 1

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "GMUTE FAILED | %s",
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "GMUTE ERROR | %s",
                e,
            )

        await asyncio.sleep(0.15)

    result = (
        "🔇 <b>GLOBAL MUTE COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent Global Mute: ON</b>\n"
        "🔇 Future joins: <b>AUTO MUTE</b>\n\n"

        f"📦 <b>Total:</b> {total}\n"
        f"🔇 <b>Muted:</b> {muted}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}"
    )

    await status_message.edit_text(
        result,
        parse_mode="HTML",
    )

    await send_action_log(
        context,
        "GMUTE",
        user_id,
        username,
        first_name,
        admin,
        reason,
        total,
        muted,
        skipped,
        failed,
    )


# =========================================================
# GLOBAL UNMUTE
# =========================================================

async def ungmute(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    admin = update.effective_user

    if not admin:
        return

    if not is_admin(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(admin)

    if not context.args and not get_reply_user(update):

        await update.message.reply_text(
            "Usage:\n\n"
            "/ungmute 123456789\n"
            "/ungmute @username\n\n"
            "Reply ചെയ്ത് /ungmute"
        )

        return

    user_id = get_target_from_command(
        update,
        context,
    )

    if user_id is None:

        await update.message.reply_text(
            "❌ User not found."
        )

        return

    reason = get_reason(
        update,
        context,
    )

    username, first_name = get_target_info(
        user_id,
        update,
    )

    remove_global_mute(user_id)

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "🔊 Global mute removed."
        )

        return

    target_mention = get_user_mention(
        user_id,
        username,
        first_name,
    )

    status_message = await update.message.reply_text(
        "🔊 <b>GLOBAL UNMUTE STARTED...</b>\n\n"

        f"👤 Target: {target_mention}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent Global Mute: OFF</b>\n\n"

        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    normal_permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    )

    total = len(chats)
    unmuted = 0
    skipped = 0
    failed = 0

    for chat in chats:

        chat_id = chat["_id"]

        try:

            if not await bot_is_admin(
                context,
                chat_id,
            ):

                skipped += 1
                continue

            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=normal_permissions,
            )

            unmuted += 1

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "UNGMUTE FAILED | %s",
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "UNGMUTE ERROR | %s",
                e,
            )

        await asyncio.sleep(0.15)

    result = (
        "🔊 <b>GLOBAL UNMUTE COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

        "🛡️ <b>Permanent Global Mute: OFF</b>\n"
        "✅ Future joins will NOT be auto-muted.\n\n"

        f"📦 <b>Total:</b> {total}\n"
        f"🔊 <b>Unmuted:</b> {unmuted}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}"
    )

    await status_message.edit_text(
        result,
        parse_mode="HTML",
    )

    await send_action_log(
        context,
        "UNGMUTE",
        user_id,
        username,
        first_name,
        admin,
        reason,
        total,
        unmuted,
        skipped,
        failed,
    )


# =========================================================
# CHATS
# =========================================================

async def chats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    admin = update.effective_user

    if not admin:
        return

    if not is_admin(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    all_chats = list(
        chats_col.find({})
    )

    if not all_chats:

        await update.message.reply_text(
            "📭 No registered chats."
        )

        return

    text = "📋 <b>REGISTERED CHATS</b>\n\n"

    for i, chat in enumerate(
        all_chats,
        1,
    ):

        title = escape(
            chat.get("title")
            or "Unknown"
        )

        chat_id = chat["_id"]

        chat_type = escape(
            chat.get("type")
            or "unknown"
        )

        text += (
            f"{i}. <b>{title}</b>\n"
            f"🆔 <code>{chat_id}</code>\n"
            f"📁 {chat_type}\n\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# LOAD SETTINGS
# =========================================================

def load_settings():

    global ANTISPAM_ENABLED

    data = settings_col.find_one(
        {"_id": "antispam"}
    )

    if data:

        ANTISPAM_ENABLED = bool(
            data.get(
                "enabled",
                True,
            )
        )

    else:

        # Default = ON
        ANTISPAM_ENABLED = True


# =========================================================
# MAIN
# =========================================================

def main():

    # Load persistent settings before starting bot
    load_ban_words()
    load_settings()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "panel",
            panel,
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            panel,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        )
    )

    app.add_handler(
        CommandHandler(
            "gban",
            gban,
        )
    )

    app.add_handler(
        CommandHandler(
            "ungban",
            ungban,
        )
    )

    app.add_handler(
        CommandHandler(
            "unban",
            ungban,
        )
    )

    app.add_handler(
        CommandHandler(
            "gmute",
            gmute,
        )
    )

    app.add_handler(
        CommandHandler(
            "ungmute",
            ungmute,
        )
    )

    app.add_handler(
        CommandHandler(
            "unmute",
            ungmute,
        )
    )

    app.add_handler(
        CommandHandler(
            "chats",
            chats,
        )
    )

    # -----------------------------------------------------
    # PANEL CALLBACKS
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            panel_callback,
        )
    )

    # -----------------------------------------------------
    # PANEL TEXT INPUT
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            panel_text_input,
        ),
        group=0,
    )

    # -----------------------------------------------------
    # GROUP MODERATION
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            moderation_message,
        ),
        group=1,
    )

    # -----------------------------------------------------
    # BOT JOIN / LEAVE
    # -----------------------------------------------------

    app.add_handler(
        ChatMemberHandler(
            bot_membership_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # -----------------------------------------------------
    # USER JOIN / LEAVE
    # -----------------------------------------------------

    app.add_handler(
        ChatMemberHandler(
            user_membership_update,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # -----------------------------------------------------
    # START LOG
    # -----------------------------------------------------

    logger.info(
        "================================="
    )

    logger.info(
        "GLOBAL BAN BOT STARTED"
    )

    logger.info(
        "Permanent GBAN + Auto GBAN"
    )

    logger.info(
        "Permanent GMUTE + Auto GMUTE"
    )

    logger.info(
        "Global Ban Words"
    )

    logger.info(
        "Global Link Filter"
    )

    logger.info(
        "Phone + Email Filter"
    )

    logger.info(
        "Telegram Invite Filter"
    )

    logger.info(
        "Anti-Spam"
    )

    logger.info(
        "Unique Link Logging"
    )

    logger.info(
        "Admin Management"
    )

    logger.info(
        "OWNER IDS: %s",
        list(OWNER_IDS),
    )

    logger.info(
        "LOGS GROUP: %s",
        LOGS_GROUP_ID,
    )

    logger.info(
        "BAN WORDS: %s",
        len(ban_words_cache),
    )

    logger.info(
        "ANTISPAM: %s",
        "ON" if ANTISPAM_ENABLED else "OFF",
    )

    logger.info(
        "================================="
    )

    # -----------------------------------------------------
    # POLLING
    # -----------------------------------------------------

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
