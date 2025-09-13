from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
import requests
import base64
import uuid
import os
import json
import re
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from datetime import date

# ------------------------------
# Load environment variables
# ------------------------------
load_dotenv()

KLARNA_USERNAME = os.getenv("KLARNA_USERNAME")
KLARNA_PASSWORD = os.getenv("KLARNA_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://ai-engineering-malik-alansi-1.onrender.com")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY in environment")
if not KLARNA_USERNAME or not KLARNA_PASSWORD:
    raise RuntimeError("❌ Missing KLARNA_USERNAME or KLARNA_PASSWORD in environment")

# ------------------------------
# Config
# ------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)
KLARNA_API_URL = "https://api.playground.klarna.com"

# ------------------------------
# FastAPI app
# ------------------------------
app = FastAPI(title="Barbershop Booking AI Agent with Klarna")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------
# Models
# ------------------------------
class ChatMessage(BaseModel):
    message: str
    customer_name: str | None = None
    service: str | None = None

class KlarnaPaymentRequest(BaseModel):
    amount: float
    service: str
    customer_name: str

class SlotRequest(BaseModel):
    date: str
    time: str

# ------------------------------
# Mock DB
# ------------------------------
available_slots = {
    "2025-09-13": ["10:00", "11:00", "14:00"],
    "2025-09-14": ["09:00", "12:00", "15:00"]
}
bookings = {}       # booking_id → booking details
klarna_orders = {}  # klarna_order_id → html_snippet

# ------------------------------
# Helpers
# ------------------------------
def check_availability(date_str: str, time: str):
    return time in available_slots.get(date_str, [])

def create_klarna_order(amount: float, service: str, customer_name: str):
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
        "order_amount": int(amount * 100),
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

    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)

    return response.json()

def build_messages(user_text: str, conversation_history):
    slots_text = json.dumps(available_slots, indent=2, ensure_ascii=False)
    today_str = date.today().isoformat()

    system_prompt = f"""
You are a friendly booking assistant for a barbershop.

RULES:
- Use ONLY these available slots when confirming a booking:
{slots_text}
- If the requested slot is not available, suggest available times.
- If all details are provided (customer_name, date YYYY-MM-DD, time HH:MM, service),
  output a SINGLE LINE of JSON ONLY:
  {{"service": "Haircut", "customer_name": "...", "date": "YYYY-MM-DD", "time": "HH:MM"}}
- If details are missing, ask a simple follow-up question.
- Today’s date: {today_str}
"""

    return [
        {"role": "system", "content": system_prompt},
        *conversation_history,
        {"role": "user", "content": user_text},
    ]

conversation_history = []

# ------------------------------
# Endpoints
# ------------------------------
@app.get("/")
async def root():
    return {"status": "ok", "message": "Barbershop Booking AI Agent is running 🚀"}

@app.get("/chatbot")
async def chatbot_ui():
    return FileResponse("chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_ui():
    return FileResponse("dashboard.html")

@app.get("/api/bookings")
async def get_bookings():
    return bookings

@app.get("/api/slots")
async def get_slots():
    return available_slots

@app.post("/api/slots")
async def add_slot(slot: SlotRequest):
    if slot.date not in available_slots:
        available_slots[slot.date] = []
    if slot.time not in available_slots[slot.date]:
        available_slots[slot.date].append(slot.time)
    return {"status": "ok", "slots": available_slots}

@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    user_message = user_input.message
    customer_name = user_input.customer_name
    service = user_input.service

    context_note = ""
    if customer_name:
        context_note += f"\nCustomer name: {customer_name}"
    if service:
        context_note += f"\nService requested: {service}"

    messages = build_messages(user_message + context_note, conversation_history)

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=200
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        return {"reply": f"⚠️ Error contacting AI: {str(e)}"}

    conversation_history.append({"role": "user", "content": user_message})
    conversation_history.append({"role": "assistant", "content": reply})

    booking_match = re.search(r"\{.*\}", reply)
    if booking_match:
        try:
            booking_data = json.loads(booking_match.group())
            date_str = booking_data["date"]
            time = booking_data["time"]

            if not check_availability(date_str, time):
                return {"status": "unavailable", "reply": "❌ Sorry, that slot is not available."}

            available_slots[date_str].remove(time)
            booking_id = str(uuid.uuid4())
            bookings[booking_id] = {"booking": booking_data, "status": "pending"}
            return {
                "status": "reserved",
                "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking_data['customer_name']} at {time} on {date_str}.<br><br>💳 Would you like to pay now?",
                "booking_id": booking_id
            }
        except Exception:
            pass

    return {"reply": reply}

@app.post("/pay/klarna")
async def pay_with_klarna(payment: KlarnaPaymentRequest):
    order = create_klarna_order(payment.amount, payment.service, payment.customer_name)
    order_id = order.get("order_id")
    snippet = order.get("html_snippet")

    # ✅ fallback to hosted checkout if snippet fails
    if snippet and "klarna-unsupported-page" not in snippet:
        klarna_orders[order_id] = snippet
        checkout_url = f"{PUBLIC_URL}/checkout?klarna_order_id={order_id}"
    else:
        checkout_url = f"https://api.playground.klarna.com/checkout/orders/{order_id}"

    return {
        "status": "klarna_order_created",
        "order_id": order_id,
        "redirect_url": checkout_url
    }

@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(klarna_order_id: str):
    snippet = klarna_orders.get(klarna_order_id)
    if not snippet:
        return HTMLResponse("<h1>⚠️ Klarna checkout not found for this order</h1>", status_code=404)

    return f"""
    <html>
      <head><title>Klarna Checkout</title></head>
      <body>{snippet}</body>
    </html>
    """

@app.get("/confirmation")
async def confirmation_page(klarna_order_id: str):
    for booking_id, info in bookings.items():
        if info["status"] == "pending":
            info["status"] = "paid"
    redirect_url = f"/chatbot?payment=success&order_id={klarna_order_id}"
    return RedirectResponse(url=redirect_url)

@app.post("/klarna/push")
async def klarna_push(request: Request):
    klarna_order_id = request.query_params.get("klarna_order_id")
    body = await request.json()
    print(f"💳 Klarna push received for {klarna_order_id}: {body}")
    return {"status": "received", "order_id": klarna_order_id}
