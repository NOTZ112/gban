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

    Username must have been seen by the bot
    previously.
    """

    if not value:
        return None

    value = value.strip()

    # -----------------------------------------------------
    # USER ID
    # -----------------------------------------------------

    try:
        return int(value)

    except ValueError:
        pass

    # -----------------------------------------------------
    # USERNAME
    # -----------------------------------------------------

    username = value.lstrip("@").lower()

    user = users_col.find_one(
        {"username": username},
        {"_id": 1},
    )

    if user:
        return int(user["_id"])

    return None


def get_reply_user(update: Update):
    """
    Returns the user whose message was replied to.
    """

    if not update.message:
        return None

    replied = update.message.reply_to_message

    if not replied:
        return None

    return replied.from_user


def get_user_mention(user_id: int, username: str = "", first_name: str = "User"):
    """
    Creates a clickable Telegram mention.

    If username exists:
        @username

    Otherwise:
        clickable first name using tg://user?id=
    """

    if username:
        safe_username = escape(username.lstrip("@"))
        return f"@{safe_username}"

    safe_name = escape(first_name or "User")

    return (
        f'<a href="tg://user?id={user_id}">'
        f"{safe_name}"
        f"</a>"
    )


def get_target_name(
    user_id: int,
    target_data=None,
    reply_user=None,
):
    """
    Gets best available target display name.
    """

    username = ""
    first_name = "User"

    if reply_user:

        username = reply_user.username or ""
        first_name = reply_user.first_name or "User"

    elif target_data:

        username = (
            target_data.get("username")
            or ""
        )

        first_name = (
            target_data.get("first_name")
            or "User"
        )

    return get_user_mention(
        user_id=user_id,
        username=username,
        first_name=first_name,
    )


async def bot_is_admin(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> bool:

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


def current_time():
    return datetime.now().strftime(
        "%d %b %Y • %H:%M:%S"
    )


# =========================================================
# LOGS
# =========================================================

async def send_action_log(
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    target_id: int,
    target_username: str,
    target_first_name: str,
    admin,
    reason: str,
    total: int,
    success: int,
    skipped: int,
    failed: int,
):

    # -----------------------------------------------------
    # TARGET MENTION
    # -----------------------------------------------------

    target_mention = get_user_mention(
        user_id=target_id,
        username=target_username,
        first_name=target_first_name,
    )

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    admin_name = escape(
        admin.username
        if admin.username
        else admin.full_name
        or "Admin"
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

    safe_reason = escape(
        reason if reason else "Not specified"
    )

    text = (
        f"{title}\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"

        f"🆔 <b>Target ID:</b> "
        f"<code>{target_id}</code>\n\n"

        f"👮 <b>Action By:</b> @{admin_name}\n"

        f"🆔 <b>Admin ID:</b> "
        f"<code>{admin.id}</code>\n\n"

        f"📝 <b>Reason:</b> {safe_reason}\n\n"

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

    # -----------------------------------------------------
    # BOT BECAME ADMIN
    # -----------------------------------------------------

    if new_status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):

        save_chat(chat)

        logger.info(
            "CHAT REGISTERED | %s | %s | %s",
            chat.id,
            chat.title,
            chat.type,
        )

    # -----------------------------------------------------
    # BOT LEFT / REMOVED
    # -----------------------------------------------------

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

        "🔨 <code>/gban USER_ID reason</code>\n"
        "🔨 <code>/gban @username reason</code>\n"
        "🔨 <code>Reply → /gban reason</code>\n\n"

        "♻️ <code>/ungban USER_ID</code>\n"
        "♻️ <code>/ungban @username</code>\n"
        "♻️ <code>Reply → /ungban</code>\n\n"

        "🔇 <code>/gmute USER_ID reason</code>\n"
        "🔇 <code>/gmute @username reason</code>\n"
        "🔇 <code>Reply → /gmute reason</code>\n\n"

        "🔊 <code>/ungmute USER_ID</code>\n"
        "🔊 <code>/ungmute @username</code>\n"
        "🔊 <code>Reply → /ungmute</code>\n\n"

        "📋 <code>/chats</code>"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# =========================================================
# TARGET RESOLUTION
# =========================================================

def get_target_from_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    # -----------------------------------------------------
    # FIRST: COMMAND ARGUMENT
    # -----------------------------------------------------

    if context.args:

        target = context.args[0]

        user_id = resolve_user(target)

        if user_id is not None:
            return user_id

    # -----------------------------------------------------
    # SECOND: REPLY USER
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # TARGET WAS FIRST ARGUMENT
    # -----------------------------------------------------

    if resolve_user(first) is not None:

        return " ".join(
            context.args[1:]
        ).strip()

    # -----------------------------------------------------
    # REPLY COMMAND
    # -----------------------------------------------------

    if get_reply_user(update):

        return " ".join(
            context.args
        ).strip()

    return " ".join(
        context.args[1:]
    ).strip()


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

            "അല്ലെങ്കിൽ User message reply ചെയ്ത്:\n"
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

    reply_user = get_reply_user(update)

    target_data = users_col.find_one(
        {"_id": user_id}
    )

    if reply_user:

        save_user(reply_user)

        target_username = (
            reply_user.username or ""
        )

        target_first_name = (
            reply_user.first_name or "User"
        )

    elif target_data:

        target_username = (
            target_data.get("username")
            or ""
        )

        target_first_name = (
            target_data.get("first_name")
            or "User"
        )

    else:

        target_username = ""
        target_first_name = "User"

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "📭 No registered chats found."
        )

        return

    status_message = await update.message.reply_text(
        "🔨 <b>GLOBAL BAN STARTED...</b>\n\n"

        f"👤 Target: "
        f"{get_user_mention(user_id, target_username, target_first_name)}\n"

        f"🆔 ID: <code>{user_id}</code>\n"
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
                "Unexpected GBAN error: %s",
                e,
            )

        await asyncio.sleep(0.15)

    target_mention = get_user_mention(
        user_id,
        target_username,
        target_first_name,
    )

    result = (
        "🔨 <b>GLOBAL BAN COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

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
        context=context,
        action="GBAN",
        target_id=user_id,
        target_username=target_username,
        target_first_name=target_first_name,
        admin=admin,
        reason=reason,
        total=total,
        success=banned,
        skipped=skipped,
        failed=failed,
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

            "അല്ലെങ്കിൽ User message reply ചെയ്ത്:\n"
            "/ungban"
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

    reply_user = get_reply_user(update)

    target_data = users_col.find_one(
        {"_id": user_id}
    )

    if reply_user:

        save_user(reply_user)

        target_username = reply_user.username or ""
        target_first_name = reply_user.first_name or "User"

    elif target_data:

        target_username = target_data.get("username") or ""
        target_first_name = target_data.get("first_name") or "User"

    else:

        target_username = ""
        target_first_name = "User"

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "📭 No registered chats found."
        )

        return

    status_message = await update.message.reply_text(
        "♻️ <b>GLOBAL UNGBAN STARTED...</b>\n\n"

        f"👤 Target: "
        f"{get_user_mention(user_id, target_username, target_first_name)}\n"

        f"🆔 ID: <code>{user_id}</code>\n"
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
                "UNGBAN FAILED | User=%s | Chat=%s | %s",
                user_id,
                chat_id,
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "Unexpected UNGBAN error: %s",
                e,
            )

        await asyncio.sleep(0.15)

    target_mention = get_user_mention(
        user_id,
        target_username,
        target_first_name,
    )

    result = (
        "♻️ <b>GLOBAL UNGBAN COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

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
        context=context,
        action="UNGBAN",
        target_id=user_id,
        target_username=target_username,
        target_first_name=target_first_name,
        admin=admin,
        reason=reason,
        total=total,
        success=unbanned,
        skipped=skipped,
        failed=failed,
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

            "/gmute 123456789 Spam\n\n"

            "/gmute @username Spam\n\n"

            "അല്ലെങ്കിൽ User message reply ചെയ്ത്:\n"
            "/gmute Spam"
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

    reply_user = get_reply_user(update)

    target_data = users_col.find_one(
        {"_id": user_id}
    )

    if reply_user:

        save_user(reply_user)

        target_username = reply_user.username or ""
        target_first_name = reply_user.first_name or "User"

    elif target_data:

        target_username = target_data.get("username") or ""
        target_first_name = target_data.get("first_name") or "User"

    else:

        target_username = ""
        target_first_name = "User"

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "📭 No registered chats found."
        )

        return

    status_message = await update.message.reply_text(
        "🔇 <b>GLOBAL MUTE STARTED...</b>\n\n"

        f"👤 Target: "
        f"{get_user_mention(user_id, target_username, target_first_name)}\n"

        f"🆔 ID: <code>{user_id}</code>\n"
        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    total = len(chats)

    muted = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------
    # ALL MESSAGE PERMISSIONS OFF
    # -----------------------------------------------------

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
                "GMUTE FAILED | User=%s | Chat=%s | %s",
                user_id,
                chat_id,
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "Unexpected GMUTE error: %s",
                e,
            )

        await asyncio.sleep(0.15)

    target_mention = get_user_mention(
        user_id,
        target_username,
        target_first_name,
    )

    result = (
        "🔇 <b>GLOBAL MUTE COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

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
        context=context,
        action="GMUTE",
        target_id=user_id,
        target_username=target_username,
        target_first_name=target_first_name,
        admin=admin,
        reason=reason,
        total=total,
        success=muted,
        skipped=skipped,
        failed=failed,
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

            "അല്ലെങ്കിൽ User message reply ചെയ്ത്:\n"
            "/ungmute"
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

    reply_user = get_reply_user(update)

    target_data = users_col.find_one(
        {"_id": user_id}
    )

    if reply_user:

        save_user(reply_user)

        target_username = reply_user.username or ""
        target_first_name = reply_user.first_name or "User"

    elif target_data:

        target_username = target_data.get("username") or ""
        target_first_name = target_data.get("first_name") or "User"

    else:

        target_username = ""
        target_first_name = "User"

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "📭 No registered chats found."
        )

        return

    status_message = await update.message.reply_text(
        "🔊 <b>GLOBAL UNMUTE STARTED...</b>\n\n"

        f"👤 Target: "
        f"{get_user_mention(user_id, target_username, target_first_name)}\n"

        f"🆔 ID: <code>{user_id}</code>\n"
        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    total = len(chats)

    unmuted = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------
    # NORMAL MEMBER PERMISSIONS
    # -----------------------------------------------------

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
                "UNGMUTE FAILED | User=%s | Chat=%s | %s",
                user_id,
                chat_id,
                e,
            )

        except Exception as e:

            failed += 1

            logger.exception(
                "Unexpected UNGMUTE error: %s",
                e,
            )

        await asyncio.sleep(0.15)

    target_mention = get_user_mention(
        user_id,
        target_username,
        target_first_name,
    )

    result = (
        "🔊 <b>GLOBAL UNMUTE COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> {target_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"

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
        context=context,
        action="UNGMUTE",
        target_id=user_id,
        target_username=target_username,
        target_first_name=target_first_name,
        admin=admin,
        reason=reason,
        total=total,
        success=unmuted,
        skipped=skipped,
        failed=failed,
    )


# =========================================================
# /CHATS
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

    text = (
        "📋 <b>REGISTERED CHATS</b>\n\n"
    )

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
    # START
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # -----------------------------------------------------
    # GBAN
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "gban",
            gban,
        )
    )

    # -----------------------------------------------------
    # UNGBAN
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # GMUTE
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "gmute",
            gmute,
        )
    )

    # -----------------------------------------------------
    # UNGMUTE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # CHATS
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "chats",
            chats,
        )
    )

    # -----------------------------------------------------
    # BOT MEMBERSHIP
    # -----------------------------------------------------

    app.add_handler(
        ChatMemberHandler(
            bot_membership_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    # -----------------------------------------------------
    # LOG
    # -----------------------------------------------------

    logger.info(
        "================================="
    )

    logger.info(
        "GLOBAL BAN BOT STARTED"
    )

    logger.info(
        "GBAN + UNGBAN + GMUTE + UNGMUTE"
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
