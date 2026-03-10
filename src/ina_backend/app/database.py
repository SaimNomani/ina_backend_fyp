from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings

database_url=settings.DATABASE_URL_PROD

if not database_url:
    database_url=settings.DATABASE_URL_DEV

engine = create_async_engine(
    database_url, 
    future=True, 
    echo=True,
    pool_pre_ping=True,  # Verify connection before usage
    pool_size=10,        # Standard pool size
    max_overflow=20      # Allow extra connections during load
)
AsyncSessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()

# dependency for routes
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
