#!/usr/bin/env python3
"""Twitch OAuth helper — generates auth URLs and exchanges codes for tokens.

Usage:
    python tools/twitch_auth.py url                    # Print both OAuth URLs
    python tools/twitch_auth.py exchange CODE          # Exchange auth code for tokens
    python tools/twitch_auth.py validate TOKEN         # Check if a token is valid

Environment variables (or reads from .env.docker):
    TWITCH_CLIENT_ID       — Twitch application Client ID
    TWITCH_CLIENT_SECRET   — Twitch application Client Secret
"""

import argparse
import json
import os
import sys

try:
    import httpx
except ImportError:
    print("httpx not installed. Run: pip install httpx")
    sys.exit(1)

# All scopes needed for the bot account (moderator in the channel)
BOT_SCOPES = [
    "user:read:chat",
    "user:write:chat",
    "user:bot",
    "channel:bot",
    "moderator:manage:banned_users",
    "moderator:manage:chat_messages",
    "moderator:manage:announcements",
    "moderator:manage:shoutouts",
    "moderator:manage:warnings",
    "moderator:manage:automod",
    "moderator:read:chatters",
    "moderator:read:banned_users",
    "clips:edit",
]

# Scopes needed for the broadcaster (channel owner)
BROADCASTER_SCOPES = [
    "channel:bot",
    "channel:manage:polls",
    "channel:manage:predictions",
    "channel:manage:broadcast",
    "channel:read:subscriptions",
]

REDIRECT_URI = "http://localhost:3000"


def load_env(key: str) -> str:
    """Load a value from environment or .env.docker file."""
    val = os.environ.get(key)
    if val:
        return val

    # Try reading from .env.docker
    env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.docker")
    if not os.path.exists(env_file):
        env_file = os.path.join(os.path.dirname(__file__), "..", ".env.docker")

    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")

    return ""


def cmd_url(client_id: str) -> None:
    """Print OAuth URLs for both bot and broadcaster."""
    bot_scope_str = "+".join(BOT_SCOPES)
    broadcaster_scope_str = "+".join(BROADCASTER_SCOPES)

    bot_url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={client_id}&redirect_uri={REDIRECT_URI}"
        f"&scope={bot_scope_str}"
    )
    broadcaster_url = (
        f"https://id.twitch.tv/oauth2/authorize?response_type=code"
        f"&client_id={client_id}&redirect_uri={REDIRECT_URI}"
        f"&scope={broadcaster_scope_str}"
    )

    print("=" * 60)
    print("BOT TOKEN — log in as the bot account (e.g. SilkBot7)")
    print("=" * 60)
    print(f"\nScopes ({len(BOT_SCOPES)}):")
    for s in BOT_SCOPES:
        print(f"  - {s}")
    print(f"\nURL:\n{bot_url}\n")

    print("=" * 60)
    print("BROADCASTER TOKEN — log in as the channel owner (e.g. silk)")
    print("=" * 60)
    print(f"\nScopes ({len(BROADCASTER_SCOPES)}):")
    for s in BROADCASTER_SCOPES:
        print(f"  - {s}")
    print(f"\nURL:\n{broadcaster_url}\n")

    print("-" * 60)
    print("After visiting a URL, the page won't load (nothing on localhost:3000).")
    print("Copy the 'code' parameter from the browser URL bar, then run:")
    print("  python tools/twitch_auth.py exchange THE_CODE")


def cmd_exchange(client_id: str, client_secret: str, code: str) -> None:
    """Exchange an authorization code for access + refresh tokens."""
    if not client_secret:
        print("Error: TWITCH_CLIENT_SECRET not set.")
        sys.exit(1)

    resp = httpx.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
    )

    data = resp.json()
    if "access_token" in data:
        print("Success! Add these to your .env.docker:\n")
        print(f"  ACCESS_TOKEN={data['access_token']}")
        print(f"  REFRESH_TOKEN={data['refresh_token']}")
        print(f"\n  Scopes: {' '.join(data.get('scope', []))}")
        print(f"  Token type: {data.get('token_type')}")
        print(f"  Expires in: {data.get('expires_in')}s")
    else:
        print(f"Error: {json.dumps(data, indent=2)}")


def cmd_validate(token: str) -> None:
    """Validate a token and show its scopes."""
    resp = httpx.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"},
    )
    data = resp.json()
    if "login" in data:
        print(f"Valid! User: {data['login']} (ID: {data['user_id']})")
        print(f"Scopes ({len(data.get('scopes', []))}):")
        for s in data.get("scopes", []):
            print(f"  - {s}")
        print(f"Expires in: {data.get('expires_in', '?')}s")
    else:
        print(f"Invalid: {json.dumps(data, indent=2)}")


def main():
    parser = argparse.ArgumentParser(description="Twitch OAuth helper for Nymeria")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("url", help="Print OAuth URLs for bot and broadcaster")

    ex = sub.add_parser("exchange", help="Exchange auth code for tokens")
    ex.add_argument("code", help="Authorization code from redirect URL")

    val = sub.add_parser("validate", help="Validate a token")
    val.add_argument("token", help="Access token to validate")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    client_id = load_env("TWITCH_CLIENT_ID")
    if not client_id:
        print("Error: TWITCH_CLIENT_ID not set in environment or .env.docker")
        sys.exit(1)

    if args.command == "url":
        cmd_url(client_id)
    elif args.command == "exchange":
        client_secret = load_env("TWITCH_CLIENT_SECRET")
        cmd_exchange(client_id, client_secret, args.code)
    elif args.command == "validate":
        cmd_validate(args.token)


if __name__ == "__main__":
    main()
