import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError

from ..database import get_db
from ..models import NegotiationOutcome
from ..schemas import NegotiationOutcomeCreate, NegotiationOutcomeResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/",
    response_model=NegotiationOutcomeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Persist a negotiation outcome",
    description=(
        "Called by the bargaining agent (fire-and-forget) when a negotiation "
        "ends with an ACCEPTED or DEAL outcome. No auth required. "
        "Duplicate session_id returns 409 Conflict."
    ),
)
async def create_negotiation_outcome(
    payload: NegotiationOutcomeCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Persist the final outcome of a negotiation session.

    Steps:
    1. Convert message_history Pydantic models → plain dicts for JSON storage.
    2. Insert a NegotiationOutcome row.
    3. Handle duplicate session_id (retry safety) — return 409.
    4. Return 201 with id, session_id, outcome, created_at.
    """

    # 1. Serialize message_history to plain dicts so SQLAlchemy can store as JSON.
    #    We use `by_alias=True` so the "from" key (not "from_") is written to the DB.
    messages_json = None
    if payload.message_history:
        messages_json = [
            msg.model_dump(by_alias=True, exclude_none=True)
            for msg in payload.message_history
        ]

    # 2. Build the ORM row
    outcome_row = NegotiationOutcome(
        session_id=payload.session_id,
        outcome=payload.outcome,
        asking_price=payload.asking_price,
        final_price=payload.final_price,
        discount_percent=payload.discount_percent,
        total_turns=payload.total_turns,
        user_language=payload.user_language,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        message_history=messages_json,
    )

    db.add(outcome_row)

    try:
        await db.commit()
        await db.refresh(outcome_row)
        logger.info(
            "NegotiationOutcome saved: session_id=%s outcome=%s final_price=%s",
            outcome_row.session_id,
            outcome_row.outcome,
            outcome_row.final_price,
        )
    except IntegrityError:
        # Duplicate session_id — the agent retried an already-persisted outcome.
        await db.rollback()
        logger.warning("Duplicate negotiation outcome ignored: session_id=%s", payload.session_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Outcome for session '{payload.session_id}' already recorded.",
        )

    return outcome_row
