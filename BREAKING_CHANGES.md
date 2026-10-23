# Breaking changes — v2.0.0 (draft, in progress)

Running log of changes that need user action when upgrading from v1.x. Add an entry whenever a change alters behavior, configuration, or stored data.

## Requirements

- **Home Assistant 2024.11.0 or newer** (`hacs.json` previously said 2024.1.0, but the config flow already needed 2024.11 APIs such as `_get_reauth_entry`). The integration now also relies on `OptionsFlow.config_entry`, typed `entry.runtime_data` and Python 3.12 syntax. It is developed and tested against the current Home Assistant release only; older versions between 2024.11 and today are untested.

## Token rotator (`tools/pat_rotator`)

- **Listens on `127.0.0.1` by default** (it used to listen on all interfaces). If Home Assistant runs on another machine, add `"bind_address": "<this machine's LAN/VPN IP>"` (or `"0.0.0.0"`) to the rotator's `config.json`.
- **API key required for non-loopback binds.** Add `"api_key": "<long random string>"` to `config.json` (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`); the rotator refuses to start without one when `bind_address` isn't loopback. Enter the same value in Home Assistant under **Settings → Devices & Services → Samsung FamilyHub → Configure → Rotator API key**.
  - Upgrade order: update the integration and set the key first (the header is harmless to an old rotator), then update the rotator and restart it.
- **`/tokens` no longer repeats each token under a second name.** Removed duplicate keys `pat`, `food_token` and `kics_food_token` (use `smartthings_pat`, `samsung_food_token`, `kics_token`), and the unused endpoint aliases `/food_circles_token` and `/location`. Anything of yours that reads the old names (the integration from v1.1 on already reads the new ones first) must switch.
- `/health` now returns only `{"status": "healthy"}` (token availability flags removed).
- Failure screenshots / HTML dumps are **off by default**; set `PAT_ROTATOR_KEEP_DEBUG=1` to enable. They are written next to the scripts, no longer to `/tmp`. The 2FA prompt screenshot moved from `/tmp/samsung_2fa_prompt.png` to the rotator folder.
- `requirements.txt` now lists `requests`; re-run `setup.sh` (or `pip install -r requirements.txt`).
- Token, session and debug files are written with owner-only (0600) permissions. Existing files keep their old permissions until rewritten: run `chmod 600 config.json smartthings_session.json *.txt` once.

## Token rotator — rewritten

`tools/pat_rotator` was rewritten as a small package (`rotator/`) with the same HTTP endpoints, `config.json` keys and file names, so an existing deployment's session cookies, tokens and refresh token carry over. Behavior differences:

- **Start command unchanged** (`python main.py`); the three `generate_*.py` scripts remain as thin wrappers (same as `main.py pat|food|kics`). Importing functions from the old modules no longer works.
- **One browser at a time.** The PAT, Food and KICS jobs no longer launch Chromium concurrently or race on the shared session file.
- **Rotation schedule follows the token file's age**, so a token refreshed by hand is picked up immediately. After Samsung demands 2FA/CAPTCHA, automatic attempts pause until the token file changes (no more retries every 10 minutes).
- **New optional `config.json` keys:** `headless` (default `true`) and `session_keepalive_minutes` (default `0`, off); `python main.py session` reports whether the saved SmartThings session is still signed in.
- **Failed rotations back off** (10 min, 20 min, 40 min ... capped at 4 h) instead of retrying every 10 minutes forever, so a changed Samsung login page can't trigger dozens of automated logins overnight.
- **`next_rotation_at` honors your configured intervals** (the PAT value used to be fixed at 23 h regardless of `rotation_interval_hours`).
- **`/food_token` no longer falls back to the KICS token** (it used to serve one for the other). `/kics_token` still falls back to the old `active_bearer_token.txt` name.
- **Samsung Food token is only saved if Whisk accepts it.** Previously an unvalidated "first captured" value could be written.
- **Log file**: `rotator.log` now rotates at 256 KB (keeps `rotator.log.1`) instead of being trimmed to 100 lines every Sunday.
- PAT names are timestamped (`ha-fridge-camera-YYYYMMDD-HHMMSS`).
- CLI: `generate_food_token.py` takes `--no-headless` (the old `--headless` flag was a no-op). `generate_kics_token.py` accepts the same flags as before.
- `scripts/create_oauth_app.py` was removed (unused helper for the Standalone OAuth setup).

## Integration — image links

- Food-circle image URLs are signed with a new random per-installation secret (stored in `.storage/samsung_familyhub_fridge.view_secret`) instead of the config entry ID. Existing `?sig=` URLs stop working and are replaced at the next food sync (≤10 min). The `authSig` query parameter is no longer accepted (it used to grant access with any value).
- Image filenames must match `[A-Za-z0-9_-]{1,64}.jpg`; `inventory.json` is no longer servable.

## Integration — token and location sources removed

The rotator (set under **Configure → Rotator URL / API key**) is now the single source for tokens. Removed:

- **`input_text` helpers**: `input_text.smartthings_pat`, `input_text.samsung_food_token`, `input_text.kics_food_token`, `input_text.kics_location_id` and `input_text.pat_rotator_url` are no longer read, listened to, or written. The matching REST sensors/automations from the v1.0.x setup can be deleted. (They also exposed live tokens to the recorder database.)
- **Token file scanning**: `samsung_food_token.txt`, `kics_food_token.txt` and `active_bearer_token.txt` are no longer searched for under the config dir, `/config`, `/tmp`, the integration folder, the repo `scripts/` folder, or `~/smartthings_pat_rotator`.
- **Location ID fallbacks**: `sensor.kics_food_token` / `sensor.samsung_food_token` attributes, the "device whose name contains 'hub'" guess, and `config.json` file reads are gone. New order: *Configure → Fridge Location ID* override → SmartThings device record → rotator `/location_id`. The integration no longer writes into `input_text.kics_location_id`.
- The legacy `www/samsung_familyhub/food_circles/inventory.json` fallback is gone (the cache lives in `.storage/samsung_familyhub/food_circles/`).

New (non-breaking): last-known KICS/Whisk tokens and the location ID are cached in `.storage/samsung_familyhub_fridge.token_cache` so a brief rotator outage during an HA restart doesn't stop the food sensor.

## Integration — polling

- **SmartThings status is now polled every 60 seconds by default** (it used to be every 10 s, hard-coded). Set **Configure → Fridge status poll interval** (10–3600 s) to change it. A longer interval means door-close detection, and therefore the post-close camera refresh, can lag by up to that long.
- **HTTP 429 (rate limited)** is handled quietly: the poll is skipped and one warning is logged per episode, instead of an error on every poll. Several Home Assistants sharing one token can trip the limit.

## Integration — structure and behavior

**Entities now belong to a "Family Hub" device and use translated names.**

- **Existing installs:** entity IDs and unique IDs are unchanged, so dashboards and automations keep working. Friendly names gain the device prefix ("Family Hub Top", "Family Hub Food inventory", ...). The entities are grouped under a new **Family Hub** device.
- **New installs (and anyone who removes and re-adds the integration):** IDs are `camera.family_hub_top`, `camera.family_hub_middle`, `camera.family_hub_bottom`, `sensor.family_hub_last_updated_at` and `sensor.family_hub_food_inventory`. The two sensors have different IDs from older installs (`sensor.last_updated_at`, `sensor.fridge_food_inventory`).
- **The README dashboard templates use the new-install IDs.** If your install shows the old IDs, replace `sensor.family_hub_food_inventory` with `sensor.fridge_food_inventory` in them (or rename the entity under Settings → Devices & Services → Entities).

Other changes:

- **Camera images appear within seconds of startup and about a minute after a door closes.** Each poll now reads the fridge status first, downloads images whenever their file IDs changed, and sends the camera-refresh command in the same pass. Before, those steps took turns across polls (two to three poll intervals), which got much slower once polling became 60 s.
- **Setup waits for the first poll.** A bad token or unreachable SmartThings now makes the entry retry (or ask for re-authentication) instead of loading with nothing working. Failed polls make entities unavailable and are logged once, instead of being retried silently for ten polls.
- **Polling no longer depends on the "Last Updated At" sensor.** The cameras are coordinator-driven too, so disabling that sensor no longer freezes the camera images. A camera is *unavailable* until its first image has been downloaded.
- **Services** (`refresh`, `refresh_food_inventory`) are registered once for the integration (they used to be re-registered per entry and bound to the last-loaded fridge), accept an optional `config_entry_id`, and raise translated errors (`auth_failed`, `rate_limited`, `refresh_failed`, `no_food_inventory`, `no_loaded_entry`).
- **Token updates no longer reload the integration.** Saving a rotated PAT or OAuth refresh token used to trigger a full reload (and, in Standalone OAuth mode, burn the single-use refresh token again). Only *option* changes reload now.
- **Standalone OAuth: the access token renews itself** shortly before it expires. Previously it was refreshed only at startup, so it stopped working after about 24 hours and asked for re-authorization. The rotated refresh token is saved after every renewal, and a rejected refresh token starts re-authentication.
- **Samsung IoT token refresh no longer touches the config entry from a worker thread.**
- **Re-adding the same fridge is refused** (`already_configured`) when its device ID is known. New PAT entries now resolve and store the device ID during setup.
- **Diagnostics** are available (token, OAuth credentials, rotator URL/key and IDs are redacted).
- The food inventory sensor's `items` attribute is **no longer stored in the recorder database** (it is still in the live state, so templates and cards are unaffected).
- The removed "borrowed SmartThings OAuth" mode is no longer offered in the setup menu or translations; existing entries using it show a re-authentication prompt.
- Internals (only matters if you import them): `DataCoordinator` is now `coordinator.FridgeCoordinator`; `SamsungFoodClient`/`SamsungFoodCoordinator` moved to `food.py`; `FamilyHub` takes `(token, device_id)` with no `hass`; `hass.data[DOMAIN]["hub"]` is gone (use `entry.runtime_data`).

## Integration — HTTP client

- The integration now uses Home Assistant's shared `aiohttp` session for every request (SmartThings, Samsung, the rotator, Samsung Food/KICS and the image downloads) instead of `requests` in worker threads. No configuration changes. Practical effects: food-circle images are downloaded a few at a time, and the integration no longer holds worker threads during slow requests.
- Internals (only matters if you import them): `FamilyHub(session, token, device_id)`, its methods and `SmartThingsOAuth`/`SamsungAccountAuth`/`refresh_samsung_iot_token` are now coroutines that take an `aiohttp.ClientSession`. `scripts/get_token.py` and `scripts/get_samsung_iot_token.py` were updated accordingly (`scripts/get_samsung_iot_token.py` also had missing imports).

## Log hygiene

- Debug-level details only: location IDs are no longer logged at INFO; response bodies are no longer included in warnings/exception messages.

---

_Still to come (from CODE_REVIEW.md): C-2/C-3/C-4 auth lifecycle, entity/device changes (S-5, may change entity IDs), services (S-2). Add entries here as they land._
