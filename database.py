import os
from datetime import datetime, date as DateType, time as TimeType
from sqlalchemy import create_engine, Column, String, Integer, Boolean, inspect, text, Date, Time
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------------------------
# Database URL (Postgres recommended)
# ------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # Works with postgresql://... from Supabase, Neon, etc.
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
    date = Column(Date)    # ✅ Postgres DATE type
    time = Column(Time)    # ✅ Postgres TIME type
    status = Column(String, default="pending")  # pending / paid / cancelled
    created_at = Column(String, default=lambda: datetime.utcnow().isoformat())


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(Date, index=True)
    time = Column(Time)
    available = Column(Boolean, default=True)

# ------------------------------
# Create tables (if not exist)
# ------------------------------
Base.metadata.create_all(bind=engine)

# ------------------------------
# Helpers for safe parsing
# ------------------------------
def to_date(v):
    if isinstance(v, DateType):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v).date()
        except Exception:
            return None
    return None

def to_time(v):
    if isinstance(v, TimeType):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(f"2000-01-01T{v}").time()
        except Exception:
            return None
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
