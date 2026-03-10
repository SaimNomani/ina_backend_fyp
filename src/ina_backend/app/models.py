from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
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
