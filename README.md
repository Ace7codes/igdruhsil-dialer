# igdruhsil-dialer

Browser-based cold-call softphone for [Visark](https://visark.ai) sales outreach. Make outbound calls and receive inbound calls entirely in the browser over WebRTC — no personal phone bridged into the call path.

Built for the Visark sales sprint: a lightweight dialer layer alongside email, used against manually enriched leads (LinkedIn, Google, etc.) when Apollo phone data is not usable.

**Production URL:** [https://dialer.igdruhsil.com](https://dialer.igdruhsil.com) — fronted by a dedicated Cloudflare Tunnel (TLS at Cloudflare's edge), single-user form login.

## How it works

A **WebRTC softphone via Twilio Voice**, not a server-side phone bridge.

**Outbound**

1. User signs in, clicks **Go online** (grants mic + unlocks audio), then **Dial** with a prospect number.
2. The browser (Twilio Voice JS SDK) connects with a short-lived access token from `/api/token`.
3. Twilio hits the voice handler, which returns TwiML dialing the prospect with the Twilio number as caller ID.
4. Audio flows browser <-> prospect over WebRTC.

**Inbound**

1. A prospect calls the Twilio number.
2. Twilio hits the voice handler, which returns TwiML ringing the browser client.
3. The UI rings (synthesized ringtone) and shows **Answer / Reject**.

The voice handler detects direction from the call itself (`From=client:...` => outbound, otherwise inbound), so it does the right thing regardless of which URL the TwiML App / number webhook is pointed at — a mis-wired webhook can't loop. Inbound calls are logged the moment they ring and again at hang-up, so **missed calls are recorded** (shown in red).

**Recording:** while a call is connected the browser mixes the local + remote WebRTC audio (`MediaRecorder` over Web Audio) and uploads the file to the server on hang-up — captured over the channel in-browser, no Twilio recording fee. Toggle with `RECORD_CALLS`. Logs and recordings persist to a `./data` volume so they survive restarts.

Twilio webhooks are validated with `X-Twilio-Signature` (the signed URL is rebuilt from `PUBLIC_BASE_URL`, so it survives the tunnel). Everything else is gated by a single-user session login.

## Stack

| Layer | Choice |
|-------|--------|
| Backend | Python / Flask (gunicorn) |
| Voice | Twilio Voice API + `@twilio/voice-sdk` (browser) |
| Frontend | Single HTML page, vanilla JS, no framework |
| Auth | Form login + signed session cookie |
| Ingress | Cloudflare Tunnel (`cloudflared`), TLS at the edge |
| Runtime | Docker Compose (app + its own cloudflared) |

## Project layout

```
app.py              Flask backend (auth, tokens, TwiML, call/message log)
static/             UI — index.html, login.html, call-logs.html, app.js, style.css, logo.png
Dockerfile          App image (gunicorn)
docker-compose.yml  Dialer app + dedicated cloudflared tunnel
env.example         Environment variable template
requirements.txt    Python dependencies
```

## Local development

```bash
cp env.example .env          # fill in Twilio creds + BASIC_AUTH_* + SECRET_KEY
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py                # serves on 127.0.0.1:3001
```

Open `http://localhost:3001`. For real Twilio webhooks locally, use a tunnel (ngrok, etc.) and point your TwiML App / number webhooks at the public URL. Set `TWILIO_VALIDATE=false` for local curl testing without signatures.

## Production deploy (Docker + Cloudflare Tunnel)

The app runs as a self-contained Compose project with its **own** Cloudflare tunnel — no host ports, no nginx, no certbot, isolated from anything else on the box.

1. **Create a dedicated tunnel** in Cloudflare Zero Trust → Networks → Tunnels (type: Docker). Copy the tunnel token into `CLOUDFLARE_TUNNEL_TOKEN` in `.env`. Add a Public Hostname: `dialer.igdruhsil.com` (HTTP) → `http://dialer:3001`.

2. **Configure `.env`** from `env.example`: Twilio creds, `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` (login), `SECRET_KEY` (`python -c "import secrets; print(secrets.token_hex(32))"`), and the tunnel token.

3. **Build and run:**

   ```bash
   docker compose up -d --build
   ```

   > `app.py` is baked into the image (not mounted), so backend changes require a rebuild. If a build seems to run stale code: `docker compose build --no-cache dialer && docker compose up -d --force-recreate`. The `static/` and `.env` are mounted live.

4. **Point Twilio at it:**

   | Twilio setting | URL | Method |
   |---|---|---|
   | Phone Number → "A call comes in" | `https://dialer.igdruhsil.com/api/twiml/inbound` | POST |
   | Phone Number → "Call status changes" (for inbound logging) | `https://dialer.igdruhsil.com/api/call-status` | POST |
   | TwiML App → Voice Request URL | `https://dialer.igdruhsil.com/api/twiml/outbound` | POST |

   Enable destination countries under Voice → Geographic Permissions.

## API routes

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Softphone UI (session-gated) |
| `GET` / `POST` | `/login` | Login form / submit |
| `GET` | `/logout` | Clear session |
| `GET` | `/call-logs` | Full raw call-event log (new tab) |
| `GET` | `/api/token` | Mint Twilio Voice access token |
| `POST` | `/api/voice`, `/api/twiml/outbound`, `/api/twiml/inbound` | Voice TwiML — direction auto-detected (Twilio webhook) |
| `POST` | `/api/call-status` | Call status callbacks (Twilio webhook) |
| `POST` | `/api/dial-result` | Inbound dial outcome — answered vs missed (Twilio webhook) |
| `POST` | `/api/sms/inbound` | Inbound SMS (Twilio webhook) |
| `GET` | `/api/calls` | Raw call log for the UI |
| `POST` | `/api/calls/clear` | Clear the call log |
| `GET` | `/api/messages` | Inbound SMS log for the UI |
| `GET` | `/api/recordings` | Saved call recordings (browser-captured) |
| `POST` | `/api/recordings/upload` | Upload a browser-recorded call (session-gated) |
| `GET` | `/recordings/<file>` | Stream/download a recording (session-gated) |

## Security notes

- Never commit `.env` — it holds Twilio secrets and the tunnel token.
- Twilio webhook routes are exempt from login; the app validates `X-Twilio-Signature` instead.
- Login credentials and `SECRET_KEY` live in `.env`; the session cookie is `Secure`, `HttpOnly`, `SameSite=Lax`.

## Out of scope

Outbound SMS (needs A2P 10DLC registration), transcription, auto-dialing, CRM integration, multi-user. Logs and recordings persist to JSONL/files on a `./data` volume — no database, which is fine for a single user.

## License

See [LICENSE](LICENSE).
