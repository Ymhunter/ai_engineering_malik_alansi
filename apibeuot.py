# main.py
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
import base64
import uuid
import openai
import os

# ------------------------------
# Config
# ------------------------------
openai.api_key = "OPENAI_API_KEY"

KLARNA_API_URL = "https://api.playground.klarna.com"
KLARNA_USERNAME = "YOUR_KLARNA_USERNAME"
KLARNA_PASSWORD = "YOUR_KLARNA_PASSWORD"

# 👇 Public URL (ngrok / production / dummy)
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://example.com")

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent with Klarna")

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
    url = f"{KLARNA_API_URL}/checkout/v3/orders"

    # Encode Klarna credentials
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
        "order_amount": int(amount * 100),  # öre
        "order_tax_amount": 0,
        "order_lines": [
            {
                "type": "physical",
                "reference": order_id,
                "name": service,
                "quantity": 1,
                "unit_price": int(amount * 100),
                "total_amount": int(amount * 100),
                "total_tax_amount": 0,
                "tax_rate": 0   # Klarna requires this field
            }
        ],
        

"merchant_urls": {
    "terms": f"{PUBLIC_URL}/terms",
    "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id={order_id}",
    "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id={order_id}",
    "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id={order_id}"
}

    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)

    return response.json()

# ------------------------------
# API Endpoints
# ------------------------------
@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    booking = BookingRequest(
        service="Haircut",
        date="2025-09-13",
        time="10:00",
        customer_name="John"
    )

    if not check_availability(booking.date, booking.time):
        return {"status": "unavailable", "booking": booking.dict()}

    available_slots[booking.date].remove(booking.time)
    booking_id = str(uuid.uuid4())
    bookings[booking_id] = {"booking": booking.dict(), "status": "pending"}

    return {"status": "reserved", "booking_id": booking_id, "booking": booking.dict()}

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
