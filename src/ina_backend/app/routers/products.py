"""
routers/products.py
===================
SaaS product-management endpoints — all require JWT auth (same mechanism as
tenant_config.py / analytics.py).

POST /api/saas/products
    Manually add a single product.  Validates list_price > 0, mam > 0, and
    mam <= list_price.  Returns 409 if the external_id already exists.

POST /api/saas/products/upload
    Accepts a multipart CSV file.  For each row, validates that mam <= list_price
    and both values are > 0.  Rows whose external_id already exists are reported
    as skipped (duplicate) instead of silently overwriting.

GET  /api/saas/products
    Returns all active + inactive products belonging to the authenticated tenant,
    sorted by name.

PUT  /api/saas/products/{product_id}
    Inline edit: accepts any subset of { name, list_price, mam, currency, active }.
    Re-validates mam <= list_price and both > 0 after the patch is applied.

DELETE /api/saas/products/{product_id}
    Delete a single product by its DB id.

POST /api/saas/products/bulk-delete
    Delete multiple products at once.  Accepts a JSON body with a list of product ids.
"""

import csv
import io
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ina_backend.app.auth import get_current_tenant
from src.ina_backend.app.database import get_db
from src.ina_backend.app.models import Tenant, TenantProduct

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Local schemas
# ---------------------------------------------------------------------------

class ProductOut(BaseModel):
    """A single product as returned by the list and upload endpoints."""
    id: int
    external_id: str
    name: str
    list_price: float
    mam: float
    currency: str
    active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True  # Pydantic v2 (replaces orm_mode)


class ProductCreateRequest(BaseModel):
    """Schema for manually creating a single product."""
    external_id: str
    name: str
    list_price: float
    mam: float
    currency: str = "PKR"
    active: bool = True

    @field_validator("list_price", "mam", mode="before")
    @classmethod
    def must_be_greater_than_zero(cls, v: float) -> float:
        if v is not None and v <= 0:
            raise ValueError("Value must be greater than 0")
        return v

    @model_validator(mode="after")
    def mam_must_not_exceed_price(self):
        if self.mam > self.list_price:
            raise ValueError(
                f"mam ({self.mam}) cannot exceed list_price ({self.list_price})"
            )
        return self


class ProductPatchRequest(BaseModel):
    """
    All fields are optional so the caller can send only what changed.
    Validation is run after the patch is applied to the existing values,
    not on the partial payload alone — see the endpoint logic.
    """
    name: Optional[str] = None
    list_price: Optional[float] = None
    mam: Optional[float] = None
    currency: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("list_price", "mam", mode="before")
    @classmethod
    def must_be_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("Value must be greater than 0")
        return v


class BulkDeleteRequest(BaseModel):
    """Request body for deleting multiple products at once."""
    product_ids: List[int]


class UploadSummary(BaseModel):
    inserted: int
    updated: int
    skipped: int
    errors: list[dict]  # [{row, reason}]


# ---------------------------------------------------------------------------
# CSV column aliases: we accept both "external_id" and "product_id" etc.
# to be forgiving about headers tenants might export from their own systems.
# ---------------------------------------------------------------------------

_COL_ALIASES: dict[str, str] = {
    # canonical name → accepted aliases
    "external_id": ["external_id", "product_id", "sku", "id"],
    "name":        ["name", "product_name", "title"],
    "list_price":  ["list_price", "price", "selling_price"],
    "mam":         ["mam", "minimum_price", "floor_price", "min_price"],
    "currency":    ["currency", "currency_code"],
}

_REQUIRED_COLS = {"external_id", "name", "list_price", "mam"}


def _resolve_headers(raw_headers: list[str]) -> dict[str, str]:
    """
    Given the actual CSV header row, build a mapping
    canonical_name → actual_csv_column_name.
    Raises ValueError if a required canonical column cannot be resolved.
    """
    lower_headers = {h.strip().lower(): h.strip() for h in raw_headers}
    resolved: dict[str, str] = {}

    for canonical, aliases in _COL_ALIASES.items():
        for alias in aliases:
            if alias in lower_headers:
                resolved[canonical] = lower_headers[alias]
                break

    missing = _REQUIRED_COLS - set(resolved.keys())
    if missing:
        raise ValueError(
            f"CSV is missing required columns: {', '.join(sorted(missing))}. "
            f"Accepted aliases — "
            + "; ".join(
                f"{c}: {_COL_ALIASES[c]}" for c in sorted(missing)
            )
        )
    return resolved


# ---------------------------------------------------------------------------
# POST /api/saas/products/upload
# ---------------------------------------------------------------------------

_MAX_CSV_BYTES = 5 * 1024 * 1024  # 5 MB safety cap


@router.post("/upload", response_model=UploadSummary, status_code=200)
async def upload_products(
    file: UploadFile = File(..., description="CSV file with product data"),
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload products via CSV.

    Required columns (case-insensitive, aliases accepted):
      external_id / product_id / sku / id
      name / product_name / title
      list_price / price / selling_price
      mam / minimum_price / floor_price / min_price

    Optional columns:
      currency / currency_code  (defaults to 'PKR')

    Rows with validation errors are skipped; the rest are upserted atomically.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    raw_bytes = await file.read()
    if len(raw_bytes) > _MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large — maximum allowed size is {_MAX_CSV_BYTES // 1024} KB",
        )

    # Decode — try UTF-8 first, fall back to latin-1 (common for Excel exports)
    try:
        text = raw_bytes.decode("utf-8-sig")   # utf-8-sig strips the BOM if present
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file appears to be empty")

    try:
        col_map = _resolve_headers(list(reader.fieldnames))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    inserted = 0
    updated = 0
    skipped = 0
    errors: list[dict] = []

    validated_rows: list[dict] = []

    for row_num, row in enumerate(reader, start=2):  # row 1 = header
        ext_id  = row.get(col_map["external_id"], "").strip()
        name    = row.get(col_map["name"], "").strip()
        lp_raw  = row.get(col_map["list_price"], "").strip()
        mam_raw = row.get(col_map["mam"], "").strip()
        currency = row.get(col_map.get("currency", ""), "PKR").strip() or "PKR"

        # --- Basic presence checks ---
        if not ext_id:
            errors.append({"row": row_num, "reason": "external_id is blank"})
            skipped += 1
            continue
        if not name:
            errors.append({"row": row_num, "reason": "name is blank"})
            skipped += 1
            continue

        # --- Numeric parsing ---
        try:
            list_price = float(lp_raw)
        except (ValueError, TypeError):
            errors.append({"row": row_num, "reason": f"list_price '{lp_raw}' is not a valid number"})
            skipped += 1
            continue
        try:
            mam = float(mam_raw)
        except (ValueError, TypeError):
            errors.append({"row": row_num, "reason": f"mam '{mam_raw}' is not a valid number"})
            skipped += 1
            continue

        # --- Business rule: prices must be > 0 ---
        if list_price <= 0:
            errors.append({"row": row_num, "reason": f"list_price ({list_price}) must be greater than 0"})
            skipped += 1
            continue
        if mam <= 0:
            errors.append({"row": row_num, "reason": f"mam ({mam}) must be greater than 0"})
            skipped += 1
            continue

        # --- Business rule: mam must not exceed list_price ---
        if mam > list_price:
            errors.append({
                "row": row_num,
                "reason": f"mam ({mam}) > list_price ({list_price}) — rejected",
            })
            skipped += 1
            continue

        validated_rows.append({
            "external_id": ext_id,
            "name":        name,
            "list_price":  list_price,
            "mam":         mam,
            "currency":    currency.upper()[:10],
        })

    if not validated_rows and not errors:
        raise HTTPException(status_code=400, detail="CSV contained no data rows")

    # --- Duplicate detection: skip rows whose external_id already exists ---
    rows_to_insert: list[dict] = []
    if validated_rows:
        ext_ids = [r["external_id"] for r in validated_rows]
        existing_result = await db.execute(
            select(TenantProduct.external_id).where(
                and_(
                    TenantProduct.tenant_id == current_tenant.id,
                    TenantProduct.external_id.in_(ext_ids),
                )
            )
        )
        existing_ids = {row[0] for row in existing_result.fetchall()}

        for r in validated_rows:
            if r["external_id"] in existing_ids:
                errors.append({
                    "row": "—",
                    "reason": f"Product ID '{r['external_id']}' already exists — skipped",
                })
                skipped += 1
            else:
                rows_to_insert.append({
                    "tenant_id":   current_tenant.id,
                    "external_id": r["external_id"],
                    "name":        r["name"],
                    "list_price":  r["list_price"],
                    "mam":         r["mam"],
                    "currency":    r["currency"],
                    "active":      True,
                })

    # --- Insert only genuinely new rows ---
    if rows_to_insert:
        stmt = pg_insert(TenantProduct).values(rows_to_insert)

        try:
            await db.execute(stmt)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "products/upload: DB insert failed for tenant_id=%s", current_tenant.id
            )
            raise HTTPException(status_code=500, detail="Database error during insert")

        inserted = len(rows_to_insert)
        logger.info(
            "products/upload: inserted %d rows for tenant_id=%s",
            inserted,
            current_tenant.id,
        )

    return UploadSummary(
        inserted=inserted,
        updated=0,
        skipped=skipped,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# GET /api/saas/products
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[ProductOut])
async def list_products(
    active_only: bool = False,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    List all products for the authenticated tenant, sorted by name.

    Query params:
      active_only=true   — return only active products (default: false, return all)
    """
    stmt = select(TenantProduct).where(TenantProduct.tenant_id == current_tenant.id)
    if active_only:
        stmt = stmt.where(TenantProduct.active.is_(True))
    stmt = stmt.order_by(TenantProduct.name)

    result = await db.execute(stmt)
    products = result.scalars().all()
    return products


# ---------------------------------------------------------------------------
# PUT /api/saas/products/{product_id}
# ---------------------------------------------------------------------------

@router.put("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: int,
    patch: ProductPatchRequest,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Inline edit for a single product.  Only supply the fields you want changed;
    unmentioned fields keep their current values.

    Business rule enforced after merge: mam must not exceed list_price.
    """
    # Fetch and ownership-check in one shot
    result = await db.execute(
        select(TenantProduct).where(
            and_(
                TenantProduct.id == product_id,
                TenantProduct.tenant_id == current_tenant.id,
            )
        )
    )
    product: TenantProduct | None = result.scalars().first()
    if not product:
        raise HTTPException(
            status_code=404,
            detail=f"Product {product_id} not found or does not belong to your account",
        )

    # Apply the patch — only fields explicitly set in the request body
    patch_data = patch.model_dump(exclude_unset=True)

    if "name" in patch_data:
        product.name = patch_data["name"].strip()
        if not product.name:
            raise HTTPException(status_code=422, detail="name must not be blank")

    if "list_price" in patch_data:
        product.list_price = patch_data["list_price"]

    if "mam" in patch_data:
        product.mam = patch_data["mam"]

    if "currency" in patch_data:
        product.currency = patch_data["currency"].upper()[:10]

    if "active" in patch_data:
        product.active = patch_data["active"]

    # Validate after merge so partial patches (e.g. only mam) are checked
    # against the *current* list_price, not a missing one.
    if product.list_price <= 0:
        raise HTTPException(
            status_code=422,
            detail="list_price must be greater than 0",
        )
    if product.mam <= 0:
        raise HTTPException(
            status_code=422,
            detail="mam must be greater than 0",
        )
    if product.mam > product.list_price:
        raise HTTPException(
            status_code=422,
            detail=(
                f"mam ({product.mam}) cannot exceed list_price ({product.list_price})"
            ),
        )

    try:
        await db.commit()
        await db.refresh(product)
    except Exception:
        await db.rollback()
        logger.exception(
            "products/update: DB commit failed for product_id=%s tenant_id=%s",
            product_id,
            current_tenant.id,
        )
        raise HTTPException(status_code=500, detail="Could not save changes")

    logger.info(
        "TenantProduct updated: id=%s tenant_id=%s fields=%s",
        product_id,
        current_tenant.id,
        list(patch_data.keys()),
    )
    return product


# ---------------------------------------------------------------------------
# POST /api/saas/products  (manual single-product creation)
# ---------------------------------------------------------------------------

@router.post("/", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductCreateRequest,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually add a single product.

    Returns 409 if a product with the same external_id already exists
    for this tenant.
    """
    # Check for duplicate external_id
    existing = await db.execute(
        select(TenantProduct).where(
            and_(
                TenantProduct.tenant_id == current_tenant.id,
                TenantProduct.external_id == body.external_id.strip(),
            )
        )
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=409,
            detail=f"Product ID '{body.external_id}' already exists",
        )

    product = TenantProduct(
        tenant_id=current_tenant.id,
        external_id=body.external_id.strip(),
        name=body.name.strip(),
        list_price=body.list_price,
        mam=body.mam,
        currency=body.currency.upper()[:10],
        active=body.active,
    )

    try:
        db.add(product)
        await db.commit()
        await db.refresh(product)
    except Exception:
        await db.rollback()
        logger.exception(
            "products/create: DB insert failed for tenant_id=%s", current_tenant.id
        )
        raise HTTPException(status_code=500, detail="Could not create product")

    logger.info(
        "TenantProduct created: id=%s external_id=%s tenant_id=%s",
        product.id,
        product.external_id,
        current_tenant.id,
    )
    return product


# ---------------------------------------------------------------------------
# DELETE /api/saas/products/{product_id}
# ---------------------------------------------------------------------------

@router.delete("/{product_id}", status_code=200)
async def delete_product(
    product_id: int,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a single product by its database ID.

    Returns 404 if the product does not exist or does not belong to
    the authenticated tenant.
    """
    result = await db.execute(
        select(TenantProduct).where(
            and_(
                TenantProduct.id == product_id,
                TenantProduct.tenant_id == current_tenant.id,
            )
        )
    )
    product: TenantProduct | None = result.scalars().first()
    if not product:
        raise HTTPException(
            status_code=404,
            detail=f"Product {product_id} not found or does not belong to your account",
        )

    try:
        await db.delete(product)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "products/delete: DB delete failed for product_id=%s tenant_id=%s",
            product_id,
            current_tenant.id,
        )
        raise HTTPException(status_code=500, detail="Could not delete product")

    logger.info(
        "TenantProduct deleted: id=%s tenant_id=%s",
        product_id,
        current_tenant.id,
    )
    return {"detail": f"Product {product_id} deleted successfully"}


# ---------------------------------------------------------------------------
# POST /api/saas/products/bulk-delete
# ---------------------------------------------------------------------------

@router.post("/bulk-delete", status_code=200)
async def bulk_delete_products(
    body: BulkDeleteRequest,
    current_tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete multiple products at once.

    Accepts a JSON body: { "product_ids": [1, 2, 3] }

    Only products belonging to the authenticated tenant are deleted.
    Returns a summary of how many were deleted and which IDs were not found.
    """
    if not body.product_ids:
        raise HTTPException(status_code=422, detail="product_ids must not be empty")

    # Fetch all matching products owned by this tenant
    result = await db.execute(
        select(TenantProduct).where(
            and_(
                TenantProduct.id.in_(body.product_ids),
                TenantProduct.tenant_id == current_tenant.id,
            )
        )
    )
    products = result.scalars().all()
    found_ids = {p.id for p in products}
    not_found_ids = [pid for pid in body.product_ids if pid not in found_ids]

    try:
        for product in products:
            await db.delete(product)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "products/bulk-delete: DB delete failed for tenant_id=%s",
            current_tenant.id,
        )
        raise HTTPException(status_code=500, detail="Could not delete products")

    logger.info(
        "TenantProduct bulk-delete: deleted %d products for tenant_id=%s",
        len(found_ids),
        current_tenant.id,
    )
    return {
        "deleted": len(found_ids),
        "deleted_ids": sorted(found_ids),
        "not_found_ids": not_found_ids,
    }
