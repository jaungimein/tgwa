import re
import aiohttp
from config import TMDB_API_KEY, logger

POSTER_BASE_URL = 'https://image.tmdb.org/t/p/original'

async def get_imdb_details(imdb_id):
    """
    Fetch rating and plot using IMDb ID.
    """
    if not imdb_id:
        return {}
    try:
        url = f"https://imdb.iamidiotareyoutoo.com/search?tt={imdb_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"IMDB returned error for {imdb_id}: {data.get('Error')}")
                    return {}
                data = await resp.json()
                return{
                    "name": data.get("short").get("name"),
                    "rating": data.get("short").get("aggregateRating").get("ratingValue"),
                    "year": data.get("top").get("releaseYear").get("year"),
                    "plot": data.get("short").get("description")
                }
            
    except Exception as e:
        logger.error(f"IMDb API error: {e}")
        return {}
    
def get_cast_and_crew(tmdb_type, movie_id):
    """
    Fetches the cast and crew details (starring actors and director) for a movie or TV show.
    """
    import requests
    cast_crew_url = f'https://api.themoviedb.org/3/{tmdb_type}/{movie_id}/credits?api_key={TMDB_API_KEY}&language=en-US'
    response = requests.get(cast_crew_url)
    cast_crew_data = response.json()

    starring = [member['name'] for member in cast_crew_data.get('cast', [])[:5]]
    director = next((member['name'] for member in cast_crew_data.get('crew', []) if member['job'] == 'Director'), "")
    return {"starring": starring, "director": director}

async def format_tmdb_info(tmdb_type, movie_id, data):
    cast_crew = get_cast_and_crew(tmdb_type, movie_id)

    if tmdb_type == 'movie':
        imdb_id = data.get('imdb_id')
        imdb_info = await get_imdb_details(imdb_id) if imdb_id else {}

        title = imdb_info.get('name') or data.get('title')
        genre = extract_genres(data)
        genre_tags = " ".join([genre_tag_with_emoji(g) for g in genre])
        release_year = imdb_info.get('year') or data.get('release_date', '') if data.get('release_date') else ""
        director = cast_crew.get('director')
        starring = ", ".join(cast_crew.get('starring', [])) if cast_crew.get('starring') else None
        spoken_languages = ", ".join([lang.get('name', '') for lang in data.get('spoken_languages', [])])
        runtime = format_duration(data.get('runtime')) if data.get('runtime') else ""
        rating = imdb_info.get('rating')
        if rating is not None:
            rating_str = f"{rating}"
        else:
            rating_str = None

        plot = truncate_overview(imdb_info.get('plot') or data.get('overview'))

        message = f"<b>🎬 Title:</b> {title}\n"
        message += f"<b>📆 Release:</b> {release_year}\n" if release_year else ""
        message += f"<b>⭐ Rating:</b> {rating_str} / 10\n" if rating_str else ""
        message += f"<b>⏳️ Duration:</b> {runtime}\n" if runtime else ""
        message += f"<b>🅰️ Languages:</b> {spoken_languages}\n" if spoken_languages else ""
        message += f"<b>🔞 Adult:</b> Yes\n" if data.get('adult') else ""
        message += f"<b>⚙️ Genre:</b> {genre_tags}\n" if genre_tags else ""
        message += "\n"
        message += f"<b>📝 Story:</b> {plot}\n\n" if plot else ""
        message += f"<b>🎬 Director:</b> {director}\n" if director else ""
        message += f"<b>🎭 Stars:</b> {starring}\n" if starring else ""

        return message.strip(), title, rating, release_year, plot, imdb_id

    elif tmdb_type == 'tv':
        imdb_id = get_tv_imdb_id_sync(movie_id)
        imdb_info = await get_imdb_details(imdb_id) if imdb_id else {}

        title = imdb_info.get('name') or data.get('name')
        genre = extract_genres(data)
        genre_tags = " ".join([genre_tag_with_emoji(g) for g in genre])
        release_year = imdb_info.get('year') or data.get('first_air_date', '') if data.get('first_air_date') else ""
        director = ", ".join([creator['name'] for creator in data.get('created_by', [])]) if data.get('created_by') else cast_crew.get('director')
        starring = ", ".join(cast_crew.get('starring', [])) if cast_crew.get('starring') else None
        spoken_languages = ", ".join([lang.get('name', '') for lang in data.get('spoken_languages', [])])
        rating = imdb_info.get('rating')
        if rating is not None:
            rating_str = f"{rating}"
        else:
            rating_str = None

        plot = truncate_overview(imdb_info.get('plot') or data.get('overview'))
        
        message = f"<b>📺 Title:</b> {title}\n"
        message += f"<b>📅 Release:</b> {release_year}\n" if release_year else ""
        message += f"<b>📺 Seasons:</b> {data.get('number_of_seasons', '')}\n" if data.get('number_of_seasons') else ""
        message += f"<b>📺 Episodes:</b> {data.get('number_of_episodes', '')}\n" if data.get('number_of_episodes') else ""
        message += f"<b>⭐ Rating:</b> {rating_str} / 10\n" if rating_str else ""
        message += f"<b>🅰️ Languages:</b> {spoken_languages}\n" if spoken_languages else ""
        message += f"<b>🔞 Adult:</b> Yes\n" if data.get('adult') else ""
        message += f"<b>⚙️ Genre:</b> {genre_tags}\n" if genre_tags else ""
        message += "\n"
        message += f"<b>📝 Story:</b> {plot}\n\n" if plot else ""
        message += f"<b>🎬 Director:</b> {director}\n" if director else ""
        message += f"<b>🎭 Stars:</b> {starring}\n" if starring else ""

        return message.strip(), title, rating, release_year, plot, imdb_id
    else:
        return "Unknown type. Unable to format information."
    

def get_tv_imdb_id_sync(tv_id):
    import requests
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/external_ids?api_key={TMDB_API_KEY}"
    resp = requests.get(url)
    data = resp.json()
    return data.get("imdb_id")

async def get_info(tmdb_type, tmdb_id):
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    image_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/images?api_key={TMDB_API_KEY}&language=en-US&include_image_language=en,hi'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as detail_response:
                data = await detail_response.json()
                async with session.get(image_url) as movie_response:
                    images = await movie_response.json()
                message, title, rating, release_year, plot, imdb_id = await format_tmdb_info(tmdb_type, tmdb_id, data)

                poster_path = data.get('poster_path', None)
                if 'backdrops' in images and images['backdrops']:
                    backdrop_path = images['backdrops'][0]['file_path']
                else:
                    backdrop_path = None
                path = backdrop_path or poster_path
                poster_url = f"https://image.tmdb.org/t/p/original{path}" if path else None

                video_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}'
                async with session.get(video_url) as video_response:
                    video_data = await video_response.json()
                    trailer_url = None
                    for video in video_data.get('results', []):
                        if video['site'] == 'YouTube' and video['type'] == 'Trailer':
                            trailer_url = f"https://www.youtube.com/watch?v={video['key']}"
                            break

                return {"message": message, "poster_url": poster_url, "poster_path": poster_path, 
                        "title": title, "rating": rating, "year": release_year, "plot": plot, "trailer_url": trailer_url, "imdb_id": imdb_id}
    except Exception as e:
        logger.error(f"Error fetching TMDB data: {e}")
        return {"message": f"Error: {str(e)}", "poster_url": None, "poster_path": None}

def truncate_overview(overview):
    """
    Truncate the overview if it exceeds the specified limit.

    Args:
    - overview (str): The overview text from the API.

    Returns:
    - str: Truncated overview with an ellipsis if it exceeds the limit.
    """
    if not overview:
        return None
    MAX_OVERVIEW_LENGTH = 600  # Define your maximum character length for the summary
    if len(overview) > MAX_OVERVIEW_LENGTH:
        return overview[:MAX_OVERVIEW_LENGTH] + "..."
    return overview

async def get_movie_id(movie_name, release_year=None):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={movie_name}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()
                if search_data.get('results'):
                    results = search_data['results']
                    if release_year:
                        # Filter by release year if provided
                        results = [
                            result for result in results
                            if 'release_date' in result and result['release_date'] and result['release_date'][:4] == str(release_year)
                        ]
                    if results:
                        result = results[0]
                        return {
                            "id": result['id'],
                            "media_type": "movie"
                        }
        return None
    except Exception as e:
        logger.error(f"Error fetching TMDb movie by name: {e}")
        return

async def get_tv_id(tv_name, first_air_year=None):
    tmdb_search_url = f'https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={tv_name}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(tmdb_search_url) as search_response:
                search_data = await search_response.json()
                if search_data.get('results'):
                    results = search_data['results']
                    if first_air_year:
                        # Filter by first air year if provided
                        results = [
                            result for result in results
                            if 'first_air_date' in result and result['first_air_date'] and result['first_air_date'][:4] == str(first_air_year)
                        ]
                    if results:
                        result = results[0]
                        return {
                            "id": result['id'],
                            "media_type": "tv"
                        }
        return None
    except Exception as e:
        logger.error(f"Error fetching TMDb TV by name: {e}")
        return
    
GENRE_EMOJI_MAP = {
    "Action": "🥊", "Adventure": "🌋", "Animation": "🎬", "Comedy": "😂",
    "Crime": "🕵️", "Documentary": "🎥", "Drama": "🎭", "Family": "👨‍👩‍👧‍👦",
    "Fantasy": "🧙", "History": "📜", "Horror": "👻", "Music": "🎵",
    "Mystery": "🕵️‍♂️", "Romance": "❤️", "ScienceFiction": "🤖",
    "Sci-Fi": "🤖", "SciFi": "🤖", "TV Movie": "📺", "Thriller": "🔪",
    "War": "⚔️", "Western": "🤠", "Sport": "🏆", "Biography": "📖"
}

def clean_genre_name(genre):
    return re.sub(r'[^A-Za-z0-9]', '', genre)

def genre_tag_with_emoji(genre):
    clean_name = clean_genre_name(genre)
    emoji = GENRE_EMOJI_MAP.get(clean_name, "")
    return f"#{clean_name}{' ' + emoji if emoji else ''}"

def extract_genres(data):
    genres = []
    for genre in data.get('genres', []):
        # Split genre names containing '&' into separate genres
        if '&' in genre['name']:
            parts = [g.strip() for g in genre['name'].split('&')]
            genres.extend(parts)
        else:
            genres.append(genre['name'])
    return genres

def format_duration(duration):
    """
    Format duration in minutes to 'Xh YYmin' format.
    """
    try:
        mins = int(duration)
        hours = mins // 60
        mins = mins % 60
        return f"{hours}h {mins:02d}min" if hours else f"{mins}min"
    except Exception:
        return duration or ""
    
async def get_tv_imdb_id(tv_id):
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/external_ids?api_key={TMDB_API_KEY}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return data.get("imdb_id")
