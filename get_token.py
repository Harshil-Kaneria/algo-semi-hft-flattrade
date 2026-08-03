#!/usr/bin/env python3
"""
=============================================================================
FLATTRADE DAILY TOKEN GENERATOR
-----------------------------------------------------------------------------
Flattrade uses the Noren OMS auth flow. Session tokens (susertoken) expire
every 24 hours and must be refreshed each trading morning.

HOW FLATTRADE AUTH WORKS (from official docs / pythonAPI repo):
  1. You create a Pi API App once at https://wall.flattrade.in/login
     → Pi Menu → Create New API Key
     → Set a Redirect URL (e.g. http://localhost:8080)
     → Note the generated API Key (app_key / client_id)

  2. Every morning: open the login URL in browser:
         https://auth.flattrade.in/?app_key=<FLATTRADE_API_KEY>

  3. Log in with User ID + Password + OTP.

  4. Browser redirects to your Redirect URL with:
         http://localhost:8080/?code=XXXXXX&client_id=...
     The `code` in the URL IS the session token (susertoken).
     No server-side secret exchange is needed — Flattrade does NOT have
     a code→token exchange endpoint like some other brokers.

  5. Call  api.set_session(userid=user_id, password='', usertoken=code)
     to activate the session.

This script automates steps 2–5 and saves FLATTRADE_API_TOKEN to .env.

Setup (first time only)
-----------------------
    copy .env.example .env      (Windows)
    cp   .env.example .env      (Linux/Mac)
    # Then edit .env and fill in FLATTRADE_USER_ID and FLATTRADE_API_KEY

Daily usage
-----------
    python get_token.py
=============================================================================
"""

import os
import sys
import urllib.parse
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
                  f"FLATTRADE_USER_ID and FLATTRADE_API_KEY then re-run.")
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


def main():
    print("=" * 65)
    print("  FLATTRADE DAILY TOKEN GENERATOR")
    print("=" * 65)

    env = _read_env()
    user_id = env.get("FLATTRADE_USER_ID", "").strip()
    api_key = env.get("FLATTRADE_API_KEY", "").strip()

    # Prompt for missing required values
    if not user_id:
        user_id = input("\nEnter your Flattrade User ID (e.g. FT12345): ").strip()
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

    # Build auth URL using the Pi app_key (this is the official Flattrade flow)
    auth_url = f"https://auth.flattrade.in/?app_key={api_key}"

    print(f"\n1. Opening Flattrade login in your browser:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    print("2. Log in with:  User ID + Password + OTP / 2FA")
    print("3. After login, your browser redirects to your Redirect URL.")
    print("   The URL will look like:")
    print("       http://localhost:8080/?code=XXXXXX&client_id=...")
    print("4. Copy the FULL redirected URL (or just the code value).\n")

    redirected_input = input("Paste the redirected URL (or just the code): ").strip()
    if not redirected_input:
        print("[ERROR] Nothing entered.")
        sys.exit(1)

    # Extract the code from URL if a full URL was pasted
    token = redirected_input
    if "?" in redirected_input or "//" in redirected_input:
        parsed = urllib.parse.urlparse(redirected_input)
        params = urllib.parse.parse_qs(parsed.query)
        # Flattrade redirect: ?code=XXXXX (code IS the susertoken directly)
        if "code" in params:
            token = params["code"][0]
        elif "token" in params:
            token = params["token"][0]
        elif "susertoken" in params:
            token = params["susertoken"][0]

    token = token.strip()
    if not token:
        print("[ERROR] Could not extract a token from the input.")
        sys.exit(1)

    # Save to .env — the code from the redirect IS the susertoken.
    # Flattrade does NOT require a separate code→token API exchange.
    # api.set_session(userid=user_id, password='', usertoken=token) is all that's needed.
    _save_env({"FLATTRADE_API_TOKEN": token})
    print(f"\n[SUCCESS] Token starts with: {token[:10]}...")
    print(f"[SUCCESS] Saved FLATTRADE_API_TOKEN to {ENV_FILE}")
    print(f"\nYou can now run:  python trader.py")


if __name__ == "__main__":
    main()
