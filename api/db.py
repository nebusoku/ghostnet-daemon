from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base

# For now: SQLite file in repo root.
# Later you can swap this to Postgres by changing DATABASE_URL.
DATABASE_URL = "sqlite:///./ghostnet.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # needed for SQLite + FastAPI
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
