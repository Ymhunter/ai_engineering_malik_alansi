import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, Boolean, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------------------------
# Database URL
# ------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

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
    date = Column(String)   # YYYY-MM-DD
    time = Column(String)   # HH:MM
    status = Column(String, default="pending")  # pending / paid / cancelled
    created_at = Column(String, default=lambda: datetime.utcnow().isoformat())


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(String, index=True)   # YYYY-MM-DD
    time = Column(String)               # HH:MM
    available = Column(Boolean, default=True)

# ------------------------------
# Create tables (if not exist)
# ------------------------------
Base.metadata.create_all(bind=engine)

# ------------------------------
# Ensure created_at column exists (for old DBs)
# ------------------------------
def ensure_created_at_column():
    inspector = inspect(engine)
    cols = [c["name"] for c in inspector.get_columns("bookings")]
    if "created_at" not in cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN created_at VARCHAR"))
            conn.commit()

ensure_created_at_column()
