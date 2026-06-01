"""Visark cold-call dialer — WebRTC softphone backend.

Routes:
  GET  /                      -> the single-page softphone UI
  GET  /api/token             -> mint a Twilio Voice access token (JWT) for the browser
  POST /api/twiml/outbound    -> TwiML: bridge browser -> prospect  (Twilio webhook)
  POST /api/twiml/inbound     -> TwiML: ring the browser client     (Twilio webhook)
  POST /api/call-status       -> Twilio call status callbacks        (Twilio webhook)
  GET  /api/calls             -> recent-call log for the UI to poll

Auth model: single-user form login with a signed session cookie (see
require_auth), unless BASIC_AUTH_USER is unset — e.g. when you protect the
hostname with Cloudflare Access at the edge instead. The Twilio webhooks are
exempt from login and instead validated via X-Twilio-Signature; the signed URL
is reconstructed deterministically from PUBLIC_BASE_URL so it works through the
Cloudflare tunnel regardless of forwarded-proto header behaviour.
"""
import hmac
import logging
import os
import re
import secrets
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_from_directory, session)
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

# Single-user login credentials. Leave BASIC_AUTH_USER unset to disable auth
# (e.g. when Cloudflare Access guards the hostname at the edge instead).
BASIC_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_PASS = os.environ.get("BASIC_AUTH_PASSWORD", "")

# Twilio webhooks: signature-validated, never basic-auth'd (Twilio can't log in).
WEBHOOK_PATHS = {
    "/api/voice",
    "/api/twiml/outbound",
    "/api/twiml/inbound",
    "/api/call-status",
    "/api/dial-result",
    "/api/sms/inbound",
}

app = Flask(__name__, static_folder="static", static_url_path="/static")
# Behind the Cloudflare tunnel; trust forwarded proto/host for any URL building.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
# SECRET_KEY persists sessions across restarts; a random fallback just means you
# re-login after a redeploy.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=7)
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

_validator = RequestValidator(AUTH_TOKEN)
# Most-recent-first ring buffers for the UI.
_calls = deque(maxlen=20)
_messages = deque(maxlen=50)


def normalize_e164(raw):
    """Strip formatting (spaces, dashes, parens) but keep a leading '+'."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    return "+" + digits if raw.startswith("+") else digits


def _now_str():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")


def _log_call(event):
    """Append a call event to the in-memory log (timestamps if not provided)."""
    event.setdefault("timestamp", _now_str())
    app.logger.info("call %s", event)
    _calls.appendleft(event)


@app.before_request
def require_auth():
    """Form-login session gate. Twilio webhooks (signature-validated), the login
    page, and static assets are exempt."""
    if not BASIC_USER:
        return  # auth disabled / handled upstream
    p = request.path
    if p in WEBHOOK_PATHS or p == "/login" or p.startswith("/static/"):
        return
    if session.get("authed"):
        return
    # Page navigations get the login screen; API calls get a plain 401.
    if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
        return redirect("/login")
    return Response("Authentication required", 401)


@app.after_request
def add_no_cache(resp):
    """Single-user tool — never let Cloudflare or the browser serve a stale UI."""
    resp.headers["Cache-Control"] = "no-store"
    return resp


def twilio_webhook(f):
    """Reject any request to a Twilio webhook that isn't signed by Twilio.

    The signed URL is rebuilt from PUBLIC_BASE_URL + path so validation does not
    depend on how the Cloudflare tunnel forwards scheme/host headers.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if VALIDATE:
            signature = request.headers.get("X-Twilio-Signature", "")
            url = (PUBLIC_BASE_URL + request.path) if PUBLIC_BASE_URL else request.url
            if not _validator.validate(url, request.form, signature):
                abort(403)
        return f(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/call-logs")
def call_logs_page():
    return send_from_directory("static", "call-logs.html")


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authed"):
        return redirect("/")
    return send_from_directory("static", "login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    user = request.form.get("username", "")
    pw = request.form.get("password", "")
    if (BASIC_USER and hmac.compare_digest(user, BASIC_USER)
            and hmac.compare_digest(pw, BASIC_PASS)):
        session["authed"] = True
        session.permanent = True
        return redirect("/")
    return redirect("/login?error=1")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


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


@app.route("/api/voice", methods=["POST"])
@app.route("/api/twiml/outbound", methods=["POST"])
@app.route("/api/twiml/inbound", methods=["POST"])
@twilio_webhook
def twiml_voice():
    """Single voice handler — direction is detected from the call, not the URL.

    This is deliberately resilient: it does the right thing no matter which of
    these URLs the TwiML App or the phone number is pointed at, so a mis-wired
    inbound webhook can't run the outbound logic and loop.

      From = "client:..."  -> browser dialing out  -> <Dial><Number>prospect</Number>
      otherwise (real PSTN) -> inbound call          -> <Dial><Client>bolaji</Client> (rings browser)
    """
    resp = VoiceResponse()
    from_ = request.form.get("From") or ""

    if from_.startswith("client:"):
        # Outbound: our WebRTC client placed the call.
        to = normalize_e164(request.form.get("To"))
        if not to:
            resp.say("No destination number was provided.")
        elif to == PHONE_NUMBER:
            resp.say("You cannot dial this line's own number.")
        else:
            dial = Dial(caller_id=PHONE_NUMBER, answer_on_bridge=True)
            status_cb = f"{PUBLIC_BASE_URL}/api/call-status" if PUBLIC_BASE_URL else None
            dial.number(
                to,
                status_callback=status_cb,
                status_callback_event="initiated ringing answered completed",
                status_callback_method="POST",
            )
            resp.append(dial)
    else:
        # Inbound: a real phone called our Twilio number -> ring the browser.
        # Log the ring immediately so a missed call is never lost; the Dial
        # action below then records the final outcome (answered / missed).
        _log_call({
            "call_sid": request.form.get("CallSid"),
            "status": "incoming",
            "from": from_,
            "to": request.form.get("To"),
            "direction": "inbound",
            "duration": "",
        })
        action = f"{PUBLIC_BASE_URL}/api/dial-result" if PUBLIC_BASE_URL else None
        dial = Dial(answer_on_bridge=True, action=action, method="POST")
        dial.client(CLIENT_IDENTITY)
        resp.append(dial)

    return str(resp), 200, {"Content-Type": "text/xml"}


@app.route("/api/dial-result", methods=["POST"])
@twilio_webhook
def dial_result():
    """Final outcome of an inbound <Dial><Client>: answered vs missed."""
    status = request.form.get("DialCallStatus") or "unknown"
    if status in ("no-answer", "busy", "failed", "canceled"):
        status = "missed"
    _log_call({
        "call_sid": request.form.get("CallSid"),
        "status": status,
        "from": request.form.get("From"),
        "to": request.form.get("To"),
        "direction": "inbound",
        "duration": request.form.get("DialCallDuration", ""),
    })
    return str(VoiceResponse()), 200, {"Content-Type": "text/xml"}


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
    """Full raw call-event log (for the /call-logs detail view)."""
    return jsonify(list(_calls))


@app.route("/api/calls/clear", methods=["POST"])
def clear_calls():
    """Wipe the in-memory call log. Session-gated (not a Twilio webhook)."""
    _calls.clear()
    return ("", 204)


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
    """Inbound-message log for the UI (session-gated)."""
    return jsonify(list(_messages))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT)
