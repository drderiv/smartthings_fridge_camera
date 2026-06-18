# 🚀 Feature Enhancements & Incremental Capabilities

This document details the major functional enhancements, architectural upgrades, and incremental capabilities introduced in this fork relative to the parent [`TryTryAgain/smartthings_fridge_camera`](https://github.com/TryTryAgain/smartthings_fridge_camera) repository (which itself built upon the original foundation by [ibielopolskyi](https://github.com/ibielopolskyi/smartthings_fridge_camera)).

---

## 📋 Executive Summary of Changes

| Area | Fork Parent ([TryTryAgain](https://github.com/TryTryAgain/smartthings_fridge_camera)) | This Enhanced Version |
| :--- | :--- | :--- |
| **Food Inventory Tracking** | ❌ Not supported | ✅ **AI Food Manager (`sensor.fridge_food_inventory`)** with names, stock photo thumbnails, locations, and expiration dates |
| **Inventory Sync Trigger** | ❌ None | ✅ **Automatic door-close triggered sync** with cloud commit delay + 10-minute periodic polling |
| **PAT Lifetime & Rotation** | ⚠️ Expired after 24h (broke PAT mode) | ✅ **Automated 23-hour PAT rotator microservice** with local REST server and zero-downtime updates |
| **Dynamic Token Reload** | ❌ Required manual re-auth / reload | ✅ **Dynamic listeners** on `input_text.smartthings_pat` and `input_text.samsung_food_token` (zero restarts) |
| **Async Architecture** | ⚠️ Synchronous file reads on startup | ✅ Fully non-blocking async executor dispatch and resilient in-memory caching |
| **Dashboard UI** | 📷 Basic camera entity cards | 🥗 **Pixel-perfect 2-column Dashboard Template** (Food table + aspect-ratio stacked door cameras) |

---

## 🥗 1. AI Food Manager & Inventory Tracking

For Samsung Family Hub refrigerators equipped with the internal AI food vision camera, this version adds a full inventory tracking platform without adding individual entity clutter.

* **Primary Sensor**: [`sensor.fridge_food_inventory`](custom_components/samsung_familyhub_fridge/sensor.py)
  * **State**: Active food item count (e.g. `33`).
  * **Attributes**:
    * `total_items`: Total count of active items currently in the fridge.
    * `last_synced`: ISO timestamp of the last *successful* cloud synchronization.
    * `items`: Rich array of item objects including `name`, `location`, `expiration_date`, `added_at` (epoch timestamp), `image_url` (high-resolution FoodDB catalog stock photography), and `ai_suggested_names`.
* **Door-Close Triggered Sync**:
  * Connected in [`custom_components/samsung_familyhub_fridge/api.py`](custom_components/samsung_familyhub_fridge/api.py): When door contact sensors close and camera snapshot file IDs refresh, a delayed (~12s) background refresh is triggered to allow Samsung's cloud vision models to commit newly detected items.
* **Resilient In-Memory Caching**:
  * Implemented in [`SamsungFoodClient._fetch_food_items_sync`](custom_components/samsung_familyhub_fridge/api.py): If a transient network timeout or connection error occurs, existing inventory items are **preserved** in memory rather than wiped to zero, and `last_synced` accurately reflects the timestamp of the last successful sync.
* **Graceful Opt-In (Zero Clutter for Non-AI Fridges)**:
  * If no `samsung_food_token.txt` or `input_text.samsung_food_token` is provided, the food inventory sensor and background polling tasks are completely skipped, keeping non-AI fridge instances 100% lightweight and clutter-free.
* **Manual Refresh Service**:
  * Added `samsung_familyhub_fridge.refresh_food_inventory` in [`services.yaml`](custom_components/samsung_familyhub_fridge/services.yaml) and [`__init__.py`](custom_components/samsung_familyhub_fridge/__init__.py) for on-demand cloud synchronization.

---

## 🔑 2. Automated SmartThings PAT Rotation Microservice

On December 30, 2024, Samsung deprecated indefinite SmartThings Personal Access Tokens (PATs) and enforced a strict 24-hour expiration limit, breaking legacy PAT-based integrations.

* **Standalone Portable Package** (intended to be run on a Linux server outside of Home Assistant): Located in [`tools/pat_rotator/`](tools/pat_rotator/)
  * **Automated Headless Renewal**: [`tools/pat_rotator/generate_pat.py`](tools/pat_rotator/generate_pat.py) uses Playwright browser automation with saved session cookies (`smartthings_session.json`) to log into `account.smartthings.com/tokens` and mint fresh 24-hour tokens with full device scopes every 23 hours.
  * **Built-in REST Microservice**: [`tools/pat_rotator/main.py`](tools/pat_rotator/main.py) hosts a lightweight HTTP server on port `8765` serving `GET http://<SERVER_IP>:8765/pat`.
  * **Built-in File Logging**: Direct writes to `rotator.log` (no shell redirection needed).
  * **Automated Weekly Maintenance**: Background thread prunes `rotator.log` every **Sunday at midnight** (00:00:00) down to the most recent 100 lines.
  * **Automated Setup**: [`tools/pat_rotator/setup.sh`](tools/pat_rotator/setup.sh) provides 1-command virtual environment creation, dependency installation, and Playwright Chromium setup.

---

## ⚡ 3. Dynamic Zero-Restart Token Synchronization

* **Dynamic State Listeners**:
  * Implemented in [`custom_components/samsung_familyhub_fridge/api.py`](custom_components/samsung_familyhub_fridge/api.py): Listens for state changes on `input_text.smartthings_pat` and `input_text.samsung_food_token`.
  * When a new PAT is fetched by the Home Assistant REST sensor automation, the integration updates the config entry on disk and reloads the API client dynamically without requiring Home Assistant reboots.
* **Non-Blocking Async Event Loop**:
  * Offloads all file reads (`samsung_food_token.txt`, `smartthings_pat.txt`) and synchronous HTTP calls to Home Assistant's threadpool executor via `hass.async_add_executor_job()`, eliminating `Detected blocking call inside event loop` warnings.

---

## 🔐 4. Multi-Mode Authentication & Modern OAuth2 Support

* **Flexible Auth Modes** in [`custom_components/samsung_familyhub_fridge/config_flow.py`](custom_components/samsung_familyhub_fridge/config_flow.py) & [`__init__.py`](custom_components/samsung_familyhub_fridge/__init__.py):
  1. `oauth`: Reuses credentials from Home Assistant core's built-in `smartthings` integration via `config_entry_oauth2_flow`.
  2. `standalone_oauth`: Direct SmartThings Developer OAuth2 credentials with automatic refresh token persistence.
  3. `pat`: Legacy / rotated Personal Access Token support with auto-reload.

---

## 🛠️ 5. Samsung Food Token Extractor Utility

* **Script**: [`scripts/dump_samsung_food.py`](scripts/dump_samsung_food.py)
  * Playwright utility to authenticate with Samsung SSO / Samsung Food (Whisk).
  * Captures long-lived Bearer tokens and stores them in `samsung_food_token.txt`.
  * Handles cursor-based multi-page pagination (`paging.cursors.after`) across hundreds of account food items.
  * Automatic reCAPTCHA detection with fallback logging.

---

## 📊 6. Production Dashboard Card Templates

* Documented in [`README.md`](README.md#dashboard-card-templates):
  * **2-Column Balanced 50/50 Layout**: Places the responsive Food Inventory table side-by-side with stacked door camera feeds.
  * **Relative Elapsed Age ("Days Old")**: Automatically computes elapsed days from epoch timestamps (`(now() - added_at) / 86400`).
  * **Fixed Column Widths & Alignments**: Locks Food Name to 160px and Days Old to 75px with vertical/horizontal flex centering across all headers and rows.
  * **Native Table Formatting**: Uses an SVG placeholder image fallback to force 100% pixel-perfect equal row heights whether an item has a catalog icon or not.
  * **Camera Aspect Ratio**: Standardizes door camera feeds to `5:6` aspect ratio for clean visual stacking.

---

## 📁 Key File Index

* [`custom_components/samsung_familyhub_fridge/api.py`](custom_components/samsung_familyhub_fridge/api.py) — Core coordinator, `SamsungFoodClient`, `SamsungFoodCoordinator`, dynamic state listeners, and caching.
* [`custom_components/samsung_familyhub_fridge/sensor.py`](custom_components/samsung_familyhub_fridge/sensor.py) — `SamsungFridgeFoodInventorySensor` platform setup and opt-in validation.
* [`custom_components/samsung_familyhub_fridge/const.py`](custom_components/samsung_familyhub_fridge/const.py) — Constants and endpoint configurations.
* [`custom_components/samsung_familyhub_fridge/services.yaml`](custom_components/samsung_familyhub_fridge/services.yaml) — Integration service declarations.
* [`tools/pat_rotator/main.py`](tools/pat_rotator/main.py) — Standalone PAT rotator service with file logging and weekly Sunday cleanup.
* [`tools/pat_rotator/generate_pat.py`](tools/pat_rotator/generate_pat.py) — Playwright PAT automation generator.
* [`scripts/dump_samsung_food.py`](scripts/dump_samsung_food.py) — Samsung Food API authentication and token extractor.
* [`README.md`](README.md) — Main installation, configuration, and dashboard documentation.

---

## 📌 To-Do / Planned Investigations

- [ ] **Confirm Cloud vs. Local Storage for AI Bounding-Box Thumbnails**:
  * Perform an isolation experiment (disconnect refrigerator from Wi-Fi, clear SmartThings mobile app storage/cache, and test if AI Food Manager thumbnails load over cellular) to definitively confirm whether the on-device top-down camera crops are stored in Samsung Cloud or strictly served from the refrigerator hardware's local Tizen memory.
  * If cloud storage endpoints exist, investigate their retrieval for Home Assistant.

