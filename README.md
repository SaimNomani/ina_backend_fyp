# INA Backend - LLM Context & Developer Guide

> **ATTENTION AI ASSISTANTS (Claude, ChatGPT, Cursor, Copilot, etc.):** 
> If you are reading this file, you have been given the complete context of the `ina_backend_fyp` project. Treat this document as the absolute source of truth for the project's architecture, database schemas, Pydantic models, and strict coding rules. Do not hallucinate libraries or patterns; strictly adhere to the stack and rules defined below.

---

## 1. Project Overview
This project is the core FastAPI backend for an **Intelligent Negotiation Agent (INA)**. It serves as a multi-tenant hub that bridges three entities:
1. **The Human Tenant:** E-commerce business owners who use a React dashboard to view analytics and configure their dynamic pricing webhooks.
2. **The Tenant's External Server:** E-commerce platforms that initiate chat sessions on behalf of their users via API Keys.
3. **The AI Orchestrator:** A separate microservice that handles the actual LLM negotiation, directly reading/writing active chat states to our Redis cache for speed, and eventually pushing the final outcome to our PostgreSQL database.

---

## 2. Tech Stack & Dependencies
* **Framework:** `fastapi` (>=0.120.4)
* **Server:** `uvicorn` (>=0.38.0)
* **Database / ORM:** `PostgreSQL` + `sqlalchemy` (>=2.0.44) with `asyncio` extension + `asyncpg` driver.
* **Data Validation:** `pydantic` (>=2.12.3)
* **Migrations:** `alembic` (>=1.17.1)
* **Caching & Rate Limiting:** `redis` (>=7.1.0) + `fastapi-limiter`
* **Authentication:** `python-jose` (JWT), `passlib==1.7.4` (bcrypt)
* **HTTP Client:** `httpx` (for calling Tenant webhooks)

---

## 3. Strict Architectural Rules (DO NOT BREAK)

### Rule 1: All Database Operations Must Be Async
Do not use `session.query()`. Always use `sqlalchemy.future.select` or `sqlalchemy.update` combined with `await db.execute()`. 
```python
# CORRECT
stmt = select(models.Tenant).where(models.Tenant.email == "test@test.com")
result = await db.execute(stmt)
tenant = result.scalars().first()
```

### Rule 2: Redis Session Format
The AI Orchestrator microservice relies on a very specific Redis structure. 
* **Do NOT use Redis Hashes (`hset` / `hgetall`).** 
* The Key must be a bare string UUID (e.g., `"123e4567-e89b-12d3-a456-426614174000"`). Do NOT prefix it with `"session:"`.
* The Value must be a dumped JSON string containing the `messages` array.
```python
# CORRECT REDIS WRITE (session.py)
await redis_client.set(session_id, json.dumps(session_data), ex=86400)

# CORRECT REDIS READ (analytics.py)
raw = await redis_client.get(session_id)
session_data = json.loads(raw)
```

### Rule 3: Dual-Layer Authentication
* **Human Dashboard:** Protected by JWTs. Use `Depends(auth.get_current_tenant)`.
* **Machine-to-Machine:** Tenant servers and the AI Orchestrator authenticate using a static `api_key` prefixed with `"ina_key_"`. Do not require JWTs on `/session/init` or `/negotiations/`.

### Rule 4: Handling Pydantic Reserved Keywords
In the `NegotiationMessage` schema, we use the key `"from"` to designate "user" or "ina". Because `from` is a reserved keyword in Python, the Pydantic model is defined as `from_: str = Field(alias="from")`. When dumping this model to save to PostgreSQL JSON, you MUST use `model_dump(by_alias=True)` to convert it back to `"from"`.

---

## 4. Complete Database Schema (`app/models.py`)

```python
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from .database import Base

class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    client_policy_api_endpoint = Column(String(512), nullable=True) # Webhook for rules
    client_api_key = Column(String(255), nullable=True) # Starts with ina_key_
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AnalyticsLog(Base):
    __tablename__ = "analytics_logs"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"))
    result = Column(String)       # "DEAL", "NO_DEAL", "TIMEOUT"
    final_price = Column(Float)   
    transcript_summary = Column(String) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class NegotiationOutcome(Base):
    __tablename__ = "negotiation_outcomes"
    id               = Column(Integer, primary_key=True, index=True)
    session_id       = Column(String(255), unique=True, index=True, nullable=False)
    outcome          = Column(String(50), nullable=False)          # "ACCEPTED", "DEAL"
    asking_price     = Column(Float, nullable=False)
    final_price      = Column(Float, nullable=False)
    discount_percent = Column(Float, nullable=True)
    total_turns      = Column(Integer, nullable=True)
    user_language    = Column(String(50), nullable=True)
    started_at       = Column(DateTime(timezone=True), nullable=True)
    ended_at         = Column(DateTime(timezone=True), nullable=True)
    message_history  = Column(JSON, nullable=True)                 # Array of message dicts
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
```

---

## 5. Core API Endpoints & Flow Context

### A. Tenant Onboarding (`routers/auth.py` & `tenant_config.py`)
* `POST /api/auth/register`: Takes `email`/`password`. Hashes password via bcrypt, generates an `ina_key_...` via `secrets.token_hex(16)`, saves `Tenant`, returns JWT.
* `POST /api/auth/login`: OAuth2 flow. Returns JWT.
* `POST /api/v1/tenant/configuration`: Requires JWT. Updates `client_policy_api_endpoint`.

### B. Session Bootstrapping (`routers/session.py` & `policy.py`)
* `POST /api/v1/session/init`: Called by Tenant's E-commerce Server. 
  * Validates `api_key`.
  * Generates a UUID `session_id`.
  * Creates a starting JSON dictionary with empty `messages: []`.
  * Saves to Redis via `await redis_client.set(session_id, json.dumps(state), ex=86400)`.
* `GET /api/v1/policy/{tenant_id}/{context_id}`: Backend uses `httpx.AsyncClient` to perform a GET request to the Tenant's `client_policy_api_endpoint` to retrieve dynamic pricing rules (MAM and Asking Price).

### C. The Orchestrator Phase (External)
* The external AI Orchestrator bypasses our API. It connects directly to Redis, `GET`s the `session_id`, appends chat turns to the `messages` array, and `SET`s it back. We do not handle active chat messaging in Postgres to avoid I/O bottlenecks.

### D. Final Persistence (`routers/negotiations.py` & `analytics.py`)
* `POST /api/negotiations/`: Called by the Orchestrator when chat ends.
  * Inputs: `session_id`, `outcome`, `final_price`, and `message_history` (Array of `NegotiationMessage`).
  * Process: Converts Pydantic models to dicts using `by_alias=True`. Saves to `NegotiationOutcome`. Catches `IntegrityError` and returns `409 Conflict` if the Orchestrator accidentally double-submits.
* `POST /api/v1/analytics/log`: The Orchestrator pushes a lightweight summary here. We read Redis to find the `tenant_id` associated with the `session_id`, then write to `AnalyticsLog`.
* `GET /api/v1/analytics/`: The Dashboard pulls this. Requires JWT. Runs `func.sum()` and `func.count()` on `AnalyticsLog` scoped strictly to the authenticated `tenant_id`.

---

## 6. Directory Structure
```text
ina_backend_fyp/
├── alembic/                 # Database migration scripts
├── alembic.ini
├── pyproject.toml           # Poetry dependencies
└── src/
    └── ina_backend/
        └── app/
            ├── main.py          # FastAPI app, CORS, Router Includes
            ├── database.py      # AsyncEngine, Base, get_db injection
            ├── models.py        # SQLAlchemy Tables
            ├── schemas.py       # Pydantic validation models
            ├── auth.py          # JWT logic, bcrypt, get_current_tenant
            ├── config.py        # Pydantic Settings (ENV vars)
            ├── redis_client.py  # Async Redis connection pool singleton
            └── routers/         # Endpoint logic
                ├── analytics.py
                ├── auth.py
                ├── negotiations.py
                ├── policy.py
                ├── session.py
                └── tenant_config.py
```

> **Final Note to AI:** If the user asks you to implement a new feature, add a column, or create a route, ALWAYS refer to the `schemas.py` and `models.py` definitions above to ensure your code matches the existing data structures perfectly. Respect the async conventions and the Pydantic V2 syntax natively supported by FastAPI.
