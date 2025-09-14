import os
import uuid
import json
import re
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from dotenv import load_dotenv
import requests
from openai import OpenAI

from database import SessionLocal, Booking, Slot

# ------------------------------
# Load environment
# ------------------------------
load_dotenv()

KLARNA_USERNAME = os.getenv("KLARNA_USERNAME")
KLARNA_PASSWORD = os.getenv("KLARNA_PASSWORD")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")
KLARNA_API_URL = os.getenv("KLARNA_API_URL", "https://api.playground.klarna.com")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent")

# ------------------------------
# DB Dependency
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

class KlarnaPaymentRequest(BaseModel):
    amount: float
    service: str
    customer_name: str

# ------------------------------
# Helpers
# ------------------------------
def clean_expired_slots(db: Session):
    """Delete expired slots (string comparison works for YYYY-MM-DD HH:MM)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    expired = db.query(Slot).all()
    for s in expired:
        if f"{s.date} {s.time}" <= now_str:
            db.delete(s)
    db.commit()

# ------------------------------
# HTML Routes
# ------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("chat.html")   # same folder as main.py

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("dashboard.html")   # same folder as main.py

@app.get("/chat/test")
async def chat_test():
    return {"status": "ok", "message": "Chat endpoint is alive"}

# ------------------------------
# Chat endpoint
# ------------------------------
@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage, db: Session = Depends(get_db)):
    try:
        clean_expired_slots(db)

        slots = db.query(Slot).filter_by(available=True).all()
        future_slots = [f"{s.date} {s.time}" for s in slots]
        slot_info = ", ".join(future_slots) if future_slots else "No slots available"

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a polite barbershop assistant.
Available slots: {slot_info}.
Help the user book a haircut by asking for:
- Customer name
- Date (YYYY-MM-DD)
- Time (HH:MM)

If all info is present and matches availability, reply ONLY with JSON:
{{"service":"Haircut","date":"YYYY-MM-DD","time":"HH:MM","customer_name":"NAME"}}
Otherwise, guide the user to pick from available slots."""
                },
                {"role": "user", "content": user_input.message}
            ]
        )

        reply = response.choices[0].message.content
        booking_match = re.search(r"\{.*?\}", reply, re.DOTALL)

        if booking_match:
            try:
                booking_data = json.loads(booking_match.group())
            except json.JSONDecodeError:
                return {"status": "ok", "reply": reply}

            slot_exists = db.query(Slot).filter_by(
                date=booking_data["date"], time=booking_data["time"], available=True
            ).first()

            if not slot_exists:
                return {"status": "unavailable", "reply": "❌ Sorry, that slot is not available."}

            booking_id = str(uuid.uuid4())
            booking = Booking(
                id=booking_id,
                customer_name=booking_data["customer_name"],
                service=booking_data["service"],
                date=booking_data["date"],
                time=booking_data["time"],
                status="pending"
            )
            db.add(booking)
            slot_exists.available = False
            db.commit()

            return {
                "status": "reserved",
                "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking_data['customer_name']} "
                         f"at {booking_data['time']} on {booking_data['date']}.<br><br>"
                         "💳 Would you like to pay now?"
            }

        return {"status": "ok", "reply": reply}

    except Exception as e:
        return {"status": "error", "reply": f"⚠️ Error: {str(e)}"}

# ------------------------------
# Klarna Payment
# ------------------------------
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
                "type": "service",
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

    return response.json()

# ------------------------------
# Slots API
# ------------------------------
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

# ------------------------------
# Bookings API
# ------------------------------
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
