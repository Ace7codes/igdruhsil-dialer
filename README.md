# igdruhsil-dialer

Browser-based cold-call softphone for [Visark](https://visark.ai) sales outreach. Make outbound calls and receive inbound callbacks entirely in the browser over WebRTC � no personal phone bridged into the call path.

Built for the Visark sales sprint: a lightweight dialer layer alongside email, used against manually enriched leads (LinkedIn, Google, etc.) when Apollo phone data is not usable.

**Production URL:** [https://dialer.igdruhsil.com](https://dialer.igdruhsil.com) (nginx basic auth + TLS)

## How it works

This is a **WebRTC softphone via Twilio Voice**, not a server-side phone bridge.

**Outbound**

1. User opens the single-page UI and clicks **Dial** with a prospect number.
2. The browser (Twilio Voice JS SDK) connects using a short-lived access token from the backend.
3. Twilio hits `/api/twiml/outbound` and receives TwiML to dial the prospect with your Twilio number as caller ID.
4. Audio flows browser ? prospect over WebRTC.

**Inbound**

1. A prospect calls your Twilio number.
2. Twilio hits `/api/twiml/inbound` and receives TwiML to ring the browser client.
3. The UI shows an incoming call; the user accepts or rejects.

Twilio webhooks are validated with `X-Twilio-Signature`. The UI is protected by HTTP basic auth at the nginx edge (see [deploy/README.md](deploy/README.md)).

## Stack

| Layer | Choice |
|-------|--------|
| Backend | Python / Flask |
| Voice | Twilio Voice API + `@twilio/voice-sdk` in the browser |
| Frontend | Single HTML page, vanilla JS, no framework |
| Production | gunicorn + nginx + systemd |

## Project layout

```
app.py              Flask backend (tokens, TwiML, call log)
static/             Softphone UI (index.html, app.js, style.css)
env.example         Environment variable template
deploy/             nginx, systemd, and production deploy notes
requirements.txt    Python dependencies
```

## Local development

1. **Copy env and fill in Twilio credentials**

   ```bash
   cp env.example .env
   ```

   Required variables: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY`, `TWILIO_API_SECRET`, `TWILIO_TWIML_APP_SID`, `TWILIO_PHONE_NUMBER`, `TWILIO_CLIENT_IDENTITY`, `PUBLIC_BASE_URL`.

2. **Install dependencies**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run the app**

   ```bash
   flask --app app run --port 3001
   # or: python app.py
   ```

4. Open `http://localhost:3001`. For real Twilio webhooks locally, use a tunnel (ngrok, etc.) and point your TwiML App / number webhooks at the public URL. Set `TWILIO_VALIDATE=false` only for local curl testing without signatures.

## API routes

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Softphone UI |
| `GET` | `/api/token` | Mint Twilio Voice access token for the browser |
| `POST` | `/api/twiml/outbound` | TwiML: bridge browser ? prospect (Twilio webhook) |
| `POST` | `/api/twiml/inbound` | TwiML: ring browser on inbound call (Twilio webhook) |
| `POST` | `/api/call-status` | Call status callbacks (Twilio webhook) |
| `GET` | `/api/calls` | Recent call log for the UI |

## Production deploy

See **[deploy/README.md](deploy/README.md)** for DNS, TLS, nginx basic auth, Twilio webhook URLs, and the systemd service unit.

## Security notes

- Never commit `.env` � it holds Twilio secrets.
- Twilio webhook routes are intentionally outside basic auth; the app validates request signatures instead.
- Basic auth credentials live in nginx (`htpasswd`), not in application env.

## License

See [LICENSE](LICENSE).
