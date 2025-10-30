import re
from fastapi import APIRouter, Depends, HTTPException, Header, status
from fastapi.responses import FileResponse
from db import tmdb_col, files_col
from utility import is_user_authorized, build_search_pipeline, safe_api_call
from config import OWNER_ID, SEND_UPDATES, UPDATE_CHANNEL_ID
from tmdb import get_info
from app import bot
from bson.objectid import ObjectId
from pyrogram import enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def get_current_user(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization header missing")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization scheme")

    token = parts[1]

    try:
        user_id = int(token)
        if not await is_user_authorized(user_id):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization required — please verify through the bot first.")
        return user_id
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token format")

async def get_current_admin(user_id: int = Depends(get_current_user)):
    if user_id != OWNER_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user_id

@router.get("/tmdb")
async def get_tmdb_entries(admin_id: int = Depends(get_current_admin), page: int = 1, search: str = None):
    page_size = 10
    skip = (page - 1) * page_size
    query = {}
    if search:
        escaped_search = re.escape(search)
        query["title"] = {"$regex": escaped_search, "$options": "i"}

    entries = []
    async for entry in tmdb_col.find(query).skip(skip).limit(page_size):
        entries.append({
            "tmdb_id": entry.get("tmdb_id"),
            "title": entry.get("title"),
            "type": entry.get("tmdb_type"),
            "rating": entry.get("rating"),
            "plot": entry.get("plot"),
            "year": entry.get("year")
        })

    total_entries = await tmdb_col.count_documents(query)
    total_pages = (total_entries + page_size - 1) // page_size
    
    return {
        "entries": entries,
        "total_pages": total_pages,
        "current_page": page
    }

@router.get("/files")
async def get_files(admin_id: int = Depends(get_current_admin), page: int = 1, search: str = None):
    page_size = 10
    skip = (page - 1) * page_size
    
    if search:
        sanitized_search = bot.sanitize_query(search)
        pipeline = build_search_pipeline(sanitized_search, {}, skip, page_size)
        result = await files_col.aggregate(pipeline).to_list(length=None)
        files_data = result[0]['results'] if result and 'results' in result[0] else []
        files = []
        for file in files_data:
            files.append({
                "id": str(file.get("_id")),
                "file_name": file.get("file_name"),
                "tmdb_id": file.get("tmdb_id"),
                "poster_url": file.get("poster_url")
            })
        total_files = result[0]['totalCount'][0]['total'] if result and 'totalCount' in result[0] and result[0]['totalCount'] else 0
    else:
        files = []
        async for file in files_col.find().skip(skip).limit(page_size):
            files.append({
                "id": str(file.get("_id")),
                "file_name": file.get("file_name"),
                "tmdb_id": file.get("tmdb_id")
            })
        total_files = await files_col.count_documents({})
        
    total_pages = (total_files + page_size - 1) // page_size
    
    return {
        "files": files,
        "total_pages": total_pages,
        "current_page": page
    }

@router.post("/tmdb")
async def add_tmdb_entry(data: dict, admin_id: int = Depends(get_current_admin)):
    tmdb_id = data.get("tmdb_id")
    tmdb_type = data.get("tmdb_type")
    file_ids = data.get("file_ids", [])

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid TMDB ID")

    info = await get_info(tmdb_type, tmdb_id)
    if not info or "message" in info and info["message"].startswith("Error"):
        raise HTTPException(status_code=404, detail="TMDB ID not found")
        
    poster_path = info.get('poster_path')
    poster_url = info.get('poster_url')
    trailer_url = info.get('trailer_url')
    message_text = info.get('message')
    name = info.get('title')
    year = info.get('year')
    rating = info.get('rating')
    plot = info.get("plot")
    imdb_id = info.get("imdb_id")
    
    await upsert_tmdb_info(tmdb_id, tmdb_type, poster_path, name, year, rating, plot, trailer_url, imdb_id)

    logger.info(f"SEND_UPDATES is {SEND_UPDATES}")
    logger.info(f"Poster URL is {poster_url}")

    if SEND_UPDATES and poster_url:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎥 Trailer", url=trailer_url)]]
        ) if trailer_url else None
        
        result = await safe_api_call(
            bot.send_photo(
                UPDATE_CHANNEL_ID,
                photo=poster_url,
                caption=message_text,
                parse_mode=enums.ParseMode.HTML,
                reply_markup=keyboard
            )
        )
        logger.info(f"safe_api_call result: {result}")

    if file_ids:
        for file_id in file_ids:
            await files_col.update_one({"_id": ObjectId(file_id)}, {"$set": {"tmdb_id": tmdb_id, "tmdb_type": tmdb_type}})

    return {"status": "success"}

@router.delete("/tmdb/{tmdb_id}")
async def delete_tmdb_entry(tmdb_id: int, admin_id: int = Depends(get_current_admin)):
    await tmdb_col.delete_one({"tmdb_id": tmdb_id})
    await files_col.update_many({"tmdb_id": tmdb_id}, {"$unset": {"tmdb_id": "", "tmdb_type": ""}})
    return {"status": "success"}

@router.put("/tmdb/{tmdb_id}")
async def update_tmdb_entry(tmdb_id: int, data: dict, admin_id: int = Depends(get_current_admin)):
    rating_str = data.get("rating")
    if rating_str == "":
        rating = None
    else:
        try:
            rating = float(rating_str)
        except (ValueError, TypeError):
            rating = None

    year_str = data.get("year")
    if year_str == "":
        year = None
    else:
        try:
            year = int(year_str)
        except (ValueError, TypeError):
            year = None
            
    update_data = {
        "title": data.get("title"),
        "rating": rating,
        "plot": data.get("plot"),
        "year": year
    }
    await tmdb_col.update_one({"tmdb_id": tmdb_id}, {"$set": update_data})
    return {"status": "success"}

@router.put("/files/{file_id}")
async def update_file_poster(file_id: str, data: dict, admin_id: int = Depends(get_current_admin)):
    poster_url = data.get("poster_url")
    await files_col.update_one({"_id": ObjectId(file_id)}, {"$set": {"poster_url": poster_url}})
    return {"status": "success"}
    
@router.delete("/files/{file_id}")
async def delete_file(file_id: str, admin_id: int = Depends(get_current_admin)):
    await files_col.delete_one({"_id": ObjectId(file_id)})
    return {"status": "success"}
