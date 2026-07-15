import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Example: "
            "postgresql+asyncpg://tachafy:password@localhost:5432/tachafy_teleconsult"
        )
    # Normalize plain "postgresql://" URLs (e.g. copied from a hosting
    # provider) to use the asyncpg driver explicitly.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    _database_url(),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # Consultations are short-lived (TOKEN_TTL / TTL windows measured in
    # minutes), so a modest pool is enough for an MVP; tune under load.
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncSession:
    """FastAPI dependency: one session per request, committed on success."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise