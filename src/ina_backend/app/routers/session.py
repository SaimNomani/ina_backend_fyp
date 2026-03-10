# --- ADD THESE IMPORTS AT THE TOP ---
import uuid
from ..schemas import SessionInitRequest, SessionInitResponse # Ensure you created schemas.py
from ..redis_client import redis_client
from sqlalchemy.future import select
from fastapi import APIRouter, Depends, HTTPException, status
from src.ina_backend.app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from ..models import Tenant
from fastapi_limiter.depends import RateLimiter



router = APIRouter()

# --- NEW ENDPOINT: Week 3 Day 1 ---
@router.post("/init", response_model=SessionInitResponse, dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def initialize_session(
    payload: SessionInitRequest, 
    db: AsyncSession = Depends(get_db)
):
    """
    Week 3 Push Model: Tenant initializes a session.
    1. Validate API Key against Tenants table.
    2. Generate Session ID.
    3. Push Context (MAM, Price) to Redis.
    """
    
    # 1. Validate API Key
    # We query the Tenants table to find the tenant with this api_key
    result = await db.execute(select(Tenant).where(Tenant.client_api_key == payload.api_key))
    tenant = result.scalars().first()
    
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # 2. Generate a unique Session ID for our system
    session_id = str(uuid.uuid4())

    # 3. Store data in Redis
    # Key Format: "session:{session_id}"
    # We store it as a Hash map for easy retrieval of specific fields
    redis_key = f"session:{session_id}"
    
    await redis_client.hset(redis_key, mapping={
        "tenant_id": str(tenant.id),
        "context_id": payload.context_id,
        "mam": str(payload.mam),
        "asking_price": str(payload.asking_price),
        "active": "true"
    })
    
    # Set an expiration (TTL) so Redis doesn't fill up forever (e.g., 24 hours)
    await redis_client.expire(redis_key, 86400)

    # 4. Return the Session ID
    return SessionInitResponse(session_id=session_id, status="initialized")