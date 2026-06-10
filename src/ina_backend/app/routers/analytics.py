# # --- IMPORTS ---
# import json
# from ..schemas import AnalyticsLogCreate
# from ..models import AnalyticsLog
# from fastapi import APIRouter, Depends, HTTPException, status
# from sqlalchemy.ext.asyncio import AsyncSession
# from ..redis_client import redis_client
# from src.ina_backend.app.database import get_db
# from sqlalchemy import func
# from ..schemas import AnalyticsSummary
# from ..models import Tenant
# from sqlalchemy.future import select
# from ..auth import get_current_tenant

# # Ensure you have your auth dependency imported (from Week 1)
# # from auth import get_current_tenant

# router=APIRouter()

# # --- NEW ENDPOINT: Week 3 Day 2 ---
# @router.post("/log")
# async def log_analytics(
#     payload: AnalyticsLogCreate,
#     db: AsyncSession = Depends(get_db)
# ):
#     """
#     Week 3 Push Model: The Orchestrator reports the final outcome.
#     1. Check Redis to find which Tenant owns this session_id.
#     2. Save the result to Postgres for long-term storage.
#     """

#     # 1. Retrieve Session Data from Redis
#     #    Sessions are stored as bare UUID keys (no prefix) via SET + json.dumps
#     #    by session.py — so we must use get() + json.loads, not hgetall.
#     raw = await redis_client.get(payload.session_id)

#     if not raw:
#         # Session expired or never existed.
#         raise HTTPException(status_code=404, detail="Session not found or expired")

#     session_data = json.loads(raw)

#     tenant_id = session_data.get("tenant_id")

#     # 2. Create the Log Record in Postgres
#     new_log = AnalyticsLog(
#         session_id=payload.session_id,
#         tenant_id=int(tenant_id),  # Convert string back to int
#         result=payload.result,
#         final_price=payload.final_price,
#         transcript_summary=payload.transcript_summary
#     )

#     db.add(new_log)
#     await db.commit()
#     await db.refresh(new_log)

#     return {"status": "logged", "log_id": new_log.id}


# # --- NEW ENDPOINT: Week 3 Day 3 ---
# @router.get("/", response_model=AnalyticsSummary)
# async def get_analytics(
#     current_tenant: Tenant = Depends(get_current_tenant), # Week 1 Auth
#     db: AsyncSession = Depends(get_db)
# ):
#     """
#     Week 3: Serves the Dashboard.
#     Aggregates data strictly for the authenticated tenant.
#     """
    
#     # 1. Base Query: Filter strictly by the current tenant's ID
#     # We will run separate queries for clarity, though they can be combined.

#     # A. Total Sessions
#     q_total = select(func.count(AnalyticsLog.id)).where(
#         AnalyticsLog.tenant_id == current_tenant.id
#     )
#     result_total = await db.execute(q_total)
#     total_sessions = result_total.scalar() or 0

#     # B. Total Deals (Where result == 'DEAL')
#     q_deals = select(func.count(AnalyticsLog.id)).where(
#         (AnalyticsLog.tenant_id == current_tenant.id) & 
#         (AnalyticsLog.result == "DEAL")
#     )
#     result_deals = await db.execute(q_deals)
#     total_deals = result_deals.scalar() or 0

#     # C. Total Volume (Sum of final_price for DEALs)
#     q_volume = select(func.sum(AnalyticsLog.final_price)).where(
#         (AnalyticsLog.tenant_id == current_tenant.id) & 
#         (AnalyticsLog.result == "DEAL")
#     )
#     result_volume = await db.execute(q_volume)
#     total_volume = result_volume.scalar() or 0.0

#     # D. Average Price (Avoid division by zero)
#     average_price = 0.0
#     if total_deals > 0:
#         average_price = total_volume / total_deals

#     return AnalyticsSummary(
#         total_sessions=total_sessions,
#         total_deals=total_deals,
#         total_volume=total_volume,
#         average_price=average_price
#     )

# -------------------------------------------------------------------------------------
# -------------------------------------------------------------------------------------
# -------------------------------------------------------------------------------------
# -------------------------------------------------------------------------------------

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, cast, Date, case

from ..database import get_db
from ..models import Tenant, AnalyticsLog, NegotiationOutcome, SaasSession
from ..schemas import AnalyticsLogCreate, AnalyticsSummary
from ..auth import get_current_tenant
from ..redis_client import redis_client

logger = logging.getLogger(__name__)
router = APIRouter()

# Outcomes the orchestrator uses to signal a closed deal
_DEAL_OUTCOMES = ("ACCEPTED", "DEAL")


# ──────────────────────────────────────────────────────────────────────────────
# POST /log  — legacy endpoint kept for demo/backward compat
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/log")
async def log_analytics(
    payload: AnalyticsLogCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Demo-flow push model: orchestrator reports outcome.
    Requires the session to still be alive in Redis (demo sessions only).
    SaaS sessions are tracked via NegotiationOutcome → SaasSession.
    """
    raw = await redis_client.get(payload.session_id)
    if not raw:
        raise HTTPException(
            status_code=404, detail="Session not found or expired")

    session_data = json.loads(raw)
    tenant_id = session_data.get("tenant_id")

    new_log = AnalyticsLog(
        session_id=payload.session_id,
        tenant_id=int(tenant_id),
        result=payload.result,
        final_price=payload.final_price,
        transcript_summary=payload.transcript_summary,
    )
    db.add(new_log)
    await db.commit()
    await db.refresh(new_log)
    return {"status": "logged", "log_id": new_log.id}


# ──────────────────────────────────────────────────────────────────────────────
# GET /  — aggregate summary for the four stat cards
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/", response_model=AnalyticsSummary)
async def get_analytics(
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Reads from NegotiationOutcome joined through SaasSession for tenant isolation.
    This is where the orchestrator actually writes data.
    """
    tid = current_tenant.id
    join_cond = NegotiationOutcome.session_id == SaasSession.id

    # A. Total sessions (Chats) — every SaasSession row for this tenant
    # Decoupled from NegotiationOutcome to include active & abandoned chats
    q_total = (
        select(func.count(SaasSession.id))
        .where(SaasSession.tenant_id == tid)
    )
    total_sessions: int = (await db.execute(q_total)).scalar() or 0

    # B. Total deals — only ACCEPTED or DEAL outcomes
    q_deals = (
        select(func.count(NegotiationOutcome.id))
        .join(SaasSession, join_cond)
        .where(
            SaasSession.tenant_id == tid,
            NegotiationOutcome.outcome.in_(_DEAL_OUTCOMES),
        )
    )
    total_deals: int = (await db.execute(q_deals)).scalar() or 0

    # C. Total volume — sum of final_price for closed deals
    q_volume = (
        select(func.coalesce(func.sum(NegotiationOutcome.final_price), 0.0))
        .join(SaasSession, join_cond)
        .where(
            SaasSession.tenant_id == tid,
            NegotiationOutcome.outcome.in_(_DEAL_OUTCOMES),
        )
    )
    total_volume: float = float((await db.execute(q_volume)).scalar() or 0.0)

    average_price = (total_volume / total_deals) if total_deals > 0 else 0.0

    return AnalyticsSummary(
        total_sessions=total_sessions,
        total_deals=total_deals,
        total_volume=total_volume,
        average_price=average_price,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /daily  — last-7-days breakdown for the chart
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/daily")
async def get_daily_analytics(
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns per-day counts for the rolling 7-day window ending today.
    Response: [{ label: "Mon", date: "2025-06-01", total: 12, deals: 7 }, ...]
    Missing days are filled with zeros — the frontend never needs to handle gaps.
    """
    tid = current_tenant.id
    today = datetime.now(timezone.utc).date()
    window_start = datetime(
        today.year, today.month, today.day, tzinfo=timezone.utc
    ) - timedelta(days=6)

    # 1. Total Chats per day (from SaasSession directly)
    q_chats = (
        select(
            cast(SaasSession.created_at, Date).label("day"),
            func.count(SaasSession.id).label("total"),
        )
        .where(
            SaasSession.tenant_id == tid,
            SaasSession.created_at >= window_start,
        )
        .group_by(cast(SaasSession.created_at, Date))
    )
    chat_rows = (await db.execute(q_chats)).all()

    # 2. Deals Closed per day (from NegotiationOutcome)
    q_deals = (
        select(
            cast(NegotiationOutcome.created_at, Date).label("day"),
            func.count(NegotiationOutcome.id).label("deals"),
        )
        .join(SaasSession, NegotiationOutcome.session_id == SaasSession.id)
        .where(
            SaasSession.tenant_id == tid,
            NegotiationOutcome.created_at >= window_start,
            NegotiationOutcome.outcome.in_(_DEAL_OUTCOMES),
        )
        .group_by(cast(NegotiationOutcome.created_at, Date))
    )
    deal_rows = (await db.execute(q_deals)).all()

    # Merge into O(1) lookup map
    day_map = {}
    for r in chat_rows:
        day_map[str(r.day)] = {"total": r.total, "deals": 0}
        
    for r in deal_rows:
        day_str = str(r.day)
        if day_str not in day_map:
            day_map[day_str] = {"total": 0, "deals": 0}
        day_map[day_str]["deals"] = r.deals

    # Build a guaranteed 7-entry list, filling gaps with zeros
    result = []
    for offset in range(7):
        d = today - timedelta(days=6 - offset)
        key = str(d)
        entry = day_map.get(key, {"total": 0, "deals": 0})
        result.append(
            {
                "label": d.strftime("%a"),  # "Mon", "Tue", …
                "date": key,
                "total": entry["total"],
                "deals": entry["deals"],
            }
        )

    return result
