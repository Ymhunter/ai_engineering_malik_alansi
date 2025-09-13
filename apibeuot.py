from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
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

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
            "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id=XYZ",
            "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id=XYZ",
            "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id=XYZ"
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
- If the requested slot is not available, suggest available times for that date or other dates.
- Do NOT invent restrictions like "current week only".
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

# ------------------------------
# Conversation memory
# ------------------------------
conversation_history = []

# ------------------------------
# API Endpoints
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

@app.post("/api/bookings/{booking_id}/confirm")
async def confirm_booking(booking_id: str):
    if booking_id not in bookings:
        raise HTTPException(status_code=404, detail="Booking not found")
    bookings[booking_id]["status"] = "confirmed"
    return {"status": "confirmed", "booking": bookings[booking_id]}

@app.delete("/api/bookings/{booking_id}")
async def cancel_booking(booking_id: str):
    if booking_id not in bookings:
        raise HTTPException(status_code=404, detail="Booking not found")
    # free the slot again
    b = bookings[booking_id]["booking"]
    if b["date"] in available_slots:
        available_slots[b["date"]].append(b["time"])
    else:
        available_slots[b["date"]] = [b["time"]]
    del bookings[booking_id]
    return {"status": "cancelled", "id": booking_id}

@app.get("/api/slots")
async def get_slots():
    return available_slots

@app.post("/api/slots")
async def add_slot(data: dict):
    date_str = data.get("date")
    time = data.get("time")
    if not date_str or not time:
        raise HTTPException(status_code=400, detail="date and time required")
    if date_str not in available_slots:
        available_slots[date_str] = []
    if time not in available_slots[date_str]:
        available_slots[date_str].append(time)
    return {"status": "added", "slots": available_slots}

@app.delete("/api/slots")
async def remove_slot(data: dict):
    date_str = data.get("date")
    time = data.get("time")
    if not date_str or not time:
        raise HTTPException(status_code=400, detail="date and time required")
    if date_str in available_slots and time in available_slots[date_str]:
        available_slots[date_str].remove(time)
    return {"status": "removed", "slots": available_slots}

@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    global conversation_history
    conversation_history.append({"role": "user", "content": user_input.message})

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=build_messages(user_input.message, conversation_history),
            temperature=0.2
        )

        reply = completion.choices[0].message.content
        conversation_history.append({"role": "assistant", "content": reply})

        match = re.search(r"\{.*\}", reply, re.DOTALL)
        if match:
            try:
                booking = json.loads(match.group(0))
                date_str, time, name = booking["date"], booking["time"], booking["customer_name"]

                if not check_availability(date_str, time):
                    alternatives = available_slots.get(date_str, [])
                    if alternatives:
                        return {
                            "status": "unavailable",
                            "reply": f"❌ Sorry, {date_str} at {time} is not available. Available times: {', '.join(alternatives)}."
                        }
                    else:
                        return {
                            "status": "unavailable",
                            "reply": f"❌ Sorry, no slots available on {date_str}. Please choose another day."
                        }

                # Reserve booking
                available_slots[date_str].remove(time)
                booking_id = str(uuid.uuid4())
                bookings[booking_id] = {"booking": booking, "status": "pending"}

                return {
                    "status": "reserved",
                    "reply": f"✅ Reserved! Booking ID: {booking_id} for {name} at {time} on {date_str}."
                }
            except Exception as e:
                print("❌ Failed to parse booking JSON:", e)

        return {"status": "chat", "reply": reply}

    except Exception as e:
        print("❌ GPT error:", e)
        return {"status": "error", "reply": "⚠️ Sorry, I had trouble responding. Please try again."}

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
