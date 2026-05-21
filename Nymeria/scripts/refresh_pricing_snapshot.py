"""Refresh the bundled LiteLLM pricing snapshot.

Downloads the latest model_prices_and_context_window.json from the LiteLLM
repository and overwrites the bundled snapshot at
``nymeria/config/data/litellm_model_prices.json``. Run this manually before
releases to keep the offline fallback fresh.

Usage:
    python3 scripts/refresh_pricing_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "main/model_prices_and_context_window.json"
)

SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent
    / "nymeria"
    / "config"
    / "data"
    / "litellm_model_prices.json"
)


def main() -> int:
    print(f"Fetching {LITELLM_PRICING_URL}")
    try:
        response = httpx.get(LITELLM_PRICING_URL, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 1

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        print(f"Response is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict) or "sample_spec" not in payload:
        print("Response does not look like the LiteLLM pricing JSON", file=sys.stderr)
        return 1

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Wrote {len(payload)} model entries to {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
