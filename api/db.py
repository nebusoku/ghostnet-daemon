# api/db.py
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .settings import settings

# Single source of truth for Base
Base = declarative_base()

DATABASE_URL = settings.database_url

# SQLite needs check_same_thread for FastAPI multithreading;
# Postgres (and most others) should NOT use it.
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """
    Create tables (bootstrap).
    Import models lazily to avoid circular imports and ensure all tables are registered on Base.
    """
    from . import models  # noqa: F401  (register model classes on Base)

    Base.metadata.create_all(bind=engine)


def utcnow() -> datetime:
    """
    Naive UTC "now", replacing the deprecated datetime.utcnow().

    Deliberately naive: the timestamp columns are TIMESTAMP WITHOUT TIME
    ZONE, and mixing an aware value with the naive ones already stored
    raises TypeError on subtraction -- which would break the absence-digest
    arithmetic in api/player_memory.py.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
