#!/usr/bin/env python3
"""Create a clean API_ONLY SmartThings OAuth App using a Personal Access Token.

This script creates an OAuth 2.0 app with the exact required scopes and
redirect URI on SmartThings, and outputs the client_id and client_secret.

Usage:
    .venv/bin/python scripts/create_oauth_app.py --token YOUR_PAT
"""

import argparse
import sys
import uuid
import requests


def main():
    parser = argparse.ArgumentParser(description="Create a SmartThings OAuth-In App")
    parser.add_argument("--token", required=True, help="Your valid SmartThings PAT")
    parser.add_argument(
        "--redirect-uri",
        default="https://httpbin.org/get",
        help="OAuth redirect URI (default: https://httpbin.org/get)",
    )
    args = parser.parse_args()

    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }

    # Generate a unique appName slug
    unique_suffix = uuid.uuid4().hex[:6]
    app_name = f"familyhub-camera-{unique_suffix}"

    payload = {
        "appName": app_name,
        "displayName": "FamilyHub Fridge Camera",
        "description": "FamilyHub Fridge Camera Integration",
        "appType": "API_ONLY",
        "classifications": ["CONNECTED_SERVICE"],
        "oauth": {
            "clientName": "FamilyHub Fridge Camera",
            "scope": ["r:devices:*", "w:devices:*", "x:devices:*"],
            "redirectUris": [args.redirect_uri],
        },
    }

    print(f"Creating OAuth app '{app_name}' via SmartThings API...")
    r = requests.post("https://api.smartthings.com/v1/apps", headers=headers, json=payload)
    if not r.ok:
        print(f"ERROR: Failed to create app (HTTP {r.status_code}):\n{r.text}", file=sys.stderr)
        sys.exit(1)

    app_data = r.json()
    app_id = app_data.get("app", {}).get("appId") or app_data.get("appId")
    oauth_data = app_data.get("oauth", {})
    client_id = (
        app_data.get("oauthClientId")
        or oauth_data.get("clientId")
        or oauth_data.get("oauthClientId")
    )
    client_secret = (
        app_data.get("oauthClientSecret")
        or oauth_data.get("clientSecret")
        or oauth_data.get("oauthClientSecret")
    )

    # If secret wasn't returned in the create response, generate it
    if not client_secret and app_id:
        sec_r = requests.post(
            f"https://api.smartthings.com/v1/apps/{app_id}/oauth/generateClientSecret",
            headers=headers,
        )
        if sec_r.ok:
            client_secret = sec_r.json().get("oauthClientSecret")

    if not client_id or not client_secret:
        print(f"ERROR: Could not obtain client credentials:\n{app_data}", file=sys.stderr)
        sys.exit(1)

    print()
    print("=" * 70)
    print("  SUCCESS: SmartThings OAuth App Created")
    print("=" * 70)
    print(f"  App Name:      {app_name}")
    print(f"  Client ID:     {client_id}")
    print(f"  Client Secret: {client_secret}")
    print(f"  Redirect URI:  {args.redirect_uri}")
    print("=" * 70)
    print()
    print("Now run get_token.py to complete authorization:")
    print(
        f"  .venv/bin/python scripts/get_token.py --client-id \"{client_id}\" "
        f"--client-secret \"{client_secret}\""
    )
    print()


if __name__ == "__main__":
    main()
