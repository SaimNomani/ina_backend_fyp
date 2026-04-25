from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func, text
from .database import Base


class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    client_policy_api_endpoint = Column(String(512), nullable=True)
    client_api_key = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # add other fields (name, plan, etc.) as needed


# Ensure Base is imported from database.py as you did in Week 1

class AnalyticsLog(Base):
    __tablename__ = "analytics_logs"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, unique=True, index=True) # Link to the session
    tenant_id = Column(Integer, ForeignKey("tenants.id")) # Link to the tenant
    
    result = Column(String)       # "DEAL", "NO_DEAL", "TIMEOUT"
    final_price = Column(Float)   # The agreed price (or null)
    transcript_summary = Column(String) # A short summary of the chat
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class NegotiationOutcome(Base):
    """Stores the final outcome when a negotiation ends (ACCEPTED / DEAL)."""
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
    message_history  = Column(JSON, nullable=True)                 # stored as jsonb array
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
