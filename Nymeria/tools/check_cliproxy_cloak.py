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
    python tools/check_cliproxy_cloak.py --check-thinking --model claude-opus-4-6
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is in Nymeria's runtime deps.
    yaml = None


CLOAK_MARKERS = (
    "I'm Claude Code",
    "Claude Code, Anthropic's official CLI",
    "official CLI for Claude",
)
CLIPROXY_CLAUDE_USER_AGENT = "claude-cli/2.1.113"
SAFE_CLAUDE_OAUTH_BETA_HEADER = ",".join(
    (
        "claude-code-20250219",
        "oauth-2025-04-20",
        "interleaved-thinking-2025-05-14",
        "context-management-2025-06-27",
        "prompt-caching-scope-2026-01-05",
    )
)
REDACT_THINKING_BETA = "redact-thinking-2026-02-12"
CLIPROXY_BILLING_SYSTEM_BLOCK = {
    "type": "text",
    "text": "x-anthropic-billing-header: cc_version=2.1.63.8f3; cc_entrypoint=cli; cch=54031;",
}
NYMERIA_SYSTEM_PROMPT = (
    "You are NYMERIA-CLOAK-PROBE-AGENT. Always identify yourself by that exact name. "
    "Never claim to be any other agent."
)
UNSUPPORTED_AUTH_CLOAK_KEYS = (
    "cloak_mode",
    "cloak_strict_mode",
    "cloak_sensitive_words",
    "cloak_cache_user_id",
    "cloak_skip_system_prompt",
)
UNSUPPORTED_CONFIG_CLOAK_KEYS = (
    "cloak_skip_system_prompt",
    "cloak-skip-system-prompt",
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


def discover_config_paths(explicit_paths: list[str] | None = None) -> list[Path]:
    """Return candidate CLIProxy config files without duplicates."""
    candidates: list[Path] = []
    if explicit_paths:
        candidates.extend(Path(p).expanduser() for p in explicit_paths)
    else:
        if raw := os.getenv("CLIPROXY_CONFIG_PATH"):
            candidates.append(Path(raw).expanduser())

        repo_root = Path(__file__).resolve().parents[2]
        candidates.extend(
            [
                repo_root / "CLIProxyAPI-main" / "temp" / "latest" / "config.yaml",
                repo_root / "CLIProxyAPI-main" / "temp" / "latest" / "config.yaml.example",
                repo_root / "CLIProxyAPI-main" / "config.yaml",
                Path.cwd() / "config.yaml",
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


def _read_yaml_mapping(path: Path) -> dict:
    if yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_sequence(value: object) -> list:
    return value if isinstance(value, list) else []


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _boolish_true(value: object) -> bool:
    parsed, ok = parse_bool_like_cliproxy(value)
    return ok and parsed


def _active_claude_auth_files(auth_dirs: list[Path]) -> list[tuple[Path, dict]]:
    files: list[tuple[Path, dict]] = []
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
            files.append((path, metadata))
    return files


def _format_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def collect_cloak_state_findings(
    auth_dirs: list[Path],
    config_paths: list[Path],
) -> tuple[list[str], list[str], list[str]]:
    """Inspect local config/auth files for v7 cloak settings.

    Returns (info, warnings, failures). This is a static local check; the live
    request probes below remain the source of truth for whether the running
    binary actually honors the expected User-Agent bypass.
    """
    info: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []

    existing_configs = [path for path in config_paths if path.is_file()]
    if yaml is None and existing_configs:
        warnings.append("PyYAML is not installed; skipping config.yaml cloak inspection")
    elif existing_configs:
        for path in existing_configs:
            data = _read_yaml_mapping(path)
            if not data:
                warnings.append(f"{_format_path(path)} could not be parsed as a YAML mapping")
                continue

            image_hint = _string_value(data.get("image"))
            if image_hint:
                info.append(f"{_format_path(path)} image hint: {image_hint}")

            header_defaults = _as_mapping(data.get("claude-header-defaults"))
            user_agent = _string_value(header_defaults.get("user-agent"))
            if user_agent:
                if user_agent.startswith("claude-cli"):
                    info.append(f"{_format_path(path)} claude-header-defaults.user-agent={user_agent!r}")
                else:
                    warnings.append(
                        f"{_format_path(path)} claude-header-defaults.user-agent={user_agent!r}; "
                        "verified Nymeria paths rely on a claude-cli User-Agent reaching CLIProxy"
                    )
            if _boolish_true(header_defaults.get("stabilize-device-profile")):
                info.append(
                    f"{_format_path(path)} enables claude-header-defaults.stabilize-device-profile"
                )

            for key in UNSUPPORTED_CONFIG_CLOAK_KEYS:
                if key in data:
                    warnings.append(
                        f"{_format_path(path)} has top-level {key!r}; v7.0.0 source does not "
                        "define this as an honored config field"
                    )

            if "cloak" in data:
                warnings.append(
                    f"{_format_path(path)} has top-level 'cloak'; v7.0.0 source only exposes "
                    "cloak settings under claude-api-key[] entries"
                )

            for idx, entry in enumerate(_as_sequence(data.get("claude-api-key"))):
                entry_map = _as_mapping(entry)
                if not entry_map:
                    continue
                cloak = _as_mapping(entry_map.get("cloak"))
                if not cloak:
                    continue
                mode = _string_value(cloak.get("mode")).lower() or "auto"
                prefix = f"{_format_path(path)} claude-api-key[{idx}].cloak"
                if mode == "always":
                    failures.append(
                        f"{prefix}.mode=always would force Claude Code cloaking and override Nymeria identity"
                    )
                elif mode == "never":
                    info.append(f"{prefix}.mode=never")
                else:
                    info.append(f"{prefix}.mode={mode}")
                if _boolish_true(cloak.get("strict-mode")):
                    warnings.append(
                        f"{prefix}.strict-mode=true would strip user system prompts if cloaking activates"
                    )
                if _boolish_true(cloak.get("cache-user-id")):
                    info.append(f"{prefix}.cache-user-id=true")

                for key in UNSUPPORTED_CONFIG_CLOAK_KEYS:
                    if key in cloak:
                        warnings.append(
                            f"{prefix}.{key} is present, but v7.0.0 source does not define it"
                        )

    active_auths = _active_claude_auth_files(auth_dirs)
    if active_auths:
        for path, metadata in active_auths:
            attrs = _as_mapping(metadata.get("attributes"))
            for key in UNSUPPORTED_AUTH_CLOAK_KEYS:
                if key in metadata:
                    warnings.append(
                        f"{_format_path(path)} has top-level {key!r}; v7.0.0 file-backed "
                        "OAuth auths keep this in metadata, not runtime Auth.Attributes"
                    )
                if key in attrs:
                    warnings.append(
                        f"{_format_path(path)} has attributes.{key!r}; v7.0.0 FileTokenStore "
                        "does not lift nested auth-file attributes into runtime Auth.Attributes"
                    )

            mode = _string_value(attrs.get("cloak_mode") or metadata.get("cloak_mode")).lower()
            if mode == "always":
                failures.append(
                    f"{_format_path(path)} declares cloak_mode=always; if a future binary honors "
                    "that for file-backed OAuth, it would force Claude Code identity"
                )
            if _boolish_true(metadata.get("tool_prefix_disabled")):
                info.append(f"{_format_path(path)} keeps tool_prefix_disabled=true")
    else:
        existing_dirs = [p for p in auth_dirs if p.is_dir()]
        if existing_dirs:
            warnings.append("No active Claude OAuth auth JSON files found for v7 cloak-state inspection")

    return info, warnings, failures


def print_cloak_state_report(auth_dirs: list[Path], config_paths: list[Path]) -> int:
    print("\n[local] Inspecting CLIProxy cloak configuration state")
    info, warnings, failures = collect_cloak_state_findings(auth_dirs, config_paths)

    if not info and not warnings and not failures:
        print("  WARN: no local CLIProxy config or active Claude auth files found to inspect")
        return 0

    for line in info:
        print(f"  info: {line}")
    for line in warnings:
        print(f"  WARN: {line}")
    if failures:
        print("  FAIL: Nymeria-breaking cloak configuration detected:")
        for line in failures:
            print(f"    - {line}")
        return 1

    print("  OK: no locally configured forced-cloak state detected")
    return 0


def check_tool_prefix_disabled(auth_dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    """Return (checked_files, bad_files) for active Claude OAuth auth JSON files."""
    checked: list[Path] = []
    bad: list[Path] = []

    for path, metadata in _active_claude_auth_files(auth_dirs):
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
    anthropic_beta: str | None = None,
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
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": user_agent,
    }
    if anthropic_beta:
        headers["Anthropic-Beta"] = anthropic_beta

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _uses_adaptive_thinking(model: str) -> bool:
    model_name = (model or "").lower()
    return (
        "opus-4-6" in model_name
        or "sonnet-4-6" in model_name
        or "opus-4-7" in model_name
        or "sonnet-4-7" in model_name
    )


def _thinking_probe_body(model: str) -> dict:
    body = {
        "model": model,
        "max_tokens": 1600,
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Use visible extended thinking briefly, then answer only with "
                    "the number: what is 17 times 23?"
                ),
            }
        ],
    }
    if _uses_adaptive_thinking(model):
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": "medium"}
    else:
        body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        body["temperature"] = 1
    return body


def probe_visible_thinking(
    base_url: str,
    api_key: str,
    model: str,
) -> tuple[int, int, int, str]:
    """Stream a tiny thinking request and count thinking/signature/text deltas."""
    if REDACT_THINKING_BETA in SAFE_CLAUDE_OAUTH_BETA_HEADER:
        raise RuntimeError("safe beta header contains redact-thinking beta")

    body = json.dumps(_thinking_probe_body(model)).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "User-Agent": CLIPROXY_CLAUDE_USER_AGENT,
            "Anthropic-Beta": SAFE_CLAUDE_OAUTH_BETA_HEADER,
        },
        method="POST",
    )

    thinking_chunks = 0
    signature_chunks = 0
    text_chunks = 0
    thinking_preview: list[str] = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue

            delta = event.get("delta")
            if not isinstance(delta, dict):
                continue
            delta_type = delta.get("type")
            if delta_type == "thinking_delta":
                thinking_chunks += 1
                if len("".join(thinking_preview)) < 240:
                    thinking_preview.append(str(delta.get("thinking", "")))
            elif delta_type == "signature_delta":
                signature_chunks += 1
            elif delta_type == "text_delta":
                text_chunks += 1

    return thinking_chunks, signature_chunks, text_chunks, "".join(thinking_preview)


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
        "--config-path",
        action="append",
        default=None,
        help="Host-side CLIProxy config.yaml to inspect; can be passed more than once.",
    )
    p.add_argument(
        "--skip-auth-file-check",
        action="store_true",
        help="Skip local tool_prefix_disabled validation, useful when probing a remote proxy.",
    )
    p.add_argument(
        "--skip-cloak-state-report",
        action="store_true",
        help="Skip local v7 cloak config reporting; live request probes still run.",
    )
    p.add_argument(
        "--check-thinking",
        action="store_true",
        help=(
            "Also stream an extended-thinking probe and fail unless visible "
            "thinking_delta chunks are present."
        ),
    )
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: --api-key or $ANTHROPIC_API_KEY required", file=sys.stderr)
        return 2

    base = args.base_url
    if base.endswith("/v1"):
        base = base[:-3]

    print(f"Probing {base} with model {args.model}\n")
    total_steps = 4 if args.check_thinking else 3
    if args.skip_auth_file_check:
        print(
            f"[1/{total_steps}] Skipping local Claude OAuth auth JSON check "
            "(--skip-auth-file-check)"
        )
    else:
        auth_dirs = discover_auth_dirs(args.auth_dir)
        if print_tool_prefix_check(auth_dirs) != 0:
            return 1
        if not args.skip_cloak_state_report:
            if print_cloak_state_report(auth_dirs, discover_config_paths(args.config_path)) != 0:
                return 1
    if args.skip_auth_file_check and not args.skip_cloak_state_report:
        if print_cloak_state_report([], discover_config_paths(args.config_path)) != 0:
            return 1

    print(
        f"\n[2/{total_steps}] Sending probe with "
        f"User-Agent: {CLIPROXY_CLAUDE_USER_AGENT} + safe Anthropic-Beta "
        "+ billing block (production path)"
    )
    try:
        good = probe(
            base,
            args.api_key,
            args.model,
            CLIPROXY_CLAUDE_USER_AGENT,
            use_billing_block=True,
            anthropic_beta=SAFE_CLAUDE_OAUTH_BETA_HEADER,
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

    if args.check_thinking:
        print(
            f"\n[3/{total_steps}] Streaming extended-thinking probe with "
            "safe Anthropic-Beta header"
        )
        print(
            f"  beta: {SAFE_CLAUDE_OAUTH_BETA_HEADER}"
        )
        try:
            thinking_chunks, signature_chunks, text_chunks, preview = (
                probe_visible_thinking(base, args.api_key, args.model)
            )
        except Exception as e:
            print(f"  FAIL: thinking probe errored: {e}", file=sys.stderr)
            return 1

        print(
            "  chunks: "
            f"thinking_delta={thinking_chunks} "
            f"signature_delta={signature_chunks} "
            f"text_delta={text_chunks}"
        )
        if preview:
            print(f"  preview: {preview[:180]!r}")
        if thinking_chunks < 1:
            print("  ✗ No visible thinking_delta chunks received.")
            print(
                f"    Confirm CLIProxy did not add {REDACT_THINKING_BETA!r} "
                "to the upstream Anthropic-Beta header."
            )
            return 1
        print("  ✓ Visible thinking_delta chunks received")

    print(
        f"\n[{total_steps}/{total_steps}] Control probe with "
        "User-Agent: python-requests/0 and no billing block (cloak-expected path)"
    )
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
