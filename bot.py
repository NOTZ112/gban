import os
import asyncio
import logging
from datetime import datetime
from html import escape

from pymongo import MongoClient

from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError, Forbidden

from telegram.ext import (
    Application,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
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

# Permanent global actions
global_bans_col = db["global_bans"]
global_mutes_col = db["global_mutes"]

users_col.create_index(
    "username",
    sparse=True,
)


# =========================================================
# HELPERS
# =========================================================

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


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
    """
    Supports:

    123456789
    @username
    username

    Username must have been seen by the bot previously.
    """

    if not value:
        return None

    value = value.strip()

    # User ID
    try:
        return int(value)
    except ValueError:
        pass

    # Username
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

    # First try argument
    if context.args:

        user_id = resolve_user(
            context.args[0]
        )

        if user_id is not None:
            return user_id

    # Then reply
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

    # Target + reason
    if resolve_user(first) is not None:

        return " ".join(
            context.args[1:]
        ).strip()

    # Reply + reason
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
# GLOBAL DATABASE
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
# LOGGING
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
# BOT MEMBERSHIP
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

    # Bot became admin
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

        # -------------------------------------------------
        # When bot becomes admin in a new chat,
        # automatically apply existing global bans/mutes
        # to users only when Telegram gives us member updates.
        # -------------------------------------------------

    # Bot left
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

    # Save every user we see
    save_user(user)

    # Ignore bots
    if user.is_bot:
        return

    new_status = member.status

    # -----------------------------------------------------
    # USER JOINED / BECAME MEMBER
    # -----------------------------------------------------

    if new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):

        # =================================================
        # GLOBAL BAN CHECK
        # =================================================

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

        # =================================================
        # GLOBAL MUTE CHECK
        # =================================================

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
# GLOBAL BAN
# =========================================================

async def gban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    admin = update.effective_user

    if not admin:
        return

    if not is_owner(admin.id):

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

    reason = get_reason(
        update,
        context,
    )

    username, first_name = get_target_info(
        user_id,
        update,
    )

    # =====================================================
    # SAVE PERMANENT GLOBAL BAN
    # =====================================================

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

    if not is_owner(admin.id):

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

    # Remove permanent ban
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

    if not is_owner(admin.id):

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

    reason = get_reason(
        update,
        context,
    )

    username, first_name = get_target_info(
        user_id,
        update,
    )

    # =====================================================
    # SAVE PERMANENT GLOBAL MUTE
    # =====================================================

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

    if not is_owner(admin.id):

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

    # Remove permanent mute
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

    if not is_owner(admin.id):

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
# MAIN
# =========================================================

def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("gban", gban)
    )

    app.add_handler(
        CommandHandler("ungban", ungban)
    )

    app.add_handler(
        CommandHandler("unban", ungban)
    )

    app.add_handler(
        CommandHandler("gmute", gmute)
    )

    app.add_handler(
        CommandHandler("ungmute", ungmute)
    )

    app.add_handler(
        CommandHandler("unmute", ungmute)
    )

    app.add_handler(
        CommandHandler("chats", chats)
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
    # START
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
        "Reply + Username + User ID + Mention"
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
