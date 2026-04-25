from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime

class TenantCreate(BaseModel):
    email: EmailStr
    password: str
    client_policy_api_endpoint: str | None = None

class TenantOut(BaseModel):
    id: int
    email: EmailStr
    client_policy_api_endpoint: str | None

    class Config:
        orm_mode = True

class Token(BaseModel):
    access_token: str
    token_type: str

class AuthResponse(Token):
    tenant_id: int
    api_key: str | None = None


class TenantConfigIn(BaseModel):
    client_policy_api_endpoint: str

class TenantConfigOut(BaseModel):
    client_policy_api_endpoint: str | None
    client_api_key: str | None


class TenantRuleInput(BaseModel):
    mam: float
    asking_price: float



class SessionInitRequest(BaseModel):
    api_key: str           # The Tenant's authentication key (from Week 2)
    context_id: str        # The Tenant's internal ID for this user/chat
    mam: float             # Minimum Acceptable Margin
    asking_price: float    # The starting price

class SessionInitResponse(BaseModel):
    session_id: str        # We return our generated Session ID
    status: str


class AnalyticsLogCreate(BaseModel):
    session_id: str
    result: str           # e.g., "DEAL" or "NO_DEAL"
    final_price: Optional[float] = None
    transcript_summary: Optional[str] = None

class AnalyticsSummary(BaseModel):
    total_sessions: int
    total_deals: int
    total_volume: float   # Total value of all deals
    average_price: float


# ---------------------------------------------------------------------------
# Negotiation Outcomes — posted by the bargaining agent (fire-and-forget)
# ---------------------------------------------------------------------------

class NegotiationMessage(BaseModel):
    """One turn in the negotiation message history."""
    from_: str = Field(alias="from")   # "user" or "ina"  ('from' is a reserved keyword)
    text: str
    user_offer: Optional[float] = None
    bot_offer: Optional[float] = None

    class Config:
        populate_by_name = True        # allow both 'from' and 'from_' on input


class NegotiationOutcomeCreate(BaseModel):
    """Payload the bargaining agent POSTs when a negotiation ends."""
    session_id: str
    outcome: str                       # "ACCEPTED" or "DEAL"
    asking_price: float
    final_price: float
    discount_percent: Optional[float] = None
    total_turns: Optional[int] = None
    user_language: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    message_history: Optional[List[NegotiationMessage]] = None


class NegotiationOutcomeResponse(BaseModel):
    """Returned after a successful save."""
    id: int
    session_id: str
    outcome: str
    created_at: datetime

    class Config:
        orm_mode = True