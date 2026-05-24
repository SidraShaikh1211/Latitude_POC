from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.settings import settings


engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_wal(dbapi_connection, _connection_record):
    if "sqlite" in settings.database_url:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Columns added after the initial schema landed. Each tuple is
# (table, column_name, sql_type). On SQLite we backfill these with ALTER
# TABLE ADD COLUMN so older app.db files keep working without a wipe.
_BACKFILL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("submissions", "outcome", "VARCHAR(32)"),
    ("submissions", "determination_narrative", "TEXT"),
    ("submissions", "missing_info", "JSON"),
    ("submissions", "claim_response_bundle", "JSON"),
    ("submissions", "payer_responded_at", "DATETIME"),
    ("submissions", "metadata_input_tokens", "INTEGER"),
    ("submissions", "metadata_output_tokens", "INTEGER"),
    ("submissions", "metadata_cache_read_tokens", "INTEGER"),
    ("submissions", "metadata_cache_creation_tokens", "INTEGER"),
    ("submissions", "metadata_duration_seconds", "FLOAT"),
    ("cases", "metrics", "JSON"),
)


async def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # In-place column backfill for existing SQLite databases. Idempotent:
        # checks PRAGMA table_info() before issuing ALTER TABLE so a fresh DB
        # is a no-op.
        if "sqlite" in settings.database_url:
            for table, column, sql_type in _BACKFILL_COLUMNS:
                existing = await conn.execute(text(f"PRAGMA table_info({table})"))
                cols = {row[1] for row in existing.fetchall()}
                if column not in cols:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"),
                    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
