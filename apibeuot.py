from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
import base64
import uuid
import openai
import os
import json
import re
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# ------------------------------
# Load environment variables
# ------------------------------
load_dotenv()

KLARNA_USERNAME = os.getenv("KLARNA_USERNAME")
KLARNA_PASSWORD = os.getenv("KLARNA_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")
if not KLARNA_USERNAME or not KLARNA_PASSWORD:
    raise RuntimeError("❌ Missing KLARNA_USERNAME or KLARNA_PASSWORD in environment")
if not PUBLIC_URL:
    raise RuntimeError("❌ Missing PUBLIC_URL in environment")

# ------------------------------
# Config
# ------------------------------
openai.api_key = OPENAI_API_KEY
KLARNA_API_URL = "https://api.playground.klarna.com"

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent with Klarna")

# Enable CORS so frontend fetch() works
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production restrict to your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------
# Pydantic models
# ------------------------------
class ChatMessage(BaseModel):
    message: str

class BookingRequest(BaseModel):
    service: str
    date: str
    time: str
    customer_name: str

class KlarnaPaymentRequest(BaseModel):
    amount: float
    service: str
    customer_name: str

# ------------------------------
# Mock "database"
# ------------------------------
available_slots = {
    "2025-09-13": ["10:00", "11:00", "14:00"],
    "2025-09-14": ["09:00", "12:00", "15:00"]
}
bookings = {}

# ------------------------------
# Helpers
# ------------------------------
def check_availability(date: str, time: str):
    return time in available_slots.get(date, [])

def create_klarna_order(amount: float, service: str, customer_name: str):
    """Create Klarna checkout order"""
    url = f"{KLARNA_API_URL}/checkout/v3/orders"

    auth = base64.b64encode(f"{KLARNA_USERNAME}:{KLARNA_PASSWORD}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }

    order_id = str(uuid.uuid4())

    data = {
        "purchase_country": "SE",
        "purchase_currency": "SEK",
        "locale": "sv-SE",
        "order_amount": int(amount * 100),  # in öre
        "order_tax_amount": 0,
        "order_lines": [
            {
                "type": "physical",  # ✅ Klarna requires accepted type
                "reference": order_id,
                "name": service,
                "quantity": 1,
                "unit_price": int(amount * 100),
                "total_amount": int(amount * 100),
                "total_tax_amount": 0,
                "tax_rate": 0
            }
        ],
        "merchant_urls": {
            "terms": f"{PUBLIC_URL}/terms",
            "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id=XYZ",
            "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id=XYZ",
            "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id=XYZ"
        }
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        print("❌ Klarna error:", response.text)
        raise HTTPException(status_code=500, detail=response.text)

    return response.json()

def extract_booking_from_message(message: str) -> BookingRequest:
    """Try GPT first, then fallback to regex extraction"""
    try:
        completion = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a booking assistant for a barbershop."},
                {"role": "user", "content": f"Extract service, date (YYYY-MM-DD), time (HH:MM 24h), and customer_name from: {message}. Return JSON only."}
            ],
            temperature=0
        )
        content = completion.choices[0].message["content"]
        print("✅ GPT output:", content)
        parsed = json.loads(content)
        return BookingRequest(**parsed)
    except Exception as e:
        print("❌ GPT failed, using regex fallback:", e)

        # Regex fallback
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", message)
        time_match = re.search(r"\d{2}:\d{2}", message)
        name_match = re.search(r"for\s+(\w+)", message, re.IGNORECASE)

        return BookingRequest(
            service="Haircut",
            date=date_match.group(0) if date_match else "",
            time=time_match.group(0) if time_match else "",
            customer_name=name_match.group(1) if name_match else ""
        )

# ------------------------------
# API Endpoints
# ------------------------------
@app.get("/")
async def root():
    return {"status": "ok", "message": "Barbershop Booking AI Agent is running 🚀"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    text = user_input.message.strip().lower()

    # Friendly greeting for smalltalk
    if text in ["hi", "hello", "hey"]:
        return {
            "status": "greeting",
            "reply": "👋 Hello! I can help you book a haircut. Please tell me your name, the date (YYYY-MM-DD), and the time (HH:MM)."
        }

    booking = extract_booking_from_message(user_input.message)

    # Check what info is missing
    missing = []
    if not booking.customer_name:
        missing.append("your name")
    if not booking.date:
        missing.append("the date (YYYY-MM-DD)")
    if not booking.time:
        missing.append("the time (HH:MM)")

    if missing:
        return {
            "status": "incomplete",
            "reply": f"Could you please provide {' and '.join(missing)} for your booking?"
        }

    # If slot is unavailable, suggest alternatives
    if not check_availability(booking.date, booking.time):
        alternatives = available_slots.get(booking.date, [])
        if alternatives:
            return {
                "status": "unavailable",
                "reply": f"❌ Sorry, {booking.date} at {booking.time} is not available. Available times are: {', '.join(alternatives)}."
            }
        else:
            return {
                "status": "unavailable",
                "reply": f"❌ Sorry, no slots available on {booking.date}. Please choose another day."
            }

    # Reserve booking
    available_slots[booking.date].remove(booking.time)
    booking_id = str(uuid.uuid4())
    bookings[booking_id] = {"booking": booking.dict(), "status": "pending"}

    return {
        "status": "reserved",
        "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking.customer_name} at {booking.time} on {booking.date}."
    }

@app.post("/pay/klarna")
async def pay_with_klarna(payment: KlarnaPaymentRequest):
    order = create_klarna_order(payment.amount, payment.service, payment.customer_name)
    return {
        "status": "klarna_order_created",
        "order_id": order.get("order_id"),
        "html_snippet": order.get("html_snippet")
    }

@app.post("/klarna/push")
async def klarna_push(request: Request):
    klarna_order_id = request.query_params.get("klarna_order_id")
    body = await request.json()
    print(f"💳 Klarna push received for {klarna_order_id}: {body}")
    return {"status": "received", "order_id": klarna_order_id}

# ------------------------------
# Serve chatbot frontend
# ------------------------------
@app.get("/chatbot")
async def chatbot_ui():
    return FileResponse("chat.html")
