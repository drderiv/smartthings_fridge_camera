# SmartThings Family Hub Fridge Camera Integration for Home Assistant

This is a custom integration to output SmartThings Family Hub fridge camera feeds and live AI Food Manager inventory in [HomeAssistant](https://home-assistant.io).

> 💡 **What's New in this Enhanced Fork?**  
> Check out the complete [**Feature Enhancements & Incremental Capabilities Guide (CHANGELOG_ENHANCEMENTS.md)**](CHANGELOG_ENHANCEMENTS.md) for full details on the AI Food Manager integration, automated 24-hour PAT rotation microservice, multi-mode OAuth authentication, and resilient caching.

<p float="left">
  <img src="./assets/presentation/dashboard-demo.png" width=600  alt="dashboard-demo"/>
</p>

**Please be aware that this implementation is a proof of concept. Don't expect everything to work!**

# Installation

When it comes to the installation, you have two options:
- Option 1: Install via HACS
- Option 2: Manual Installation

## Option 1: Install via HACS

First, navigate to the HACS tab on your Home Assistant instance. On this page, click the three dots in the top right corner and select "Custom repositories":
<p float="left">
  <img src="assets/install/install-step-1.png" width=1200  alt="install-step-1"/>
</p>


In the floating window, please enter the link to the repository and select "Integration" as the type. (Just copy the link from the browser as shown)
<p float="left">
  <img src="assets/install/install-step-2.png" width=600  alt="install-step-2"/>
  <img src="assets/install/install-step-2_1.png" width=600  alt="install-step-2_1"/>
</p>


After clicking the "Add" button, the repository should be added at the top as follows:
<p float="left">
  <img src="assets/install/install-step-3.png" width=1200  alt="install-step-3"/>
</p>


Next, search for your recently added repository in the HACS search bar and click on it:
<p float="left">
  <img src="assets/install/install-step-4.png" width=1200  alt="install-step-4"/>
</p>


Click the "Download" button in the bottom right:
<p float="left">
  <img src="assets/install/install-step-5.png" width=1200  alt="install-step-5"/>
</p>


Confirm the download of the latest version by clicking "Download". If everything works, you should see a success message afterwards:
<p float="left">
  <img src="assets/install/install-step-6.png" width=1200  alt="install-step-6"/>
</p>


### !!! Please restart Home Assistant for the changes to take effect !!!


### CONGRATULATIONS <3

You have successfully added the integration to your Home Assistant instance.


## Option 2: Manual Installation

Install it as you would do with any Home Assistant custom component:

1. Download the `custom_components` folder from the repository.
2. Copy the `samsung_familyhub_fridge` directory into the `custom_components` directory of your Home Assistant installation. The `custom_components` directory resides within your Home Assistant configuration directory.</br>
**Note**: if the `custom_components` directory does not exist, you need to create it.
After a correct installation, your configuration directory should look like the following:
    ```
    └── ...
    └── configuration.yaml
    └── custom_components
        └── samsung_familyhub_fridge
            └── __init__.py
            └── manifest.json
            └── api.py
            └── camera.py
            └── ...
    ```

For reference:
<p float="left">
  <img src="assets/install/install-step-manual-1.png" width=600  alt="install-step-manual-1"/>
  <img src="assets/install/install-step-manual-2.png" width=600  alt="install-step-manual-2"/>
</p>

### !!! Make sure to reboot Home Assistant after importing all files !!!


# Configuration

After the installation was successful, you can now configure the integration.

Navigate to "Settings" > "Devices & service":
<p float="left">
  <img src="assets/config/config-step-1.png" width=1200  alt="config-step-1"/>
</p>


Click "Add Integration" in the bottom right:
<p float="left">
  <img src="assets/config/config-step-2.png" width=1200  alt="config-step-2"/>
</p>


Search for the FamilyHub Integration you just downloaded and select it:
<p float="left">
  <img src="assets/config/config-step-3.png" width=1200  alt="config-step-3"/>
</p>


You need to enter your Smartthings Token and your Device ID. The token is used to access your SmartThings account. The device ID identifies your fridge.</br>
You can create a token from here: https://account.smartthings.com/tokens.</br>
And get your device ID from here: https://my.smartthings.com/advanced/devices.</br>
Click "Submit" to finish the setup:
<p float="left">
  <img src="assets/config/config-step-4.png" width=1200  alt="config-step-4"/>
</p>


If everything worked, you should see a success message:
<p float="left">
  <img src="assets/config/config-step-5.png" width=1200  alt="config-step-5"/>
</p>


Now let's add the camera to your dashboard. Navigate to your dashboard and add a card. Select the "Picture entity" card:
<p float="left">
  <img src="assets/config/config-step-6.png" width=1200  alt="config-step-6"/>
</p>


As the entity, you need to select your camera. You will see more than one camera entity. Just select the one that is working for you:
<p float="left">
  <img src="assets/config/config-step-7.png" width=1200  alt="config-step-7"/>
</p>


Make sure to select the additional settings as follows and click "Save":
<p float="left">
  <img src="assets/config/config-step-8.png" width=1200  alt="config-step-8"/>
</p>

# 🥗 Live Food Circles & AI Food Manager Inventory

For Samsung Family Hub refrigerators equipped with internal food cameras and AI Food Manager, this integration extracts and displays your active fridge inventory—including **live circular camera crops** cut directly from the fridge shelves, food names, compartments, and expiration dates—as a single clean entity:

* **Entity**: `sensor.samsung_fridge_food_inventory`
* **State**: Count of active items currently inside the fridge (e.g. `37`)
* **Attributes**: `total_items`, `last_synced`, and `items` (rich list of active items with circular crop image URLs, expiration dates, and timestamps).

### Setup (Opt-In & Zero Emulator):

* **Method 1: Fully Automated Continuous Operation (Recommended)**:
  Run the companion **Tri-Token Rotator microservice** located in [`tools/pat_rotator/`](tools/pat_rotator/README.md). It runs on any Linux machine or server, automatically bootstrapping and renewing your:
  - **SmartThings PAT** (every 23 hours via headless automation)
  - **Samsung Food Token** (every 28 days)
  - **KICS Live Food Circles Token** (every 23 hours via OAuth rolling refresh)
  
  **No Android emulator, rooting, or packet sniffing is required!** All tokens and refrigerator location data are served to Home Assistant over local REST endpoints.

* **Home Assistant Configuration & Options Flow**:
  - If running Home Assistant on a different device or VM from your rotator server, go to **Settings → Devices & Services → Samsung FamilyHub**, click **Configure**, and enter your rotator server URL (e.g. `http://192.168.1.50:8765`).
  - You can also adjust the Food Circles sync interval (default: 600s / 10 minutes) or override your Refrigerator Location ID.
  - The integration automatically detects the tokens and registers `sensor.fridge_food_inventory`. If no token is provided, the sensor is omitted so non-AI fridges remain uncluttered.
  - **Zero YAML & Zero Routine Polling**: Home Assistant queries the rotator once at startup via `GET /tokens` and caches tokens in memory until their scheduled renewal time (`next_rotation_at`), eliminating continuous polling and network overhead.

---

### Dashboard Card Templates

#### 1. Recommended 50/50 Balanced Layout (Food Inventory Table + Door Cameras)

In your dashboard's **Raw Configuration Editor**, use:

```yaml
views:
  - title: Smart Fridge
    path: fridge
    icon: mdi:fridge-food
    type: masonry
    cards:
      # Left Column: Food Inventory List with Days Old & Aligned Headers
      - type: markdown
        title: 🥗 Smart Fridge Inventory
        content: |
          {% set last_synced = state_attr('sensor.fridge_food_inventory', 'last_synced') %}
          **Active Items**: {{ state_attr('sensor.fridge_food_inventory', 'total_items') or 0 }} &nbsp;|&nbsp; **Last Synced**: {{ as_timestamp(last_synced, default=0) | timestamp_custom('%b %d %-I:%M %p', default='Just now') if last_synced else 'Just now' }}

          ---

          | Item | Food Name &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Days Old |
          | :---: | :--- | :---: |
          {% set now_ts = as_timestamp(now()) -%}
          {% for item in (state_attr('sensor.fridge_food_inventory', 'items') or []) | sort(attribute='added_at', reverse=true) -%}
          {% set raw_ts = item.added_at | int(0) -%}
          {% set sec_ts = (raw_ts / 1000) if raw_ts > 10000000000 else raw_ts -%}
          {% set days_old = ((now_ts - sec_ts) / 86400) | round(0, 'floor') | int if sec_ts > 0 else '—' -%}
          | {% if item.image_url %}<a href="{{ item.image_url }}" target="_blank" title="View full photo"><img src="{{ item.image_url }}" width="32" height="32"/></a>{% else %}<img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ctext y='24' font-size='22'%3E🍽️%3C/text%3E%3C/svg%3E" width="32" height="32"/>{% endif %} | **{{ item.name }}**{% if item.image_url %} <a href="{{ item.image_url }}" target="_blank" title="View full photo">🔎</a>{% endif %}{% if item.expiration_date %}<br/><small>⏳ Exp: {{ item.expiration_date }}</small>{% endif %} | {{ days_old }} |
          {% endfor %}

      # Right Column: ~50% Wider Stacked Door Cameras
      - type: vertical-stack
        cards:
          - type: picture-entity
            title: 🚪 Left Door Shelves
            entity: camera.family_hub_top
            camera_view: live
            aspect_ratio: '5:6'
            show_state: false

          - type: picture-entity
            title: 🚪 Right Door Shelves
            entity: camera.family_hub_middle
            camera_view: live
            aspect_ratio: '5:6'
            show_state: false
```

#### 2. Visual Photo Grid (Markdown Card)

```yaml
type: markdown
title: 🍎 Fridge Food Gallery
content: >
  <div style="display: flex; flex-wrap: wrap; gap: 12px; justify-content: center;">
  {% for item in state_attr('sensor.fridge_food_inventory', 'items') or [] %}
    <div style="text-align: center; width: 85px; background: rgba(255,255,255,0.05); padding: 8px; border-radius: 10px;">
      {% if item.image_url %}
        <img src="{{ item.image_url }}" width="60" height="60" style="border-radius: 8px; object-fit: cover;"/>
      {% else %}
        <div style="font-size: 32px; height: 60px; line-height: 60px;">🍽️</div>
      {% endif %}
      <div style="font-size: 11px; font-weight: bold; margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">{{ item.name }}</div>
    </div>
  {% endfor %}
  </div>
```

---

## 🔧 Troubleshooting

### Camera Images Missing / Dead Links (`HTTP 404 - not-found-fileId`)
If camera snapshots fail to load or Home Assistant logs `not-found-fileId` (and the official SmartThings mobile app also cannot load the pictures):
* **Remote Fix (No physical access needed)**: In your network manager (e.g. UniFi Network, router admin), **force a Wi-Fi reconnect** for the refrigerator. Even if the fridge responds to local ICMP pings, forcing a Wi-Fi reconnect drops stale TCP/TLS cloud upload sockets on the fridge's Tizen OS and triggers a fresh connection to Samsung's media upload server, restoring camera images immediately.
* **On-Site Fix**: Reboot the Family Hub display using the red rocker switch behind the top-right door hinge cover, or go to **Settings ➔ About Family Hub ➔ Restart**.

---

Credits
-------

This integration was developed by [ibielopolskyi][ibielopolskyi].<br/>
HACS integration was added by [CurryPlayer][curryplayer].<br/>
Special thanks to [HalloTschuess][hallotschuess], [TryTryAgain][trytryagain], and [Gemini 3.8 Flash][gemini].<br/>

[ibielopolskyi]: https://github.com/ibielopolskyi
[curryplayer]: https://github.com/CurryPlayer
[hallotschuess]: https://github.com/HalloTschuess
[trytryagain]: https://github.com/TryTryAgain
[gemini]: https://deepmind.google/technologies/gemini/
