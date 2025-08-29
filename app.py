from dotenv import load_dotenv
load_dotenv()  # Add this at the top of your file

from flask import Flask, request, jsonify
import requests
import json
from datetime import datetime, timedelta
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import openai  # This will now work with version 0.28
import base64
import json



app = Flask(__name__)

# Replace these with your values from the "Getting Started" page


ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
PHONE_NUMBER_ID = os.environ.get('PHONE_NUMBER_ID')
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
openai.api_key = OPENAI_API_KEY




# Google Calendar setup - using base64 encoded service account key
SERVICE_ACCOUNT_BASE64 = os.environ.get('SERVICE_ACCOUNT_BASE64')
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Initialize calendar service
calendar_service = None
if SERVICE_ACCOUNT_BASE64:
    try:
        service_account_info = json.loads(base64.b64decode(SERVICE_ACCOUNT_BASE64).decode('utf-8'))
        credentials = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        calendar_service = build('calendar', 'v3', credentials=credentials)
        print("✅ Google Calendar service initialized successfully")
    except Exception as e:
        print(f"❌ Error initializing Google Calendar service: {e}")
        calendar_service = None
else:
    print("⚠️ SERVICE_ACCOUNT_BASE64 not set - Google Calendar functions will be limited")



SCOPES = ['https://www.googleapis.com/auth/calendar']
openai.api_key = OPENAI_API_KEY  # This is the syntax for openai==0.28

#Client-specific prompt - CUSTOMIZE THIS FOR THE LANGUAGE INSTRUCTOR
INSTRUCTOR_PROMPT = """
You are Elissa, a friendly and professional German language instructor.
You offer private lessons and group classes.

Key information about your business:
- Pricing: Free 30-minute trial lesson. $50 per hour for private lessons. $20 per person for group classes.
- Schedule: New beginner groups start on the first Monday of each month.
- Available hours: Monday-Friday 9 AM - 6 PM, Saturday 10 AM - 2 PM
- Your goal: To help students book a trial lesson and answer their questions.

Always be helpful, professional, and encourage users to book a trial lesson.
If someone wants to book an appointment, confirm with them and proceed with booking.
"""

# Simple in-memory storage for demo purposes
booking_requests = {}

def get_available_slots():
    """Get available time slots from Google Calendar for the next 7 days"""
    try:
        if not calendar_service:
            print("⚠️ Google Calendar service not available, using fallback slots")
            return generate_fallback_slots()
        
        # Get current time and time 7 days from now
        now = datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
        next_week = (datetime.utcnow() + timedelta(days=7)).isoformat() + 'Z'
        
        # Get busy times from calendar
        calendar_id = 'bilal.ahmed.q.777@gmail.com'
        freebusy_result = calendar_service.freebusy().query(
            body={
                "timeMin": now,
                "timeMax": next_week,
                "items": [{"id": calendar_id}]
            }
        ).execute()
        
        # Extract busy periods
        busy_periods = freebusy_result['calendars'][calendar_id].get('busy', [])
        
        # Define working hours (Monday-Friday 9-6, Saturday 10-2)
        available_slots = []
        current_date = datetime.utcnow()
        
        for day in range(7):  # Next 7 days
            check_date = current_date + timedelta(days=day)
            
            # Skip Sundays
            if check_date.weekday() == 6:  # Sunday
                continue
                
            # Set working hours based on day of week
            if check_date.weekday() == 5:  # Saturday
                start_hour, end_hour = 10, 14
            else:  # Monday-Friday
                start_hour, end_hour = 9, 18
            
            # Generate 30-minute slots within working hours
            for hour in range(start_hour, end_hour):
                for minute in [0, 30]:
                    slot_start = check_date.replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )
                    slot_end = slot_start + timedelta(minutes=30)
                    
                    # Check if this slot overlaps with any busy period
                    is_available = True
                    for busy in busy_periods:
                        busy_start = datetime.fromisoformat(busy['start'].replace('Z', '+00:00'))
                        busy_end = datetime.fromisoformat(busy['end'].replace('Z', '+00:00'))
                        
                        if not (slot_end <= busy_start or slot_start >= busy_end):
                            is_available = False
                            break
                    
                    if is_available and slot_start > datetime.utcnow():
                        available_slots.append(slot_start)
        
        return available_slots[:10]  # Return first 10 available slots
        
    except Exception as e:
        print(f"❌ Error getting available slots: {e}")
        # Fallback: generate some default slots if calendar access fails
        return generate_fallback_slots()
def generate_fallback_slots():
    """Generate fallback slots if calendar access fails"""
    fallback_slots = []
    current_date = datetime.utcnow()
    
    for day in range(1, 4):  # Next 3 days
        for hour in [10, 14, 16]:  # 10 AM, 2 PM, 4 PM
            slot = current_date + timedelta(days=day)
            slot = slot.replace(hour=hour, minute=0, second=0, microsecond=0)
            fallback_slots.append(slot)
    
    return fallback_slots

def get_ai_response(user_message, phone_number):
    """Get response from OpenAI GPT-3.5 using version 0.28 syntax"""
    try:
        # Check if user is in booking flow first
        if phone_number in booking_requests:
            if booking_requests[phone_number]['state'] == 'awaiting_date_selection':
                # User is selecting from available dates
                if 'option' in user_message.lower() or any(char.isdigit() for char in user_message):
                    try:
                        # Try to parse the selected option number
                        selected_option = int(''.join(filter(str.isdigit, user_message)))
                        available_slots = booking_requests[phone_number]['slots']
                        
                        if 1 <= selected_option <= len(available_slots):
                            selected_slot = available_slots[selected_option - 1]
                            booking_requests[phone_number] = {
                                'state': 'awaiting_confirmation',
                                'selected_slot': selected_slot
                            }
                            formatted_date = selected_slot.strftime("%A, %B %d at %I:%M %p")
                            return f"Great! You selected {formatted_date}. Should I confirm this appointment? Please reply 'yes' to confirm or 'no' to choose another time."
                        else:
                            return "Please select a valid option number from the list above."
                    except ValueError:
                        return "Please select an option by number (e.g., '1', '2', etc.)."
                
                elif 'no' in user_message.lower() or 'cancel' in user_message.lower():
                    del booking_requests[phone_number]
                    return "Okay, booking cancelled. Let me know if you'd like to schedule another time!"
                else:
                    return "Please select an available time slot by number, or say 'no' to cancel."
            
            elif booking_requests[phone_number]['state'] == 'awaiting_confirmation':
                if 'yes' in user_message.lower() or 'confirm' in user_message.lower() or 'sure' in user_message.lower():
                    # Handle booking confirmation
                    selected_slot = booking_requests[phone_number]['selected_slot']
                    event_id = create_calendar_event(phone_number, selected_slot)
                    
                    if event_id:
                        del booking_requests[phone_number]
                        formatted_date = selected_slot.strftime("%A, %B %d at %I:%M %p")
                        return f"✅ Appointment confirmed! I've scheduled your session for {formatted_date}. You'll receive a calendar invitation shortly. Looking forward to seeing you!"
                    else:
                        return "❌ Sorry, I couldn't create the calendar event. Please try again later."
                
                elif 'no' in user_message.lower() or 'cancel' in user_message.lower():
                    # Go back to date selection
                    available_slots = get_available_slots()
                    booking_requests[phone_number] = {
                        'state': 'awaiting_date_selection',
                        'slots': available_slots
                    }
                    return show_available_slots(available_slots)
                else:
                    return "Please reply 'yes' to confirm your booking or 'no' to choose another time."
            
            # If already in booking but not confirmation stage
            return "I'm here to help with your booking. What would you like to know?"
        
        # Check if USER wants to book (not the AI's response)
        booking_keywords = ['book', 'appointment', 'schedule', 'reserve', 'lesson', 'class', 'trial', 'meeting', 'available', 'time', 'date']
        user_wants_to_book = any(keyword in user_message.lower() for keyword in booking_keywords)
        
        # Use GPT-3.5 for all responses
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": INSTRUCTOR_PROMPT},
                {"role": "user", "content": user_message}
            ],
            max_tokens=150,
            temperature=0.7
        )
        
        ai_reply = response.choices[0].message['content'].strip()
        
        # Only initiate booking flow if USER asked for it and AI confirms
        if user_wants_to_book and ('book' in ai_reply.lower() or 'appointment' in ai_reply.lower() or 'available' in ai_reply.lower()):
            # Get available slots from calendar
            available_slots = get_available_slots()
            
            if available_slots:
                booking_requests[phone_number] = {
                    'state': 'awaiting_date_selection',
                    'slots': available_slots
                }
                # Show available slots to user
                slot_message = show_available_slots(available_slots)
                ai_reply += f"\n\n{slot_message}"
            else:
                ai_reply += "\n\nI'm currently fully booked for the next week. Please check back later for availability!"
            
        return ai_reply
        
    except Exception as e:
        print(f"❌ OpenAI Error: {e}")
        return "I'm having trouble connecting to my knowledge base. Please try again later."

def show_available_slots(available_slots):
    """Format available slots into a user-friendly message"""
    message = "Here are my available time slots for the next week:\n\n"
    
    for i, slot in enumerate(available_slots, 1):
        formatted_date = slot.strftime("%A, %B %d at %I:%M %p")
        message += f"{i}. {formatted_date}\n"
    
    message += "\nPlease reply with the number of your preferred time slot (e.g., '1', '2', etc.)."
    return message

def create_calendar_event(phone_number, event_time):
    """Actually create a Google Calendar event"""
    try:
        if not calendar_service:
            print("❌ Google Calendar service not available")
            return None
            
        start_time = event_time.isoformat()
        end_time = (event_time + timedelta(minutes=30)).isoformat()  # 30-minute lesson
        
        event = {
            'summary': f'German Lesson - Client ({phone_number})',
            'description': '30-minute German language trial lesson',
            'start': {'dateTime': start_time, 'timeZone': 'Europe/Berlin'},
            'end': {'dateTime': end_time, 'timeZone': 'Europe/Berlin'},
        }
        
        calendar_id = 'bilal.ahmed.q.777@gmail.com'
        
        event = calendar_service.events().insert(
            calendarId=calendar_id,
            body=event
        ).execute()
        
        print(f"✅ Google Calendar Event Created!")
        print(f"   - Calendar: {calendar_id}")
        print(f"   - Event ID: {event.get('id')}")
        print(f"   - Time: {event_time}")
        
        return event.get('id')
        
    except Exception as e:
        print(f"❌ Failed to create calendar event: {e}")
        return None


@app.get('/webhook')
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        print("✅ Webhook verified successfully!")
        return challenge, 200
    else:
        print("❌ Webhook verification failed!")
        return "Verification failed", 403

@app.post('/webhook')
def handle_webhook():
    data = request.get_json()
    print("\n=== RAW INCOMING JSON ===")
    print(json.dumps(data, indent=2))
    print("=======================\n")

    try:
        value = data['entry'][0]['changes'][0]['value']
        
        if 'messages' in value:
            message_data = value['messages'][0]
            phone_number = message_data['from']
            message_body = message_data['text']['body']
            
            print(f"📱 Message from {phone_number}: {message_body}")
            
            # Get AI response instead of if-else logic
            reply = get_ai_response(message_body, phone_number)
            print(f"🤖 AI Reply: {reply}")

            send_message(phone_number, reply)
            
        else:
            print("ℹ️ Received a webhook, but it wasn't a new message.")
            
    except KeyError as e:
        print(f"❌ KeyError: Could not find {e} in the JSON data.")
    except Exception as e:
        print(f"❌ An error occurred: {e}")

    return jsonify({"status": "ok"}), 200

def send_message(to, message):
    """Sends a message via the WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": { "body": message }
    }
    
    response = requests.post(url, headers=headers, json=data)
    print(f"📤 API Response Status: {response.status_code}")
    return response

if __name__ == '__main__':
    app.run(debug=True, port=5000)
