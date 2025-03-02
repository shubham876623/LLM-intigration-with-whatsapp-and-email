from flask import Flask, request
import openai
import os
import redis
import sendgrid
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv
import re

# Load environment variables
load_dotenv()

app = Flask(__name__)

# SendGrid API Configuration
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
sg = sendgrid.SendGridAPIClient(SENDGRID_API_KEY)

# OpenAI API Key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Redis client for session management
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

# Authentication steps
AUTH_STEPS = ["last_4_digits", "dob", "last_name", "statement_period"]

# Expected Authentication Values
CORRECT_AUTH_DETAILS = {
    "last_4_digits": "1234",
    "dob": "9.9.99",
    "last_name": None  # Any last name is allowed
}

# --- Helper Functions ---
def detect_language(user_message):
    """Detect the user's language using GPT-4o."""
    prompt = f'Identify the language of this text: "{user_message}". Respond with only the language name.'
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )
    return response["choices"][0]["message"]["content"].strip()

def translate_text(text, target_language):
    """Translate text dynamically using GPT-4o."""
    if target_language.lower() in ["english", "en"]:
        return text  # No translation needed
    prompt = f'Translate the following text into {target_language}: "{text}"'
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )
    return response["choices"][0]["message"]["content"].strip()

def detect_intent(user_message):
    """Detect user intent using GPT-4o with more structured output."""
    prompt = f"""
    Classify the intent of this message: "{user_message}"

    Return ONLY one of these:
    - "STATEMENT" → If the user requests a bank statement (e.g., "I need my bank statement", "Can you send my statement?", "Show my transaction history").
    - "GREETING" → If the user sends a greeting (e.g., "Hi", "Hello", "Good morning").
    - "LANGUAGE_CHANGE" → If the user asks to change language (e.g., "Can we continue in Hindi?").
    - "OTHER" → If it doesn't fit any of the above.

    Your response should be ONLY the category name (e.g., "STATEMENT") without any explanation.
    """

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )

    intent = response["choices"][0]["message"]["content"].strip().upper()
    print(f"📌 DEBUG: Detected Intent → {intent}")
    print(f"📜 DEBUG: User Message → {user_message}")

    return intent

def send_reply_email(to_email, original_subject, reply_body):
    """Send a reply email."""
    email = Mail(
        from_email=SENDER_EMAIL,
        to_emails=to_email,
        subject=f"Re: {original_subject}",
        plain_text_content=reply_body
    )
    response = sg.send(email)
    print(f"✅ Reply Sent! Status Code: {response.status_code}")

@app.route("/email_bot", methods=["POST"])
def email_bot():
    """Handle incoming emails dynamically and respond accordingly."""
    user_email = request.form.get("from")
    original_subject = request.form.get("subject")
    user_message = request.form.get("text") or request.form.get("html") or ""

    # Remove previous replies in email threads
    user_message = re.sub(r"(?i)On .*? wrote:.*", "", user_message, flags=re.DOTALL).strip()

    # Detect user intent
    user_intent = detect_intent(user_message)
    session_key = f"user:{user_email}"
    
    # ✅ Ensure auth_step is always initialized
    auth_step = redis_client.hget(session_key, "auth_step") or ""  # Use empty string if None
    user_language = redis_client.hget(session_key, "language") or detect_language(user_message)
    redis_client.hset(session_key, "language", user_language)

    print(f"📌 DEBUG: Detected Intent → {user_intent}")
    print(f"📜 DEBUG: User Message → {user_message}")
    print(f"🔑 DEBUG: Auth Step → {auth_step}")

    # ✅ Handle greeting - Reset session properly
    if user_intent == "GREETING":
        redis_client.delete(session_key)  # Fully reset the session
        redis_client.hset(session_key, "language", user_language)  # Restore language after reset
        response_text = translate_text("Hello! How may I assist you today?", user_language)
        send_reply_email(user_email, original_subject, response_text)
        return "Reply Sent", 200

    # ✅ Handle language change request properly
    if user_intent == "LANGUAGE_CHANGE":
        redis_client.hset(session_key, "language", user_language)
        response_text = translate_text(f"We will continue in {user_language}. How may I assist you?", user_language)
        send_reply_email(user_email, original_subject, response_text)
        return "Reply Sent", 200

    # ✅ Handle authentication process
    if not auth_step:  # If authentication hasn't started
        if user_intent == "STATEMENT":
            redis_client.hset(session_key, "auth_step", AUTH_STEPS[0])
            response_text = translate_text("Please provide the last 4 digits of your account number.", user_language)
        else:
            response_text = translate_text("I'm happy to assist you. Please specify your request.", user_language)
    else:  # Continue authentication
        expected_value = CORRECT_AUTH_DETAILS.get(auth_step)

        if expected_value and user_message.strip() != expected_value:
            response_text = translate_text(f"Incorrect. Please provide your {auth_step.replace('_', ' ')}.", user_language)
        else:
            redis_client.hset(session_key, auth_step, user_message)
            auth_step_index = AUTH_STEPS.index(auth_step)
            next_step = AUTH_STEPS[auth_step_index + 1] if auth_step_index + 1 < len(AUTH_STEPS) else None

            if next_step:
                redis_client.hset(session_key, "auth_step", next_step)
                response_text = translate_text(f"Thank you. Now, provide your {next_step.replace('_', ' ')}.", user_language)
            else:
                # ✅ Ensure session is cleared after successful authentication
                redis_client.delete(session_key)
                response_text = translate_text("Your bank statement will be sent. Anything else?", user_language)

    send_reply_email(user_email, original_subject, response_text)
    return "Reply Sent", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
