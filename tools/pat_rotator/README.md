# Token Rotator

A small service that keeps the credentials the Samsung FamilyHub integration needs fresh, and serves them to Home Assistant over HTTP. It runs on any Linux machine; Home Assistant itself does not need Chromium.

| Token | Lifetime | Rotated | How |
|---|---|---|---|
| SmartThings PAT | 24 h (hard limit) | every 23 h | headless Chromium signs in to account.smartthings.com/tokens |
| Samsung Food (Whisk) token | ~30 days | every 28 days | headless Chromium with your saved Samsung session |
| KICS Food Circles token | 24 h access, 90 d rolling refresh | every 23 h | plain HTTP refresh-token exchange (no browser after the first bootstrap) |

## Setup

```bash
cd tools/pat_rotator
./setup.sh          # venv, dependencies, Chromium, config.json
```

Edit `config.json`:

```json
{
  "samsung_email": "you@example.com",
  "samsung_password": "…",
  "two_factor_method": "device",
  "port": 8765,
  "bind_address": "127.0.0.1",
  "api_key": "",
  "rotation_interval_hours": 23,
  "food_rotation_interval_days": 28,
  "kics_rotation_interval_hours": 23,
  "location_id": "OPTIONAL_SMARTTHINGS_LOCATION_ID"
}
```

| Key | Meaning |
|---|---|
| `samsung_email`, `samsung_password` | Your Samsung account. Keep `config.json` at mode `0600` (setup.sh does this; the service warns otherwise). |
| `two_factor_method` | `"device"` (approve on your Galaxy phone/tablet) or `"sms"` (type the code you receive). Only used when Samsung asks for verification. |
| `port`, `bind_address` | Where to listen. The default `127.0.0.1` is reachable only from this machine. Use this machine's LAN/VPN address (or `0.0.0.0`) if Home Assistant runs elsewhere. |
| `api_key` | Shared secret clients send as `Authorization: Bearer <key>`. **Required when `bind_address` is not loopback**; the service refuses to start otherwise. Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `*_interval_*` | Rotation intervals. Defaults suit the token lifetimes above. |
| `location_id` | Optional. Your fridge's SmartThings location ID, served at `/location_id`. Edits apply without a restart. |
| `kics_refresh_token` | Optional. Seeds the KICS refresh token if you already have one. |

Start it:

```bash
.venv/bin/python main.py                  # foreground
nohup .venv/bin/python main.py &          # background; logs go to rotator.log (rotates at 256 KB)
```

## Home Assistant

In **Settings → Devices & Services → Samsung FamilyHub → Configure** set **Rotator URL** (e.g. `http://10.0.0.5:8765`) and, if you set one, **Rotator API key**. Home Assistant reads `/tokens` once at startup and then only when a token reaches its `next_rotation_at` or Samsung returns 401/403. No YAML, REST sensors or helper entities are needed.

## HTTP API

All endpoints are `GET`; every one except `/health` needs the API key when one is configured.

| Endpoint | Response |
|---|---|
| `/pat` | `{"token", "status", "type", "generated_at", "next_rotation_at", "ttl_seconds"}` |
| `/food_token` | same shape, plus `location_id` |
| `/kics_token` | same shape, plus `location_id` |
| `/location_id` | `{"location_id", "status"}`, 404 if unset |
| `/tokens` | `smartthings_pat`, `samsung_food_token`, `kics_token`, `location_id`, and `pat_`/`food_`/`kics_next_rotation_at` |
| `/health` | `{"status": "healthy"}` (no key needed, reveals nothing) |

A missing token returns 503 `{"error", "status": "unavailable"}`.

## When Samsung asks for verification

Saved session cookies normally keep everything running unattended. If Samsung demands a 2FA code or CAPTCHA, a headless run can't answer it. The service logs that and **pauses automatic attempts for that token** (to avoid failed-login lockouts) until its token file changes. Refresh the session in a terminal:

```bash
cd tools/pat_rotator
.venv/bin/python generate_pat.py                 # add --no-headless to watch the browser
.venv/bin/python generate_pat.py --2fa-method sms
```

With `device`, approve the prompt on your phone/tablet; with `sms`, type the code. When the new PAT is written, the service notices and resumes by itself. If the code prompt went to a tablet, a screenshot of it is saved as `samsung_2fa_prompt.png` next to the scripts.

Other one-off commands (same as `main.py pat|food|kics`):

```bash
.venv/bin/python generate_food_token.py
.venv/bin/python generate_kics_token.py --login          # first-time KICS bootstrap from the saved session
.venv/bin/python generate_kics_token.py --refresh-now
.venv/bin/python generate_kics_token.py --validate-only
```

### Debugging a failed login

The SmartThings tokens page can display a live PAT, so failure screenshots/HTML are **off by default**. Start the service (or script) with `PAT_ROTATOR_KEEP_DEBUG=1` to write `smartthings_pat_error.png/.html` (owner-only) next to the scripts, and delete them afterwards.

## Files

All state lives in this folder, with owner-only permissions: `config.json`, `smartthings_pat.txt`, `samsung_food_token.txt`, `kics_food_token.txt`, `kics_refresh_token.txt`, `smartthings_session.json` (your Samsung login cookies — treat like a password), `rotator.log`.

## Run at boot (systemd)

`/etc/systemd/system/token-rotator.service`:

```ini
[Unit]
Description=Samsung FamilyHub token rotator
After=network-online.target

[Service]
User=<YOUR_USER>
WorkingDirectory=/home/<YOUR_USER>/<path-to>/tools/pat_rotator
ExecStart=/home/<YOUR_USER>/<path-to>/tools/pat_rotator/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now token-rotator
```

## Notes

* Each PAT is created with a timestamped name; the service does not delete old ones, so you may want to prune them occasionally at account.smartthings.com/tokens.
* Automating a Samsung login is inherently fragile: Samsung can change its pages or add challenges at any time. The KICS token avoids this by using refresh tokens, and is the model to prefer wherever Samsung offers it.
