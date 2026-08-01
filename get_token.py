#!/usr/bin/env python3
"""
=============================================================================
FLATTRADE DAILY TOKEN GENERATOR
-----------------------------------------------------------------------------
Flattrade session tokens (api_token / susertoken) expire every 24 hours.

This helper script:
1. Reads user_id, api_key, and api_secret from trader_config.json.
2. Opens the Flattrade Auth URL in your default web browser automatically.
3. Accepts the redirected URL you paste after logging in.
4. Extracts the request code, exchanges it with Flattrade for a session token.
5. Automatically updates 'api_token' in trader_config.json.

Usage
-----
    python get_token.py
=============================================================================
"""

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "trader_config.json"


def main():
    if not CONFIG_FILE.exists():
        print(f"[ERROR] {CONFIG_FILE} not found. Run trader.py first to generate config.")
        sys.exit(1)

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    user_id = cfg.get("user_id", "").strip()
    api_key = cfg.get("api_key", "").strip() or user_id
    api_secret = cfg.get("api_secret", "").strip()

    print("=" * 65)
    print("  FLATTRADE DAILY TOKEN GENERATOR")
    print("=" * 65)

    if not user_id or user_id == "YOUR_USER_ID":
        user_id = input("Enter your Flattrade User ID: ").strip()
        cfg["user_id"] = user_id
        api_key = user_id

    auth_url = f"https://auth.flattrade.in/?app_key={api_key}"
    print(f"\n1. Opening login URL in your web browser:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    print("2. Log in with your Flattrade User ID, Password, and OTP / 2FA.")
    print("3. After logging in, copy the FULL URL from your browser address bar.")
    print("   (It will look like: https://127.0.0.1/?code=XXXXXX&client_id=...)\n")

    redirected_input = input("Paste the redirected URL (or code): ").strip()

    if not redirected_input:
        print("[ERROR] No URL or code entered.")
        sys.exit(1)

    code = redirected_input

    # If full URL pasted, extract code or token
    if "http://" in redirected_input or "https://" in redirected_input or "?" in redirected_input:
        parsed = urllib.parse.urlparse(redirected_input)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            code = params["code"][0]
        elif "token" in params:
            token = params["token"][0]
            print(f"\n[SUCCESS] Extracted token directly from URL: {token[:10]}...")
            cfg["api_token"] = token
            CONFIG_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            print(f"[SUCCESS] Saved 'api_token' in {CONFIG_FILE}")
            return

    code = code.strip()
    print(f"\nExtracted Request Code: {code}")

    # Check if api_secret exists for 2-step token exchange
    if not api_secret or api_secret == "YOUR_API_SECRET":
        api_secret = input("\nEnter your Flattrade API Secret (or press Enter if using code directly): ").strip()
        if api_secret:
            cfg["api_secret"] = api_secret

    if api_secret:
        print("Exchanging request code for session token via Flattrade API...")
        raw_str = api_key + code + api_secret
        api_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

        token_url = "https://piconnect.flattrade.in/NorenWClientTP/Token"
        payload = json.dumps({
            "api_key": api_key,
            "request_code": code,
            "api_secret": api_hash
        }).encode("utf-8")

        req = urllib.request.Request(token_url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                if res_data.get("stat") == "Ok" and res_data.get("token"):
                    token = res_data["token"]
                    print(f"\n[SUCCESS] Session token generated: {token[:10]}...")
                    cfg["api_token"] = token
                    CONFIG_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
                    print(f"[SUCCESS] Saved new 'api_token' to {CONFIG_FILE}")
                    return
                else:
                    print(f"[WARN] Token exchange API returned: {res_data}")
        except Exception as exc:
            print(f"[WARN] API token exchange request failed: {exc}")

    # Fallback: store code directly as api_token
    cfg["api_token"] = code
    CONFIG_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    print(f"\n[SUCCESS] Saved 'api_token' to {CONFIG_FILE}")


if __name__ == "__main__":
    main()
