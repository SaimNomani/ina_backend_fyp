"""
routers/saas_session.py
=======================
SaaS tenant endpoints for session initialisation and price verification.

POST /api/saas/session/init
    - Validates the tenant's public API key via the Origin header.
    - Looks up the product from TenantProduct.
    - Persists a SaasSession row (with mam_snapshot frozen from DB — never
      returned to the browser).
    - Mirrors the session into Redis so the existing AI orchestrator works
      without any changes.
    - Returns { session_id, list_price, currency, expires_at }.

POST /api/saas/session/verify
    - Reads X-INA-Tenant / X-INA-Timestamp / X-INA-Signature headers.
    - Re-derives HMAC-SHA256(webhook_secret, "{timestamp}.{raw_body}") and
      compares in constant time.
    - Inside a single DB transaction checks SaasSession.status == "AGREED"
      and that the final_price matches, then flips status to "VERIFIED".
      This is atomic, so replay attacks are impossible.
    - Returns { valid: true, price, verifiedAt }.
"""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.ina_backend.app.database import get_db
from src.ina_backend.app.models import AllowedDomain, SaasSession, Tenant, TenantProduct
from src.ina_backend.app.redis_client import redis_client

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Schemas (local — no need to pollute the shared schemas.py file)
# ---------------------------------------------------------------------------

class SaasSessionInitRequest(BaseModel):
    """
    Body sent by the tenant's storefront JS widget when a buyer starts
    negotiating.

    publicKey  – the tenant's client_api_key (acts as the "who am I" token)
    productId  – tenant's own external product identifier (TenantProduct.external_id)
    """
    publicKey: str
    productId: str


class SaasSessionInitResponse(BaseModel):
    session_id: str
    list_price: float
    currency: str
    expires_at: datetime


class SaasVerifyRequest(BaseModel):
    """
    Body sent by the tenant's *server-side* webhook handler after the buyer
    agrees to a price.  The signature in the headers is what we validate.
    """
    session_id: str
    final_price: float


class SaasVerifyResponse(BaseModel):
    valid: bool
    price: float
    verifiedAt: datetime


# ---------------------------------------------------------------------------
# Helper: normalise an Origin / Referer value to a bare hostname
# ---------------------------------------------------------------------------

def _normalise_origin(raw: str) -> str:
    """
    Given a value like 'https://shop.example.com' or 'shop.example.com'
    return just the netloc / hostname, lower-cased, with the leading 'www.'
    stripped so tenants don't have to register both variants.

    Raises ValueError if the value cannot be parsed to a hostname.
    """
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        # Treat as a bare host (e.g. already normalised, or from a unit test)
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"Cannot extract hostname from: {raw!r}")
    # Strip optional leading 'www.' for a consistent lookup key
    if host.startswith("www."):
        host = host[4:]
    return host.lower()


# ---------------------------------------------------------------------------
# POST /api/saas/session/init
# ---------------------------------------------------------------------------

SESSION_TTL_HOURS = 24


@router.post("/init", response_model=SaasSessionInitResponse)
async def saas_session_init(
    payload: SaasSessionInitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Initialise a SaaS negotiation session.

    Security model
    --------------
    1. The tenant is identified by *publicKey* (their client_api_key).
    2. The request origin is validated against AllowedDomain to prevent
       other websites from piggybacking on a tenant's key.
    3. MAM is read from the database and stored server-side; it is *never*
       echoed back to the browser.
    """

    # ------------------------------------------------------------------
    # 1. Resolve tenant from publicKey
    # ------------------------------------------------------------------
    result = await db.execute(
        select(Tenant).where(Tenant.client_api_key == payload.publicKey)
    )
    tenant: Tenant | None = result.scalars().first()
    if not tenant:
        logger.warning("saas/init: unknown publicKey=%s", payload.publicKey[:8] + "…")
        raise HTTPException(status_code=401, detail="Invalid public key")

    # ------------------------------------------------------------------
    # 2. Validate the request origin against AllowedDomain
    # ------------------------------------------------------------------
    raw_origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw_origin:
        raise HTTPException(
            status_code=403,
            detail="Missing Origin header — widget must run in a browser context",
        )

    try:
        normalised_host = _normalise_origin(raw_origin)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=f"Unparseable Origin: {exc}") from exc

    domain_result = await db.execute(
        select(AllowedDomain).where(
            and_(
                AllowedDomain.tenant_id == tenant.id,
                AllowedDomain.domain == normalised_host,
            )
        )
    )
    if not domain_result.scalars().first():
        logger.warning(
            "saas/init: origin '%s' not in allowed domains for tenant_id=%s",
            normalised_host,
            tenant.id,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Origin '{normalised_host}' is not authorised for this tenant",
        )

    # ------------------------------------------------------------------
    # 3. Look up the product
    # ------------------------------------------------------------------
    product_result = await db.execute(
        select(TenantProduct).where(
            and_(
                TenantProduct.tenant_id == tenant.id,
                TenantProduct.external_id == payload.productId,
                TenantProduct.active.is_(True),
            )
        )
    )
    product: TenantProduct | None = product_result.scalars().first()
    if not product:
        raise HTTPException(
            status_code=404,
            detail=f"Product '{payload.productId}' not found or inactive",
        )

    # ------------------------------------------------------------------
    # 4. Create the session
    # ------------------------------------------------------------------
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)

    saas_session = SaasSession(
        id=session_id,
        tenant_id=tenant.id,
        product_id=product.id,
        origin_domain=normalised_host,
        mam_snapshot=product.mam,           # frozen at creation — never returned
        list_price_snap=product.list_price,
        status="ACTIVE",
        expires_at=expires_at,
    )
    db.add(saas_session)

    try:
        await db.commit()
        await db.refresh(saas_session)
        logger.info(
            "SaasSession created: session_id=%s tenant_id=%s product=%s",
            session_id,
            tenant.id,
            payload.productId,
        )
    except Exception:
        await db.rollback()
        logger.exception("saas/init: failed to persist SaasSession %s", session_id)
        raise HTTPException(status_code=500, detail="Could not create session")

    # ------------------------------------------------------------------
    # 5. Mirror into Redis (same layout as demo session.py so orchestrator
    #    needs zero changes)
    # ------------------------------------------------------------------
    redis_payload = {
        "tenant_id": str(tenant.id),
        "context_id": session_id,           # use session_id as context key
        "mam": product.mam,                 # orchestrator needs this internally
        "asking_price": product.list_price,
        "active": True,
        "messages": [],
        "offer_count": 0,
        "status": "negotiating",
        "last_bot_offer": None,
        # SaaS-specific marker so orchestrator / analytics can distinguish
        "saas": True,
    }
    try:
        await redis_client.set(
            session_id,
            json.dumps(redis_payload),
            ex=int(timedelta(hours=SESSION_TTL_HOURS).total_seconds()),
        )
    except Exception:
        # Non-fatal — the DB row is the source of truth; Redis is a cache.
        # Log prominently so ops can investigate.
        logger.exception(
            "saas/init: Redis write failed for session %s — orchestrator may not work",
            session_id,
        )

    # ------------------------------------------------------------------
    # 6. Return public info only — MAM is NOT in the response
    # ------------------------------------------------------------------
    return SaasSessionInitResponse(
        session_id=session_id,
        list_price=product.list_price,
        currency=product.currency,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# POST /api/saas/session/verify
# ---------------------------------------------------------------------------

@router.post("/verify", response_model=SaasVerifyResponse)
async def saas_session_verify(
    request: Request,
    payload: SaasVerifyRequest,
    x_ina_tenant: str = Header(..., alias="X-INA-Tenant"),
    x_ina_timestamp: str = Header(..., alias="X-INA-Timestamp"),
    x_ina_signature: str = Header(..., alias="X-INA-Signature"),
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a completed SaaS negotiation price — called by the *tenant's server*,
    never directly from the browser.

    HMAC check
    ----------
    Computes HMAC-SHA256(tenant.webhook_secret, f"{timestamp}.{raw_body}")
    and compares to the X-INA-Signature header in constant time.

    Replay prevention
    -----------------
    Inside a single DB transaction the status is checked for "AGREED" and
    immediately set to "VERIFIED".  Any subsequent call for the same
    session_id will find status != "AGREED" and be rejected.
    """

    # ------------------------------------------------------------------
    # 1. Resolve tenant from X-INA-Tenant header (tenant_id as string)
    # ------------------------------------------------------------------
    try:
        tenant_id_int = int(x_ina_tenant)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-INA-Tenant must be a numeric tenant ID")

    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id_int)
    )
    tenant: Tenant | None = tenant_result.scalars().first()
    if not tenant:
        raise HTTPException(status_code=401, detail="Unknown tenant")

    if not tenant.webhook_secret:
        raise HTTPException(
            status_code=500,
            detail="Tenant has no webhook_secret configured — contact support",
        )

    # ------------------------------------------------------------------
    # 2. Read the raw request body for HMAC computation
    #    FastAPI buffers the body; we can safely read it again after Pydantic
    #    has already parsed `payload`.
    # ------------------------------------------------------------------
    raw_body: bytes = await request.body()

    # ------------------------------------------------------------------
    # 3. HMAC-SHA256 verification
    # ------------------------------------------------------------------
    signing_input = f"{x_ina_timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected_sig = hmac.new(
        tenant.webhook_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, x_ina_signature):
        logger.warning(
            "saas/verify: HMAC mismatch for tenant_id=%s session_id=%s",
            tenant_id_int,
            payload.session_id,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    # ------------------------------------------------------------------
    # 4. Atomic status transition AGREED → VERIFIED (replay-safe)
    # ------------------------------------------------------------------
    async with db.begin_nested():  # savepoint so outer tx stays open
        # SELECT FOR UPDATE to lock the row
        session_result = await db.execute(
            select(SaasSession).where(
                and_(
                    SaasSession.id == payload.session_id,
                    SaasSession.tenant_id == tenant_id_int,
                )
            ).with_for_update()
        )
        saas_session: SaasSession | None = session_result.scalars().first()

        if not saas_session:
            raise HTTPException(status_code=404, detail="Session not found")

        if saas_session.status != "AGREED":
            raise HTTPException(
                status_code=409,
                detail=f"Session is '{saas_session.status}', expected 'AGREED'",
            )

        # Confirm the final_price matches what we stored
        if saas_session.final_price is None or abs(saas_session.final_price - payload.final_price) > 0.001:
            raise HTTPException(
                status_code=409,
                detail="final_price does not match the agreed price on record",
            )

        # Flip to VERIFIED — this is the atomic step that prevents replay
        verified_at = datetime.now(timezone.utc)
        await db.execute(
            update(SaasSession)
            .where(SaasSession.id == payload.session_id)
            .values(status="VERIFIED", verified_at=verified_at)
        )

    # Commit the outer transaction
    await db.commit()

    logger.info(
        "SaasSession VERIFIED: session_id=%s tenant_id=%s price=%.2f",
        payload.session_id,
        tenant_id_int,
        payload.final_price,
    )

    return SaasVerifyResponse(
        valid=True,
        price=payload.final_price,
        verifiedAt=verified_at,
    )
