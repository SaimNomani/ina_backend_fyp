# --- IMPORTS ---
from ..schemas import AnalyticsLogCreate
from ..models import AnalyticsLog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..redis_client import redis_client
from src.ina_backend.app.database import get_db
from sqlalchemy import func
from ..schemas import AnalyticsSummary
from ..models import Tenant
from sqlalchemy.future import select
from ..auth import get_current_tenant

# Ensure you have your auth dependency imported (from Week 1)
# from auth import get_current_tenant

router=APIRouter()

# --- NEW ENDPOINT: Week 3 Day 2 ---
@router.post("/log")
async def log_analytics(
    payload: AnalyticsLogCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Week 3 Push Model: The Orchestrator reports the final outcome.
    1. Check Redis to find which Tenant owns this session_id.
    2. Save the result to Postgres for long-term storage.
    """

    # 1. Retrieve Session Data from Redis
    redis_key = f"session:{payload.session_id}"
    session_data = await redis_client.hgetall(redis_key)

    if not session_data:
        # If Redis returns empty, the session might have expired or is invalid.
        # We can either reject it or save it as 'Unknown Tenant'. 
        # For now, let's reject it to ensure data integrity.
        raise HTTPException(status_code=404, detail="Session not found or expired")

    tenant_id = session_data.get("tenant_id")

    # 2. Create the Log Record in Postgres
    new_log = AnalyticsLog(
        session_id=payload.session_id,
        tenant_id=int(tenant_id),  # Convert string back to int
        result=payload.result,
        final_price=payload.final_price,
        transcript_summary=payload.transcript_summary
    )

    db.add(new_log)
    await db.commit()
    await db.refresh(new_log)

    return {"status": "logged", "log_id": new_log.id}


# --- NEW ENDPOINT: Week 3 Day 3 ---
@router.get("/", response_model=AnalyticsSummary)
async def get_analytics(
    current_tenant: Tenant = Depends(get_current_tenant), # Week 1 Auth
    db: AsyncSession = Depends(get_db)
):
    """
    Week 3: Serves the Dashboard.
    Aggregates data strictly for the authenticated tenant.
    """
    
    # 1. Base Query: Filter strictly by the current tenant's ID
    # We will run separate queries for clarity, though they can be combined.

    # A. Total Sessions
    q_total = select(func.count(AnalyticsLog.id)).where(
        AnalyticsLog.tenant_id == current_tenant.id
    )
    result_total = await db.execute(q_total)
    total_sessions = result_total.scalar() or 0

    # B. Total Deals (Where result == 'DEAL')
    q_deals = select(func.count(AnalyticsLog.id)).where(
        (AnalyticsLog.tenant_id == current_tenant.id) & 
        (AnalyticsLog.result == "DEAL")
    )
    result_deals = await db.execute(q_deals)
    total_deals = result_deals.scalar() or 0

    # C. Total Volume (Sum of final_price for DEALs)
    q_volume = select(func.sum(AnalyticsLog.final_price)).where(
        (AnalyticsLog.tenant_id == current_tenant.id) & 
        (AnalyticsLog.result == "DEAL")
    )
    result_volume = await db.execute(q_volume)
    total_volume = result_volume.scalar() or 0.0

    # D. Average Price (Avoid division by zero)
    average_price = 0.0
    if total_deals > 0:
        average_price = total_volume / total_deals

    return AnalyticsSummary(
        total_sessions=total_sessions,
        total_deals=total_deals,
        total_volume=total_volume,
        average_price=average_price
    )