import uuid
import json
import logging

from sqlalchemy import null
from ..schemas import SessionInitRequest, SessionInitResponse
from ..redis_client import redis_client
from sqlalchemy.future import select
from fastapi import APIRouter, Depends, HTTPException
from src.ina_backend.app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from ..models import Tenant, NegotiationOutcome
from fastapi_limiter.depends import RateLimiter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/init", response_model=SessionInitResponse, dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def initialize_session(
    payload: SessionInitRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Tenant initializes a negotiation session.
    1. Validate API Key against Tenants table.
    2. Generate Session ID.
    3. Store session as a JSON string in Redis (SET, not HSET).

    Key format  : bare session_id UUID (no prefix)
    Value format: JSON string
    Reason      : AI Orchestrator reads via client.get(session_id) + json.loads(),
                  so both the key format and value type must match exactly.
    """

    # 1. Validate API Key
    result = await db.execute(select(Tenant).where(Tenant.client_api_key == payload.api_key))
    tenant = result.scalars().first()

    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # 2. Generate a unique Session ID
    session_id = str(uuid.uuid4())

    # 3. Store session in Redis
    #    - Key  : bare UUID (no "session:" prefix) — matches client.get(session_id) in Orchestrator
    #    - Value: JSON string                      — matches json.loads(raw) in Orchestrator
    #    - TTL  : 24 hours
    session_data = {
        "tenant_id": str(tenant.id),
        "context_id": payload.context_id,
        "mam": payload.mam,           # stored as float, not string
        "asking_price": payload.asking_price,
        "active": True,
        "messages": [],               # pre-initialize so orchestrator can safely append
        "offer_count": 0,
        "status": "negotiating",
        "last_bot_offer": None,
    }
    try:
        await redis_client.set(session_id, json.dumps(session_data), ex=86400)
        logger.info("Session stored: session_id=%s tenant_id=%s",
                    session_id, tenant.id)
    except Exception:
        logger.exception("Failed to write session %s to Redis", session_id)
        raise HTTPException(
            status_code=503, detail="Session store unavailable")

    # 4. Return Session ID to frontend
    return SessionInitResponse(session_id=session_id, status="initialized")


@router.get("/{session_id}/final_price", summary="Get final price for a completed session")
async def get_final_price(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch the final agreed price for a completed negotiation by session ID.
    """
    result = await db.execute(
        select(NegotiationOutcome.final_price)
        .where(NegotiationOutcome.session_id == session_id)
    )
    final_price = result.scalars().first()

    if final_price is None:
        raise HTTPException(
            status_code=404, detail="Outcome not found for this session")

    return {"session_id": session_id, "final_price": final_price}
