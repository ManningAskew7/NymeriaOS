#!/usr/bin/env python3
"""Verify CLIProxy is not silently cloaking requests.

Background: CLIProxy v6.9.36+ gates full "cloaking" (Claude Code system-prompt
replacement, fake user_id, sensitive-word obfuscation) on the client's incoming
User-Agent. When the client sends User-Agent: claude-cli/*, full cloaking is
skipped. Nymeria still sends the lightweight v6.9.0-style OAuth billing
fingerprint as a separate structured system block; without it, Sonnet/Opus
OAuth requests can return a misleading 429 even though the token is valid.

Run this after:
- Upgrading langchain-anthropic / anthropic SDK
- Upgrading the CLIProxy image
- Any change to providers.py
- Suspecting model-identity drift in Claude responses

Usage:
    python tools/check_cliproxy_cloak.py
    python tools/check_cliproxy_cloak.py --base-url http://cli-proxy-api:8317 --api-key cpx-...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


CLOAK_MARKERS = (
    "I'm Claude Code",
    "Claude Code, Anthropic's official CLI",
    "official CLI for Claude",
)
CLIPROXY_BILLING_SYSTEM_BLOCK = {
    "type": "text",
    "text": "x-anthropic-billing-header: cc_version=2.1.63.8f3; cc_entrypoint=cli; cch=54031;",
}
NYMERIA_SYSTEM_PROMPT = (
    "You are NYMERIA-CLOAK-PROBE-AGENT. Always identify yourself by that exact name. "
    "Never claim to be any other agent."
)


def probe(
    base_url: str,
    api_key: str,
    model: str,
    user_agent: str,
    *,
    use_billing_block: bool,
) -> dict:
    system = [
        CLIPROXY_BILLING_SYSTEM_BLOCK,
        {"type": "text", "text": NYMERIA_SYSTEM_PROMPT},
    ] if use_billing_block else NYMERIA_SYSTEM_PROMPT

    body = json.dumps({
        "model": model,
        "max_tokens": 60,
        "system": system,
        "messages": [{"role": "user", "content": "Who are you? One short sentence."}],
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", "http://localhost:8317"))
    p.add_argument("--api-key", default=os.getenv("ANTHROPIC_API_KEY", ""))
    p.add_argument("--model", default="claude-sonnet-4-5-20250929")
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: --api-key or $ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    base = args.base_url
    if base.endswith("/v1"):
        base = base[:-3]

    print(f"Probing {base} with model {args.model}\n")

    print("[1/2] Sending probe with User-Agent: claude-cli/2.1.113 + billing block (production path)")
    try:
        good = probe(
            base,
            args.api_key,
            args.model,
            "claude-cli/2.1.113",
            use_billing_block=True,
        )
    except Exception as e:
        print(f"  FAIL: probe request errored: {e}", file=sys.stderr)
        return 1

    text = good.get("content", [{}])[0].get("text", "")
    tokens = good.get("usage", {}).get("input_tokens", -1)
    tier = good.get("usage", {}).get("service_tier", "?")
    print(f"  text:   {text[:140]!r}")
    print(f"  tokens: input={tokens}  tier={tier}")

    cloak_hit = any(marker in text for marker in CLOAK_MARKERS)
    nymeria_hit = "NYMERIA-CLOAK-PROBE-AGENT" in text or "PROBE-AGENT" in text

    if cloak_hit:
        print("  ✗ CLOAK ACTIVE: response contains Claude Code identity markers")
        print("    Cause: CLIProxy is injecting the Claude Code system prompt despite")
        print("    the claude-cli User-Agent. Check that providers.py default_headers")
        print("    is reaching the proxy unmodified.")
        return 1
    if not nymeria_hit:
        print("  ⚠ Probe identity NYMERIA-CLOAK-PROBE-AGENT not echoed back")
        print("    System prompt may be partially overridden. Inspect the full response.")
        return 1
    if tier != "standard":
        print(f"  ⚠ service_tier={tier!r} — expected 'standard' (subscription tier).")
        print("    May indicate Anthropic routed this to extra-usage-balance.")
        return 1

    print("  ✓ Clean: no full cloak, Nymeria identity preserved, subscription tier")

    print("\n[2/2] Control probe with User-Agent: python-requests/0 and no billing block (cloak-expected path)")
    try:
        bad = probe(
            base,
            args.api_key,
            args.model,
            "python-requests/0",
            use_billing_block=False,
        )
    except Exception as e:
        print(f"  WARN: control probe errored: {e}", file=sys.stderr)
        bad = None

    if bad:
        bad_text = bad.get("content", [{}])[0].get("text", "")
        bad_tokens = bad.get("usage", {}).get("input_tokens", -1)
        print(f"  text:   {bad_text[:140]!r}")
        print(f"  tokens: input={bad_tokens}")
        if any(marker in bad_text for marker in CLOAK_MARKERS):
            print("  ✓ Cloak still triggers on non-claude-cli UA (sanity check)")
        else:
            print("  ⚠ Cloak did NOT trigger on python-requests UA either —")
            print("    proxy may have been configured to never cloak (cloak_mode=never).")
            print("    Acceptable on v6.9.0; surprising on v6.9.36.")

    print("\nResult: PASS — CLIProxy is honoring User-Agent override correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
