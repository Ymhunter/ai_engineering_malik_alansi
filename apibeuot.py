import os
import uuid
import json
import re
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
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
        # Call GPT to understand booking request
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """You are a polite barbershop assistant. 
                Your job is to help customers book a haircut by asking for:
                - Customer name
                - Date (YYYY-MM-DD)
                - Time (HH:MM)

                If all info is present, respond with a JSON object:
                {"service": "Haircut", "date": "YYYY-MM-DD", "time": "HH:MM", "customer_name": "NAME"}

                Otherwise, ask the missing questions naturally."""},
                {"role": "user", "content": user_input.message}
            ]
        )

        reply = response.choices[0].message.content

        # Try to detect JSON booking info
        booking_match = re.search(r"\{.*\}", reply)
        if booking_match:
            booking_data = json.loads(booking_match.group())

            # Validate slot
            slot_dt = datetime.strptime(f"{booking_data['date']} {booking_data['time']}", "%Y-%m-%d %H:%M")
            if slot_dt <= datetime.now():
                return {"status": "unavailable", "reply": "❌ Sorry, that time has already passed."}

            db = SessionLocal()
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
            db.commit()
            db.close()

            return {
                "status": "reserved",
                "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking_data['customer_name']} at {booking_data['time']} on {booking_data['date']}.<br><br>💳 Would you like to pay now?"
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
# Slots API (future slots only)
# ------------------------------
@app.get("/api/slots")
async def get_slots():
    db = SessionLocal()
    now = datetime.now()
    slots = db.query(Slot).filter_by(available=True).all()

    result = {}
    for s in slots:
        slot_dt = datetime.strptime(f"{s.date} {s.time}", "%Y-%m-%d %H:%M")
        if slot_dt > now:
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
