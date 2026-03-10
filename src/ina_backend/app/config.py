from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL_DEV: str | None = None
    DATABASE_URL_TEST: str | None = None
    DATABASE_URL_PROD: str | None = None
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    CORS_ORIGINS: str | None = "*"
    REDIS_URL: str | None='redis://localhost:6379'

    class Config:
        env_file = ".env"

settings = Settings()
