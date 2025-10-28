
import os
import sys
import logging
from bson import ObjectId
from pyrogram.errors import UserIsBlocked, InputUserDeactivated, ListenerTimeout, PeerIdInvalid, UserIsBot

from pyrogram import filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import OWNER_ID, LOG_CHANNEL_ID, UPDATE_CHANNEL_ID, MY_DOMAIN, SEND_UPDATES
from db import files_col, allowed_channels_col, auth_users_col, users_col, tmdb_col, db
from utility import (
    extract_channel_and_msg_id,
    get_allowed_channels,
    queue_file_for_processing,
    invalidate_search_cache,
    auto_delete_message,
    safe_api_call,
    remove_unwanted,
    restore_tmdb_photos,
    human_readable_size,
    extract_tmdb_link,
    get_info, 
    upsert_tmdb_info
)
from app import bot

logger = logging.getLogger(__name__)

broadcasting = False

@bot.on_message(filters.private & (filters.document | filters.video))
async def del_file_handler(client, message):
    try:
        reply = None
        user_id = message.from_user.id
        if user_id == OWNER_ID and message.forward_from_chat:
            channel_id = message.forward_from_chat.id if message.forward_from_chat else None
            msg_id = message.forward_from_message_id if message.forward_from_message_id else None
            if channel_id and msg_id:
                file_doc = files_col.find_one({"channel_id": channel_id, "message_id": msg_id})
                if not file_doc:
                    reply = await message.reply_text("No file found with that name in the database.")
                    return
                result = files_col.delete_one({"channel_id": channel_id, "message_id": msg_id})
                if result.deleted_count > 0:
                    reply = await message.reply_text(f"Database record deleted. File name: {file_doc['file_name']}")
        else:
            cpy_msg = await message.copy(LOG_CHANNEL_ID)
            file_link = bot.encode_file_link(cpy_msg.chat.id, cpy_msg.id)
            stream_url = f"{MY_DOMAIN}/player/{file_link}"
            buttons = [
                [
                    InlineKeyboardButton("▶️ Stream", url=stream_url)
                ]
            
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            await message.edit_reply_markup(reply_markup)
            
        if reply:
            bot.loop.create_task(auto_delete_message(message, reply))
    except Exception as e:
        logger.error(f"Error in del_file_handler: {e}")
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("copy") & filters.private & filters.user(OWNER_ID))
async def copy_file_handler(client, message):
    try:
        if len(message.command) != 4:
            await message.reply_text("<b>Usage:</b> /copy <start_link> <end_link> <dest_link>")
            return

        start_link, end_link, dest_link = message.command[1], message.command[2], message.command[3]

        try:
            source_channel_id, start_msg_id = extract_channel_and_msg_id(start_link)
            end_source_channel_id, end_msg_id = extract_channel_and_msg_id(end_link)
            dest_channel_id, _ = extract_channel_and_msg_id(dest_link)
        except ValueError as e:
            await message.reply_text(f"⚠️ <b>Invalid Link:</b> {e}")
            return

        if source_channel_id != end_source_channel_id:
            return await message.reply_text("⚠️ <b>Start and end links must be from the same channel.</b>")

        if source_channel_id == dest_channel_id:
            return await message.reply_text("⚠️ <b>Source and destination channels must be different.</b>")

        start_id = min(start_msg_id, end_msg_id)
        end_id = max(start_msg_id, end_msg_id)
        total = end_id - start_id + 1

        status_msg = await message.reply_text(
            f"🔁 <b>Copying messages from ID <code>{start_id}</code> to <code>{end_id}</code>...</b>\n"
            f"📦 <i>Total messages to check: {total}</i>"
        )

        count = 0
        failed = 0

        async with bot.copy_lock:
            for idx, msg_id in enumerate(range(start_id, end_id + 1), start=1):
                try:
                    msg = await safe_api_call(client.get_messages(source_channel_id, msg_id))
                    if not msg:
                        continue

                    media = msg.document or msg.video or msg.audio
                    if not media:
                        continue

                    caption = msg.caption or getattr(media, "file_name", "No Caption")
                    caption = remove_unwanted(caption)

                    copied_msg = await safe_api_call(client.copy_message(
                        chat_id=dest_channel_id,
                        from_chat_id=source_channel_id,
                        message_id=msg_id,
                        caption=f"<b>{caption}</b>"
                    ))

                    count += 1

                    if copied_msg:
                        await queue_file_for_processing(
                            copied_msg,
                            channel_id=dest_channel_id,
                            reply_func=message.reply_text,
                            duplicate=True
                        )

                    if idx % 10 == 0 or idx == total:
                        await safe_api_call(status_msg.edit_text(
                            f"🔁 <b>Copying in progress...</b>\n"
                            f"✅ <b>{count}</b> files copied so far.\n"
                            f"📂 <i>{idx}/{total} messages checked</i>"
                        ))

                except Exception as copy_error:
                    failed += 1
                    logger.warning(f"[copy_file_handler] Failed to copy message {msg_id}: {copy_error}")
                    continue

        await safe_api_call(status_msg.edit_text(
            f"✅ <b>Copy completed!</b>\n\n"
            f"📦 <b>Total files copied:</b> {count}\n"
            f"❌ <b>Failed to copy:</b> {failed}\n"
            f"📂 <i>Total messages checked:</i> {total}"
        ))
        invalidate_search_cache()
    except Exception as e:
        logger.error(f"[copy_file_handler] Error: {e}")
        await message.reply_text("❌ <b>An error occurred during the copy process.</b>")

@bot.on_message(filters.command("index") & filters.private & filters.user(OWNER_ID))
async def index_channel_files(client, message):
    try:
        args = message.command
        if not (3 <= len(args) <= 4):
            await message.reply_text("<b>Usage:</b> /index <start_link> <end_link> [dup]")
            return

        start_link, end_link = args[1], args[2]
        dup = len(args) == 4 and args[3].lower() == "dup"

        try:
            start_channel_id, start_msg_id = extract_channel_and_msg_id(start_link)
            end_channel_id, end_msg_id = extract_channel_and_msg_id(end_link)
        except ValueError as e:
            await message.reply_text(f"⚠️ <b>Invalid Link:</b> {e}")
            return

        if start_channel_id != end_channel_id:
            await message.reply_text("⚠️ <b>Start and end links must be from the same channel.</b>")
            return

        channel_id = start_channel_id
        allowed_channels = await get_allowed_channels()
        if channel_id not in allowed_channels:
            await message.reply_text("❌ <b>This channel is not allowed for indexing.</b>")
            return

        start_id = min(start_msg_id, end_msg_id)
        end_id = max(start_msg_id, end_msg_id)

        reply = await message.reply_text(f"🔁 <b>Indexing files from <code>{start_id}</code> to <code>{end_id}</code>...</b>\n"
                                       f"Duplicates allowed: {dup}")

        batch_size = 50
        count = 0
        for batch_start in range(start_id, end_id + 1, batch_size):
            batch_end = min(batch_start + batch_size - 1, end_id)
            ids = list(range(batch_start, batch_end + 1))
            messages = []
            try:
                messages = await safe_api_call(client.get_messages(channel_id, ids))
            except Exception as e:
                logger.warning(f"Could not get messages in batch {batch_start}-{batch_end}: {e}")

            for msg in messages:
                if not msg:
                    continue
                if msg.document or msg.video or msg.audio or msg.photo:
                    await queue_file_for_processing(
                        msg,
                        channel_id=channel_id,
                        reply_func=reply.edit_text,
                        duplicate=dup
                    )
                    count += 1
            await safe_api_call(reply.edit_text(f"🔁 <b>Indexing in progress...</b> {count} files queued so far."))

        await safe_api_call(reply.edit_text(f"✅ <b>Indexing completed!</b> Total files queued: {count}"))
        invalidate_search_cache()
    except Exception as e:
        logger.error(f"[index_channel_files] Error: {e}")
        await message.reply_text("❌ <b>An error occurred during the indexing process.</b>")

@bot.on_message(filters.private & filters.command("del") & filters.user(OWNER_ID))
async def delete_command(client, message):
    try:
        args = message.text.split(maxsplit=3)
        if len(args) < 3:
            await message.reply_text("Usage: /del <file|tmdb <link> [end_link]")
            return
        delete_type = args[1].strip().lower()
        user_input = args[2].strip()
        end_input = args[3].strip() if len(args) > 3 else None

        if delete_type == "file":
            try:
                channel_id, msg_id = extract_channel_and_msg_id(user_input)
                if end_input:
                    end_channel_id, end_msg_id = extract_channel_and_msg_id(end_input)
                    if channel_id != end_channel_id:
                        await message.reply_text("Start and end links must be from the same channel.")
                        return
                    if msg_id > end_msg_id:
                        msg_id, end_msg_id = end_msg_id, msg_id
                    result = files_col.delete_many({
                        "channel_id": channel_id,
                        "message_id": {"$gte": msg_id, "$lte": end_msg_id}
                    })
                    await message.reply_text(f"Deleted {result.deleted_count} files from {msg_id} to {end_msg_id} in channel {channel_id}.")
                else:
                    result = files_col.delete_one({"channel_id": channel_id, "message_id": msg_id})
                    await message.reply_text(f"Deleted file with message ID {msg_id} in channel {channel_id}.")
            except ValueError as e:
                await message.reply_text(f"Error: {e}")
        elif delete_type == "tmdb":
            try:
                if end_input:
                    tmdb_type = user_input.lower()
                    tmdb_id = int(end_input.strip())
                else:
                    tmdb_type, tmdb_id = await extract_tmdb_link(user_input)

                result = tmdb_col.delete_one({"tmdb_type": tmdb_type, "tmdb_id": tmdb_id})

                if result.deleted_count > 0:
                    await message.reply_text(f"Database record deleted: {tmdb_type}/{tmdb_id}.")
                else:
                    await message.reply_text(f"No TMDB record found with ID {tmdb_type}/{tmdb_id} in the database.")
            except ValueError as e:
                await message.reply_text(f"Error: {e}")
        else:
            await message.reply_text("Invalid delete type. Use 'file' or 'tmdb'.")
    except Exception as e:
        logger.error(f"Error in delete_command: {e}")
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command('restart') & filters.private & filters.user(OWNER_ID))
async def restart(client, message):
    await message.delete()
    # 🔄 Restart logic
    os.system("python3 update.py")
    os.execl(sys.executable, sys.executable, "bot.py")

@bot.on_message(filters.private & filters.command("restore") & filters.user(OWNER_ID))
async def update_info(client, message):
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.reply_text("Usage: /restore tmdb [start_objectid]")
            return
        restore_type = args[1].strip()
        start_id = args[2] if len(args) > 2 else None
        if start_id:
            try:
                start_id = ObjectId(start_id)
            except Exception:
                await message.reply_text("Invalid ObjectId format for start_id.")
                return
        if restore_type == "tmdb":
            await restore_tmdb_photos(bot, start_id)
        else:
            await message.reply_text("Invalid restore type. Use 'tmdb'.")
    except Exception as e:
        logger.error(f"Error in update_info: {e}")
        await message.reply_text(f"Error in Update Command: {e}")

@bot.on_message(filters.command("add") & filters.private & filters.user(OWNER_ID))
async def add_channel_handler(client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: /add channel_id channel_name")
        return
    try:
        channel_id = int(message.command[1])
        channel_name = " ".join(message.command[2:])
        allowed_channels_col.update_one(
            {"channel_id": channel_id},
            {"$set": {"channel_id": channel_id, "channel_name": channel_name}},
            upsert=True
        )
        await message.reply_text(f"✅ Channel {channel_id} ({channel_name}) added to allowed channels.")
    except ValueError:
        await message.reply_text("Invalid channel ID.")
    except Exception as e:
        logger.error(f"Error in add_channel_handler: {e}")
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("rm") & filters.private & filters.user(OWNER_ID))
async def remove_channel_handler(client, message: Message):
    if len(message.command) != 2:
        await message.reply_text("Usage: /rm channel_id")
        return
    try:
        channel_id = int(message.command[1])
        result = allowed_channels_col.delete_one({"channel_id": channel_id})
        if result.deleted_count:
            await message.reply_text(f"✅ Channel {channel_id} removed from allowed channels.")
        else:
            await message.reply_text("❌ Channel not found in allowed channels.")
    except ValueError:
        await message.reply_text("Invalid channel ID.")
    except Exception as e:
        logger.error(f"Error in remove_channel_handler: {e}")
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("broadcast") & filters.chat(LOG_CHANNEL_ID))
async def broadcast_handler(client, message: Message):
    global broadcasting
    if message.reply_to_message:
        if broadcasting:
            await message.reply_text("already broadcasting")
            return
        users = list(users_col.find({}, {"_id": 0, "user_id": 1}))
        total_users = len(users)
        sent_count = 0
        failed_count = 0
        removed_count = 0
        broadcasting = True

        status_message = await message.reply_text(
            f"📢 **Broadcast in progress...**\n\n"
            f"👥 **Total Users:** {total_users}\n"
            f"✅ **Sent:** {sent_count}\n"
            f"❌ **Failed:** {failed_count}\n"
            f"🗑️ **Removed:** {removed_count}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data="cancel_broadcast")]]
            )
        )

        for i, user in enumerate(users):
            if not broadcasting:
                await status_message.edit_text("📢 **Broadcast cancelled.**")
                break
            try:
                msg = message.reply_to_message
                if msg.forward_from_chat:
                    await safe_api_call(msg.copy(chat_id=user["user_id"],
                                                 caption=f"{msg.caption.html}\n\n✅ <b>Now Available!</b>",
                                                 reply_markup=msg.reply_markup
                                                 ))
                else:
                    await safe_api_call(msg.copy(user["user_id"]))
                sent_count += 1
            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid, UserIsBot):
                users_col.delete_one({"user_id": user["user_id"]})
                removed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Error broadcasting to {user['user_id']}: {e}")

            if i % 10 == 0:
                await status_message.edit_text(
                    f"📢 **Broadcast in progress...**\n\n"
                    f"👥 **Total Users:** {total_users}\n"
                    f"✅ **Sent:** {sent_count}\n"
                    f"❌ **Failed:** {failed_count}\n"
                    f"🗑️ **Removed:** {removed_count}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Cancel", callback_data="cancel_broadcast")]]
                    )
                )
        else:
            await status_message.edit_text(
                f"✅ **Broadcast finished!**\n\n"
                f"👥 **Total Users:** {total_users}\n"
                f"✅ **Sent:** {sent_count}\n"
                f"❌ **Failed:** {failed_count}\n"
                f"🗑️ **Removed:** {removed_count}"
            )

        broadcasting = False


@bot.on_callback_query(filters.regex("cancel_broadcast"))
async def cancel_broadcast_handler(client, query):
    global broadcasting
    if broadcasting:
        broadcasting = False
        await query.answer("Cancelling broadcast...", show_alert=True)
    else:
        await query.answer("No broadcast in progress.", show_alert=True)

@bot.on_message(filters.command("log") & filters.private & filters.user(OWNER_ID))
async def send_log_file(client, message: Message):
    log_file = "bot_log.txt"
    try:
        if not os.path.exists(log_file):
            await safe_api_call(message.reply_text("Log file not found."))
            return
        reply = await safe_api_call(client.send_document(message.chat.id, log_file, caption="Here is the log file."))
        bot.loop.create_task(auto_delete_message(message, reply))
    except Exception as e:
        logger.error(f"Failed to send log file: {e}")

@bot.on_message(filters.command("stats") & filters.private & filters.user(OWNER_ID))
async def stats_command(client, message: Message):
    try:
        total_auth_users = auth_users_col.count_documents({})
        total_users = users_col.count_documents({})

        pipeline = [
            {"$group": {"_id": None, "total": {"$sum": "$file_size"}}}
        ]
        result = list(files_col.aggregate(pipeline))
        total_storage = result[0]["total"] if result else 0

        stats = db.command("dbstats")
        db_storage = stats.get("storageSize", 0)

        channel_pipeline = [
            {"$group": {"_id": "$channel_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]
        channel_counts = list(files_col.aggregate(channel_pipeline))
        channel_docs = allowed_channels_col.find({}, {"_id": 0, "channel_id": 1, "channel_name": 1})
        channel_names = {c["channel_id"]: c.get("channel_name", "") for c in channel_docs}

        text = (
            f"<b>Total auth users:</b> {total_auth_users} / {total_users}\n"
            f"<b>Files size:</b> {human_readable_size(total_storage)}\n"
            f"<b>Database storage used:</b> {db_storage / (1024 * 1024):.2f} MB\n"
        )

        if not channel_counts:
            text += " <b>No files indexed yet.</b>"
        else:
            for c in channel_counts:
                chan_id = c['_id']
                chan_name = channel_names.get(chan_id, 'Unknown')
                text += f"<b>{chan_name}</b>: {c['count']} files\n"

        reply = await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
        bot.loop.create_task(auto_delete_message(message, reply))
    except Exception as e:
        logger.error(f"Error in stats_command: {e}")

@bot.on_message(filters.private & filters.command("update") & filters.user(OWNER_ID))
async def update_tmdb_info(client, message):
    try:
        args = message.text.split(maxsplit=3)
        if len(args) < 4:
            await message.reply_text("Usage: /update <tmdb_link> <start_link> <end_link>")
            return

        tmdb_link, start_link, end_link = args[1], args[2], args[3]

        try:
            tmdb_type, tmdb_id = await extract_tmdb_link(tmdb_link)
        except ValueError as e:
            await message.reply_text(f"Invalid TMDB link: {e}")
            return

        try:
            start_channel_id, start_msg_id = extract_channel_and_msg_id(start_link)
            end_channel_id, end_msg_id = extract_channel_and_msg_id(end_link)
        except ValueError as e:
            await message.reply_text(f"Invalid Telegram link: {e}")
            return

        if start_channel_id != end_channel_id:
            await message.reply_text("Start and end links must be from the same channel.")
            return

        if start_msg_id > end_msg_id:
            start_msg_id, end_msg_id = end_msg_id, start_msg_id

        result = files_col.update_many(
            {
                "channel_id": start_channel_id,
                "message_id": {"$gte": start_msg_id, "$lte": end_msg_id}
            },
            {"$set": {"tmdb_id": tmdb_id, "tmdb_type": tmdb_type}}
        )

        await message.reply_text(f"✅ Successfully updated {result.modified_count} files with TMDB ID {tmdb_id} ({tmdb_type}).")

    except Exception as e:
        logger.error(f"Error in update_tmdb_info: {e}")
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.private & filters.command("tmdb") & filters.user(OWNER_ID))
async def tmdb_command(client, message):
    try:
        if len(message.command) < 2:
            await message.reply_text("Usage: /tmdb tmdb_link")
            return

        tmdb_link = message.command[1]
        tmdb_type, tmdb_id = await extract_tmdb_link(tmdb_link)
        info = await get_info(tmdb_type, tmdb_id)
        poster_url = info.get('poster_url')
        poster_path = info.get('poster_path')
        trailer_url = info.get('trailer_url')
        message = info.get('message')
        name = info.get('title')
        year = info.get('year')
        rating = info.get('rating')
        plot = info.get("plot")
        imdb_id = info.get("imdb_id")
        upsert_tmdb_info(tmdb_id, tmdb_type, poster_path, name, year, rating, plot, trailer_url, imdb_id)
        
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎥 Trailer", url=trailer_url)]]) if trailer else None
        if poster_url and SEND_UPDATES:
            await safe_api_call(
                client.send_photo(
                    UPDATE_CHANNEL_ID,
                    photo=poster_url,
                    caption=message,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=keyboard
                )
            )
    except ValueError as e:
        await message.reply_text(f"Error: {e}")
    except Exception as e:
        logging.error(f"Error in tmdb_command: {e}")
        await safe_api_call(message.reply_text(f"An error occurred: {e}"))
    finally:
        await message.delete()

@bot.on_message(filters.command("op") & filters.chat(LOG_CHANNEL_ID))
async def chatop_handler(client, message: Message):
    args = message.text.split(maxsplit=4)
    if len(args) < 3:
        await message.reply_text(
            "Usage:\n/op send <chat_id> [reply_to_message_id] (reply to a message)\n"
            "/op del <chat_id> <message_id> or <start>-<end>"
        )
        return
    try:
        op = args[1].lower()
        chat_id = int(args[2])

        if op == "send":
            if not message.reply_to_message:
                await message.reply_text("❌ Reply to a message to send it.")
                return

            reply_to_msg_id = None
            if len(args) == 4:
                reply_to_msg_id = int(args[3])

            sent = await message.reply_to_message.copy(
                chat_id,
                reply_to_message_id=reply_to_msg_id
            )
            await message.reply_text(f"✅ Sent to {chat_id} (message_id: {sent.id})")

        elif op == "del":
            if len(args) != 4:
                await message.reply_text("Usage: /op del <chat_id> <message_id> or <start>-<end>")
                return

            msg_arg = args[3]
            if '-' in msg_arg:
                start, end = map(int, msg_arg.split('-'))
                if start > end:
                    await message.reply_text("❌ Start ID must be less than or equal to end ID.")
                    return
                await safe_api_call(client.delete_messages(chat_id, list(range(start, end + 1))))
                await message.reply_text(f"✅ Deleted messages in chat {chat_id}")
            else:
                msg_id = int(msg_arg)
                await safe_api_call(client.delete_messages(chat_id, msg_id))
                await message.reply_text(f"✅ Deleted message {msg_id} in chat {chat_id}")
        else:
            await message.reply_text("Invalid operation. Use 'send' or 'del'.")
    except ValueError:
        await message.reply_text("Invalid chat ID or message ID.")
    except Exception as e:
        logger.error(f"Error in chatop_handler: {e}")
        await message.reply_text(f"❌ Failed: {e}")

@bot.on_message(filters.command("block") & filters.private & filters.user(OWNER_ID))
async def block_user_handler(client, message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.reply_text("Usage: /block <user_id>")
        return
    try:
        user_id = int(args[1])
        users_col.update_one(
            {"user_id": user_id},
            {"$set": {"blocked": True}},
            upsert=True
        )
        await message.reply_text(f"✅ User {user_id} has been blocked.")
    except ValueError:
        await message.reply_text("Invalid user ID.")
    except Exception as e:
        logger.error(f"Error in block_user_handler: {e}")
        await message.reply_text(f"❌ Failed to block user: {e}")

@bot.on_message(filters.command("unblock") & filters.private & filters.user(OWNER_ID))
async def unblock_user_handler(client, message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.reply_text("Usage: /unblock <user_id>")
        return
    try:
        user_id = int(args[1])
        users_col.update_one(
            {"user_id": user_id},
            {"$set": {"blocked": False}},
            upsert=True
        )
        await message.reply_text(f"✅ User {user_id} has been unblocked.")
    except ValueError:
        await message.reply_text("Invalid user ID.")
    except Exception as e:
        logger.error(f"Error in unblock_user_handler: {e}")
        await message.reply_text(f"❌ Failed to unblock user: {e}")

@bot.on_message(filters.command("ap") & filters.private & filters.user(OWNER_ID))
async def add_poster_handler(_, message: Message):
    try:
        args = message.text.split(maxsplit=2)
        if len(args) < 3:
            await message.reply_text("Usage: /addposter file_link poster_url")
            return
        file_link = args[1].strip()
        channel_id, msg_id = extract_channel_and_msg_id(file_link)
        poster_url = args[2].strip()

        file_record = files_col.find_one({"channel_id": channel_id, "message_id": msg_id})
        if not file_record:
            await message.reply_text("❌ No file record found with the provided link.")
            return
        files_col.update_one(
            {"channel_id": channel_id, "message_id": msg_id},
            {"$set": {"poster_url": poster_url}}
        )
        await message.reply_text(f"✅ Poster URL added to file {file_record['file_name']}.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to add poster URL: {e}")
