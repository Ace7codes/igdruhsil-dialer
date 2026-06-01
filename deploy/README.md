# Deploy — dialer.igdruhsil.com

Reverse-proxy the app (running on `127.0.0.1:3001`) behind nginx with TLS and
single-user basic auth. Twilio webhooks bypass basic auth and are protected by
request-signature validation in the app.

## 1. DNS
Add an **A record**: `dialer.igdruhsil.com` → your server's public IP. Wait for it
to resolve (`dig +short dialer.igdruhsil.com`).

## 2. TLS cert (Let's Encrypt)
Port 80 must be free for this (stop nginx first if it's already bound):
```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d dialer.igdruhsil.com
```
Auto-renew is installed by the certbot package (systemd timer). Verify with
`sudo certbot renew --dry-run`.

## 3. Basic-auth password file
Single user. Creds live ONLY in this file (not in env):
```bash
sudo apt install -y apache2-utils
sudo htpasswd -bc /etc/nginx/.dialer_htpasswd "bolaji@visarkai.com" "VisarkOutreach2026"
sudo chmod 640 /etc/nginx/.dialer_htpasswd
sudo chown root:www-data /etc/nginx/.dialer_htpasswd
```

## 4. Enable the site
```bash
sudo cp deploy/nginx/dialer.igdruhsil.com.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/dialer.igdruhsil.com.conf /etc/nginx/sites-enabled/
sudo nginx -t        # test config
sudo systemctl reload nginx
```

## 5. Point Twilio at it
- **TwiML App** Voice Request URL → `https://dialer.igdruhsil.com/api/twiml/outbound` (POST)
- **Number** Voice webhook        → `https://dialer.igdruhsil.com/api/twiml/inbound` (POST)
- **Number** Call-status callback  → `https://dialer.igdruhsil.com/api/call-status` (POST)
- **Number** Messaging webhook      → `https://dialer.igdruhsil.com/api/sms/inbound` (POST) — inbound SMS only

## Run as a service (survives reboot)
The unit in `deploy/systemd/dialer.service` runs the app under gunicorn and
restarts it on crash. `enable` is what makes it come back after a server reboot.

First edit the unit for your server: `User`/`Group`, `WorkingDirectory`, and the
gunicorn path in `ExecStart` (all default to user `dialer` + `/opt/igdruhsil-dialer`).
The deploy dir must contain `.env` and `static/`, and the venv must have deps
installed (`venv/bin/pip install -r requirements.txt`).

```bash
sudo cp deploy/systemd/dialer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dialer      # enable = start on boot; --now = start it too
systemctl status dialer                 # should show active (running)
journalctl -u dialer -f                 # live logs (call-status / sms-inbound show here)
```
After editing the unit later: `sudo systemctl daemon-reload && sudo systemctl restart dialer`.

## Notes / gotchas
- **Webhook auth split:** `/api/twiml/outbound`, `/api/twiml/inbound`, and
  `/api/call-status` are intentionally outside basic auth. The app MUST validate
  `X-Twilio-Signature` on these (Twilio `RequestValidator` / `validateRequest`)
  since they're internet-facing — that's the defense-in-depth that justifies the
  public route (matches CLAUDE.md Phase 2).
- **UI status polling:** the browser polls call status from an *authenticated*
  path (under `location /`). Keep that path distinct from `/api/call-status`
  (Twilio's unauthenticated POST target) so the poll stays behind auth.
- **Browser will prompt for mic access** over HTTPS — that's expected.
- Basic auth is enforced by nginx at the edge; the app itself doesn't enforce it,
  but it still reads the Twilio creds from `.env`.
- If the app listens on a port other than 3001, update both the `proxy_pass`
  lines in the nginx conf and `PORT` in `.env`.
