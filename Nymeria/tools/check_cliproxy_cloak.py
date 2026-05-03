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
    python tools/check_cliproxy_cloak.py --auth-dir CLIProxyAPI-main/temp/latest/auths
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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


def parse_bool_like_cliproxy(value: object) -> tuple[bool, bool]:
    """Parse booleans the way CLIProxy's Auth.ToolPrefixDisabled does."""
    if isinstance(value, bool):
        return value, True
    if isinstance(value, str):
        trimmed = value.strip().lower()
        if not trimmed:
            return False, False
        if trimmed in {"1", "t", "true"}:
            return True, True
        if trimmed in {"0", "f", "false"}:
            return False, True
        return False, False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0, True
    return False, False


def is_truthy_tool_prefix_disabled(metadata: dict) -> bool:
    for key in ("tool_prefix_disabled", "tool-prefix-disabled"):
        if key in metadata:
            parsed, ok = parse_bool_like_cliproxy(metadata[key])
            return ok and parsed
    return False


def is_disabled_auth(metadata: dict) -> bool:
    if "disabled" not in metadata:
        return False
    parsed, ok = parse_bool_like_cliproxy(metadata["disabled"])
    return ok and parsed


def discover_auth_dirs(explicit_dirs: list[str] | None = None) -> list[Path]:
    """Return candidate host-side CLIProxy auth directories without duplicates."""
    candidates: list[Path] = []
    if explicit_dirs:
        candidates.extend(Path(p).expanduser() for p in explicit_dirs)
    else:
        for env_name in ("CLIPROXY_AUTH_DIR", "CLI_PROXY_AUTH_PATH"):
            if raw := os.getenv(env_name):
                candidates.append(Path(raw).expanduser())

        repo_root = Path(__file__).resolve().parents[2]
        candidates.extend(
            [
                repo_root / "CLIProxyAPI-main" / "temp" / "latest" / "auths",
                repo_root / "CLIProxyAPI-main" / "auths",
                Path.cwd() / "auths",
                Path.home() / ".cli-proxy-api",
            ]
        )

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in seen:
            unique.append(candidate)
            seen.add(resolved)
    return unique


def check_tool_prefix_disabled(auth_dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    """Return (checked_files, bad_files) for active Claude OAuth auth JSON files."""
    checked: list[Path] = []
    bad: list[Path] = []

    for auth_dir in auth_dirs:
        if not auth_dir.is_dir():
            continue
        for path in sorted(auth_dir.glob("*.json")):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("type", "")).lower() != "claude":
                continue
            if is_disabled_auth(metadata):
                continue
            checked.append(path)
            if not is_truthy_tool_prefix_disabled(metadata):
                bad.append(path)

    return checked, bad


def print_tool_prefix_check(auth_dirs: list[Path]) -> int:
    print("[1/3] Checking Claude OAuth auth JSON for tool_prefix_disabled")
    checked, bad = check_tool_prefix_disabled(auth_dirs)

    existing_dirs = [p for p in auth_dirs if p.is_dir()]
    if not existing_dirs:
        print("  WARN: no local CLIProxy auth directory found; pass --auth-dir to check one explicitly")
        return 0
    for auth_dir in existing_dirs:
        print(f"  auth dir: {auth_dir}")

    if not checked:
        print("  WARN: no active Claude OAuth auth JSON files found")
        return 0

    if bad:
        print("  FAIL: active Claude auth file(s) lack top-level tool_prefix_disabled=true:")
        for path in bad:
            print(f"    - {path}")
        print('  Repair: add `"tool_prefix_disabled": true` to each file, then restart CLIProxy.')
        return 1

    print(f"  ✓ Clean: {len(checked)} active Claude auth file(s) have tool_prefix_disabled=true")
    return 0


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
    p.add_argument(
        "--auth-dir",
        action="append",
        default=None,
        help="Host-side CLIProxy auth directory to inspect; can be passed more than once.",
    )
    p.add_argument(
        "--skip-auth-file-check",
        action="store_true",
        help="Skip local tool_prefix_disabled validation, useful when probing a remote proxy.",
    )
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: --api-key or $ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    base = args.base_url
    if base.endswith("/v1"):
        base = base[:-3]

    print(f"Probing {base} with model {args.model}\n")
    if args.skip_auth_file_check:
        print("[1/3] Skipping local Claude OAuth auth JSON check (--skip-auth-file-check)")
    else:
        if print_tool_prefix_check(discover_auth_dirs(args.auth_dir)) != 0:
            return 1

    print("\n[2/3] Sending probe with User-Agent: claude-cli/2.1.113 + billing block (production path)")
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

    print("\n[3/3] Control probe with User-Agent: python-requests/0 and no billing block (cloak-expected path)")
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
