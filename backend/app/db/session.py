from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import sessionmaker

from app.db.database import engine


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)