import os
import uuid
import json
import re
from typing import List, Optional, Dict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import requests
from openai import OpenAI

from database import SessionLocal, Booking, Slot

# ---------- Env ----------
load_dotenv()
KLARNA_USERNAME = os.getenv("KLARNA_USERNAME")
KLARNA_PASSWORD = os.getenv("KLARNA_PASSWORD")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")
KLARNA_API_URL = os.getenv("KLARNA_API_URL", "https://api.playground.klarna.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- App ----------
app = FastAPI(title="Barbershop Booking AI Agent")
BASE_DIR = Path(__file__).resolve().parent

# ---------- DB dep ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Schemas ----------
class HistoryMsg(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str

class ChatMessage(BaseModel):
    message: str
    history: Optional[List[HistoryMsg]] = None  # short recent history from the client

class KlarnaPaymentRequest(BaseModel):
    amount: float
    service: str
    customer_name: str

# ---------- Helpers ----------
def clean_expired_slots(db: Session):
    """Delete slots that are strictly in the past."""
    now = datetime.now()
    for s in db.query(Slot).all():
        try:
            slot_dt = datetime.strptime(f"{s.date} {s.time}", "%Y-%m-%d %H:%M")
            if slot_dt < now:
                db.delete(s)
        except ValueError:
            continue
    db.commit()

def format_slots(db: Session) -> str:
    slots = db.query(Slot).filter_by(available=True).all()
    future_slots = [f"- {s.date} at {s.time}" for s in slots]
    return "\n".join(future_slots) if future_slots else "No slots available"

# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(BASE_DIR / "chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse(BASE_DIR / "dashboard.html")

@app.get("/chat/test")
async def chat_test():
    return {"status": "ok", "message": "Chat endpoint is alive"}

# ---------- Chat ----------
@app.post("/chat")
async def chat_with_agent(payload: ChatMessage, db: Session = Depends(get_db)):
    try:
        clean_expired_slots(db)
        slot_info = format_slots(db)

        # Build the full message list: system + recent history + current user
        messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": f"""You are a polite barbershop assistant.
Available slots:\n{slot_info}

Your goal is to book a Haircut. Collect these fields and remember what the user already provided across turns:
- customer_name
- date (YYYY-MM-DD)
- time (HH:MM)

Rules:
- If some fields are missing, ask ONLY for the missing ones.
- NEVER ask again for info already provided in the conversation history.
- When ALL fields are present AND the slot is in the available list, reply ONLY with JSON on a single line:
{{"service":"Haircut","date":"YYYY-MM-DD","time":"HH:MM","customer_name":"NAME"}}
- Otherwise reply in natural language, offering the available slots (from the list above) as options if needed."""
            }
        ]

        # Append recent history from the client (only user/assistant roles)
        if payload.history:
            for m in payload.history[-8:]:
                messages.append({"role": m.role, "content": m.content})

        # Current user message
        messages.append({"role": "user", "content": payload.message})

        # Call OpenAI
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )
        reply = response.choices[0].message.content or ""

        # Try to extract booking JSON
        booking_match = re.search(r"\{.*?\}", reply, re.DOTALL)
        if booking_match:
            try:
                booking_data = json.loads(booking_match.group())
            except json.JSONDecodeError:
                return {"status": "ok", "reply": reply}

            # Validate slot availability
            slot_exists = db.query(Slot).filter_by(
                date=booking_data.get("date"),
                time=booking_data.get("time"),
                available=True
            ).first()

            if not slot_exists:
                return {
                    "status": "unavailable",
                    "reply": "❌ Sorry, that slot is not available. Please choose another from the list above."
                }

            # Create booking & mark slot unavailable
            booking_id = str(uuid.uuid4())
            booking = Booking(
                id=booking_id,
                customer_name=booking_data.get("customer_name", "Customer"),
                service=booking_data.get("service", "Haircut"),
                date=booking_data["date"],
                time=booking_data["time"],
                status="pending"
            )
            db.add(booking)
            slot_exists.available = False
            db.commit()

            return {
                "status": "reserved",
                "reply": (
                    f"✅ Reserved! Booking ID: {booking_id} for {booking.customer_name} "
                    f"at {booking.time} on {booking.date}.<br><br>💳 Would you like to pay now?"
                )
            }

        # Not complete yet: normal assistant text
        return {"status": "ok", "reply": reply}

    except Exception as e:
        return {"status": "error", "reply": f"⚠️ Error: {str(e)}"}

# ---------- Klarna ----------
@app.post("/pay/klarna")
async def pay_with_klarna(payment: KlarnaPaymentRequest):
    order_id = str(uuid.uuid4())
    data = {
        "purchase_country": "SE",
        "purchase_currency": "SEK",
        "locale": "sv-SE",
        "order_amount": int(payment.amount * 100),
        "order_tax_amount": 0,
        "order_lines": [
            {
                "type": "physical",
                "reference": order_id,
                "name": payment.service,
                "quantity": 1,
                "unit_price": int(payment.amount * 100),
                "total_amount": int(payment.amount * 100),
                "total_tax_amount": 0,
                "tax_rate": 0
            }
        ],
        "merchant_urls": {
            "terms": f"{PUBLIC_URL}/terms",
            "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id={order_id}",
            "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id={order_id}",
            "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id={order_id}"
        }
    }

    response = requests.post(
        f"{KLARNA_API_URL}/checkout/v3/orders",
        auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data
    )

    if response.status_code != 200:
        try:
            error_data = response.json()
        except Exception:
            error_data = response.text
        raise HTTPException(status_code=response.status_code, detail=error_data)

    # return Klarna's full JSON (includes html_snippet)
    return response.json()


# ---------- Slots ----------
@app.get("/api/slots")
async def get_slots(db: Session = Depends(get_db)):
    clean_expired_slots(db)
    slots = db.query(Slot).filter_by(available=True).all()
    result = {}
    for s in slots:
        result.setdefault(s.date, []).append(s.time)
    return result

@app.post("/api/slots")
async def add_slot(slot: dict, db: Session = Depends(get_db)):
    new_slot = Slot(date=slot["date"], time=slot["time"], available=True)
    db.add(new_slot)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Slot already exists")
    return {"status": "ok"}

@app.delete("/api/slots")
async def delete_slot(date: str, time: str, db: Session = Depends(get_db)):
    slot = db.query(Slot).filter_by(date=date, time=time).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    db.delete(slot)
    db.commit()
    return {"status": "deleted"}

# ---------- Bookings ----------
@app.get("/api/bookings")
async def get_bookings(db: Session = Depends(get_db)):
    bookings = db.query(Booking).all()
    return [
        {
            "id": b.id,
            "customer_name": b.customer_name,
            "service": b.service,
            "date": b.date,
            "time": b.time,
            "status": b.status,
        }
        for b in bookings
    ]
@app.get("/terms")
async def terms():
    return HTMLResponse("<h1>Terms & Conditions</h1><p>Test terms page for Klarna.</p>")

@app.get("/confirmation")
async def confirmation(klarna_order_id: str):
    return HTMLResponse(f"<h1>Payment Confirmation</h1><p>Order {klarna_order_id} confirmed.</p>")