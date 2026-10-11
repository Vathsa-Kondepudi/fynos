"""Database engine, session management, and initialization."""

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Business

# Default to SQLite in the current directory
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///bank_classifier.db")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    """Create all tables if they don't exist."""
    Base.metadata.create_all(engine)


def drop_db() -> None:
    """Drop all tables (for testing)."""
    Base.metadata.drop_all(engine)


def reset_db() -> None:
    """Drop and recreate all tables (for testing)."""
    drop_db()
    init_db()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_business(session: Session, name: str) -> Business:
    """Get a business by name, or create it if it doesn't exist."""
    business = session.query(Business).filter_by(name=name).first()
    if not business:
        business = Business(name=name)
        session.add(business)
        session.flush()  # Get the ID
    return business
