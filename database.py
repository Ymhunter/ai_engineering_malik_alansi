import os
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    Boolean,
    UniqueConstraint,
    Index,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------------------------
# Database URL
# ------------------------------
# On Render/Heroku: set DATABASE_URL in environment
# Local fallback: SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

# ------------------------------
# Engine and Session
# ------------------------------
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ------------------------------
# Models
# ------------------------------
class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String, primary_key=True, index=True)   # UUID
    customer_name = Column(String, index=True)
    service = Column(String)
    date = Column(String, index=True)   # YYYY-MM-DD
    time = Column(String, index=True)   # HH:MM
    status = Column(String, default="pending")  # pending / paid / cancelled

    __table_args__ = (
        Index("ix_booking_date_time", "date", "time"),
    )


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(String, index=True)   # YYYY-MM-DD
    time = Column(String, index=True)   # HH:MM
    available = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("date", "time", name="uq_slot_datetime"),
        Index("ix_slot_date_time", "date", "time"),
    )

# ------------------------------
# Create tables (if not exist)
# ------------------------------
Base.metadata.create_all(bind=engine)
