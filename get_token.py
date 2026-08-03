#!/usr/bin/env python3
"""
=============================================================================
FLATTRADE DAILY TOKEN GENERATOR
-----------------------------------------------------------------------------
Flattrade session tokens (api_token / susertoken) expire every 24 hours.

This helper script:
1. Reads FLATTRADE_USER_ID (and optionally API_KEY / API_SECRET) from .env.
2. Opens the Flattrade Auth URL in your default web browser automatically.
3. Accepts the redirected URL you paste after logging in.
4. Extracts the request code, exchanges it with Flattrade for a session token.
5. Writes the fresh FLATTRADE_API_TOKEN back to .env (NEVER to a committed file).

Setup
-----
    cp .env.example .env          # first time
    # fill in FLATTRADE_USER_ID in .env

Daily usage
-----------
    python get_token.py
=============================================================================
"""

import hashlib
import os
import re
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
            print(f"[INFO] Created .env from .env.example. Edit it to add your credentials.")
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
    Write updated key=value pairs into .env, preserving comments and existing lines.
    New keys are appended; existing keys are updated in-place.
    """
    existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    written = set()
    new_lines = []
    for raw in existing_lines:
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
    # Append any new keys not already in file
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def main():
    print("=" * 65)
    print("  FLATTRADE DAILY TOKEN GENERATOR")
    print("=" * 65)

    env = _read_env()
    user_id = env.get("FLATTRADE_USER_ID", "").strip()
    api_key = env.get("FLATTRADE_API_KEY", "").strip() or user_id
    api_secret = env.get("FLATTRADE_API_SECRET", "").strip()

    if not user_id or not user_id.strip():
        user_id = input("Enter your Flattrade User ID: ").strip()
        api_key = user_id
        _save_env({"FLATTRADE_USER_ID": user_id})

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
            _save_env({"FLATTRADE_API_TOKEN": token})
            print(f"[SUCCESS] Saved FLATTRADE_API_TOKEN to {ENV_FILE}")
            return


    code = code.strip()
    print(f"\nExtracted Request Code: {code}")

    # Check if api_secret exists for 2-step token exchange
    if not api_secret:
        api_secret = input("\nEnter your Flattrade API Secret (or press Enter to use code directly): ").strip()
        if api_secret:
            _save_env({"FLATTRADE_API_SECRET": api_secret})

    if api_secret:
        print("Exchanging request code for session token via Flattrade API...")
        raw_str = api_key + code + api_secret
        api_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

        token_url = "https://piconnect.flattrade.in/NorenWClientTP/Token"
        payload = f'jData={urllib.parse.quote(str({"api_key": api_key, "request_code": code, "api_secret": api_hash}))}'.encode("utf-8")
        import json as _json
        payload = _json.dumps({
            "api_key": api_key,
            "request_code": code,
            "api_secret": api_hash,
        }).encode("utf-8")

        req = urllib.request.Request(token_url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = _json.loads(resp.read().decode("utf-8"))
                if res_data.get("stat") == "Ok" and res_data.get("token"):
                    token = res_data["token"]
                    print(f"\n[SUCCESS] Session token generated: {token[:10]}...")
                    _save_env({"FLATTRADE_API_TOKEN": token})
                    print(f"[SUCCESS] Saved FLATTRADE_API_TOKEN to {ENV_FILE}")
                    return
                else:
                    print(f"[WARN] Token exchange API returned: {res_data}")
        except Exception as exc:
            print(f"[WARN] API token exchange request failed: {exc}")

    # Fallback: store code directly as api_token
    _save_env({"FLATTRADE_API_TOKEN": code})
    print(f"\n[SUCCESS] Saved FLATTRADE_API_TOKEN to {ENV_FILE}")


if __name__ == "__main__":
    main()
