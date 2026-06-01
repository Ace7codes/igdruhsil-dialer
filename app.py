"""Visark cold-call dialer — WebRTC softphone backend.

Routes:
  GET  /                      -> the single-page softphone UI
  GET  /api/token             -> mint a Twilio Voice access token (JWT) for the browser
  POST /api/twiml/outbound    -> TwiML: bridge browser -> prospect  (Twilio webhook)
  POST /api/twiml/inbound     -> TwiML: ring the browser client     (Twilio webhook)
  POST /api/call-status       -> Twilio call status callbacks        (Twilio webhook)
  GET  /api/calls             -> recent-call log for the UI to poll

Basic auth is enforced at the nginx edge (see deploy/), NOT here. The three
Twilio webhooks are excluded from basic auth and instead validated via
X-Twilio-Signature. ProxyFix makes request.url resolve to the public
https://dialer.igdruhsil.com/... URL that Twilio actually signed.
"""
import logging
import os
import re
from collections import deque
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
API_KEY = os.environ["TWILIO_API_KEY"]
API_SECRET = os.environ["TWILIO_API_SECRET"]
TWIML_APP_SID = os.environ["TWILIO_TWIML_APP_SID"]
PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
CLIENT_IDENTITY = os.environ.get("TWILIO_CLIENT_IDENTITY", "bolaji")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TOKEN_TTL = int(os.environ.get("TOKEN_TTL_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "3001"))

# Set TWILIO_VALIDATE=false for local curl testing (no real Twilio signature).
VALIDATE = os.environ.get("TWILIO_VALIDATE", "true").lower() != "false"

app = Flask(__name__, static_folder="static", static_url_path="/static")
# Trust nginx's X-Forwarded-Proto/Host so request.url is the public https URL.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

_validator = RequestValidator(AUTH_TOKEN)
# Most-recent-first ring buffers for the UI.
_calls = deque(maxlen=20)
_messages = deque(maxlen=50)


def normalize_e164(raw):
    """Strip formatting (spaces, dashes, parens) but keep a leading '+'."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    return "+" + digits if raw.startswith("+") else digits


def twilio_webhook(f):
    """Reject any request to a Twilio webhook that isn't signed by Twilio."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if VALIDATE:
            signature = request.headers.get("X-Twilio-Signature", "")
            if not _validator.validate(request.url, request.form, signature):
                abort(403)
        return f(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/token")
def token():
    """Mint a short-lived Voice access token for the browser Device."""
    grant = VoiceGrant(
        outgoing_application_sid=TWIML_APP_SID,
        incoming_allow=True,  # let this identity receive inbound calls
    )
    access = AccessToken(
        ACCOUNT_SID, API_KEY, API_SECRET,
        identity=CLIENT_IDENTITY, ttl=TOKEN_TTL,
    )
    access.add_grant(grant)
    return jsonify(token=access.to_jwt(), identity=CLIENT_IDENTITY, ttl=TOKEN_TTL)


@app.route("/api/twiml/outbound", methods=["POST"])
@twilio_webhook
def twiml_outbound():
    """Browser placed a call -> bridge to the dialed PSTN number."""
    to = normalize_e164(request.form.get("To"))
    resp = VoiceResponse()
    if not to:
        resp.say("No destination number was provided.")
        return str(resp), 200, {"Content-Type": "text/xml"}
    dial = Dial(caller_id=PHONE_NUMBER, answer_on_bridge=True)
    status_cb = f"{PUBLIC_BASE_URL}/api/call-status" if PUBLIC_BASE_URL else None
    dial.number(
        to,
        status_callback=status_cb,
        status_callback_event="initiated ringing answered completed",
        status_callback_method="POST",
    )
    resp.append(dial)
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.route("/api/twiml/inbound", methods=["POST"])
@twilio_webhook
def twiml_inbound():
    """Inbound PSTN call to our number -> ring the browser client."""
    resp = VoiceResponse()
    dial = Dial(answer_on_bridge=True)
    dial.client(CLIENT_IDENTITY)
    resp.append(dial)
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.route("/api/call-status", methods=["POST"])
@twilio_webhook
def call_status():
    """Twilio status callback -> log + keep for the UI."""
    event = {
        "call_sid": request.form.get("CallSid"),
        "status": request.form.get("CallStatus"),
        "from": request.form.get("From"),
        "to": request.form.get("To"),
        "direction": request.form.get("Direction"),
        "duration": request.form.get("CallDuration"),
        "timestamp": request.form.get("Timestamp"),
    }
    app.logger.info("call-status %s", event)
    _calls.appendleft(event)
    return ("", 204)


@app.route("/api/calls")
def calls():
    """Recent-call log for the UI (served behind nginx basic auth)."""
    return jsonify(list(_calls))


@app.route("/api/sms/inbound", methods=["POST"])
@twilio_webhook
def sms_inbound():
    """Inbound SMS to our number -> log + keep for the UI.

    Receiving SMS does not require A2P 10DLC; sending does, so there is no
    outbound send endpoint yet. We return empty TwiML (no auto-reply).
    """
    msg = {
        "from": request.form.get("From"),
        "to": request.form.get("To"),
        "body": request.form.get("Body"),
        "sid": request.form.get("MessageSid"),
    }
    app.logger.info("sms-inbound %s", msg)
    _messages.appendleft(msg)
    return str(MessagingResponse()), 200, {"Content-Type": "text/xml"}


@app.route("/api/messages")
def messages():
    """Inbound-message log for the UI (served behind nginx basic auth)."""
    return jsonify(list(_messages))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT)
