import os
import uuid
import json
import re
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy.orm import Session

from database import SessionLocal, Booking, Slot, to_date, to_time

# ------------------------------
# Load environment
# ------------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent")

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
def clean_expired_slots(db: Session):
    now = datetime.now()
    for s in db.query(Slot).all():
        try:
            slot_dt = datetime.combine(to_date(s.date), to_time(s.time))
            if slot_dt <= now:
                db.delete(s)
        except Exception:
            continue
    db.commit()

def clean_stale_bookings(db: Session):
    now = datetime.utcnow()
    stale = db.query(Booking).filter_by(status="pending").all()
    for b in stale:
        try:
            created = datetime.fromisoformat(b.created_at)
        except Exception:
            created = now - timedelta(hours=1)
        if created + timedelta(minutes=10) < now:
            slot = db.query(Slot).filter_by(date=b.date, time=b.time).first()
            if slot:
                slot.available = True
            db.delete(b)
    db.commit()

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
    for d, times in result.items():
        result[d] = sorted(set(times))
    return result

def get_bookings_sync(db: Session):
    clean_stale_bookings(db)
    bookings = db.query(Booking).all()
    result = []
    for b in bookings:
        result.append({
            "id": b.id,
            "customer_name": b.customer_name,
            "service": b.service,
            "date": b.date.isoformat() if b.date else None,
            "time": b.time.strftime("%H:%M") if b.time else None,
            "status": b.status
        })
    return result

# ------------------------------
# Pages
# ------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("dashboard.html")

# ------------------------------
# WebSockets for dashboard
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
    slots = get_slots_sync(db)
    bookings = get_bookings_sync(db)
    payload = {"slots": slots, "bookings": bookings}
    to_remove = []
    for ws in active_connections:
        try:
            await ws.send_json(payload)
        except Exception:
            to_remove.append(ws)
    for ws in to_remove:
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
                {"role": "system", "content": "You are an intent classifier. Decide if the user wants to BOOK an appointment (haircut). Answer only 'book' or 'other'."},
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
        clean_expired_slots(db)
        clean_stale_bookings(db)
        slots = get_slots_sync(db)
        future_slots = [f"{d} {t}" for d, times in slots.items() for t in times]
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
            slot_exists = db.query(Slot).filter_by(
                date=to_date(booking_data["date"]),
                time=to_time(booking_data["time"]),
                available=True
            ).first()
            if not slot_exists:
                return {"status": "unavailable", "reply": "❌ Sorry, that slot is not available."}

            booking_id = str(uuid.uuid4())
            booking = Booking(
                id=booking_id,
                customer_name=booking_data["customer_name"],
                service=booking_data["service"],
                date=to_date(booking_data["date"]),
                time=to_time(booking_data["time"]),
                status="pending"
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
    d = to_date(slot.get("date"))
    t = to_time(slot.get("time"))
    if not d or not t:
        raise HTTPException(status_code=400, detail=f"Invalid date/time: {slot}")
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
        raise HTTPException(status_code=400, detail=f"Invalid date/time: {date} {time}")
    slot = db.query(Slot).filter_by(date=d, time=t).first()
    if slot:
        db.delete(slot)
        db.commit()
        trigger_broadcast(db)
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Slot not found")

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
