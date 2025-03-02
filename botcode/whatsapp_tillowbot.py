from flask import Flask, request
from twilio.rest import Client
import openai
import os
import redis
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
openai.api_key = os.getenv("OPENAI_API_KEY")
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)
AUTH_STEPS = ["last_4_digits", "dob", "last_name", "statement_period"]
CORRECT_AUTH_DETAILS = {
    "last_4_digits": "1234",
    "dob": "9.9.99",
    "last_name": None  
}
def detect_language(user_message):
    """Detect the user's language using GPT-4o."""
    prompt = f"""
    Identify the language of the following text:
    "{user_message}"
    Respond with only the language name (e.g., English, Spanish, French, Hindi).
    """
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )

    return response["choices"][0]["message"]["content"].strip()

def translate_text(text, target_language):
    """Translate text dynamically to the user's detected language using GPT-4o."""
    
    if target_language.lower() in ["english", "en"]:
        return text  

    prompt = f"""
    Translate the following text into {target_language}, keeping it natural and polite:
    "{text}"
    """

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )

    return response["choices"][0]["message"]["content"].strip()

def detect_intent(user_message):
    """Use GPT-4o to detect if the user is requesting a bank statement, greeting, or language change."""
    
    prompt = f"""
    The user sent the following message:
    "{user_message}"

    Classify the intent:
    - If the user is requesting a **bank statement**, reply only with "STATEMENT".
    - If the user is greeting (e.g., "Hi", "Hello", "Good morning"), reply only with "GREETING".
    - If the user is requesting to change the conversation language (e.g., "Can we continue in Hindi?"), reply only with "LANGUAGE_CHANGE".
    - If it's neither, reply only with "OTHER".
    """

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )

    return response["choices"][0]["message"]["content"].strip().upper()

def detect_requested_language(user_message):
    """Extract the language name from the user's message if they request a change."""
    
    prompt = f"""
    The user sent the following message requesting a language change:
    "{user_message}"

    Identify the new language they want to use. Respond with only the language name (e.g., Spanish, French, Telugu, English, Hindi).
    If the message is unclear, respond with "UNKNOWN".
    """

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )

    return response["choices"][0]["message"]["content"].strip()

def send_whatsapp_message(to, body):
    """Send a WhatsApp message via Twilio API."""
    message = client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        body=body,
        to=to
    )
    print(f"Message Sent! SID: {message.sid}")

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    """Handle incoming WhatsApp messages dynamically using OpenAI prompts."""
    user_message = request.form.get("Body").strip()
    sender_number = request.form.get("From")
    # Detect intent first
    user_intent = detect_intent(user_message)
    if user_intent == "GREETING":
        redis_client.delete(f"user:{sender_number}")  
        response_text = "Jhon(Hello)! Welcome to Hello Bank. How may I assist you today?"
        send_whatsapp_message(sender_number, response_text)
        return "Message Sent", 200
    # Fetch session details
    session_key = f"user:{sender_number}"
    auth_step = redis_client.hget(session_key, "auth_step")
    
    if user_intent == "LANGUAGE_CHANGE":
        new_language = detect_requested_language(user_message)
        if new_language == "UNKNOWN":
            response_text = "I'm sorry, I couldn't understand the language you requested. Can you please specify the language?"
        else:
            redis_client.hset(session_key, "language", new_language)
            
            if auth_step:
                previous_question = f"Could you please provide your {auth_step.replace('_', ' ')}?"
                translated_question = translate_text(previous_question, new_language)
                response_text = translate_text(f"Yes, we can continue in {new_language}. {translated_question}", new_language)
            else:
                response_text = translate_text(f"Yes, we can continue in {new_language}. How may I assist you?", new_language)
        send_whatsapp_message(sender_number, response_text)
        return "Message Sent", 200
    # If no session, check if user is requesting a statement
    if not auth_step:
        if user_intent == "STATEMENT":
            user_language = detect_language(user_message)
            redis_client.hset(session_key, "language", user_language)
            redis_client.hset(session_key, "auth_step", AUTH_STEPS[0])  
            response_text = translate_text("To proceed, please provide the last 4 digits of your account number.", user_language)
        else:
            user_language = detect_language(user_message)
            response_text = translate_text("I'm happy to assist you. Please specify your request.", user_language)

    else:
        user_language = redis_client.hget(session_key, "language")
        expected_value = CORRECT_AUTH_DETAILS.get(auth_step)
        if expected_value and user_message.strip() != expected_value:
            response_text = translate_text(f"I'm sorry, the details you provided are incorrect. Please try again. {auth_step.replace('_', ' ')}?", user_language)
        else:
            redis_client.hset(session_key, auth_step, user_message)
            auth_step_index = AUTH_STEPS.index(auth_step)
            next_step = AUTH_STEPS[auth_step_index + 1] if auth_step_index + 1 < len(AUTH_STEPS) else None
            if next_step:
                redis_client.hset(session_key, "auth_step", next_step)
                response_text = translate_text(f"Thank you. Now, could you please provide your {next_step.replace('_', ' ')}?", user_language)
            else:
                redis_client.delete(session_key)
                response_text = translate_text(f"To confirm, you are requesting a bank statement for {user_message}. We will send it to your registered email. Is there anything else I can help you with?", user_language)

    send_whatsapp_message(sender_number, response_text)
    return "Message Sent", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
