"""Database session and engine configuration."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from echelonos.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
