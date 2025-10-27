import re
import base64
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from config import MY_DOMAIN
from utility import is_user_authorized, get_user_firstname, build_search_pipeline
from db import tmdb_col, files_col, comments_col
from tmdb import POSTER_BASE_URL
from app import bot
from config import TMDB_CHANNEL_ID
from datetime import datetime, timezone

api = FastAPI()

api.mount("/static", StaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")

api.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://{MY_DOMAIN}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency to get user_id from cookie
async def get_current_user(request: Request):
    user_id = request.cookies.get("user_id")
    if user_id:
        try:
            return int(user_id)
        except (ValueError, TypeError):
            return None
    return None


@api.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@api.post("/api/authorize")
async def api_authorize(request: Request):
    data = await request.json()
    user_id = data.get("user_id")

    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid User ID format.",
        )

    if not is_user_authorized(user_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not authorized.",
        )

    response = JSONResponse(content={"message": "Authorization successful"})
    response.set_cookie(key="user_id", value=str(user_id), httponly=True, samesite="strict")
    return response



@api.get("/", response_class=HTMLResponse)
async def index_page(request: Request, user_id: int = Depends(get_current_user)):
    if not user_id:
        return RedirectResponse(url="/login")

    first_name = await get_user_firstname(user_id)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user_id": user_id,
        "first_name": first_name,
        "poster_base_url": POSTER_BASE_URL
    })


@api.get("/api/movies")
async def get_movies(request: Request, page: int = 1, search: str = None, category: str = None, sort: str = "year", user_id: int = Depends(get_current_user)):
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    page_size = 10
    skip = (page - 1) * page_size

    query = {}
    if search:
        query["title"] = {"$regex": re.escape(search), "$options": "i"}
    if category:
        query["tmdb_type"] = category

    sort_order = []
    if sort == "rating":
        sort_order.append(("vote_average", -1))
    elif sort == "recent":
        sort_order.append(("_id", -1))
    else:  # Default to year
        sort_order.append(("release_date", -1))

    movies = list(tmdb_col.find(query).sort(sort_order).skip(skip).limit(page_size))
    total_movies = tmdb_col.count_documents(query)

    # Convert ObjectId to string
    for movie in movies:
        movie["_id"] = str(movie["_id"])

    return {
        "movies": movies,
        "total_pages": (total_movies + page_size - 1) // page_size,
        "current_page": page
    }

@api.get("/details/{tmdb_id}", response_class=HTMLResponse)
async def movie_details(request: Request, tmdb_id: str, tmdb_type: str, user_id: int = Depends(get_current_user)):
    if not user_id:
        return RedirectResponse(url="/login")

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid TMDB ID")

    details = tmdb_col.find_one({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type})

    if not details:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Details not found")

    details["_id"] = str(details["_id"])

    return templates.TemplateResponse("details.html", {
        "request": request,
        "details": details,
        "poster_base_url": POSTER_BASE_URL,
    })


@api.get("/api/details/{tmdb_id}")
async def get_movie_details(request: Request, tmdb_id: str, tmdb_type: str, page: int = 1, user_id: int = Depends(get_current_user)):
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        tmdb_id = int(tmdb_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid TMDB ID")

    page_size = 10
    skip = (page - 1) * page_size

    files = list(files_col.find({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type}).skip(skip).limit(page_size))
    total_files = files_col.count_documents({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type})

    # Convert ObjectId to string and add stream URL
    for file in files:
        file["_id"] = str(file["_id"])
        file["stream_url"] = f"https://{MY_DOMAIN}/player/{bot.encode_file_link(file['channel_id'], file['message_id'])}"

    return {
        "files": files,
        "total_pages": (total_files + page_size - 1) // page_size,
        "current_page": page
    }

@api.get("/others", response_class=HTMLResponse)
async def others_page(request: Request, user_id: int = Depends(get_current_user)):
    if not user_id:
        return RedirectResponse(url="/login")

    return templates.TemplateResponse("others.html", {
        "request": request,
        "user_id": user_id
    })

@api.get("/api/others")
async def get_others(request: Request, page: int = 1, search: str = None, sort: str = "recent", user_id: int = Depends(get_current_user)):
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    page_size = 10
    skip = (page - 1) * page_size

    sort_order = [("_id", -1)] if sort == "recent" else [("_id", 1)]

    if search:
        pipeline = build_search_pipeline(search, {"channel_id": {"$nin": TMDB_CHANNEL_ID}}, skip, page_size)
        result = list(files_col.aggregate(pipeline))
        files = result[0]['results'] if result and 'results' in result[0] else []
        total_files = result[0]['totalCount'][0]['total'] if result and 'totalCount' in result[0] and result[0]['totalCount'] else 0
    else:
        query = {"channel_id": {"$nin": TMDB_CHANNEL_ID}}
        files = list(files_col.find(query).sort(sort_order).skip(skip).limit(page_size))
        total_files = files_col.count_documents(query)

    for file in files:
        file["_id"] = str(file["_id"])
        file["stream_url"] = f"https://{MY_DOMAIN}/player/{bot.encode_file_link(file['channel_id'], file['message_id'])}"

    return {
        "files": files,
        "total_pages": (total_files + page_size - 1) // page_size,
        "current_page": page
    }

@api.post("/api/comments")
async def create_comment(request: Request, user_id: int = Depends(get_current_user)):
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    data = await request.json()
    comment_text = data.get("comment")

    if not comment_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Comment text cannot be empty.")

    comment = {
        "user_id": user_id,
        "comment": comment_text,
        "created_at": datetime.now(timezone.utc)
    }
    comments_col.insert_one(comment)
    return {"message": "Comment added successfully"}

@api.get("/api/comments")
async def get_comments(request: Request, page: int = 1, user_id: int = Depends(get_current_user)):
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    page_size = 5
    skip = (page - 1) * page_size

    comments = []
    for comment in comments_col.find().sort("_id", -1).skip(skip).limit(page_size):
        comment["_id"] = str(comment["_id"])
        comment["first_name"] = await get_user_firstname(comment["user_id"])
        comments.append(comment)

    total_comments = comments_col.count_documents({})

    return {
        "comments": comments,
        "total_pages": (total_comments + page_size - 1) // page_size,
        "current_page": page
    }

'''
@api.get("/player/{file_link}")
async def stream_player(file_link: str, request: Request):
    try:
        padding = '=' * (-len(file_link) % 4)
        decoded = base64.urlsafe_b64decode(file_link + padding).decode()
        channel_id, msg_id = map(int, decoded.split("_"))

        # You might want to add authorization checks here

        # Get the stream link from the bot
        # This is a placeholder for the actual logic to get the stream link
        stream_link = await bot.get_stream_link(channel_id, msg_id)

        return RedirectResponse(url=stream_link)

    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

'''


