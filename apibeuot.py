import os
import uuid
import json
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
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
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://your-app.onrender.com")
KLARNA_API_URL = "https://api.playground.klarna.com"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent")

# Serve static folder (chat.html, dashboard.html)

# ------------------------------
# Pydantic models
# ------------------------------
class ChatMessage(BaseModel):
    message: str
    history: list | None = None

class KlarnaPaymentRequest(BaseModel):
    amount: float
    service: str
    customer_name: str
    booking_id: str | None = None

# ------------------------------
# Helpers
# ------------------------------
def clean_expired_slots(db):
    """Delete expired slots from DB"""
    now = datetime.now()
    expired = db.query(Slot).all()
    for s in expired:
        slot_dt = datetime.strptime(f"{s.date} {s.time}", "%Y-%m-%d %H:%M")
        if slot_dt <= now:
            db.delete(s)
    db.commit()

# ------------------------------
# Root & Dashboard
# ------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("dashboard.html")

# ------------------------------
# Chat endpoint with ChatGPT
# ------------------------------
@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    try:
        db = SessionLocal()
        clean_expired_slots(db)

        slots = db.query(Slot).filter_by(available=True).all()
        future_slots = [f"{s.date} {s.time}" for s in slots]
        db.close()

        slot_info = ", ".join(future_slots) if future_slots else "No slots available"

        # Call GPT to handle the conversation
        messages = [
            {"content": f"""You are a polite barbershop assistant. 
Available slots are: {slot_info}.
Help the user book a haircut by asking for:
- Customer name
- Date (YYYY-MM-DD)
- Time (HH:MM)

❗ Important: If the user provides name + a valid available date + time, IMMEDIATELY respond with JSON in this format:
{{"service":"Haircut","date":"YYYY-MM-DD","time":"HH:MM","customer_name":"NAME"}}

Do NOT keep asking again if you already have the information. 
If any piece of info is missing, only then ask for it.
"""

            }
        ]

        if user_input.history:
            messages.extend(user_input.history)

        messages.append({"role": "user", "content": user_input.message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )

        reply = response.choices[0].message.content

        # Look for JSON in GPT reply
        booking_match = re.search(r"\{.*\}", reply)
        if booking_match:
            booking_data = json.loads(booking_match.group())

            db = SessionLocal()
            slot_exists = db.query(Slot).filter_by(
                date=booking_data["date"],
                time=booking_data["time"],
                available=True
            ).first()

            if not slot_exists:
                db.close()
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
            db.close()

            return {
                "status": "reserved",
                "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking_data['customer_name']} "
                         f"at {booking_data['time']} on {booking_data['date']}.<br><br>💳 Would you like to pay now?",
                "booking_id": booking_id
            }

        return {"status": "ok", "reply": reply}

    except Exception as e:
        return {"status": "error", "reply": f"⚠️ Error: {str(e)}"}
def clean_stale_bookings(db):
    """Delete pending bookings older than 10 minutes and free slots"""
    now = datetime.utcnow()
    stale_bookings = db.query(Booking).filter_by(status="pending").all()
    for b in stale_bookings:
        created = datetime.fromisoformat(b.created_at)
        if created + timedelta(minutes=10) < now:
            # Free up the slot
            slot = db.query(Slot).filter_by(date=b.date, time=b.time).first()
            if slot:
                slot.available = True
            db.delete(b)
    db.commit()
# ------------------------------
# Klarna Payment
# ------------------------------
@app.post("/pay/klarna")
async def pay_with_klarna(payment: KlarnaPaymentRequest):
    reference = payment.booking_id or str(uuid.uuid4())

    data = {
        "purchase_country": "SE",
        "purchase_currency": "SEK",
        "locale": "sv-SE",
        "order_amount": int(payment.amount * 100),
        "order_tax_amount": 0,
        "order_lines": [
            {
                "type": "physical",
                "reference": reference,
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
            "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id={{checkout.order.id}}",
            "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id={{checkout.order.id}}",
            "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id={{checkout.order.id}}"
        }
    }

    response = requests.post(
        f"{KLARNA_API_URL}/checkout/v3/orders",
        auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
        headers={"Content-Type": "application/json"},
        json=data,
        timeout=20
    )

    if response.status_code != 200:
        try:
            error_data = response.json()
        except Exception:
            error_data = response.text
        raise HTTPException(status_code=response.status_code, detail=error_data)

    return response.json()

# ------------------------------
# Klarna Push: mark booking paid
# ------------------------------
@app.post("/klarna/push")
async def klarna_push(klarna_order_id: str, db: SessionLocal = Depends(SessionLocal)):
    r = requests.get(
        f"{KLARNA_API_URL}/checkout/v3/orders/{klarna_order_id}",
        auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=20
    )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=r.text)

    order = r.json()
    status = order.get("status")
    lines = order.get("order_lines", [])
    ref = lines[0].get("reference") if lines else None

    if status == "checkout_complete" and ref:
        booking = db.query(Booking).filter_by(id=ref).first()
        if booking:
            booking.status = "paid"
            db.commit()
            return {"updated": True, "booking_id": booking.id}

    return {"updated": False, "status": status}

# ------------------------------
# Klarna required pages
# ------------------------------
@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return HTMLResponse("<h1>Terms & Conditions</h1><p>Sample terms page for Klarna.</p>")

@app.get("/confirmation", response_class=HTMLResponse)
async def confirmation(klarna_order_id: str = ""):
    return HTMLResponse(f"<h1>Payment Confirmation</h1><p>Klarna order: {klarna_order_id}</p>")

@app.get("/checkout", response_class=HTMLResponse)
async def checkout(klarna_order_id: str, request: Request):
    response = requests.get(
        f"{KLARNA_API_URL}/checkout/v3/orders/{klarna_order_id}",
        auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=20
    )
    if response.status_code != 200:
        return HTMLResponse(f"<h1>Klarna error</h1><pre>{response.text}</pre>", status_code=500)

    order = response.json()
    snippet = order.get("html_snippet", "")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Klarna Checkout</title></head><body>{snippet}</body></html>"""
    return HTMLResponse(html)

# ------------------------------
# Slots API
# ------------------------------
@app.get("/api/slots")
async def get_slots():
    db = SessionLocal()
    clean_expired_slots(db)
    slots = db.query(Slot).filter_by(available=True).all()
    result = {}
    for s in slots:
        result.setdefault(s.date, []).append(s.time)
    db.close()
    return result

@app.post("/api/slots")
async def add_slot(slot: dict):
    db = SessionLocal()
    new_slot = Slot(date=slot["date"], time=slot["time"], available=True)
    db.add(new_slot)
    db.commit()
    db.close()
    return {"status": "ok"}

@app.delete("/api/slots")
async def delete_slot(date: str, time: str):
    db = SessionLocal()
    slot = db.query(Slot).filter_by(date=date, time=time).first()
    if slot:
        db.delete(slot)
        db.commit()
    db.close()
    return {"status": "deleted"}

# ------------------------------
# Bookings API
# ------------------------------
@app.get("/api/bookings")
async def get_bookings():
    db = SessionLocal()
    bookings = db.query(Booking).all()
    result = [
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
    db.close()
    return result

@app.post("/api/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: str):
    db = SessionLocal()
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        db.close()
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = "cancelled"
    db.commit()
    db.close()
    return {"status": "cancelled", "booking_id": booking_id}

@app.post("/api/bookings/{booking_id}/paid")
async def mark_booking_paid(booking_id: str):
    db = SessionLocal()
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        db.close()
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = "paid"
    db.commit()
    db.close()
    return {"status": "paid", "booking_id": booking_id}
