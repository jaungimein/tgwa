
from cachetools import TTLCache

# Cache for user file counts
user_file_count = TTLCache(maxsize=1000, ttl=3600)

# Cache for query IDs
query_id_map = TTLCache(maxsize=1000, ttl=300)

# Cache for search API results
search_api_cache = TTLCache(maxsize=100, ttl=300)

# Cache for search results
search_cache = TTLCache(maxsize=100, ttl=300)
