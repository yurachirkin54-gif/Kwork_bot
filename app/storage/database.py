"""Async database bootstrap (SQLite WAL by default, PostgreSQL via DATABASE_URL)."""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_env
from app.storage.orm import Base


def sqlite_path_from_url(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    marker = url.split("///", 1)
    if len(marker) != 2:
        return None
    raw_path = marker[1].split("?", 1)[0]
    if not raw_path or raw_path == ":memory:":
        return None
    return Path(raw_path)


class Database:
    """Owns the async engine and session factory."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or get_env().database_url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database is not initialised: call await db.init() first")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database is not initialised: call await db.init() first")
        return self._session_factory

    async def init(self) -> Database:
        if self._engine is not None:
            return self
        kwargs: dict = {"future": True, "echo": False}
        if self.url.startswith("sqlite"):
            path = sqlite_path_from_url(self.url)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in self.url:
                kwargs["poolclass"] = StaticPool

        self._engine = create_async_engine(self.url, **kwargs)

        if self.url.startswith("sqlite"):

            @event.listens_for(self._engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=5000")
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()

        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False, class_=AsyncSession)
        logger.debug(f"База данных готова: {self.url}")
        return self

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
