from fastapi import APIRouter, Depends, HTTPException, Header, status
from fastapi.responses import FileResponse
from db import tmdb_col, files_col
from utility import is_user_authorized
from config import OWNER_ID
from tmdb import get_info
from bson.objectid import ObjectId
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
        if not is_user_authorized(user_id):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization required — please verify through the bot first.")
        return user_id
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token format")

async def get_current_admin(user_id: int = Depends(get_current_user)):
    if user_id != OWNER_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user_id

@router.get("/tmdb")
async def get_tmdb_entries(admin_id: int = Depends(get_current_admin)):
    entries = []
    for entry in tmdb_col.find():
        entries.append({
            "tmdb_id": entry.get("tmdb_id"),
            "title": entry.get("title"),
            "type": entry.get("tmdb_type")
        })
    return entries

@router.get("/files")
async def get_files(admin_id: int = Depends(get_current_admin)):
    files = []
    for file in files_col.find():
        files.append({
            "id": str(file.get("_id")),
            "file_name": file.get("file_name"),
            "tmdb_id": file.get("tmdb_id")
        })
    return files

@router.post("/tmdb")
async def add_tmdb_entry(data: dict, admin_id: int = Depends(get_current_admin)):
    tmdb_id = data.get("tmdb_id")
    tmdb_type = data.get("tmdb_type")
    file_ids = data.get("file_ids", [])

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid TMDB ID")

    tmdb_info = await get_info(tmdb_type, tmdb_id)
    if not tmdb_info or "message" in tmdb_info and tmdb_info["message"].startswith("Error"):
        raise HTTPException(status_code=404, detail="TMDB ID not found")

    tmdb_col.update_one({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type}, {"$set": tmdb_info}, upsert=True)

    if file_ids:
        for file_id in file_ids:
            files_col.update_one({"_id": ObjectId(file_id)}, {"$set": {"tmdb_id": tmdb_id, "tmdb_type": tmdb_type}})

    return {"status": "success"}

@router.delete("/tmdb/{tmdb_id}")
async def delete_tmdb_entry(tmdb_id: int, admin_id: int = Depends(get_current_admin)):
    tmdb_col.delete_one({"tmdb_id": tmdb_id})
    files_col.update_many({"tmdb_id": tmdb_id}, {"$unset": {"tmdb_id": "", "tmdb_type": ""}})
    return {"status": "success"}
