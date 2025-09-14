import os
from sqlalchemy import create_engine, Column, String, Integer, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------------------------
# Database URL
# ------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")
# If DATABASE_URL not set → fallback to local SQLite file

# ------------------------------
# Engine and Session
# ------------------------------
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ------------------------------
# Models
# ------------------------------
class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String, primary_key=True, index=True)
    customer_name = Column(String, index=True)
    service = Column(String)
    date = Column(String)
    time = Column(String)
    status = Column(String, default="pending")  # pending / paid


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(String, index=True)
    time = Column(String)
    available = Column(Boolean, default=True)

# ------------------------------
# Create tables (if not exist)
# ------------------------------
Base.metadata.create_all(bind=engine)
