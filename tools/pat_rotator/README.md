# Samsung FamilyHub Dual Token Rotator & Micro-Service

A lightweight, standalone background service that automatically manages and rotates both:
1. **SmartThings Personal Access Tokens (PATs)**: Rotated every 23 hours via headless browser automation to handle Samsung's 24-hour token expiry limit.
2. **Samsung Food (Whisk) Tokens**: Rotated every 28 days via headless browser automation using your saved Samsung account session to keep AI food inventory synced.

Both tokens are served to Home Assistant over local REST endpoints.

---

## Features
* **Zero External Dependencies**: Completely self-contained in this directory.
* **Dual Rotation Loops**: 
  - SmartThings PAT rotated every 23 hours.
  - Samsung Food token rotated every 28 days (and verified on startup).
* **Local REST Endpoints**:
  - `GET http://<SERVER_IP>:8765/pat` ➔ `{"token": "<PAT>", "status": "ok"}`
  - `GET http://<SERVER_IP>:8765/food_token` ➔ `{"token": "<FOOD_TOKEN>", "status": "ok"}`
  - `GET http://<SERVER_IP>:8765/tokens` ➔ Combined JSON with both tokens.
* **Automated Log Maintenance**: Writes directly to `rotator.log` and automatically trims it every Sunday at midnight to keep only the most recent 100 lines.

---

## Installation & Setup (On Any Linux Server)

### 1. Run Setup
```bash
cd tools/pat_rotator
./setup.sh
```

### 2. Configure Credentials & 2FA Preferences
Edit `config.json`:
```json
{
  "samsung_email": "your_samsung_account@example.com",
  "samsung_password": "your_samsung_password",
  "two_factor_method": "device",
  "port": 8765,
  "rotation_interval_hours": 23,
  "food_rotation_interval_days": 28
}
```

#### Configuration Options:
* `samsung_email` & `samsung_password`: Your Samsung account credentials.
* `two_factor_method`: Delivery method for Samsung Two-Factor Authentication (2FA) during initial session creation or when session cookies hard-expire:
  * `"device"` *(default)*: Sends a push notification to your Galaxy phone or tablet. Simply tap **"Yes"** on your device to approve!
  * `"sms"`: Automatically re-routes the 2FA prompt to your mobile phone via SMS text message and prompts for the 6-digit code in the terminal (recommended if you are travelling or do not use a Galaxy phone).
* `port`: HTTP server port (default: `8765`).
* `rotation_interval_hours`: Frequency to generate a fresh PAT (default: `23` hours).
* `food_rotation_interval_days`: Frequency to refresh Samsung Food token (default: `28` days).

### 3. Run
```bash
# In background / nohup (main.py writes directly to rotator.log):
nohup .venv/bin/python main.py &

# Or in a screen/tmux session:
.venv/bin/python main.py
```

---

## Troubleshooting & Session Maintenance (2FA & Cookie Expiry)

### How Sessions Work
The rotator caches your authenticated browser session to `smartthings_session.json` and updates it on every rotation. Under normal operation, sliding expiration keeps this session active indefinitely without any intervention.

### If Samsung Demands Interactive 2FA or CAPTCHA
If Samsung Account session cookies hard-expire (e.g. after ~30 days or when Samsung updates terms of service), Samsung's identity provider may demand human verification (a push confirmation, an SMS code, or a visual CAPTCHA).

When running headlessly in the background, automated Chromium cannot guess a 2FA code or solve a picture CAPTCHA. It will log a notice and retry every 10 minutes:
```text
[ERROR] Samsung account requires 2FA or device approval, but rotator is running non-interactively in background mode.
Please run '.venv/bin/python generate_pat.py' interactively in a terminal to authenticate once and refresh smartthings_session.json.
```

### How to Refresh the Session
1. Open a terminal or screen session in your rotator directory:
   ```bash
   cd tools/pat_rotator   # (or ~/smartthings_pat_rotator)
   .venv/bin/python generate_pat.py
   ```
2. **If using `"two_factor_method": "device"`**:
   Samsung will push a notification to your Galaxy phone or tablet. Simply tap **"Yes"** on your device. The script detects approval within seconds and finishes automatically.
3. **If using `"two_factor_method": "sms"`**:
   The script will request an SMS text message to your registered mobile phone and prompt you in the terminal:
   ```text
   Enter the 2FA verification code:
   ```
   Type the 6-digit code and hit **Enter**.
4. **If Samsung presents a visual CAPTCHA puzzle**:
   Run the command with a visible browser UI on a computer with a desktop display:
   ```bash
   .venv/bin/python generate_pat.py --no-headless
   ```
   *(Alternatively, run it on your local workstation with `--no-headless`, solve the CAPTCHA once, and copy the updated `smartthings_session.json` to your server).*
5. Once `generate_pat.py` prints `SUCCESS! New PAT: ...`, your session in `smartthings_session.json` is refreshed for another month, and the background service will resume autonomous rotation.

### Diagnostic Screenshots
Whenever a generation failure occurs, the rotator automatically captures:
- A screenshot of the blocking page: `smartthings_pat_error.png` (and `/tmp/smartthings_pat_error.png`)
- The full page HTML: `smartthings_pat_error.html` (and `/tmp/smartthings_pat_error.html`)
You can inspect these files to immediately see the exact screen that was displayed.

---

## Home Assistant Configuration

### 1. Create Helper Entities
In Home Assistant: **Settings → Devices & Services → Helpers → Create Helper → Text**:
* **Helper 1 (PAT)**:
  * Name: `smartthings_pat`
  * Entity ID: `input_text.smartthings_pat`
  * Max Length: `255`
* **Helper 2 (Food Token)**:
  * Name: `samsung_food_token`
  * Entity ID: `input_text.samsung_food_token`
  * Max Length: `255`

### 2. Add REST Sensors to `configuration.yaml`
```yaml
sensor:
  - platform: rest
    name: "SmartThings PAT"
    resource: "http://<YOUR_LINUX_SERVER_IP>:8765/pat"
    value_template: "{{ value_json.token }}"
    scan_interval: 3600 # Polls every hour

  - platform: rest
    name: "Samsung Food Token"
    resource: "http://<YOUR_LINUX_SERVER_IP>:8765/food_token"
    value_template: "{{ value_json.token }}"
    scan_interval: 86400 # Polls once daily
```

### 3. Add Sync Automations to `automations.yaml`
```yaml
- alias: "Sync SmartThings PAT from Rotator"
  description: "Copies fetched PAT from REST sensor to input_text.smartthings_pat"
  trigger:
    - platform: state
      entity_id: sensor.smartthings_pat
    - platform: homeassistant
      event: start
  condition:
    - condition: template
      value_template: >-
        {{ states('sensor.smartthings_pat') not in ['unknown', 'unavailable', 'None', ''] }}
  action:
    - service: input_text.set_value
      target:
        entity_id: input_text.smartthings_pat
      data:
        value: "{{ states('sensor.smartthings_pat') }}"

- alias: "Sync Samsung Food Token from Rotator"
  description: "Copies fetched Food Token from REST sensor to input_text.samsung_food_token"
  trigger:
    - platform: state
      entity_id: sensor.samsung_food_token
    - platform: homeassistant
      event: start
  condition:
    - condition: template
      value_template: >-
        {{ states('sensor.samsung_food_token') not in ['unknown', 'unavailable', 'None', ''] }}
  action:
    - service: input_text.set_value
      target:
        entity_id: input_text.samsung_food_token
      data:
        value: "{{ states('sensor.samsung_food_token') }}"
```

### 4. Restart Home Assistant
Restart Home Assistant to load the REST sensors and automations. The integration's dynamic listeners will automatically keep cameras, door sensors, and fridge food inventory updated continuously!

---

## Optional: Systemd Service Setup (For Auto-Start on Boot)

Create `/etc/systemd/system/smartthings-rotator.service`:
```ini
[Unit]
Description=SmartThings & Samsung Food Token Rotator Service
After=network.target

[Service]
Type=simple
User=robert
WorkingDirectory=/home/robert/smartthings_pat_rotator
ExecStart=/home/robert/smartthings_pat_rotator/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now smartthings-rotator
```
