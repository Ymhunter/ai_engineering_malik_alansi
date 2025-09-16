import os
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, Slot, Booking, to_date, to_time  # reuse helpers if available

# ------------------------------
# Database URL
# ------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(bind=engine)

# ------------------------------
# Normalize existing records
# ------------------------------
def normalize_db():
    db = SessionLocal()

    # ✅ Normalize Slots
    for slot in db.query(Slot).all():
        d = to_date(slot.date)
        t = to_time(slot.time)
        if not d or not t:
            print(f"❌ Removing bad slot {slot.id}")
            db.delete(slot)
            continue
        slot.date = d
        slot.time = t.replace(second=0, microsecond=0)  # ensure HH:MM only

    # ✅ Normalize Bookings
    for b in db.query(Booking).all():
        d = to_date(b.date)
        t = to_time(b.time)
        if not d or not t:
            print(f"❌ Removing bad booking {b.id}")
            db.delete(b)
            continue
        b.date = d
        b.time = t.replace(second=0, microsecond=0)

        if isinstance(b.created_at, str):
            try:
                b.created_at = datetime.fromisoformat(b.created_at.replace("Z", "+00:00"))
            except Exception:
                b.created_at = datetime.utcnow()

    db.commit()
    db.close()
    print("✅ Database normalized!")

# ------------------------------
# Run
# ------------------------------
if __name__ == "__main__":
    normalize_db()
