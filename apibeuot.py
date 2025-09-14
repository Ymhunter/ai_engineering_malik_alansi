from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
import requests, base64, uuid, os, re, json
from datetime import date
from dotenv import load_dotenv
from database import SessionLocal, Booking, Slot

# Load env
load_dotenv()

KLARNA_USERNAME = os.getenv("KLARNA_USERNAME")
KLARNA_PASSWORD = os.getenv("KLARNA_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://ai-engineering-malik-alansi-1.onrender.com")

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY")
if not KLARNA_USERNAME or not KLARNA_PASSWORD:
    raise RuntimeError("❌ Missing Klarna credentials")

client = OpenAI(api_key=OPENAI_API_KEY)
KLARNA_API_URL = "https://api.playground.klarna.com"

app = FastAPI(title="Barbershop Booking AI Agent with Klarna")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
# Helpers
# ------------------------------
def check_availability(db, date_str: str, time: str):
    slot = db.query(Slot).filter_by(date=date_str, time=time, available=True).first()
    return slot is not None

def create_klarna_order(amount: float, service: str, customer_name: str):
    url = f"{KLARNA_API_URL}/checkout/v3/orders"
    auth = base64.b64encode(f"{KLARNA_USERNAME}:{KLARNA_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}

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
                "tax_rate": 0,
            }
        ],
        "merchant_urls": {
            "terms": f"{PUBLIC_URL}/terms",
            "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id={order_id}",
            "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id={order_id}",
            "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id={order_id}",
        },
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)
    return response.json()

def build_messages(user_text: str, conversation_history):
    today_str = date.today().isoformat()
    system_prompt = f"""
You are a booking assistant for a barbershop.
RULES:
- If user provides all details (service, name, date YYYY-MM-DD, time HH:MM),
  output a SINGLE JSON ONLY: {{"service":"Haircut","customer_name":"...","date":"YYYY-MM-DD","time":"HH:MM"}}
- If details missing, ask a short question.
- Today’s date: {today_str}
"""
    return [
        {"role": "system", "content": system_prompt},
        *conversation_history,
        {"role": "user", "content": user_text},
    ]

conversation_history = []

klarna_orders = {}

# ------------------------------
# Routes
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
    db = SessionLocal()
    bookings = db.query(Booking).all()
    result = [b.__dict__ for b in bookings]
    db.close()
    for r in result: r.pop("_sa_instance_state", None)
    return result

@app.get("/api/slots")
async def get_slots():
    db = SessionLocal()
    slots = db.query(Slot).filter_by(available=True).all()
    result = {}
    for s in slots:
        result.setdefault(s.date, []).append(s.time)
    db.close()
    return result

@app.post("/api/slots")
async def add_slot(slot: SlotRequest):
    db = SessionLocal()
    new_slot = Slot(date=slot.date, time=slot.time, available=True)
    db.add(new_slot)
    db.commit()
    db.close()
    return {"status": "ok"}

@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage):
    user_message = user_input.message
    db = SessionLocal()

    messages = build_messages(user_message, conversation_history)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, max_tokens=200
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
            if not check_availability(db, booking_data["date"], booking_data["time"]):
                return {"status": "unavailable", "reply": "❌ Sorry, that slot is not available."}

            # Reserve slot
            slot = db.query(Slot).filter_by(date=booking_data["date"], time=booking_data["time"]).first()
            if slot:
                slot.available = False
                db.add(slot)

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
                "reply": f"✅ Reserved! Booking ID: {booking_id} for {booking_data['customer_name']} at {booking_data['time']} on {booking_data['date']}.<br><br>💳 Would you like to pay now?",
                "booking_id": booking_id,
            }
        except Exception:
            pass

    db.close()
    return {"reply": reply}

@app.post("/pay/klarna")
async def pay_with_klarna(payment: KlarnaPaymentRequest):
    order = create_klarna_order(payment.amount, payment.service, payment.customer_name)
    order_id = order.get("order_id")
    snippet = order.get("html_snippet")

    if snippet and "klarna-unsupported-page" not in snippet:
        klarna_orders[order_id] = snippet
        checkout_url = f"{PUBLIC_URL}/checkout?klarna_order_id={order_id}"
    else:
        checkout_url = f"https://api.playground.klarna.com/checkout/orders/{order_id}"

    return {"status": "klarna_order_created", "order_id": order_id, "redirect_url": checkout_url}

@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(klarna_order_id: str):
    snippet = klarna_orders.get(klarna_order_id)
    if not snippet:
        return HTMLResponse("<h1>⚠️ Klarna checkout not found</h1>", status_code=404)
    return f"<html><body>{snippet}</body></html>"

@app.get("/confirmation")
async def confirmation_page(klarna_order_id: str):
    db = SessionLocal()
    for b in db.query(Booking).filter_by(status="pending").all():
        b.status = "paid"
        db.add(b)
    db.commit()
    db.close()
    return RedirectResponse(url=f"/chatbot?payment=success&order_id={klarna_order_id}")

@app.post("/klarna/push")
async def klarna_push(request: Request):
    klarna_order_id = request.query_params.get("klarna_order_id")
    body = await request.json()
    print(f"💳 Klarna push for {klarna_order_id}: {body}")
    return {"status": "received", "order_id": klarna_order_id}
