import os
import redis.asyncio as redis
from dotenv import load_dotenv
from src.ina_backend.app.config import settings

load_dotenv()

REDIS_URL = settings.REDIS_URL


# Create a Redis client instance
# decode_responses=True ensures we get Strings back, not Bytes
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

async def get_redis():
    """Dependency to get the redis client"""
    return redis_client