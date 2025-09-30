import os, base64, json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
from pytz import timezone

# Google APIs
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Gemini AI
import google.generativeai as genai

# ---------------- CONFIG ----------------
load_dotenv()
app = Flask(__name__)

# WhatsApp
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

# Instagram
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

# Shared
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SERVICE_ACCOUNT_BASE64 = os.getenv("SERVICE_ACCOUNT_BASE64")

# Configure Gemini - FIXED MODEL NAMES
genai.configure(api_key=GEMINI_API_KEY)

# Try different model names - Gemini 1.5 Flash might not be available in all regions
try:
    model = genai.GenerativeModel("gemini-1.5-flash")
    print("✅ Using model: gemini-1.5-flash")
except Exception as e:
    print(f"❌ gemini-1.5-flash not available: {e}")
    try:
        model = genai.GenerativeModel("gemini-1.0-pro")
        print("✅ Using model: gemini-1.0-pro")
    except Exception as e2:
        print(f"❌ gemini-1.0-pro not available: {e2}")
        try:
            model = genai.GenerativeModel("gemini-pro")
            print("✅ Using model: gemini-pro")
        except Exception as e3:
            print(f"❌ All Gemini models failed: {e3}")
            model = None

# Google Calendar
SCOPES = ["https://www.googleapis.com/auth/calendar"]
calendar_service = None
if SERVICE_ACCOUNT_BASE64:
    try:
        service_account_info = json.loads(
            base64.b64decode(SERVICE_ACCOUNT_BASE64).decode("utf-8")
        )
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=SCOPES
        )
        calendar_service = build("calendar", "v3", credentials=credentials)
        print("✅ Google Calendar initialized")
    except Exception as e:
        print(f"❌ Calendar init error: {e}")
else:
    print("⚠️ SERVICE_ACCOUNT_BASE64 not set, calendar disabled")

# ---------------- PROMPT ----------------
INSTRUCTOR_PROMPT = """
You are Expat Launch Assistant 🤖. 
You help expats moving to or living in Germany with:
- Visa & immigration (work visas, Blue Card, residence permits)
- Housing & relocation (apartments, Anmeldung, rental contracts)
- Career coaching (CV, LinkedIn, job search, interviews)
- Integration (German language resources, settling in)

Always be professional, supportive, and encourage booking an appointment.
If unsure, say: "I'm sorry, I can't answer that. Please contact Expat Launch at info@expatlaunch.de."
"""

# ---------------- MEMORY ----------------
booking_requests = {}

# ---------------- HELPERS ----------------
def get_available_slots():
    try:
        if not calendar_service:
            return generate_fallback_slots()

        # Use Berlin timezone
        berlin_tz = timezone('Europe/Berlin')
        now = datetime.now(berlin_tz)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        time_min = start_of_today.isoformat()
        time_max = (start_of_today + timedelta(days=7)).isoformat()
        calendar_id = "primary"

        freebusy_result = calendar_service.freebusy().query(
            body={
                "timeMin": time_min,
                "timeMax": time_max, 
                "items": [{"id": calendar_id}]
            }
        ).execute()

        busy_periods = freebusy_result["calendars"][calendar_id].get("busy", [])
        available_slots = []

        for day in range(7):
            check_date = start_of_today + timedelta(days=day)
            if check_date.weekday() == 6:  # Skip Sunday
                continue
                
            start_hour, end_hour = (10, 14) if check_date.weekday() == 5 else (9, 18)  # Saturday: 10-14, Weekdays: 9-18
            
            for hour in range(start_hour, end_hour):
                for minute in [0, 30]:
                    slot_start = check_date.replace(hour=hour, minute=minute)
                    slot_end = slot_start + timedelta(minutes=30)
                    
                    # Check if slot is available (not busy)
                    is_available = True
                    for busy in busy_periods:
                        busy_start = datetime.fromisoformat(busy["start"].replace('Z', '+00:00')).astimezone(berlin_tz)
                        busy_end = datetime.fromisoformat(busy["end"].replace('Z', '+00:00')).astimezone(berlin_tz)
                        
                        if not (slot_end <= busy_start or slot_start >= busy_end):
                            is_available = False
                            break
                    
                    if is_available and slot_start > now:
                        available_slots.append(slot_start)

        return available_slots[:10]  # Return max 10 slots
    except Exception as e:
        print(f"❌ Error getting slots: {e}")
        return generate_fallback_slots()

def generate_fallback_slots():
    slots = []
    berlin_tz = timezone('Europe/Berlin')
    now = datetime.now(berlin_tz)
    
    for d in range(1, 4):
        for h in [10, 14, 16]:
            slot = now + timedelta(days=d)
            slot = slot.replace(hour=h, minute=0, second=0, microsecond=0)
            slots.append(slot)
    return slots

def show_available_slots(slots):
    msg = "Here are my available slots:\n\n"
    for i, s in enumerate(slots, 1):
        msg += f"{i}. {s.strftime('%A, %B %d at %H:%M')}\n"
    return msg + "\nReply with the number of your choice."

def create_calendar_event(user_id, event_time):
    try:
        if not calendar_service:
            return None
            
        berlin_tz = timezone('Europe/Berlin')
        start = event_time
        end = event_time + timedelta(minutes=30)
        
        event = {
            "summary": f"Expat Launch Consultation - {user_id}",
            "description": f"Consultation session with {user_id}",
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": "Europe/Berlin",
            },
            "end": {
                "dateTime": end.isoformat(), 
                "timeZone": "Europe/Berlin",
            },
        }
        
        created_event = calendar_service.events().insert(
            calendarId="primary", 
            body=event
        ).execute()
        
        print("✅ Event created:", created_event.get("htmlLink"))
        return created_event.get("id")
    except Exception as e:
        print(f"❌ Event creation error: {e}")
        return None

def get_ai_response(user_message, user_id):
    try:
        # Ongoing booking flow
        if user_id in booking_requests:
            state = booking_requests[user_id]["state"]
            if state == "awaiting_date_selection":
                try:
                    # Extract number from message
                    choice_str = ''.join(filter(str.isdigit, user_message))
                    if choice_str:
                        choice = int(choice_str)
                        slots = booking_requests[user_id]["slots"]
                        if 1 <= choice <= len(slots):
                            selected_slot = slots[choice-1]
                            booking_requests[user_id] = {
                                "state": "awaiting_confirmation", 
                                "slot": selected_slot
                            }
                            return f"You chose: {selected_slot.strftime('%A, %B %d at %H:%M')}\n\nReply 'yes' to confirm or 'no' to cancel."
                        else:
                            return f"Please choose a number between 1 and {len(slots)}."
                    else:
                        return "Please reply with the number of your preferred slot."
                except ValueError:
                    return "Please reply with the number of your preferred slot."
                    
            elif state == "awaiting_confirmation":
                if user_message.lower().strip() in ["yes", "ja", "y", "confirm"]:
                    slot = booking_requests[user_id]["slot"]
                    event_id = create_calendar_event(user_id, slot)
                    del booking_requests[user_id]
                    if event_id:
                        return f"✅ Appointment confirmed for {slot.strftime('%A, %B %d at %H:%M')}!\n\nWe're looking forward to helping you with your expat journey! 🎉"
                    else:
                        return "❌ Failed to create calendar event. Please contact us directly at info@expatlaunch.de"
                elif user_message.lower().strip() in ["no", "nein", "n", "cancel"]:
                    del booking_requests[user_id]
                    return "❌ Booking cancelled. Let me know if you'd like to see available slots again."
                else:
                    return "Please reply 'yes' to confirm or 'no' to cancel."

        # If Gemini model is not available, use fallback responses
        if model is None:
            return get_fallback_response(user_message, user_id)

        # Normal AI reply with Gemini
        try:
            response = model.generate_content(f"{INSTRUCTOR_PROMPT}\n\nUser: {user_message}")
            ai_reply = response.text if response and response.text else "Hi 👋 I'm here to help with your expat journey in Germany!"
        except Exception as ai_error:
            print(f"❌ Gemini API error: {ai_error}")
            return get_fallback_response(user_message, user_id)

        # Detect booking intent
        booking_keywords = ["book", "appointment", "schedule", "lesson", "termin", "meeting", "consultation"]
        if any(k in user_message.lower() for k in booking_keywords):
            slots = get_available_slots()
            if slots:
                booking_requests[user_id] = {
                    "state": "awaiting_date_selection", 
                    "slots": slots
                }
                ai_reply += f"\n\n{show_available_slots(slots)}"
            else:
                ai_reply += "\n\n❌ Sorry, no available slots found. Please contact us directly at info@expatlaunch.de"

        return ai_reply
    except Exception as e:
        print(f"❌ AI error: {e}")
        return "Sorry, I'm facing an issue. Please try again later or contact us at info@expatlaunch.de."

def get_fallback_response(user_message, user_id):
    """Fallback responses when Gemini is not available"""
    user_message_lower = user_message.lower()
    
    # Greetings
    if any(word in user_message_lower for word in ["hello", "hi", "hey", "hallo"]):
        return "Hello! 👋 I'm Expat Launch Assistant. How can I help you with your expat journey in Germany today?"
    
    # Booking intent
    elif any(word in user_message_lower for word in ["book", "appointment", "schedule", "termin", "meeting"]):
        slots = get_available_slots()
        if slots:
            booking_requests[user_id] = {"state": "awaiting_date_selection", "slots": slots}
            return f"I'd be happy to schedule a consultation! {show_available_slots(slots)}"
        else:
            return "I'd be happy to schedule a consultation! Please contact us at info@expatlaunch.de for available slots."
    
    # Common questions
    elif any(word in user_message_lower for word in ["visa", "immigration", "blue card"]):
        return "For visa and immigration matters, I can help with:\n• Work visas\n• Blue Card EU\n• Residence permits\n• Family reunification\n\nWould you like to book a consultation for personalized advice?"
    
    elif any(word in user_message_lower for word in ["housing", "apartment", "anmeldung", "rent"]):
        return "For housing in Germany, I can assist with:\n• Finding apartments\n• Understanding rental contracts\n• Anmeldung (registration)\n• Deposit and utilities\n\nWould you like to discuss your specific situation?"
    
    elif any(word in user_message_lower for word in ["job", "career", "cv", "resume", "interview"]):
        return "For career coaching, I offer help with:\n• German-style CV/Lebenslauf\n• LinkedIn optimization\n• Job search strategies\n• Interview preparation\n\nWould you like to book a career coaching session?"
    
    elif any(word in user_message_lower for word in ["german", "language", "integration"]):
        return "For integration support, I can provide:\n• German language learning resources\n• Cultural integration tips\n• Finding language courses\n• Community connections\n\nHow can I specifically help with your integration?"
    
    # Default response
    else:
        return "Thanks for your message! I specialize in helping expats with:\n\n• Visa & immigration\n• Housing & relocation\n• Career coaching\n• Integration support\n\nPlease contact info@expatlaunch.de for detailed assistance, or let me know if you'd like to book a consultation!"

# ---------------- SEND MESSAGES ----------------
def send_whatsapp_message(to, message):
    try:
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}", 
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp", 
            "to": to, 
            "type": "text", 
            "text": {"body": message}
        }
        response = requests.post(url, headers=headers, json=data)
        print(f"📤 WhatsApp response: {response.status_code}")
        if response.status_code != 200:
            print(f"❌ WhatsApp error: {response.text}")
        return response
    except Exception as e:
        print(f"❌ WhatsApp send error: {e}")
        return None

def send_instagram_message(recipient_id, message):
    try:
        url = "https://graph.facebook.com/v18.0/me/messages"
        headers = {
            "Authorization": f"Bearer {PAGE_ACCESS_TOKEN}", 
            "Content-Type": "application/json"
        }
        data = {
            "recipient": {"id": recipient_id}, 
            "message": {"text": message}
        }
        response = requests.post(url, headers=headers, json=data)
        print(f"📤 Instagram response: {response.status_code}")
        return response
    except Exception as e:
        print(f"❌ Instagram send error: {e}")
        return None

# ---------------- ROUTES ----------------
@app.route("/")
def home():
    return jsonify({
        "status": "running", 
        "whatsapp": bool(ACCESS_TOKEN), 
        "instagram": bool(PAGE_ACCESS_TOKEN),
        "calendar": bool(calendar_service),
        "gemini_model": "available" if model else "unavailable"
    })

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified")
        return challenge, 200
    print("❌ Webhook verification failed")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.get_json()
    print("📩 Incoming webhook data")
    
    try:
        if not data:
            return jsonify({"status": "no data"}), 200

        entries = data.get("entry", [])
        if not entries:
            return jsonify({"status": "no entries"}), 200

        for entry in entries:
            # Handle WhatsApp
            if "changes" in entry:
                for change in entry["changes"]:
                    value = change.get("value", {})
                    if "messages" in value:
                        for msg in value["messages"]:
                            if msg.get("type") == "text":
                                phone = msg["from"]
                                text = msg["text"]["body"]
                                print(f"📱 WhatsApp message from {phone}: {text}")
                                reply = get_ai_response(text, phone)
                                send_whatsapp_message(phone, reply)

            # Handle Instagram
            elif "messaging" in entry:
                for messaging in entry["messaging"]:
                    if "message" in messaging and "text" in messaging["message"]:
                        sender_id = messaging["sender"]["id"]
                        text = messaging["message"]["text"]
                        print(f"📷 Instagram message from {sender_id}: {text}")
                        reply = get_ai_response(text, sender_id)
                        send_instagram_message(sender_id, reply)

    except Exception as e:
        print(f"❌ Webhook processing error: {e}")
        import traceback
        traceback.print_exc()
        
    return jsonify({"status": "ok"})

# ---------------- RUN ----------------
if __name__ == "__main__":
    print("🚀 Starting Expat Launch Assistant...")
    print(f"📱 WhatsApp: {'✅' if ACCESS_TOKEN else '❌'}")
    print(f"📷 Instagram: {'✅' if PAGE_ACCESS_TOKEN else '❌'}")
    print(f"📅 Calendar: {'✅' if calendar_service else '❌'}")
    print(f"🤖 Gemini AI: {'✅' if model else '❌'}")
    
    app.run(debug=True, host="0.0.0.0", port=5000)