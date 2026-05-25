"""
routers/domains.py
==================
Manage the list of allowed origins for a tenant's SaaS widget.

GET    /api/saas/domains          — list all domains for the authenticated tenant
POST   /api/saas/domains          — add a new allowed domain
DELETE /api/saas/domains/{id}     — remove a domain by its DB row id

Domain strings are normalised before insert:
  - scheme stripped (https://, http://)
  - leading "www." stripped (so tenants don't have to register both variants)
  - any path, query-string, or fragment dropped
  - trailing slashes removed
  - lower-cased

The result is a bare hostname, e.g. "shop.example.com", which matches exactly
what _normalise_origin() in saas_session.py produces from the live Origin header.
"""

import logging
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.ina_backend.app.auth import get_current_tenant
from src.ina_backend.app.database import get_db
from src.ina_backend.app.models import AllowedDomain, Tenant

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Local schemas
# ---------------------------------------------------------------------------

class DomainIn(BaseModel):
    domain: str

    @field_validator("domain", mode="before")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("domain must not be blank")
        return v.strip()


class DomainOut(BaseModel):
    id: int
    domain: str
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Helper: normalise a raw domain/URL string to a bare, lowercase hostname
# ---------------------------------------------------------------------------

def _normalise_domain(raw: str) -> str:
    """
    Accept any of these formats and return a clean bare hostname:

      shop.example.com
      https://shop.example.com
      https://shop.example.com/
      https://shop.example.com/some/path?q=1
      www.shop.example.com

    Raises ValueError with a human-readable message if the result is empty.
    """
    raw = raw.strip()

    # If the value has no scheme, add one so urlparse treats the whole thing
    # as a netloc rather than a path.
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = parsed.hostname or ""   # hostname is already lower-cased by urlparse

    if not host:
        raise ValueError(f"Cannot extract a hostname from: {raw!r}")

    # Drop leading "www." so the stored value matches what browsers send
    # in Origin headers (which never include www for sub-domains, but might
    # for root domains — we normalise both sides the same way).
    if host.startswith("www."):
        host = host[4:]

    if not host:
        raise ValueError(f"Domain resolved to an empty string from: {raw!r}")

    return host


# ---------------------------------------------------------------------------
# GET /api/saas/domains
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[DomainOut])
async def list_domains(
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return all allowed domains for the authenticated tenant, ordered by domain name."""
    result = await db.execute(
        select(AllowedDomain)
        .where(AllowedDomain.tenant_id == current_tenant.id)
        .order_by(AllowedDomain.domain)
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# POST /api/saas/domains
# ---------------------------------------------------------------------------

@router.post("/", response_model=DomainOut, status_code=201)
async def add_domain(
    payload: DomainIn,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Add a new allowed origin domain.

    The domain string is normalised before storage — you can pass
    'https://shop.example.com/' or 'shop.example.com' and both are stored
    identically as 'shop.example.com'.

    Returns 409 if the domain is already registered for this tenant.
    """
    try:
        normalised = _normalise_domain(payload.domain)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_domain = AllowedDomain(
        tenant_id=current_tenant.id,
        domain=normalised,
    )
    db.add(new_domain)

    try:
        await db.commit()
        await db.refresh(new_domain)
    except IntegrityError:
        # Unique constraint (tenant_id, domain) violated — duplicate entry
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"'{normalised}' is already an allowed domain for your account",
        )
    except Exception:
        await db.rollback()
        logger.exception(
            "domains/add: DB error for tenant_id=%s domain=%s",
            current_tenant.id,
            normalised,
        )
        raise HTTPException(status_code=500, detail="Could not save domain")

    logger.info(
        "AllowedDomain added: id=%s tenant_id=%s domain=%s",
        new_domain.id,
        current_tenant.id,
        normalised,
    )
    return new_domain


# ---------------------------------------------------------------------------
# DELETE /api/saas/domains/{domain_id}
# ---------------------------------------------------------------------------

@router.delete("/{domain_id}", status_code=204)
async def delete_domain(
    domain_id: int,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Remove an allowed domain by its row ID.

    Returns 404 if the ID doesn't exist or belongs to a different tenant
    (ownership check prevents tenants from deleting each other's domains).
    """
    result = await db.execute(
        select(AllowedDomain).where(
            and_(
                AllowedDomain.id == domain_id,
                AllowedDomain.tenant_id == current_tenant.id,
            )
        )
    )
    domain: AllowedDomain | None = result.scalars().first()

    if not domain:
        raise HTTPException(
            status_code=404,
            detail=f"Domain {domain_id} not found or does not belong to your account",
        )

    await db.delete(domain)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "domains/delete: DB error for tenant_id=%s domain_id=%s",
            current_tenant.id,
            domain_id,
        )
        raise HTTPException(status_code=500, detail="Could not delete domain")

    logger.info(
        "AllowedDomain deleted: id=%s tenant_id=%s domain=%s",
        domain_id,
        current_tenant.id,
        domain.domain,
    )
    # 204 No Content — no response body
