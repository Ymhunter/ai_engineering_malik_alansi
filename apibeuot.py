import os
import uuid
import json
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException
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

# Serve static folder (chat.html, dashboard.html, CSS, JS)
app.mount("/static", StaticFiles(directory="static"), name="static")

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
    return FileResponse("static/chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("static/dashboard.html")

# ------------------------------
# Chat endpoint with ChatGPT
# ------------------------------
@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    try:
        db = SessionLocal()
        clean_expired_slots(db)  # ✅ remove past slots

        slots = db.query(Slot).filter_by(available=True).all()
        future_slots = [f"{s.date} {s.time}" for s in slots]
        db.close()

        slot_info = ", ".join(future_slots) if future_slots else "No slots available"

        # Call GPT to handle the conversation
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a polite barbershop assistant. 
                    Available slots are: {slot_info}.
                    Help the user book a haircut by asking for:
                    - Customer name
                    - Date (YYYY-MM-DD)
                    - Time (HH:MM)

                    If all info is present and matches availability, reply with JSON:
                    {{"service":"Haircut","date":"YYYY-MM-DD","time":"HH:MM","customer_name":"NAME"}}

                    Otherwise, guide the user to pick from available slots."""
                },
                {"role": "user", "content": user_input.message}
            ]
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
            # Mark slot as unavailable after booking
            slot_exists.available = False
            db.commit()
            db.close()

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
        raise HTTPException(status_code=500, detail=response.text)

    return response.json()

# ------------------------------
# Slots API
# ------------------------------
@app.get("/api/slots")
async def get_slots():
    db = SessionLocal()
    clean_expired_slots(db)  # ✅ cleanup old slots
    slots = db.query(Slot).filter_by(available=True).all()

    result = {}
    for s in slots:
        result.setdefault(s.date, []).append(s.time)

    db.close()
    return result

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
