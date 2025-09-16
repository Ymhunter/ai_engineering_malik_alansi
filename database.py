import os
from datetime import datetime, date as DateType, time as TimeType
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean,
    inspect, text, Date, Time, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------------------------
# Database URL (Postgres recommended, fallback SQLite)
# ------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # Works with postgresql://... from Supabase, Neon, Render, etc.
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
    date = Column(Date)     # ✅ Proper DATE type
    time = Column(Time)     # ✅ Proper TIME type
    status = Column(String, default="pending")  # pending / paid / cancelled
    created_at = Column(String, default=lambda: datetime.utcnow().isoformat())


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(Date, index=True)
    time = Column(Time)
    available = Column(Boolean, default=True)

    # ✅ Prevent duplicate slots for same date+time
    __table_args__ = (UniqueConstraint("date", "time", name="uq_slot_datetime"),)


# ------------------------------
# Create tables (if not exist)
# ------------------------------
Base.metadata.create_all(bind=engine)

# ------------------------------
# Helpers for safe parsing
# ------------------------------
def to_date(v):
    """Convert string/Date to datetime.date"""
    if not v:
        return None
    if isinstance(v, DateType):
        return v
    try:
        return datetime.fromisoformat(str(v)).date()
    except Exception:
        try:
            return datetime.strptime(str(v), "%Y-%m-%d").date()
        except Exception:
            return None


def to_time(v):
    """Convert string/Time to datetime.time"""
    if not v:
        return None
    if isinstance(v, TimeType):
        return v
    text_v = str(v)
    try:
        # Handles "09:00", "09:00:00", "09:00:00.000Z"
        if "Z" in text_v:
            text_v = text_v.replace("Z", "")
        return datetime.fromisoformat(f"2000-01-01T{text_v}").time()
    except Exception:
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text_v, fmt).time()
            except Exception:
                continue
    return None

# ------------------------------
# SQLite-only migration helper
# ------------------------------
def ensure_created_at_column():
    if DATABASE_URL.startswith("sqlite"):
        inspector = inspect(engine)
        cols = [c["name"] for c in inspector.get_columns("bookings")]
        if "created_at" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE bookings ADD COLUMN created_at VARCHAR"))
                conn.commit()

ensure_created_at_column()
