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
