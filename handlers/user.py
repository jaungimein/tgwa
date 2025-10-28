
import logging
from datetime import datetime, timezone
from pyrogram import filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import ChatAdminRequired, UserAlreadyParticipant

from config import LOG_CHANNEL_ID, BOT_USERNAME, BACKUP_CHANNEL
from utility import (
    add_user,
    is_token_valid,
    authorize_user,
    get_user_link,
    safe_api_call,
    is_user_subscribed,
    auto_delete_message,
    get_allowed_channels,
    queue_file_for_processing,
    invalidate_search_cache,
    file_queue,
    is_user_authorized,
    tokens_col,
    generate_token, get_token_link,
    shorten_url,
)
from query_helper import store_query
from app import bot

logger = logging.getLogger(__name__)

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    reply_msg = None
    try:
        user_id = message.from_user.id
        user_link = await get_user_link(message.from_user)
        first_name = message.from_user.first_name or "there"
        username = message.from_user.username or None
        user_doc = add_user(user_id)

        if user_doc["_new"]:
            log_msg = f"👤 New user added:\nID: <code>{user_id}</code>\n"
            if first_name:
                log_msg += f"First Name: <b>{first_name}</b>\n"
            if username:
                log_msg += f"Username: @{username}\n"
            await safe_api_call(
                bot.send_message(LOG_CHANNEL_ID, log_msg, parse_mode=enums.ParseMode.HTML)
            )

        if user_doc.get("blocked", True):   
            return

        if len(message.command) == 2 and message.command[1].startswith("token_"):
            if is_token_valid(message.command[1][6:], user_id):
                authorize_user(user_id)
                reply_msg = await safe_api_call(message.reply_text("Great! You're all set to get files. ✅"))
                await safe_api_call(bot.send_message(LOG_CHANNEL_ID, f"✅ User <b>{user_link} | <code>{user_id}</code></b> authorized via @{BOT_USERNAME}"))
            else:
                reply_msg = await safe_api_call(message.reply_text("Oh no! It looks like your access key is invalid or has expired. Please get a new one. 🔑"))
                await safe_api_call(bot.send_message(LOG_CHANNEL_ID, f"❌ User <b>{user_link} | <code>{user_id}</code></b> used invalid or expired token."))
        else:

            if BACKUP_CHANNEL and not await is_user_subscribed(client, user_id):
                reply = await safe_api_call(message.reply_text(
                    text=(
                        "To get started, please join our updates channel. "
                        "It's the best way to stay in the loop! 😊"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔔 Join Updates", url=f"https://t.me/{BACKUP_CHANNEL}")]]
                    )
                ))
                bot.loop.create_task(auto_delete_message(message, reply))
                return

            reply_markup = None
            if not is_user_authorized(user_id):
                now = datetime.now(timezone.utc)
                token_doc = tokens_col.find_one({"user_id": user_id, "expiry": {"$gt": now}})
                token_id = token_doc["token_id"] if token_doc else generate_token(user_id)
                short_link = await shorten_url(get_token_link(token_id, BOT_USERNAME))
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗝️ Verify", url=short_link)]])

            joined_date = user_doc.get("joined", "Unknown")
            joined_str = joined_date.strftime("%Y-%m-%d %H:%M") if isinstance(joined_date, datetime) else str(joined_date)

            welcome_text = (
                f"Hi <b>{first_name}</b>, welcome! 👋\n\n"
                "I'm here to help you find what you're looking for.\n\n "
                f"Your Login ID <code>{user_id}</code>\n\n"
               f"👤 Joined: {joined_str}"
            )
            reply_msg = await safe_api_call(message.reply_text(
                welcome_text,
                quote=True,
                reply_to_message_id=message.id,
                reply_markup=reply_markup,
            ))
    except Exception as e:
        logger.error(f"⚠️ An unexpected error occurred in start_handler: {e}")

    if reply_msg:
        bot.loop.create_task(auto_delete_message(message, reply_msg))

@bot.on_message(filters.channel & (filters.document | filters.video | filters.audio | filters.photo))
async def channel_file_handler(client, message):
    try:
        allowed_channels = await get_allowed_channels()
        if message.chat.id not in allowed_channels:
            return

        await queue_file_for_processing(message)
        await file_queue.join()
        invalidate_search_cache()
    except Exception as e:
        logger.error(f"Error in channel_file_handler: {e}")

@bot.on_message(filters.group & filters.service)
async def delete_service_messages(client, message):
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Failed to delete service message in chat {message.chat.id}: {e}")

@bot.on_chat_join_request()
async def approve_join_request_handler(client, join_request):
    try:
        await client.approve_chat_join_request(join_request.chat.id, join_request.from_user.id)
        await safe_api_call(bot.send_message(LOG_CHANNEL_ID, f"✅ Approved join request for {join_request.from_user.mention} in {join_request.chat.title}"))
    except (ChatAdminRequired, UserAlreadyParticipant) as e:
        logger.warning(f"Could not approve join request: {e}")
    except Exception as e:
        logger.error(f"Failed to approve join request: {e}")
