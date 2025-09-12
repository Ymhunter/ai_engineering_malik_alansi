from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests
import base64
import uuid
import openai
import os
import json
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import re

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

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
def check_availability(date: str, time: str):
    return time in available_slots.get(date, [])

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
        print("❌ Klarna error:", response.text)
        raise HTTPException(status_code=500, detail=response.text)

    return response.json()

# ------------------------------
# Conversation memory (simple, global for demo)
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

@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    global conversation_history

    # Save user message to conversation history
    conversation_history.append({"role": "user", "content": user_input.message})

    try:
        # Let GPT handle the conversation
        completion = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """
You are a friendly AI assistant for a barbershop. 
- Greet the customer politely.
- Answer small talk naturally (e.g. "how are you?").
- Help them book a haircut by collecting name, date (YYYY-MM-DD), and time (HH:MM).
- If details are missing, ask for them.
- When customer provides all details, clearly state the booking request in JSON format: 
  {"service": "Haircut", "customer_name": "...", "date": "YYYY-MM-DD", "time": "HH:MM"}
                """}
            ] + conversation_history,
            temperature=0.5
        )

        reply = completion.choices[0].message["content"]
        print("🤖 GPT Reply:", reply)

        # Save assistant reply
        conversation_history.append({"role": "assistant", "content": reply})

        # --- Try to detect a JSON booking confirmation in GPT's reply ---
        match = re.search(r"\{.*\}", reply, re.DOTALL)
        if match:
            try:
                booking = json.loads(match.group(0))
                date, time, name = booking["date"], booking["time"], booking["customer_name"]

                if not check_availability(date, time):
                    alternatives = available_slots.get(date, [])
                    if alternatives:
                        return {
                            "status": "unavailable",
                            "reply": f"❌ Sorry, {date} at {time} is not available. Available times: {', '.join(alternatives)}."
                        }
                    else:
                        return {
                            "status": "unavailable",
                            "reply": f"❌ Sorry, no slots available on {date}. Please choose another day."
                        }

                # Reserve booking
                available_slots[date].remove(time)
                booking_id = str(uuid.uuid4())
                bookings[booking_id] = {"booking": booking, "status": "pending"}

                return {
                    "status": "reserved",
                    "reply": f"✅ Reserved! Booking ID: {booking_id} for {name} at {time} on {date}."
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
