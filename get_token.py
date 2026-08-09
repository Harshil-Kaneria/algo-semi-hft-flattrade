#!/usr/bin/env python3
"""
=============================================================================
FLATTRADE DAILY TOKEN GENERATOR
-----------------------------------------------------------------------------
Flattrade uses the Noren OMS auth flow with SHA-256 token exchange.
Session tokens (susertoken) expire every 24 hours (midnight IST) and must be
refreshed each trading morning.

HOW FLATTRADE AUTH WORKS (from official Flattrade docs: https://pi.flattrade.in/docs):
  1. You create a Pi API App once at https://wall.flattrade.in/login
     → Pi Menu → Create New API Key
     → Set a Redirect URL (e.g. http://localhost/flatrade/ or http://localhost:8080)
     → Copy the generated API Key and API Secret.

  2. Every morning: open the login URL in browser:
         https://auth.flattrade.in/?app_key=<FLATTRADE_API_KEY>

  3. Log in with User ID + Password + OTP / 2FA.

  4. Browser redirects to your Redirect URL with:
         http://localhost/flatrade/?code=REQUEST_CODE&client=...
     The `code` in the URL is the ONE-TIME request_code.

  5. Exchange request_code for the daily session token:
     Compute SHA-256 hash of (api_key + request_code + api_secret), then
     POST to https://authapi.flattrade.in/trade/apitoken

  6. Save the returned `token` into .env as FLATTRADE_API_TOKEN.

This script automates steps 2–6.

Daily usage
-----------
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

ENV_FILE = Path(__file__).parent / ".env"
ENV_EXAMPLE = Path(__file__).parent / ".env.example"


def _read_env() -> dict:
    """Parse .env into a dict. Creates .env from .env.example if missing."""
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            import shutil
            shutil.copy(ENV_EXAMPLE, ENV_FILE)
            print(f"[INFO] Created .env from .env.example — please fill in "
                  f"FLATTRADE_USER_ID, FLATTRADE_API_KEY, and FLATTRADE_API_SECRET then re-run.")
            sys.exit(0)
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    env = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def _save_env(updates: dict) -> None:
    """
    Write updated key=value pairs into .env, preserving comments and existing
    lines. New keys are appended; existing keys are updated in-place.
    """
    existing = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    written: set = set()
    new_lines = []
    for raw in existing:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            new_lines.append(raw)
            continue
        key = line.partition("=")[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            new_lines.append(raw)
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def exchange_request_code_for_token(api_key: str, api_secret: str, request_code: str) -> str:
    """
    Exchange request_code for the daily session token (susertoken)
    via POST to https://authapi.flattrade.in/trade/apitoken using SHA-256 hash.
    """
    # Hash calculation per Flattrade spec: SHA-256(api_key + request_code + api_secret)
    raw_str = f"{api_key}{request_code}{api_secret}"
    api_secret_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

    payload = {
        "api_key": api_key,
        "request_code": request_code,
        "api_secret": api_secret_hash,
    }

    url = "https://authapi.flattrade.in/trade/apitoken"
    headers = {"Content-Type": "application/json"}
    body_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8")
            data = json.loads(resp_body)
            if data.get("stat") == "Ok" and "token" in data:
                return data["token"]
            else:
                emsg = data.get("emsg") or data.get("message") or resp_body
                print(f"\n[ERROR] Flattrade Token Exchange Failed: {emsg}")
                sys.exit(1)
    except Exception as exc:
        print(f"\n[ERROR] HTTP request to Flattrade auth API ({url}) failed: {exc}")
        sys.exit(1)


def main():
    print("=" * 65)
    print("  FLATTRADE DAILY TOKEN GENERATOR (Official API Flow)")
    print("=" * 65)

    env = _read_env()
    user_id = env.get("FLATTRADE_USER_ID", "").strip()
    api_key = env.get("FLATTRADE_API_KEY", "").strip()
    api_secret = env.get("FLATTRADE_API_SECRET", "").strip()

    # Prompt for missing required values
    if not user_id:
        user_id = input("\nEnter your Flattrade User ID (e.g. FZ21639): ").strip()
        if not user_id:
            print("[ERROR] User ID is required.")
            sys.exit(1)
        _save_env({"FLATTRADE_USER_ID": user_id})
        print(f"[INFO] Saved FLATTRADE_USER_ID to {ENV_FILE}")

    if not api_key:
        print("\n[INFO] FLATTRADE_API_KEY not set in .env.")
        print("  Create a Pi API app at: https://wall.flattrade.in/login")
        print("  → Pi Menu → Create New API Key → set Redirect URL → copy API Key")
        api_key = input("Enter your Pi API Key (app_key): ").strip()
        if not api_key:
            print("[ERROR] API Key is required.")
            sys.exit(1)
        _save_env({"FLATTRADE_API_KEY": api_key})
        print(f"[INFO] Saved FLATTRADE_API_KEY to {ENV_FILE}")

    if not api_secret:
        print("\n[INFO] FLATTRADE_API_SECRET not set in .env.")
        print("  Find your API Secret under Pi Menu → API Keys on https://wall.flattrade.in")
        api_secret = input("Enter your Pi API Secret: ").strip()
        if not api_secret:
            print("[ERROR] API Secret is required.")
            sys.exit(1)
        _save_env({"FLATTRADE_API_SECRET": api_secret})
        print(f"[INFO] Saved FLATTRADE_API_SECRET to {ENV_FILE}")

    # Build auth URL using the Pi app_key
    auth_url = f"https://auth.flattrade.in/?app_key={api_key}"

    print(f"\n1. Opening Flattrade login in your browser:\n   {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("2. Log in with: User ID + Password + OTP / 2FA")
    print("3. After login, your browser redirects to your Redirect URL.")
    print("   The URL will look like:")
    print("       http://localhost/flatrade/?code=XXXXXX&client=...\n")
    print("4. Copy and paste the FULL redirected URL (or just the request code value).\n")

    redirected_input = input("Paste the redirected URL (or request code): ").strip()
    if not redirected_input:
        print("[ERROR] Nothing entered.")
        sys.exit(1)

    # Extract request_code from URL if a full URL was pasted
    request_code = redirected_input
    if "?" in redirected_input or "//" in redirected_input:
        parsed = urllib.parse.urlparse(redirected_input)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            request_code = params["code"][0]
        elif "request_code" in params:
            request_code = params["request_code"][0]
        elif "token" in params:
            request_code = params["token"][0]

    request_code = request_code.strip()
    if not request_code:
        print("[ERROR] Could not extract request_code from input.")
        sys.exit(1)

    print(f"\nExchanging request code ({request_code[:8]}...) for daily session token...")
    session_token = exchange_request_code_for_token(api_key, api_secret, request_code)

    _save_env({"FLATTRADE_API_TOKEN": session_token})
    print(f"\n[SUCCESS] Daily Session Token generated!")
    print(f"[SUCCESS] Token starts with: {session_token[:15]}...")
    print(f"[SUCCESS] Saved FLATTRADE_API_TOKEN to {ENV_FILE}")
    print(f"\nYou can now run:  python trader.py")


if __name__ == "__main__":
    main()
