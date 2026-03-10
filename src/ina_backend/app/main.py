from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .routers import auth, policy, tenant_config, session, analytics

from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter

import asyncio
from .database import engine, Base

from .redis_client import redis_client

app = FastAPI(title="INA Backend")

# CORS configuration
if settings.CORS_ORIGINS != "*":
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",")]
else:
    origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    # This connects the limiter to Redis when the app boots up
    await FastAPILimiter.init(redis_client)

# No need after almebic
#
# Auto-create tables every time app runs
# @app.on_event("startup")
# async def create_tables():
#     async with engine.begin() as conn:
#         await conn.run_sync(Base.metadata.create_all)
#     print("✅ Tables checked/created successfully.")

# Include routers
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(policy.router, prefix="/api/v1/policy", tags=["Policy"])
app.include_router(tenant_config.router, prefix="/api/v1/tenant", tags=["Tenant Config"])
app.include_router(session.router, prefix="/api/v1/session", tags=["Session"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"])


@app.get("/")
def root():
    return {"message": "INA Backend API is running"}
