# """
# routers/saas_session.py
# =======================
# SaaS tenant endpoints for session initialisation and price verification.

# POST /api/saas/session/init
#     - Validates the tenant's public API key via the Origin header.
#     - Looks up the product from TenantProduct.
#     - Persists a SaasSession row (with mam_snapshot frozen from DB — never
#       returned to the browser).
#     - Mirrors the session into Redis so the existing AI orchestrator works
#       without any changes.
#     - Returns { session_id, list_price, currency, expires_at }.

# POST /api/saas/session/verify
#     - Reads X-INA-Tenant / X-INA-Timestamp / X-INA-Signature headers.
#     - Re-derives HMAC-SHA256(webhook_secret, "{timestamp}.{raw_body}") and
#       compares in constant time.
#     - Inside a single DB transaction checks SaasSession.status == "AGREED"
#       and that the final_price matches, then flips status to "VERIFIED".
#       This is atomic, so replay attacks are impossible.
#     - Returns { valid: true, price, verifiedAt }.
# """

# import hashlib
# import hmac
# import json
# import logging
# import uuid
# from datetime import datetime, timedelta, timezone
# from urllib.parse import urlparse

# from fastapi import APIRouter, Depends, Header, HTTPException, Request
# from pydantic import BaseModel
# from sqlalchemy import and_, select, update
# from sqlalchemy.ext.asyncio import AsyncSession
# from typing import List, Optional

# from src.ina_backend.app.database import get_db
# from src.ina_backend.app.models import AllowedDomain, SaasSession, Tenant, TenantProduct, NegotiationOutcome
# from src.ina_backend.app.redis_client import redis_client

# logger = logging.getLogger(__name__)

# router = APIRouter()

# # ---------------------------------------------------------------------------
# # Schemas (local — no need to pollute the shared schemas.py file)
# # ---------------------------------------------------------------------------

# class SaasSessionInitRequest(BaseModel):
#     """
#     Body sent by the tenant's storefront JS widget when a buyer starts
#     negotiating.

#     publicKey  – the tenant's client_api_key (acts as the "who am I" token)
#     productId  – tenant's own external product identifier (TenantProduct.external_id)
#     """
#     publicKey: str
#     productId: str


# class SaasSessionInitResponse(BaseModel):
#     session_id: str
#     list_price: float
#     currency: str
#     expires_at: datetime





# class SaasVerifyRequest(BaseModel):
#     """
#     Body sent by the tenant's *server-side* webhook handler after the buyer
#     agrees to a price.  The signature in the headers is what we validate.
#     """
#     session_id: str
#     final_price: float


# class SaasVerifyResponse(BaseModel):
#     valid: bool
#     price: float
#     verifiedAt: datetime


# # ---------------------------------------------------------------------------
# # Helper: normalise an Origin / Referer value to a bare hostname
# # ---------------------------------------------------------------------------

# def _normalise_origin(raw: str) -> str:
#     """
#     Given a value like 'https://shop.example.com' or 'shop.example.com'
#     return just the netloc / hostname, lower-cased, with the leading 'www.'
#     stripped so tenants don't have to register both variants.

#     Raises ValueError if the value cannot be parsed to a hostname.
#     """
#     raw = raw.strip()
#     if not raw.startswith(("http://", "https://")):
#         # Treat as a bare host (e.g. already normalised, or from a unit test)
#         raw = "https://" + raw
#     parsed = urlparse(raw)
#     host = parsed.hostname or ""
#     if not host:
#         raise ValueError(f"Cannot extract hostname from: {raw!r}")
#     # Strip optional leading 'www.' for a consistent lookup key
#     if host.startswith("www."):
#         host = host[4:]
#     return host.lower()


# # ---------------------------------------------------------------------------
# # POST /api/saas/session/init
# # ---------------------------------------------------------------------------

# SESSION_TTL_HOURS = 24


# @router.post("/init", response_model=SaasSessionInitResponse)
# async def saas_session_init(
#     payload: SaasSessionInitRequest,
#     request: Request,
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Initialise a SaaS negotiation session.

#     Security model
#     --------------
#     1. The tenant is identified by *publicKey* (their client_api_key).
#     2. The request origin is validated against AllowedDomain to prevent
#        other websites from piggybacking on a tenant's key.
#     3. MAM is read from the database and stored server-side; it is *never*
#        echoed back to the browser.
#     """

#     # ------------------------------------------------------------------
#     # 1. Resolve tenant from publicKey
#     # ------------------------------------------------------------------
#     result = await db.execute(
#         select(Tenant).where(Tenant.client_api_key == payload.publicKey)
#     )
#     tenant: Tenant | None = result.scalars().first()
#     if not tenant:
#         logger.warning("saas/init: unknown publicKey=%s", payload.publicKey[:8] + "…")
#         raise HTTPException(status_code=401, detail="Invalid public key")

#     # ------------------------------------------------------------------
#     # 2. Validate the request origin against AllowedDomain
#     # ------------------------------------------------------------------
#     raw_origin = request.headers.get("origin") or request.headers.get("referer") or ""
#     if not raw_origin:
#         raise HTTPException(
#             status_code=403,
#             detail="Missing Origin header — widget must run in a browser context",
#         )

#     try:
#         normalised_host = _normalise_origin(raw_origin)
#     except ValueError as exc:
#         raise HTTPException(status_code=403, detail=f"Unparseable Origin: {exc}") from exc

#     domain_result = await db.execute(
#         select(AllowedDomain).where(
#             and_(
#                 AllowedDomain.tenant_id == tenant.id,
#                 AllowedDomain.domain == normalised_host,
#             )
#         )
#     )
#     if not domain_result.scalars().first():
#         logger.warning(
#             "saas/init: origin '%s' not in allowed domains for tenant_id=%s",
#             normalised_host,
#             tenant.id,
#         )
#         raise HTTPException(
#             status_code=403,
#             detail=f"Origin '{normalised_host}' is not authorised for this tenant",
#         )

#     # ------------------------------------------------------------------
#     # 3. Look up the product
#     # ------------------------------------------------------------------
#     product_result = await db.execute(
#         select(TenantProduct).where(
#             and_(
#                 TenantProduct.tenant_id == tenant.id,
#                 TenantProduct.external_id == payload.productId,
#                 TenantProduct.active.is_(True),
#             )
#         )
#     )
#     product: TenantProduct | None = product_result.scalars().first()
#     if not product:
#         raise HTTPException(
#             status_code=404,
#             detail=f"Product '{payload.productId}' not found or inactive",
#         )

#     # ------------------------------------------------------------------
#     # 4. Create the session
#     # ------------------------------------------------------------------
#     session_id = str(uuid.uuid4())
#     now = datetime.now(timezone.utc)
#     expires_at = now + timedelta(hours=SESSION_TTL_HOURS)

#     saas_session = SaasSession(
#         id=session_id,
#         tenant_id=tenant.id,
#         product_id=product.id,
#         origin_domain=normalised_host,
#         mam_snapshot=product.mam,           # frozen at creation — never returned
#         list_price_snap=product.list_price,
#         status="ACTIVE",
#         expires_at=expires_at,
#     )
#     db.add(saas_session)

#     try:
#         await db.commit()
#         await db.refresh(saas_session)
#         logger.info(
#             "SaasSession created: session_id=%s tenant_id=%s product=%s",
#             session_id,
#             tenant.id,
#             payload.productId,
#         )
#     except Exception:
#         await db.rollback()
#         logger.exception("saas/init: failed to persist SaasSession %s", session_id)
#         raise HTTPException(status_code=500, detail="Could not create session")

#     # ------------------------------------------------------------------
#     # 5. Mirror into Redis (same layout as demo session.py so orchestrator
#     #    needs zero changes)
#     # ------------------------------------------------------------------
#     redis_payload = {
#         "tenant_id": str(tenant.id),
#         "context_id": session_id,           # use session_id as context key
#         "mam": product.mam,                 # orchestrator needs this internally
#         "asking_price": product.list_price,
#         "active": True,
#         "messages": [],
#         "offer_count": 0,
#         "status": "negotiating",
#         "last_bot_offer": None,
#         # SaaS-specific marker so orchestrator / analytics can distinguish
#         "saas": True,
#     }
#     try:
#         await redis_client.set(
#             session_id,
#             json.dumps(redis_payload),
#             ex=int(timedelta(hours=SESSION_TTL_HOURS).total_seconds()),
#         )
#     except Exception:
#         # Non-fatal — the DB row is the source of truth; Redis is a cache.
#         # Log prominently so ops can investigate.
#         logger.exception(
#             "saas/init: Redis write failed for session %s — orchestrator may not work",
#             session_id,
#         )

#     # ------------------------------------------------------------------
#     # 6. Return public info only — MAM is NOT in the response
#     # ------------------------------------------------------------------
#     return SaasSessionInitResponse(
#         session_id=session_id,
#         list_price=product.list_price,
#         currency=product.currency,
#         expires_at=expires_at,
#     )




# # ---------------------------------------------------------------------------
# # POST /api/saas/session/verify
# # ---------------------------------------------------------------------------

# @router.post("/verify", response_model=SaasVerifyResponse)
# async def saas_session_verify(
#     request: Request,
#     payload: SaasVerifyRequest,
#     x_ina_tenant: str = Header(..., alias="X-INA-Tenant"),
#     x_ina_timestamp: str = Header(..., alias="X-INA-Timestamp"),
#     x_ina_signature: str = Header(..., alias="X-INA-Signature"),
#     db: AsyncSession = Depends(get_db),
# ):
#     """
#     Verify a completed SaaS negotiation price — called by the *tenant's server*,
#     never directly from the browser.

#     HMAC check
#     ----------
#     Computes HMAC-SHA256(tenant.webhook_secret, f"{timestamp}.{raw_body}")
#     and compares to the X-INA-Signature header in constant time.

#     Replay prevention
#     -----------------
#     Inside a single DB transaction the status is checked for "AGREED" and
#     immediately set to "VERIFIED".  Any subsequent call for the same
#     session_id will find status != "AGREED" and be rejected.
#     """

#     # ------------------------------------------------------------------
#     # 1. Resolve tenant from X-INA-Tenant header (tenant_id as string)
#     # ------------------------------------------------------------------
#     try:
#         tenant_id_int = int(x_ina_tenant)
#     except ValueError:
#         raise HTTPException(status_code=400, detail="X-INA-Tenant must be a numeric tenant ID")

#     tenant_result = await db.execute(
#         select(Tenant).where(Tenant.id == tenant_id_int)
#     )
#     tenant: Tenant | None = tenant_result.scalars().first()
#     if not tenant:
#         raise HTTPException(status_code=401, detail="Unknown tenant")

#     if not tenant.webhook_secret:
#         raise HTTPException(
#             status_code=500,
#             detail="Tenant has no webhook_secret configured — contact support",
#         )

#     # ------------------------------------------------------------------
#     # 2. Read the raw request body for HMAC computation
#     #    FastAPI buffers the body; we can safely read it again after Pydantic
#     #    has already parsed `payload`.
#     # ------------------------------------------------------------------
#     raw_body: bytes = await request.body()

#     # ------------------------------------------------------------------
#     # 3. HMAC-SHA256 verification
#     # ------------------------------------------------------------------
#     signing_input = f"{x_ina_timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
#     expected_sig = hmac.new(
#         tenant.webhook_secret.encode("utf-8"),
#         signing_input,
#         hashlib.sha256,
#     ).hexdigest()

#     if not hmac.compare_digest(expected_sig, x_ina_signature):
#         logger.warning(
#             "saas/verify: HMAC mismatch for tenant_id=%s session_id=%s",
#             tenant_id_int,
#             payload.session_id,
#         )
#         raise HTTPException(status_code=401, detail="Invalid signature")

#     # ------------------------------------------------------------------
#     # 4. Atomic status transition AGREED → VERIFIED (replay-safe)
#     # ------------------------------------------------------------------
#     async with db.begin_nested():  # savepoint so outer tx stays open
#         # SELECT FOR UPDATE to lock the row
#         session_result = await db.execute(
#             select(SaasSession).where(
#                 and_(
#                     SaasSession.id == payload.session_id,
#                     SaasSession.tenant_id == tenant_id_int,
#                 )
#             ).with_for_update()
#         )
#         saas_session: SaasSession | None = session_result.scalars().first()

#         if not saas_session:
#             raise HTTPException(status_code=404, detail="Session not found")

#         if saas_session.status != "AGREED":
#             raise HTTPException(
#                 status_code=409,
#                 detail=f"Session is '{saas_session.status}', expected 'AGREED'",
#             )

#         # Confirm the final_price matches what we stored
#         if saas_session.final_price is None or abs(saas_session.final_price - payload.final_price) > 0.001:
#             raise HTTPException(
#                 status_code=409,
#                 detail="final_price does not match the agreed price on record",
#             )

#         # Flip to VERIFIED — this is the atomic step that prevents replay
#         verified_at = datetime.now(timezone.utc)
#         await db.execute(
#             update(SaasSession)
#             .where(SaasSession.id == payload.session_id)
#             .values(status="VERIFIED", verified_at=verified_at)
#         )

#     # Commit the outer transaction
#     await db.commit()

#     logger.info(
#         "SaasSession VERIFIED: session_id=%s tenant_id=%s price=%.2f",
#         payload.session_id,
#         tenant_id_int,
#         payload.final_price,
#     )

#     return SaasVerifyResponse(
#         valid=True,
#         price=payload.final_price,
#         verifiedAt=verified_at,
#     )

import hashlib
import hmac
import json                # ← ADDED: needed for Redis serialization
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, select, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from src.ina_backend.app.database import get_db
from src.ina_backend.app.models import (
    AllowedDomain, SaasSession, Tenant, TenantProduct, NegotiationOutcome,
)
from src.ina_backend.app.redis_client import redis_client

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class SaasSessionInitRequest(BaseModel):
    publicKey: str
    productId: str
    anonId:    str


class SaasSessionInitResponse(BaseModel):
    session_id:      str
    list_price:      float
    currency:        str
    expires_at:      datetime
    resumed:         bool = False
    status:          str = "ACTIVE"
    final_price:     Optional[float] = None
    message_history: Optional[List[dict]] = None
    # ← ADDED: restores widget counter on resume
    offer_count:     Optional[int] = None


class SaasVerifyRequest(BaseModel):
    session_id:  str
    final_price: float


class SaasVerifyResponse(BaseModel):
    valid:      bool
    price:      float
    verifiedAt: datetime


SESSION_TTL_HOURS = 24


# ── Redis write-through helper ────────────────────────────────────────────────

def _build_redis_payload(
    *,
    mam:          float,
    asking_price: float,
    tenant_id:    int,
    product_id:   str,
    created_at:   str,
    messages:     list = None,
    offer_count:  int = 0,
    # orchestrator uses "negotiating", NOT "ACTIVE"
    status:       str = "negotiating",
    last_bot_offer: Optional[float] = None,
) -> str:
    """
    Serialize the session payload into the exact JSON format that the
    orchestrator's `SessionData` Pydantic model expects.

    Field mapping (Postgres → Redis / orchestrator):
        SaasSession.mam_snapshot    → mam
        SaasSession.list_price_snap → asking_price

    MAM is deliberately included here — this string lives in Redis (server-side)
    and is NEVER returned to the browser by any endpoint.
    """
    return json.dumps({
        "mam":            float(mam),
        "asking_price":   float(asking_price),
        "messages":       messages or [],
        "offer_count":    offer_count,
        "status":         status,
        "last_bot_offer": last_bot_offer,
        "tenant_id":      str(tenant_id),
        "product_id":     str(product_id),
        "created_at":     created_at,
    })


# ── Domain normaliser ─────────────────────────────────────────────────────────

def _normalise_origin(raw: str) -> str:
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.lower()


# ── Advisory-lock helper (prevents duplicate sessions on rapid reload) ────────

def _session_lock_key(tenant_id: int, product_id: int, anon_id: str) -> int:
    """
    Deterministic bigint from (tenant, product, anon) for pg_advisory_xact_lock.
    Concurrent /init calls with the same triple are serialised so the second
    caller always sees the row committed by the first.
    """
    raw = f"saas_session:{tenant_id}:{product_id}:{anon_id}"
    digest = hashlib.md5(raw.encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


# ── Rate-limit helper ─────────────────────────────────────────────────────────

async def _is_new_session_rate_limited(
    redis,
    tenant_id:           int,
    product_external_id: str,
    anon_id:             str,
    client_ip:           str,
) -> bool:
    """
    Two-layer rate limit — primary: anon_id (≤2/24h), secondary: IP (≤5/24h).
    Returns True if either layer is saturated.
    """
    WINDOW = SESSION_TTL_HOURS * 3600

    key_anon = f"ina:newsess:{tenant_id}:{product_external_id}:{anon_id}"
    count_anon = await redis.incr(key_anon)
    if count_anon == 1:
        await redis.expire(key_anon, WINDOW)
    if count_anon > 2:
        return True

    key_ip = f"ina:newsess_ip:{tenant_id}:{product_external_id}:{client_ip}"
    count_ip = await redis.incr(key_ip)
    if count_ip == 1:
        await redis.expire(key_ip, WINDOW)
    if count_ip > 5:
        return True

    return False


# ═════════════════════════════════════════════════════════════════════════════
# POST /init
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/init", response_model=SaasSessionInitResponse)
async def saas_session_init(
    request: Request,
    payload: SaasSessionInitRequest,
    db:      AsyncSession = Depends(get_db),
):
    # ── 1. Authenticate tenant via publicKey ─────────────────────────────────
    result = await db.execute(
        select(Tenant).where(Tenant.client_api_key == payload.publicKey)
    )
    tenant: Optional[Tenant] = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=401, detail="Invalid public key.")

    # ── 2. Origin validation ─────────────────────────────────────────────────
    raw_origin = request.headers.get(
        "origin") or request.headers.get("referer", "")
    normalised = _normalise_origin(raw_origin)
    allowed_q = await db.execute(
        select(AllowedDomain).where(
            AllowedDomain.tenant_id == tenant.id,
            AllowedDomain.domain == normalised,
        )
    )
    if not allowed_q.scalar_one_or_none():
        raise HTTPException(
            status_code=403,
            detail=f"Origin '{normalised}' is not in your allowed domains list.",
        )

    # ── 3. Resolve product ───────────────────────────────────────────────────
    prod_q = await db.execute(
        select(TenantProduct).where(
            TenantProduct.tenant_id == tenant.id,
            TenantProduct.external_id == payload.productId,
            TenantProduct.active == True,
        )
    )
    product: Optional[TenantProduct] = prod_q.scalar_one_or_none()
    if not product:
        raise HTTPException(
            status_code=404,
            detail=f"Product '{payload.productId}' not found or inactive.",
        )

    # ── 3b. Serialize concurrent requests for same (tenant, product, user) ────
    #    pg_advisory_xact_lock blocks until the transaction holding the same key
    #    commits or rolls back.  This turns the SELECT-then-INSERT below into an
    #    effectively atomic check-and-create, eliminating duplicates caused by
    #    rapid widget reloads or double-fires.
    lock_key = _session_lock_key(tenant.id, product.id, payload.anonId)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    # ── 4. PRIORITY 1: Resume existing ACTIVE/AGREED session ─────────────────
    now = datetime.now(timezone.utc)
    existing_q = await db.execute(
        select(SaasSession)
        .where(
            and_(
                SaasSession.tenant_id == tenant.id,
                SaasSession.product_id == product.id,
                SaasSession.anon_id == payload.anonId,
                SaasSession.status.in_(["ACTIVE", "AGREED"]),
                SaasSession.expires_at > now,
            )
        )
        .order_by(SaasSession.created_at.desc())
        .limit(1)
    )
    existing: Optional[SaasSession] = existing_q.scalar_one_or_none()

    if existing:
        logger.info(
            "Session RESUMED  session_id=%s  status=%s  anon_id=%s",
            existing.id, existing.status, payload.anonId,
        )

        # ── AGREED: negotiation already finished ─────────────────────────────
        if existing.status == "AGREED":
            message_history = None
            outcome_q = await db.execute(
                select(NegotiationOutcome).where(
                    NegotiationOutcome.session_id == existing.id
                )
            )
            outcome = outcome_q.scalar_one_or_none()
            if outcome and outcome.message_history:
                message_history = outcome.message_history

            return SaasSessionInitResponse(
                session_id=existing.id,
                list_price=existing.list_price_snap,
                currency=product.currency,
                expires_at=existing.expires_at,
                resumed=True,
                status="AGREED",
                final_price=existing.final_price,
                message_history=message_history,
                offer_count=None,
            )

        # ── ACTIVE: negotiation still in progress ─────────────────────────────
        # Check whether the Redis entry is still alive.
        # If yes  → refresh its TTL and read offer_count for the widget.
        # If gone → re-seed Redis so the next chat message passes validation.
        offer_count_from_redis = 0
        remaining_ttl = max(
            int((existing.expires_at - now).total_seconds()), 0)

        raw_redis = await redis_client.get(existing.id)
        if raw_redis:
            try:
                cached = json.loads(raw_redis)
                offer_count_from_redis = cached.get("offer_count", 0)
            except (json.JSONDecodeError, KeyError):
                pass
            if remaining_ttl > 0:
                await redis_client.expire(existing.id, remaining_ttl)
                logger.info("Redis TTL refreshed  session_id=%s  new_ttl=%ss",
                            existing.id, remaining_ttl)
        else:
            # Redis entry expired — re-seed with fresh state so the orchestrator
            # can still validate upcoming chat messages against this session.
            if remaining_ttl > 0:
                created_iso = (
                    existing.created_at.isoformat()
                    if existing.created_at
                    else now.isoformat()
                )
                redis_payload = _build_redis_payload(
                    mam=existing.mam_snapshot,
                    asking_price=existing.list_price_snap,
                    tenant_id=existing.tenant_id,
                    product_id=payload.productId,
                    created_at=created_iso,
                )
                await redis_client.set(existing.id, redis_payload, ex=remaining_ttl)
                logger.info("Redis re-seeded for stale ACTIVE session  session_id=%s  ttl=%ss",
                            existing.id, remaining_ttl)

        return SaasSessionInitResponse(
            session_id=existing.id,
            list_price=existing.list_price_snap,
            currency=product.currency,
            expires_at=existing.expires_at,
            resumed=True,
            status="ACTIVE",
            final_price=None,
            message_history=None,
            offer_count=offer_count_from_redis,
        )

    # ── 5. PRIORITY 2: Rate-limit check ──────────────────────────────────────
    client_ip = request.client.host
    if await _is_new_session_rate_limited(
        redis_client, tenant.id, payload.productId, payload.anonId, client_ip
    ):
        raise HTTPException(
            status_code=429,
            detail=(
                "Negotiation limit reached. You may only start a new negotiation "
                "for this product once per 24 hours. Please try again later."
            ),
        )

    # ── 6. PRIORITY 3: Create new session ────────────────────────────────────
    session_id = str(uuid.uuid4())
    expires_at = now + timedelta(hours=SESSION_TTL_HOURS)
    redis_ttl = int((expires_at - now).total_seconds())   # = 86400 s

    # ── 6a. Postgres commit ───────────────────────────────────────────────────
    new_session = SaasSession(
        id=session_id,
        tenant_id=tenant.id,
        product_id=product.id,
        origin_domain=normalised,
        mam_snapshot=product.mam,
        list_price_snap=product.list_price,
        status="ACTIVE",
        expires_at=expires_at,
        anon_id=payload.anonId,
    )
    db.add(new_session)
    await db.commit()

    # ── 6b. Redis write-through ───────────────────────────────────────────────
    # THE FIX: The orchestrator's validate_session() reads ONLY from Redis.
    # We write the SessionData-compatible dict here so that when the first
    # chat message arrives at POST /ina/v1/chat, Redis has the entry and
    # validate_session() returns 200 instead of 401 SESSION_EXPIRED.
    #
    # Field mapping:
    #   product.mam          → mam           (the secret price floor — server-side only)
    #   product.list_price   → asking_price  (the public starting price)
    redis_payload = _build_redis_payload(
        mam=product.mam,
        asking_price=product.list_price,
        tenant_id=tenant.id,
        product_id=payload.productId,
        created_at=now.isoformat(),
    )
    try:
        await redis_client.set(session_id, redis_payload, ex=redis_ttl)
        logger.info(
            "Session written to Redis  session_id=%s  ttl=%ss", session_id, redis_ttl
        )
    except Exception as redis_err:
        # Non-fatal: Postgres is committed. On the next widget open, the ACTIVE
        # resume path will re-seed Redis automatically.
        logger.error(
            "Redis write-through failed  session_id=%s  error=%s", session_id, redis_err
        )

    logger.info(
        "New SaasSession created  session_id=%s  tenant=%d  product=%s  anon_id=%s",
        session_id, tenant.id, payload.productId, payload.anonId,
    )

    return SaasSessionInitResponse(
        session_id=session_id,
        list_price=product.list_price,
        currency=product.currency,
        expires_at=expires_at,
        resumed=False,
        status="ACTIVE",
        offer_count=0,
    )


# ═════════════════════════════════════════════════════════════════════════════
# POST /verify
# ═════════════════════════════════════════════════════════════════════════════

@router.post("/verify", response_model=SaasVerifyResponse)
async def saas_session_verify(
    request:          Request,
    payload:          SaasVerifyRequest,
    x_ina_tenant:     str = Header(..., alias="X-INA-Tenant"),
    x_ina_timestamp:  str = Header(..., alias="X-INA-Timestamp"),
    x_ina_signature:  str = Header(..., alias="X-INA-Signature"),
    db:               AsyncSession = Depends(get_db),
):
    """
    Verify a completed SaaS negotiation price — called by the tenant's server,
    never directly from the browser.
    """
    try:
        tenant_id_int = int(x_ina_tenant)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="X-INA-Tenant must be a numeric tenant ID"
        )

    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id_int)
    )
    tenant: Optional[Tenant] = tenant_result.scalars().first()
    if not tenant:
        raise HTTPException(status_code=401, detail="Unknown tenant")

    if not tenant.webhook_secret:
        raise HTTPException(
            status_code=500,
            detail="Tenant has no webhook_secret configured — contact support",
        )

    raw_body: bytes = await request.body()
    signing_input = f"{x_ina_timestamp}.{raw_body.decode('utf-8')}".encode(
        "utf-8")
    expected_sig = hmac.new(
        tenant.webhook_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, x_ina_signature):
        logger.warning(
            "saas/verify: HMAC mismatch  tenant_id=%s  session_id=%s",
            tenant_id_int, payload.session_id,
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    async with db.begin_nested():
        session_result = await db.execute(
            select(SaasSession)
            .where(
                and_(
                    SaasSession.id == payload.session_id,
                    SaasSession.tenant_id == tenant_id_int,
                )
            )
            .with_for_update()
        )
        saas_session: Optional[SaasSession] = session_result.scalars().first()

        if not saas_session:
            raise HTTPException(status_code=404, detail="Session not found")

        if saas_session.status != "AGREED":
            raise HTTPException(
                status_code=409,
                detail=f"Session is '{saas_session.status}', expected 'AGREED'",
            )

        if (
            saas_session.final_price is None
            or abs(saas_session.final_price - payload.final_price) > 0.001
        ):
            raise HTTPException(
                status_code=409,
                detail="final_price does not match the agreed price on record",
            )

        verified_at = datetime.now(timezone.utc)
        await db.execute(
            update(SaasSession)
            .where(SaasSession.id == payload.session_id)
            .values(status="VERIFIED", verified_at=verified_at)
        )

    await db.commit()

    logger.info(
        "SaasSession VERIFIED  session_id=%s  tenant_id=%s  price=%.2f",
        payload.session_id, tenant_id_int, payload.final_price,
    )

    return SaasVerifyResponse(
        valid=True,
        price=payload.final_price,
        verifiedAt=verified_at,
    )
