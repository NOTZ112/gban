import os
import asyncio
import logging
from datetime import datetime

from pymongo import MongoClient
from telegram import Update
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

# IMPORTANT:
# Do NOT create an _id index manually.
# MongoDB already creates a unique _id index automatically.

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

    value = value.strip()

    # -----------------------------------------------------
    # Telegram User ID
    # -----------------------------------------------------

    try:
        return int(value)

    except ValueError:
        pass

    # -----------------------------------------------------
    # Username
    # -----------------------------------------------------

    username = value.lstrip("@").lower()

    user = users_col.find_one(
        {
            "username": username
        },
        {
            "_id": 1
        },
    )

    if user:
        return int(user["_id"])

    return None


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
    admin,
    reason: str,
    total: int,
    success: int,
    skipped: int,
    failed: int,
):

    target_name = (
        f"@{target_username}"
        if target_username
        else "No username"
    )

    admin_name = (
        f"@{admin.username}"
        if admin.username
        else admin.full_name
    )

    if action == "GBAN":

        title = "🔨 <b>GLOBAL BAN</b>"
        result_name = "Banned"

    else:

        title = "♻️ <b>GLOBAL UNGBAN</b>"
        result_name = "Unbanned"

    text = (
        f"{title}\n\n"

        f"👤 <b>Target:</b> {target_name}\n"
        f"🆔 <b>Target ID:</b> "
        f"<code>{target_id}</code>\n\n"

        f"👮 <b>Action By:</b> {admin_name}\n"
        f"🆔 <b>Admin ID:</b> "
        f"<code>{admin.id}</code>\n\n"

        f"📝 <b>Reason:</b> "
        f"{reason if reason else 'Not specified'}\n\n"

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
    # Bot became admin
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
    # Bot left / removed
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

        "🔨 <code>/gban USER_ID</code>\n"
        "🔨 <code>/gban USER_ID reason</code>\n"
        "🔨 <code>/gban @username reason</code>\n\n"

        "♻️ <code>/ungban USER_ID</code>\n"
        "♻️ <code>/ungban USER_ID reason</code>\n"
        "♻️ <code>/ungban @username reason</code>\n\n"

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

    # -----------------------------------------------------
    # OWNER / ADMIN CHECK
    # -----------------------------------------------------

    if not is_owner(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(admin)

    # -----------------------------------------------------
    # ARGUMENT CHECK
    # -----------------------------------------------------

    if not context.args:

        await update.message.reply_text(
            "Usage:\n\n"

            "/gban 123456789\n\n"

            "/gban 123456789 Spam\n\n"

            "/gban @username Spam"
        )

        return

    target = context.args[0]

    # Everything after target = reason
    reason = " ".join(
        context.args[1:]
    ).strip()

    # -----------------------------------------------------
    # RESOLVE USER
    # -----------------------------------------------------

    user_id = resolve_user(target)

    if user_id is None:

        await update.message.reply_text(
            "❌ User not found.\n\n"

            "User ID ഉപയോഗിക്കുക.\n\n"

            "Example:\n"
            "/gban 123456789 Spam"
        )

        return

    # -----------------------------------------------------
    # GET USER DATA
    # -----------------------------------------------------

    target_data = users_col.find_one(
        {
            "_id": user_id
        }
    )

    target_username = ""

    if target_data:

        target_username = (
            target_data.get("username")
            or ""
        )

    # -----------------------------------------------------
    # GET REGISTERED CHATS
    # -----------------------------------------------------

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "📭 No registered chats found."
        )

        return

    # -----------------------------------------------------
    # START MESSAGE
    # -----------------------------------------------------

    status_message = await update.message.reply_text(
        "🔨 <b>GLOBAL BAN STARTED...</b>\n\n"

        f"👤 Target ID: "
        f"<code>{user_id}</code>\n"

        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    total = len(chats)

    banned = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------
    # BAN LOOP
    # -----------------------------------------------------

    for chat in chats:

        chat_id = chat["_id"]

        title = (
            chat.get("title")
            or str(chat_id)
        )

        try:

            # Check bot admin status
            if not await bot_is_admin(
                context,
                chat_id,
            ):

                skipped += 1

                continue

            # Ban
            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                revoke_messages=True,
            )

            banned += 1

            logger.info(
                "GBAN SUCCESS | "
                "User=%s | Chat=%s | %s",
                user_id,
                chat_id,
                title,
            )

        except Forbidden:

            skipped += 1

            logger.warning(
                "GBAN FORBIDDEN | "
                "User=%s | Chat=%s",
                user_id,
                chat_id,
            )

        except TelegramError as e:

            failed += 1

            logger.warning(
                "GBAN FAILED | "
                "User=%s | Chat=%s | %s",
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

        # Small delay
        await asyncio.sleep(0.15)

    # -----------------------------------------------------
    # RESULT
    # -----------------------------------------------------

    result = (
        "🔨 <b>GLOBAL BAN COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> "
        f"{('@' + target_username) if target_username else 'No username'}\n"

        f"🆔 <b>ID:</b> "
        f"<code>{user_id}</code>\n\n"

        f"📦 <b>Total:</b> {total}\n"
        f"✅ <b>Banned:</b> {banned}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}"
    )

    await status_message.edit_text(
        result,
        parse_mode="HTML",
    )

    # -----------------------------------------------------
    # LOG
    # -----------------------------------------------------

    await send_action_log(
        context=context,
        action="GBAN",
        target_id=user_id,
        target_username=target_username,
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

    # -----------------------------------------------------
    # OWNER / ADMIN CHECK
    # -----------------------------------------------------

    if not is_owner(admin.id):

        await update.message.reply_text(
            "❌ You are not authorized."
        )

        return

    save_user(admin)

    # -----------------------------------------------------
    # ARGUMENT CHECK
    # -----------------------------------------------------

    if not context.args:

        await update.message.reply_text(
            "Usage:\n\n"

            "/ungban 123456789\n\n"

            "/ungban 123456789 Appeal accepted\n\n"

            "/ungban @username Appeal accepted"
        )

        return

    target = context.args[0]

    reason = " ".join(
        context.args[1:]
    ).strip()

    # -----------------------------------------------------
    # RESOLVE USER
    # -----------------------------------------------------

    user_id = resolve_user(target)

    if user_id is None:

        await update.message.reply_text(
            "❌ User not found.\n\n"
            "User ID ഉപയോഗിക്കുക."
        )

        return

    # -----------------------------------------------------
    # USER DATA
    # -----------------------------------------------------

    target_data = users_col.find_one(
        {
            "_id": user_id
        }
    )

    target_username = ""

    if target_data:

        target_username = (
            target_data.get("username")
            or ""
        )

    # -----------------------------------------------------
    # CHATS
    # -----------------------------------------------------

    chats = list(
        chats_col.find({})
    )

    if not chats:

        await update.message.reply_text(
            "📭 No registered chats found."
        )

        return

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    status_message = await update.message.reply_text(
        "♻️ <b>GLOBAL UNGBAN STARTED...</b>\n\n"

        f"👤 Target ID: "
        f"<code>{user_id}</code>\n"

        f"📦 Chats: {len(chats)}",

        parse_mode="HTML",
    )

    total = len(chats)

    unbanned = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------
    # UNBAN LOOP
    # -----------------------------------------------------

    for chat in chats:

        chat_id = chat["_id"]

        title = (
            chat.get("title")
            or str(chat_id)
        )

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

            logger.info(
                "UNGBAN SUCCESS | "
                "User=%s | Chat=%s | %s",
                user_id,
                chat_id,
                title,
            )

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "UNGBAN FAILED | "
                "User=%s | Chat=%s | %s",
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

    # -----------------------------------------------------
    # RESULT
    # -----------------------------------------------------

    result = (
        "♻️ <b>GLOBAL UNGBAN COMPLETED</b>\n\n"

        f"👤 <b>Target:</b> "
        f"{('@' + target_username) if target_username else 'No username'}\n"

        f"🆔 <b>ID:</b> "
        f"<code>{user_id}</code>\n\n"

        f"📦 <b>Total:</b> {total}\n"
        f"✅ <b>Unbanned:</b> {unbanned}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}"
    )

    await status_message.edit_text(
        result,
        parse_mode="HTML",
    )

    # -----------------------------------------------------
    # LOG
    # -----------------------------------------------------

    await send_action_log(
        context=context,
        action="UNGBAN",
        target_id=user_id,
        target_username=target_username,
        admin=admin,
        reason=reason,
        total=total,
        success=unbanned,
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

        title = (
            chat.get("title")
            or "Unknown"
        )

        chat_id = chat["_id"]

        chat_type = (
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
        CommandHandler(
            "start",
            start,
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

    # Alias
    app.add_handler(
        CommandHandler(
            "unban",
            ungban,
        )
    )

    app.add_handler(
        CommandHandler(
            "chats",
            chats,
        )
    )

    # -----------------------------------------------------
    # BOT ADMIN / CHAT REGISTRATION
    # -----------------------------------------------------

    app.add_handler(
        ChatMemberHandler(
            bot_membership_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    logger.info(
        "================================="
    )

    logger.info(
        "GLOBAL BAN BOT STARTED"
    )

    logger.info(
        "Registered OWNER IDs: %s",
        list(OWNER_IDS),
    )

    logger.info(
        "Logs Group: %s",
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
