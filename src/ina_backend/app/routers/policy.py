from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import update, select
from src.ina_backend.app import models, schemas, auth
from src.ina_backend.app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

import httpx


async def fetch_policy_from_tenant(tenant, context_id: str):
    # avoid double slashes
    url = f"{tenant.client_policy_api_endpoint.rstrip('/')}/{context_id}"
    headers = {"Authorization": f"Bearer {tenant.client_api_key}"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to connect to tenant endpoint '{url}': {str(e)}"
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Tenant API returned {response.status_code}: {response.text}"
        )

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Tenant API did not return valid JSON."
        )

    validated = schemas.TenantRuleInput(**data)
    return validated


router = APIRouter()


@router.get("/{tenant_id}/{context_id}")
async def get_policy(
    tenant_id: int = Path(...),
    context_id: str = Path(...),
    tenant=Depends(auth.get_current_tenant),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(models.Tenant).where(models.Tenant.id == tenant_id)
    result = await db.execute(stmt)
    tenant = result.scalars().first()

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not tenant.client_policy_api_endpoint or not tenant.client_api_key:
        raise HTTPException(
            status_code=400, detail="Tenant not configured yet")

    # url = f"{tenant.client_policy_api_endpoint}/{context_id}"
    # return {"tenant_endpoint": url, "client_api_key": tenant.client_api_key}

    policy_data = await fetch_policy_from_tenant(tenant, context_id)
    return policy_data
