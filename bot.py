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
# DATABASE
# =========================================================

mongo = MongoClient(MONGO_URI)

db = mongo["global_ban_bot"]

chats_col = db["chats"]
users_col = db["users"]

chats_col.create_index("_id", unique=True)
users_col.create_index("_id", unique=True)
users_col.create_index("username")


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
                "first_name": user.first_name,
                "last_name": user.last_name,
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
    chats_col.delete_one({"_id": chat_id})


def resolve_user(value: str):
    """
    Supports:
        123456789
        @username
        username

    Username lookup works for users previously seen
    by the bot.
    """

    value = value.strip()

    # Numeric ID
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

    except TelegramError:
        return False


def display_user(user):
    if not user:
        return "Unknown"

    if user.username:
        return f"@{user.username}"

    name = user.full_name or "Unknown"

    return name


def now_text():
    return datetime.now().strftime(
        "%d %b %Y • %H:%M:%S"
    )


# =========================================================
# LOG FUNCTION
# =========================================================

async def send_log(
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

    if target_username:
        target_text = f"@{target_username}"
    else:
        target_text = "No username"

    admin_username = (
        f"@{admin.username}"
        if admin.username
        else admin.full_name
    )

    if action == "GBAN":
        title = "🔨 <b>GLOBAL BAN</b>"
        success_label = "Banned"
    else:
        title = "♻️ <b>GLOBAL UNGBAN</b>"
        success_label = "Unbanned"

    text = (
        f"{title}\n\n"

        f"👤 <b>Target:</b> {target_text}\n"
        f"🆔 <b>Target ID:</b> <code>{target_id}</code>\n\n"

        f"👮 <b>Action By:</b> {admin_username}\n"
        f"🆔 <b>Admin ID:</b> <code>{admin.id}</code>\n\n"

        f"📝 <b>Reason:</b> "
        f"{reason or 'Not specified'}\n\n"

        f"📦 <b>Total Chats:</b> {total}\n"
        f"✅ <b>{success_label}:</b> {success}\n"
        f"⏭ <b>Skipped:</b> {skipped}\n"
        f"❌ <b>Failed:</b> {failed}\n\n"

        f"🕐 <b>Time:</b> {now_text()}"
    )

    try:
        await context.bot.send_message(
            chat_id=LOGS_GROUP_ID,
            text=text,
            parse_mode="HTML",
        )

    except TelegramError as e:
        logger.warning(
            "Could not send log: %s",
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

    new_status = event.new_chat_member.status

    # Bot became admin
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

    # Bot left / removed
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
        save_user(update.effective_user)

    await update.message.reply_text(
        "🛡️ <b>GLOBAL BAN BOT</b>\n\n"

        "🔨 /gban USER_ID [reason]\n"
        "♻️ /ungban USER_ID [reason]\n\n"

        "Username:\n"
        "/gban @username reason\n"
        "/ungban @username reason\n\n"

        "📋 /chats",

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

    if not context.args:
        await update.message.reply_text(
            "Usage:\n\n"
            "/gban 123456789\n"
            "/gban 123456789 Spam\n\n"
            "or\n\n"
            "/gban @username Spam"
        )
        return

    target = context.args[0]

    # Everything after first argument = reason
    reason = " ".join(context.args[1:]).strip()

    user_id = resolve_user(target)

    if user_id is None:
        await update.message.reply_text(
            "❌ User not found.\n\n"
            "User ID ഉപയോഗിക്കുക.\n\n"
            "Example:\n"
            "/gban 123456789 Spam"
        )
        return

    # Get target information from DB
    target_user = users_col.find_one(
        {"_id": user_id}
    )

    target_username = ""

    if target_user:
        target_username = (
            target_user.get("username") or ""
        )

    chats = list(chats_col.find({}))

    if not chats:
        await update.message.reply_text(
            "📭 No registered chats found."
        )
        return

    msg = await update.message.reply_text(
        "🔨 <b>GLOBAL BAN STARTED...</b>\n\n"
        f"👤 Target ID: <code>{user_id}</code>\n"
        f"📦 Chats: {len(chats)}",
        parse_mode="HTML",
    )

    total = len(chats)

    banned = 0
    skipped = 0
    failed = 0

    for chat in chats:

        chat_id = chat["_id"]
        title = chat.get(
            "title",
            str(chat_id),
        )

        try:

            # Make sure bot is still admin
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

            logger.info(
                "GBAN SUCCESS | %s | %s",
                user_id,
                title,
            )

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "GBAN FAILED | %s | %s | %s",
                user_id,
                title,
                e,
            )

        except Exception:

            failed += 1

            logger.exception(
                "Unexpected GBAN error"
            )

        await asyncio.sleep(0.15)

    # Final result
    result = (
        "🔨 <b>GLOBAL BAN COMPLETED</b>\n\n"

        f"👤 Target: "
        f"{('@' + target_username) if target_username else 'No username'}\n"

        f"🆔 ID: <code>{user_id}</code>\n\n"

        f"📦 Total: {total}\n"
        f"✅ Banned: {banned}\n"
        f"⏭ Skipped: {skipped}\n"
        f"❌ Failed: {failed}"
    )

    await msg.edit_text(
        result,
        parse_mode="HTML",
    )

    # Send logs
    await send_log(
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

    if not is_owner(admin.id):
        await update.message.reply_text(
            "❌ You are not authorized."
        )
        return

    save_user(admin)

    if not context.args:
        await update.message.reply_text(
            "Usage:\n\n"
            "/ungban 123456789\n"
            "/ungban 123456789 Reason\n\n"
            "or\n\n"
            "/ungban @username Reason"
        )
        return

    target = context.args[0]

    reason = " ".join(
        context.args[1:]
    ).strip()

    user_id = resolve_user(target)

    if user_id is None:
        await update.message.reply_text(
            "❌ User not found.\n\n"
            "User ID ഉപയോഗിക്കുക."
        )
        return

    target_user = users_col.find_one(
        {"_id": user_id}
    )

    target_username = ""

    if target_user:
        target_username = (
            target_user.get("username") or ""
        )

    chats = list(chats_col.find({}))

    if not chats:
        await update.message.reply_text(
            "📭 No registered chats found."
        )
        return

    msg = await update.message.reply_text(
        "♻️ <b>GLOBAL UNGBAN STARTED...</b>\n\n"
        f"👤 Target ID: <code>{user_id}</code>\n"
        f"📦 Chats: {len(chats)}",
        parse_mode="HTML",
    )

    total = len(chats)

    unbanned = 0
    skipped = 0
    failed = 0

    for chat in chats:

        chat_id = chat["_id"]
        title = chat.get(
            "title",
            str(chat_id),
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
                "UNGBAN SUCCESS | %s | %s",
                user_id,
                title,
            )

        except Forbidden:

            skipped += 1

        except TelegramError as e:

            failed += 1

            logger.warning(
                "UNGBAN FAILED | %s | %s | %s",
                user_id,
                title,
                e,
            )

        except Exception:

            failed += 1

            logger.exception(
                "Unexpected UNGBAN error"
            )

        await asyncio.sleep(0.15)

    result = (
        "♻️ <b>GLOBAL UNGBAN COMPLETED</b>\n\n"

        f"👤 Target: "
        f"{('@' + target_username) if target_username else 'No username'}\n"

        f"🆔 ID: <code>{user_id}</code>\n\n"

        f"📦 Total: {total}\n"
        f"✅ Unbanned: {unbanned}\n"
        f"⏭ Skipped: {skipped}\n"
        f"❌ Failed: {failed}"
    )

    await msg.edit_text(
        result,
        parse_mode="HTML",
    )

    # Send logs
    await send_log(
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

    if not admin or not is_owner(admin.id):
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

        title = chat.get(
            "title",
            "Unknown",
        )

        chat_id = chat["_id"]

        chat_type = chat.get(
            "type",
            "unknown",
        )

        text += (
            f"{i}. {title}\n"
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

    # Commands
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

    # Register/unregister chats
    app.add_handler(
        ChatMemberHandler(
            bot_membership_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    logger.info(
        "GLOBAL BAN BOT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
