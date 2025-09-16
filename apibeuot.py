import os
import uuid
import json
import re
import asyncio
from datetime import datetime, timedelta, date as DateType, time as TimeType

from fastapi import (
    FastAPI, HTTPException, Depends,
    Body, WebSocket, WebSocketDisconnect
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, Column, String, Integer, Boolean, Date, Time, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# ------------------------------
# Load environment
# ------------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./barbershop.db")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------
# Database setup
# ------------------------------
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
    date = Column(Date, index=True)
    time = Column(Time)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    date = Column(Date, index=True)
    time = Column(Time)
    available = Column(Boolean, default=True)


Base.metadata.create_all(bind=engine)

# ------------------------------
# DB dependency
# ------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ------------------------------
# Pydantic models
# ------------------------------
class ChatMessage(BaseModel):
    message: str
    history: list | None = None

# ------------------------------
# Helpers
# ------------------------------
def to_date(v) -> DateType | None:
    if isinstance(v, DateType):
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v, "%Y-%m-%d").date()
        except Exception:
            try:
                return datetime.fromisoformat(v).date()
            except Exception:
                return None
    return None


def to_time(v) -> TimeType | None:
    if isinstance(v, TimeType):
        return v
    if isinstance(v, str):
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(v, fmt).time()
            except Exception:
                continue
    return None


def to_datetime(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None

# ------------------------------
# Cleanup
# ------------------------------
def clean_expired_slots(db: Session):
    """Delete slots in the past (local time)."""
    now = datetime.now()  # ✅ local, not UTC
    for s in db.query(Slot).all():
        d = to_date(s.date)
        t = to_time(s.time)
        if not d or not t:
            continue
        if datetime.combine(d, t) <= now:
            db.delete(s)
    db.commit()


def clean_stale_bookings(db: Session):
    """Delete pending bookings older than 10 minutes and free slots."""
    now = datetime.now()
    stale = db.query(Booking).filter_by(status="pending").all()
    for b in stale:
        created = to_datetime(b.created_at) or now - timedelta(hours=1)
        if created + timedelta(minutes=10) < now:
            d = to_date(b.date)
            t = to_time(b.time)
            if d and t:
                slot = db.query(Slot).filter_by(date=d, time=t).first()
                if slot:
                    slot.available = True
            db.delete(b)
    db.commit()

# ------------------------------
# Slots & bookings helpers
# ------------------------------
def get_slots_sync(db: Session):
    clean_expired_slots(db)
    clean_stale_bookings(db)
    slots = db.query(Slot).filter_by(available=True).all()
    result: dict[str, list[str]] = {}
    for s in slots:
        d = to_date(s.date)
        t = to_time(s.time)
        if not d or not t:
            continue
        result.setdefault(d.isoformat(), []).append(t.strftime("%H:%M"))
    # normalize times
    for date, times in result.items():
        result[date] = sorted(set(times))
    return result


def get_bookings_sync(db: Session):
    clean_stale_bookings(db)
    bookings = db.query(Booking).all()
    result = []
    for b in bookings:
        d = to_date(b.date)
        t = to_time(b.time)
        if not d or not t:
            continue
        result.append({
            "id": b.id,
            "customer_name": b.customer_name,
            "service": b.service,
            "date": d.isoformat(),
            "time": t.strftime("%H:%M"),
            "status": b.status
        })
    return result

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent")

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("dashboard.html")

# ------------------------------
# WebSocket
# ------------------------------
active_connections: list[WebSocket] = []

@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)


async def broadcast_update(db: Session):
    payload = {"slots": get_slots_sync(db), "bookings": get_bookings_sync(db)}
    for ws in active_connections[:]:
        try:
            await ws.send_json(payload)
        except Exception:
            active_connections.remove(ws)


def trigger_broadcast(db: Session):
    asyncio.create_task(broadcast_update(db))

# ------------------------------
# Intent detection
# ------------------------------
@app.post("/intent")
async def detect_intent(payload: dict = Body(...)):
    message = payload.get("message", "")
    if not message:
        return {"intent": "unknown"}
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an intent classifier. Decide if the user wants to BOOK an appointment (haircut). The message may be in ANY language. Answer only 'book' or 'other'."},
                {"role": "user", "content": message}
            ]
        )
        intent = response.choices[0].message.content.strip().lower()
        if "book" in intent:
            return {"intent": "book"}
        return {"intent": "other"}
    except Exception as e:
        return {"intent": "error", "detail": str(e)}

# ------------------------------
# Chat endpoint
# ------------------------------
@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage, db: Session = Depends(get_db)):
    try:
        slots_dict = get_slots_sync(db)
        future_slots = [f"{d} {t}" for d, times in slots_dict.items() for t in times]
        slot_info = ", ".join(future_slots) if future_slots else "No slots available"

        messages = [
            {"role": "system", "content": f"""You are a polite barbershop assistant. 
Available slots are: {slot_info}.

Your task:
- Collect the customer's name
- Collect a valid available date (YYYY-MM-DD)
- Collect a valid available time (HH:MM)

❗Rules:
- If the user already gave all three (name, date, time), respond ONLY with JSON:
{{"service":"Haircut","date":"YYYY-MM-DD","time":"HH:MM","customer_name":"NAME"}}
- Do NOT ask again for details you already have.
- If something is missing, only ask for that missing part.
"""}
        ]
        if user_input.history:
            messages.extend(user_input.history)
        messages.append({"role": "user", "content": user_input.message})

        response = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
        reply = response.choices[0].message.content or ""

        booking_match = re.search(r"\{.*?\}", reply, re.DOTALL)
        if booking_match:
            booking_data = json.loads(booking_match.group())
            d = to_date(booking_data["date"])
            t = to_time(booking_data["time"])
            if not d or not t:
                return {"status": "ok", "reply": "❌ Invalid date/time format."}

            slot_exists = db.query(Slot).filter_by(date=d, time=t, available=True).first()
            if not slot_exists:
                return {"status": "unavailable", "reply": "❌ Sorry, that slot is not available."}

            booking_id = str(uuid.uuid4())
            booking = Booking(
                id=booking_id,
                customer_name=booking_data["customer_name"],
                service=booking_data["service"],
                date=d,
                time=t,
                status="pending",
                created_at=datetime.now()
            )
            db.add(booking)
            slot_exists.available = False
            db.commit()

            trigger_broadcast(db)

            return {
                "status": "reserved",
                "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking.customer_name} at {booking.time.strftime('%H:%M')} on {booking.date.isoformat()}.<br><br>💳 Pay now?",
                "booking_id": booking_id
            }

        return {"status": "ok", "reply": reply}
    except Exception as e:
        return {"status": "error", "reply": f"⚠️ Error: {str(e)}"}

# ------------------------------
# Slots API
# ------------------------------
@app.get("/api/slots")
async def get_slots(db: Session = Depends(get_db)):
    return JSONResponse(get_slots_sync(db), headers={"Cache-Control": "no-store"})


@app.post("/api/slots")
async def add_slot(slot: dict, db: Session = Depends(get_db)):
    d = to_date(slot["date"])
    t = to_time(slot["time"])
    if not d or not t:
        raise HTTPException(status_code=400, detail="Invalid date/time")
    new_slot = Slot(date=d, time=t, available=True)
    db.add(new_slot)
    db.commit()
    trigger_broadcast(db)
    return {"status": "ok"}


@app.delete("/api/slots")
async def delete_slot(date: str, time: str, db: Session = Depends(get_db)):
    d = to_date(date)
    t = to_time(time)
    if not d or not t:
        raise HTTPException(status_code=400, detail="Invalid date/time")
    slot = db.query(Slot).filter_by(date=d, time=t).first()
    if slot:
        db.delete(slot)
        db.commit()
        trigger_broadcast(db)
    return {"status": "deleted"}

# ------------------------------
# Bookings API
# ------------------------------
@app.get("/api/bookings")
async def get_bookings(db: Session = Depends(get_db)):
    return JSONResponse(get_bookings_sync(db), headers={"Cache-Control": "no-store"})


@app.post("/api/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = "cancelled"
    slot = db.query(Slot).filter_by(date=booking.date, time=booking.time).first()
    if slot:
        slot.available = True
    db.commit()
    trigger_broadcast(db)
    return {"status": "cancelled", "booking_id": booking_id}


@app.post("/api/bookings/{booking_id}/paid")
async def mark_booking_paid(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = "paid"
    db.commit()
    trigger_broadcast(db)
    return {"status": "paid", "booking_id": booking_id}
