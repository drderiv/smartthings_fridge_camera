# Upgrading from v1.x to v2.0.0

v2.0.0 changes how the integration gets its tokens and how it is built. Most of it is invisible once it is running, but there are a few things to do, in a sensible order. The full list of changes is in [BREAKING_CHANGES.md](BREAKING_CHANGES.md).

## What you need to know first

* **Home Assistant 2024.11.0 or newer** is required.
* **Tokens now come from the token rotator only.** The `input_text` helpers, REST sensors and token files that v1.0/1.1 read are no longer used. If you ran the rotator (`tools/pat_rotator`), keep running it; if you pasted a PAT by hand, it still works but expires after 24 hours (see *Without the rotator* below).
* **The rotator now requires an API key** whenever it listens on anything other than its own machine, and defaults to listening on `127.0.0.1`. If Home Assistant runs on a different machine from the rotator, you must set `bind_address` and `api_key` in the rotator's `config.json`, and enter the same key in the integration.
* **Your entity IDs do not change.** Friendly names gain a "Family Hub" prefix and everything is grouped under a **Family Hub** device. New installs (and anyone who removes and re-adds the integration) get slightly different IDs for the two sensors; see BREAKING_CHANGES.md.
* The status poll is now **every 60 seconds** by default (it was 10), and is configurable.

## Before you start

1. Make a Home Assistant backup (Settings → System → Backups).
2. Note your rotator's address and, if you have one, its `api_key`.
3. Check that Home Assistant is 2024.11.0 or newer (Settings → About).

## Upgrade, in this order

**1. Update the integration**

* *HACS:* open HACS, update **Samsung FamilyHub Fridge**, then restart Home Assistant.
* *Manual:* replace `custom_components/samsung_familyhub_fridge` with the v2.0.0 folder, then restart Home Assistant. Keep the old folder somewhere **outside** `custom_components` in case you want to roll back.

For a minute or two after the restart the entities may be unavailable; that is normal until the rotator is updated.

**2. Update the rotator** (skip if you do not use one)

On the machine running the rotator:

```bash
cd <your rotator folder>
# replace the files with the v2.0.0 tools/pat_rotator folder, keeping config.json,
# smartthings_session.json and the *token*.txt files
./setup.sh                      # creates .venv and installs Playwright, Chromium and requests
```

Then add to `config.json`:

```json
"bind_address": "<this machine's address that Home Assistant reaches>",
"api_key": "<a long random string>"
```

(`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` makes a good key.) Use `127.0.0.1` as the bind address only if Home Assistant runs on the same machine, in which case no key is required. Start it with `.venv/bin/python main.py`. Always run the rotator's scripts with `.venv/bin/python`, never with the system Python, or Playwright will be missing.

**3. Tell the integration where the rotator is**

Settings → Devices & Services → **Samsung FamilyHub** → **Configure**: set **Rotator URL** (for example `http://10.0.0.5:8765`) and **Rotator API key**, then Submit. The integration reloads by itself.

**4. Clean up (optional)**

Delete the `input_text` helpers (`smartthings_pat`, `samsung_food_token`, `kics_food_token`, `kics_location_id`, `pat_rotator_url`), the REST sensors and the automations that fed them, and any token `.txt` files in your config folder. They are no longer read, and they stored your tokens in the recorder history.

## If Home Assistant asks for a Personal Access Token

That dialog means the integration could not get a working token from the rotator. It already asked the rotator (and tried the token it offered) before showing you the dialog. The usual causes:

* **The rotator is not reachable**: wrong URL, the API key does not match, or the rotator is not running. Check `rotator.log`; a `Rejected unauthenticated request` line means the key is missing or wrong in the integration.
* **The rotator is serving an expired token** because its own rotation failed (look for `rotation failed` or a pause message in `rotator.log`). Run `.venv/bin/python generate_pat.py` on the rotator machine; when it prints `SUCCESS` the rotator and Home Assistant recover on their own.
* **You do not use a rotator**: paste a new PAT. It lasts 24 hours.

When the rotator is reachable but its token is rejected, v2.0.0 does **not** ask you straight away: the entities go unavailable, the integration keeps checking, and it recovers by itself when the rotator renews the token. It only asks you to re-authenticate after about three hours.

## Without the rotator

Everything works with a hand-pasted PAT, but SmartThings expires it after 24 hours, so you would have to paste a new one daily. The rotator exists to avoid that. (Standalone OAuth, which needs your own SmartThings app, avoids it without a rotator but is harder to set up.)

## Rolling back

Stop using v2.0.0 by putting the old integration folder back into `custom_components`, restoring your Home Assistant backup if the config entry misbehaves, and restarting. The old rotator also still works with the old integration.

## Quick checks after upgrading

* The cameras update and `Last updated at` shows a recent time.
* The food inventory sensor lists your items.
* **Download diagnostics** on the integration page (tokens are redacted) if you need to ask for help.
