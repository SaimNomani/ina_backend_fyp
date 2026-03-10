from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from sqlalchemy import update, select

from src.ina_backend.app import models, schemas, auth
from src.ina_backend.app.database import get_db
import secrets

router = APIRouter()


@router.post("/configuration", response_model=schemas.TenantConfigOut)
async def set_tenant_config(
    payload: schemas.TenantConfigIn,
    current_tenant: models.Tenant = Depends(auth.get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    # Generate API key if not already present
    new_api_key = current_tenant.client_api_key or f"ina_key_{secrets.token_hex(16)}"

    # Update the tenant record
    stmt = (
        update(models.Tenant)
        .where(models.Tenant.id == current_tenant.id)
        .values(
            client_policy_api_endpoint=payload.client_policy_api_endpoint,
            client_api_key=new_api_key
        )
    )
    await db.execute(stmt)
    await db.commit()

    # Re-fetch updated tenant as ORM model (important!)
    refreshed_stmt = select(models.Tenant).where(
        models.Tenant.id == current_tenant.id)
    result = await db.execute(refreshed_stmt)
    updated_tenant = result.scalars().first()

    if not updated_tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return schemas.TenantConfigOut(
        client_policy_api_endpoint=updated_tenant.client_policy_api_endpoint,
        client_api_key=updated_tenant.client_api_key
    )


@router.get("/configuration", response_model=schemas.TenantConfigOut)
async def get_tenant_config(
    current_tenant: models.Tenant = Depends(auth.get_current_tenant)
):
    return schemas.TenantConfigOut(
        client_policy_api_endpoint=current_tenant.client_policy_api_endpoint,
        client_api_key=current_tenant.client_api_key
    )
