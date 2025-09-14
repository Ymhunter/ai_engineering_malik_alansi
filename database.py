import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, Integer, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------------------------
# Database URL
# ------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
    created_at = Column(String, default=lambda: datetime.utcnow().isoformat())  # NEW


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
