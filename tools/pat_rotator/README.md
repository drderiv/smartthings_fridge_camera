# SmartThings PAT Rotator & Micro-Service

A lightweight, standalone service that automatically generates SmartThings Personal Access Tokens (PATs) every 12 hours via headless browser automation and serves the active token to Home Assistant over a local REST endpoint.

---

## Features
* **Zero External Dependencies**: Completely self-contained in this directory.
* **Portable**: Copy this folder to any Linux machine, Raspberry Pi, or VPS.
* **Automated Renewal**: Automatically logs into `account.smartthings.com/tokens` every 12 hours to mint a fresh 24-hour token.
* **Local REST Server**: Serves `GET http://<SERVER_IP>:8765/pat` returning `{"token": "<NEW_PAT>", "status": "ok"}`.

---

## Installation & Setup (On Any Linux Server)

### 1. Run Setup
```bash
cd smartthings_pat_rotator
./setup.sh
```

### 2. Configure Credentials
Edit `config.json`:
```json
{
  "samsung_email": "your_samsung_account@example.com",
  "samsung_password": "your_samsung_password",
  "port": 8765,
  "rotation_interval_hours": 12
}
```

### 3. Run
```bash
# In background / nohup (main.py writes directly to rotator.log):
nohup .venv/bin/python main.py &

# Or in a screen/tmux session:
.venv/bin/python main.py
```
*Note: `main.py` writes directly to `rotator.log` and automatically trims it every Sunday at midnight to keep only the most recent 100 lines.*

---

## Home Assistant Configuration (HAOS / Supervised / Container)

### 1. Create the Helper
In Home Assistant: **Settings → Devices & Services → Helpers → Create Helper → Text**:
* **Name**: `smartthings_pat`
* **Entity ID**: `input_text.smartthings_pat`
* **Max Length**: `255`

### 2. Add REST Sensor to `configuration.yaml`
```yaml
sensor:
  - platform: rest
    name: "SmartThings PAT"
    resource: "http://<YOUR_LINUX_SERVER_IP>:8765/pat"
    value_template: "{{ value_json.token }}"
    scan_interval: 3600 # Polls every hour
```

### 3. Add Sync Automation to `automations.yaml`
```yaml
alias: "Sync SmartThings PAT from Rotator"
description: "Copies fetched token from REST sensor to input_text.smartthings_pat"
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
```

### 4. Restart Home Assistant
Restart Home Assistant to load the REST sensor and automation.

---

## Optional: Systemd Service Setup (For Auto-Start on Boot)

Create `/etc/systemd/system/smartthings-rotator.service`:
```ini
[Unit]
Description=SmartThings PAT Rotator Service
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
