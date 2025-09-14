import os
import uuid
import json
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import requests
from openai import OpenAI
from sqlalchemy.orm import Session

from database import SessionLocal, Booking, Slot

# ------------------------------
# Load environment
# ------------------------------
load_dotenv()

KLARNA_USERNAME = os.getenv("KLARNA_USERNAME")
KLARNA_PASSWORD = os.getenv("KLARNA_PASSWORD")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://your-app.onrender.com")
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
# DB dependency
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
    history: list | None = None

class KlarnaPaymentRequest(BaseModel):
    amount: float
    service: str
    customer_name: str
    booking_id: str | None = None

# ------------------------------
# Helpers
# ------------------------------
def clean_expired_slots(db: Session):
    """Delete slots in the past."""
    now = datetime.now()
    for s in db.query(Slot).all():
        try:
            slot_dt = datetime.strptime(f"{s.date} {s.time}", "%Y-%m-%d %H:%M")
            if slot_dt <= now:
                db.delete(s)
        except Exception:
            continue
    db.commit()

def clean_stale_bookings(db: Session):
    """Delete pending bookings older than 10 minutes and free their slots."""
    now = datetime.utcnow()
    stale = db.query(Booking).filter_by(status="pending").all()
    for b in stale:
        try:
            created = datetime.fromisoformat(b.created_at)
        except Exception:
            # if somehow malformed, consider it stale
            created = now - timedelta(hours=1)
        if created + timedelta(minutes=10) < now:
            slot = db.query(Slot).filter_by(date=b.date, time=b.time).first()
            if slot:
                slot.available = True
            db.delete(b)
    db.commit()

# ------------------------------
# Pages
# ------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse("chat.html")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("dashboard.html")

# ------------------------------
# Chat endpoint with OpenAI
# ------------------------------
@app.post("/chat")
async def chat_with_agent(user_input: ChatMessage, db: Session = Depends(get_db)):
    try:
        clean_expired_slots(db)
        clean_stale_bookings(db)

        slots = db.query(Slot).filter_by(available=True).all()
        future_slots = [f"{s.date} {s.time}" for s in slots]
        slot_info = ", ".join(future_slots) if future_slots else "No slots available"

        messages = [
            {
                "role": "system",
                "content": f"""You are a polite barbershop assistant. 
Available slots are: {slot_info}.

Your task:
- Collect the customer's name
- Collect a valid available date (YYYY-MM-DD)
- Collect a valid available time (HH:MM)

❗Important rules:
- If the user already gave all three (name, date, time), IMMEDIATELY respond ONLY with JSON:
{{"service":"Haircut","date":"YYYY-MM-DD","time":"HH:MM","customer_name":"NAME"}}
- Do NOT ask again for details you already have.
- If something is missing, only ask for that missing part.
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
        reply = response.choices[0].message.content or ""

        # Try to extract booking JSON
        booking_match = re.search(r"\{.*?\}", reply, re.DOTALL)
        if booking_match:
            booking_data = json.loads(booking_match.group())

            slot_exists = db.query(Slot).filter_by(
                date=booking_data["date"],
                time=booking_data["time"],
                available=True
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
            # lock slot
            slot_exists.available = False
            db.commit()

            return {
                "status": "reserved",
                "reply": (
                    f"✅ Reserved! Booking ID: {booking_id} for {booking.customer_name} "
                    f"at {booking.time} on {booking.date}.<br><br>💳 Would you like to pay now?"
                ),
                "booking_id": booking_id
            }

        return {"status": "ok", "reply": reply}

    except Exception as e:
        return {"status": "error", "reply": f"⚠️ Error: {str(e)}"}

# ------------------------------
# Klarna Payment
# ------------------------------
@app.post("/pay/klarna")
async def pay_with_klarna(payment: KlarnaPaymentRequest):
    # Basic input validation
    if not KLARNA_USERNAME or not KLARNA_PASSWORD:
        raise HTTPException(status_code=500, detail="Klarna credentials are missing.")
    if payment.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0.")

    # Use booking_id so we can find the booking on Klarna push
    reference = payment.booking_id or str(uuid.uuid4())
    order_id = str(uuid.uuid4())  # our own id for merchant_urls
    total = int(payment.amount * 100)  # minor units (öre)

    payload = {
        "purchase_country": "SE",
        "purchase_currency": "SEK",
        "locale": "sv-SE",
        "order_amount": total,
        "order_tax_amount": 0,
        "order_lines": [
            {
                "type": "physical",          # must be a valid Klarna line type
                "reference": reference,      # <--- booking_id (or fallback)
                "name": payment.service or "Service",
                "quantity": 1,
                "unit_price": total,
                "total_amount": total,
                "total_tax_amount": 0,
                "tax_rate": 0
            }
        ],
        "merchant_urls": {
            # Use our own generated order_id in URLs (no {checkout.order.id} placeholders)
            "terms": f"{PUBLIC_URL}/terms",
            "checkout": f"{PUBLIC_URL}/checkout?klarna_order_id={order_id}",
            "confirmation": f"{PUBLIC_URL}/confirmation?klarna_order_id={order_id}",
            "push": f"{PUBLIC_URL}/klarna/push?klarna_order_id={order_id}",
        }
    }

    try:
        resp = requests.post(
            f"{KLARNA_API_URL}/checkout/v3/orders",
            auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=20
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Klarna request failed: {e}")

    # Forward Klarna error details if not 200
    if resp.status_code not in (200, 201):
            try:
                 detail = resp.json()
            except Exception:
                      detail = resp.text
            raise HTTPException(status_code=resp.status_code, detail=detail)

    klarna = resp.json()


    # Return what the frontend actually needs
    # - klarna['order_id'] is Klarna's own id (used by /checkout to fetch html_snippet)
    # - html_snippet can be used if you want to embed directly (we recommend redirect to /checkout)
    return {
        "order_id": klarna.get("order_id"),
        "html_snippet": klarna.get("html_snippet"),
        "reference": reference,     # booking_id echo
        "our_order_id": order_id    # the one we injected into merchant_urls
    }

# ------------------------------
# Klarna Push: mark booking paid
# ------------------------------
# ------------------------------
# Klarna Push: mark booking paid
# ------------------------------
from fastapi import Query

@app.api_route("/klarna/push", methods=["GET", "POST"])
async def klarna_push(
    klarna_order_id: str = Query(..., alias="klarna_order_id"),
    db: Session = Depends(get_db)
):
    """
    Klarna will call this after checkout is completed.
    It can be GET or POST depending on Klarna setup.
    """
    r = requests.get(
        f"{KLARNA_API_URL}/checkout/v3/orders/{klarna_order_id}",
        auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=20
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=r.text)

    order = r.json()
    status = order.get("status")
    lines = order.get("order_lines", [])
    ref = lines[0].get("reference") if lines else None

    if status in ["checkout_complete", "authorized"] and ref:
        booking = db.query(Booking).filter_by(id=ref).first()
        if booking:
            booking.status = "paid"
            db.commit()
            return {"updated": True, "booking_id": booking.id}

    return {"updated": False, "status": status}


# ------------------------------
# Klarna Confirmation Page
# ------------------------------
@app.get("/confirmation", response_class=HTMLResponse)
async def confirmation(klarna_order_id: str = "", db: Session = Depends(get_db)):
    booking_info = None
    status = "unknown"

    if klarna_order_id:
        r = requests.get(
            f"{KLARNA_API_URL}/checkout/v3/orders/{klarna_order_id}",
            auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
            headers={"Content-Type": "application/json"},
            timeout=20
        )
        if r.status_code in (200, 201):
            order = r.json()
            status = order.get("status")
            lines = order.get("order_lines", [])
            ref = lines[0].get("reference") if lines else None
            if ref:
                booking_info = db.query(Booking).filter_by(id=ref).first()

    if not booking_info:
        return HTMLResponse("<h1>⚠️ No booking found for this payment</h1>")

    return HTMLResponse(f"""
      <!DOCTYPE html>
      <html lang="en">
      <head>
        <meta charset="UTF-8" />
        <title>Payment Confirmation</title>
        <style>
          body {{
            font-family: Arial, sans-serif;
            background: #f9f9f9;
            padding: 30px;
            text-align: center;
          }}
          .status {{
            font-size: 1.5em;
            margin-top: 20px;
          }}
          .paid {{ color: #4caf50; font-weight: bold; }}
          .pending {{ color: #ff9800; font-weight: bold; }}
        </style>
      </head>
      <body>
        <h1>Booking Confirmation</h1>
        <p>Thank you, {booking_info.customer_name}!</p>
        <p>Your booking for <b>{booking_info.service}</b> is recorded:</p>
        <ul style="list-style:none; padding:0;">
          <li><b>Date:</b> {booking_info.date}</li>
          <li><b>Time:</b> {booking_info.time}</li>
          <li><b>Booking ID:</b> {booking_info.id}</li>
        </ul>
        <p class="status {booking_info.status}">Current Status: {booking_info.status} (Klarna: {status})</p>

        <script>
          async function checkStatus() {{
            try {{
              const res = await fetch("/api/bookings/{booking_info.id}");
              if (!res.ok) return;
              const booking = await res.json();
              const statusElem = document.querySelector(".status");
              statusElem.innerText = "Current Status: " + booking.status;
              statusElem.className = "status " + booking.status;

              // stop refreshing if paid or cancelled
              if (booking.status === "paid" || booking.status === "cancelled") {{
                clearInterval(polling);
              }}
            }} catch (err) {{
              console.error("Error checking booking status", err);
            }}
          }}
          const polling = setInterval(checkStatus, 5000);
        </script>
      </body>
      </html>
    """)

# Klarna required pages
# ------------------------------
@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return HTMLResponse("<h1>Terms & Conditions</h1><p>Sample terms page for Klarna.</p>")


@app.get("/checkout", response_class=HTMLResponse)
async def checkout(klarna_order_id: str, request: Request, db: Session = Depends(get_db)):
    response = requests.get(
        f"{KLARNA_API_URL}/checkout/v3/orders/{klarna_order_id}",
        auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=20
    )
    if response.status_code not in (200, 201):
        return HTMLResponse(f"<h1>Klarna error</h1><pre>{response.text}</pre>", status_code=500)

    order = response.json()
    snippet = order.get("html_snippet", "")
    lines = order.get("order_lines", []) or []
    booking_ref = lines[0].get("reference") if lines else ""

    # Only auto-fake-pay in Playground and when explicitly enabled
    is_playground = "playground" in (KLARNA_API_URL or "").lower()
    fake_auto_pay = os.getenv("FAKE_KLARNA_AUTO_PAY", "false").lower() == "true"
    inject_fake = is_playground and fake_auto_pay and booking_ref

    # NOTE: we try to check status via /api/bookings/{id} first (if you added that GET),
    # else we fall back to posting /paid directly.
    auto_pay_js = f"""
    <script>
      (function() {{
        const bookingId = "{booking_ref}";
        if (!bookingId) return;

        setTimeout(async () => {{
          try {{
            // Try to read current status (optional endpoint)
            let status = null;
            try {{
              const r = await fetch("/api/bookings/" + bookingId);
              if (r.ok) {{
                const b = await r.json();
                status = b.status;
              }}
            }} catch (_ignore) {{}}

            if (status && (status === "paid" || status === "cancelled")) {{
              console.log("No fake-pay needed. Current status:", status);
              return;
            }}

            // Mark as paid (fake) for Playground testing
            await fetch("/api/bookings/" + bookingId + "/paid", {{ method: "POST" }});
            console.log("💡 FAKE-PAID after 10s on Klarna checkout (Playground only).");
          }} catch (e) {{
            console.error("Fake auto-pay failed:", e);
          }}
        }}, 10000);
      }})();
    </script>
    """ if inject_fake else ""

    html = f"""<!doctype html>
    <html><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Klarna Checkout</title></head>
    <body>
      {snippet}
      {auto_pay_js}
      {"<p style='font:14px/1.4 sans-serif;color:#666;text-align:center;margin-top:16px'>Playground: will auto-confirm as Paid after 10s if still pending.</p>" if inject_fake else ""}
    </body></html>"""
    return HTMLResponse(html)


@app.post("/api/bookings/{booking_id}/paid")
async def mark_booking_paid(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = "paid"
    db.commit()
    return {"status": "paid", "booking_id": booking_id}

# ------------------------------
# Slots API
# ------------------------------
@app.get("/api/slots")
async def get_slots(db: Session = Depends(get_db)):
    clean_expired_slots(db)
    clean_stale_bookings(db)
    slots = db.query(Slot).filter_by(available=True).all()
    result = {}
    for s in slots:
        result.setdefault(s.date, []).append(s.time)
    return result

@app.post("/api/slots")
async def add_slot(slot: dict, db: Session = Depends(get_db)):
    new_slot = Slot(date=slot["date"], time=slot["time"], available=True)
    db.add(new_slot)
    db.commit()
    return {"status": "ok"}

@app.delete("/api/slots")
async def delete_slot(date: str, time: str, db: Session = Depends(get_db)):
    slot = db.query(Slot).filter_by(date=date, time=time).first()
    if slot:
        db.delete(slot)
        db.commit()
    return {"status": "deleted"}

# ------------------------------
# Bookings API
# ------------------------------
@app.get("/api/bookings/{booking_id}")
async def get_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {
        "id": booking.id,
        "customer_name": booking.customer_name,
        "service": booking.service,
        "date": booking.date,
        "time": booking.time,
        "status": booking.status
    }


@app.post("/api/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter_by(id=booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = "cancelled"
    # free the slot
    slot = db.query(Slot).filter_by(date=booking.date, time=booking.time).first()
    if slot:
        slot.available = True
    db.commit()
    return {"status": "cancelled", "booking_id": booking_id}
@app.get("/api/bookings")
async def get_bookings(db: Session = Depends(get_db)):
    clean_stale_bookings(db)
    bookings = db.query(Booking).all()
    result = []

    for b in bookings:
        klarna_status = None
        if getattr(b, "klarna_order_id", None):
            try:
                r = requests.get(
                    f"{KLARNA_API_URL}/checkout/v3/orders/{b.klarna_order_id}",
                    auth=(KLARNA_USERNAME, KLARNA_PASSWORD),
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )
                if r.status_code in (200, 201):
                    order = r.json()
                    klarna_status = order.get("status")
            except Exception:
                klarna_status = "unreachable"

        result.append({
            "id": b.id,
            "customer_name": b.customer_name,
            "service": b.service,
            "date": b.date,
            "time": b.time,
            "status": b.status,
            "klarna_status": klarna_status or "N/A"
        })

    return result