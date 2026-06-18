#!/usr/bin/env python3
"""SmartThings FamilyHub Fridge API Inspector & AI Food Manager Explorer.

Queries the SmartThings REST API using your current PAT to extract:
1. Full Device Definition (all components & capabilities, e.g. foodManager, viewInside)
2. Complete Device Status (all components & attributes)
3. Full Client Device Status (/devices/status)

Saves the complete raw JSON to /tmp/smartthings_fridge_full_dump.json and
prints an analysis of all food manager, camera, and item data discovered.
"""

import os
import sys
import json
import logging
import argparse
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger("fridge_inspector")


def get_token(custom_token: str = "") -> str:
    """Finds the active PAT from args, rotator server, or local files."""
    if custom_token:
        return custom_token.strip()

    # 1. Try local REST rotator server
    try:
        r = requests.get("http://127.0.0.1:8765/pat", timeout=3)
        if r.ok:
            data = r.json()
            if "token" in data and data["token"]:
                _LOGGER.info("Retrieved active PAT from local REST service (port 8765)")
                return data["token"].strip()
    except Exception:
        pass

    # 2. Try common token file locations
    paths = [
        "smartthings_pat.txt",
        "../smartthings_pat_rotator/smartthings_pat.txt",
        os.path.expanduser("~/.smartthings_pat"),
        os.path.expanduser("~/smartthings_pat_rotator/smartthings_pat.txt"),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, "r") as f:
                token = f.read().strip()
                if token:
                    _LOGGER.info("Loaded PAT from %s", p)
                    return token

    return ""


def inspect_fridge(token: str, output_file: str = "/tmp/smartthings_fridge_full_dump.json"):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.smartthings+json;v=1",
    }

    _LOGGER.info("Fetching device list from https://api.smartthings.com/v1/devices ...")
    r = requests.get("https://api.smartthings.com/v1/devices", headers=headers, timeout=15)
    if not r.ok:
        _LOGGER.error("Failed to fetch devices: HTTP %s - %s", r.status_code, r.text)
        sys.exit(1)

    devices_data = r.json()
    items = devices_data.get("items", [])
    _LOGGER.info("Found %d total device(s) on your SmartThings account.", len(items))

    dump_data = {
        "devices_list": items,
        "device_details": {},
        "device_status_full": {},
        "client_devices_status": {},
    }

    # Also fetch client.smartthings.com status endpoint
    try:
        r_client = requests.get("https://client.smartthings.com/devices/status", headers=headers, timeout=15)
        if r_client.ok:
            dump_data["client_devices_status"] = r_client.json()
    except Exception as err:
        _LOGGER.warning("Could not fetch client status endpoint: %s", err)

    # Find fridge devices
    fridge_devices = []
    for d in items:
        dev_id = d.get("deviceId")
        name = d.get("name", "")
        label = d.get("label", "")
        cat = str(d.get("components", []))
        
        # Check if it looks like a refrigerator / Family Hub
        if any(k in (name + label + cat).lower() for k in ["fridge", "refrigerator", "familyhub", "samsungce.", "viewinside"]):
            fridge_devices.append(d)
        else:
            # Also keep any device with samsungce capabilities
            for comp in d.get("components", []):
                for cap in comp.get("capabilities", []):
                    if "samsungce." in cap.get("id", ""):
                        fridge_devices.append(d)
                        break

    if not fridge_devices:
        _LOGGER.warning("No explicit fridge device matched filters; querying all %d devices...", len(items))
        fridge_devices = items

    for f_dev in fridge_devices:
        dev_id = f_dev.get("deviceId")
        label = f_dev.get("label") or f_dev.get("name") or dev_id
        _LOGGER.info("Inspecting device: %s (%s)", label, dev_id)

        # 1. Full device schema (components, capabilities)
        try:
            r_det = requests.get(f"https://api.smartthings.com/v1/devices/{dev_id}", headers=headers, timeout=15)
            if r_det.ok:
                dump_data["device_details"][dev_id] = r_det.json()
        except Exception as e:
            _LOGGER.error("Failed to get details for %s: %e", dev_id, e)

        # 2. Full device status across all components
        try:
            r_stat = requests.get(f"https://api.smartthings.com/v1/devices/{dev_id}/status", headers=headers, timeout=15)
            if r_stat.ok:
                dump_data["device_status_full"][dev_id] = r_stat.json()
        except Exception as e:
            _LOGGER.error("Failed to get status for %s: %e", dev_id, e)

    # Save full dump
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(dump_data, f, indent=2)

    _LOGGER.info("==========================================================")
    _LOGGER.info(" Complete JSON dump saved to: %s", output_file)
    _LOGGER.info("==========================================================")

    # Perform analysis
    print()
    print("=" * 60)
    print("  SMARTTHINGS FAMILY HUB CAPABILITIES ANALYSIS")
    print("=" * 60)

    for dev_id, details in dump_data["device_details"].items():
        name = details.get("label") or details.get("name")
        print(f"\nDevice: {name} (ID: {dev_id})")
        print("Components & Capabilities:")
        for comp in details.get("components", []):
            comp_id = comp.get("id")
            caps = [c.get("id") for c in comp.get("capabilities", [])]
            print(f"  - Component: [{comp_id}]")
            for cap in caps:
                highlight = "  <-- RELEVANT" if any(k in cap.lower() for k in ["food", "image", "inside", "view", "camera", "inventory"]) else ""
                print(f"      * {cap}{highlight}")

    print("\n" + "=" * 60)
    print("  STATUS DATA SCAN (Food / Images / Inventory)")
    print("=" * 60)

    for dev_id, status in dump_data["device_status_full"].items():
        comp_status = status.get("components", {})
        for comp_id, comp_data in comp_status.items():
            for cap_id, cap_data in comp_data.items():
                if any(k in cap_id.lower() for k in ["food", "image", "inside", "view", "camera", "inventory", "samsungce."]):
                    print(f"\n[Component: {comp_id}] [Capability: {cap_id}]:")
                    print(json.dumps(cap_data, indent=4))

    print("\n" + "=" * 60)
    print(f"Full raw data available at: {output_file}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="SmartThings FamilyHub Fridge API Inspector")
    parser.add_argument("--token", default="", help="SmartThings Personal Access Token")
    parser.add_argument(
        "--output",
        default="/tmp/smartthings_fridge_full_dump.json",
        help="Path to save full JSON dump (default: /tmp/smartthings_fridge_full_dump.json)",
    )
    args = parser.parse_args()

    token = get_token(args.token)
    if not token:
        print("ERROR: No SmartThings PAT found.")
        print("Please provide via --token or run the rotator service.")
        sys.exit(1)

    inspect_fridge(token, args.output)


if __name__ == "__main__":
    main()
