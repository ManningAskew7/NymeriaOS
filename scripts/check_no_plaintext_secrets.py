#!/usr/bin/env python3
"""Fail CI when runtime secret files contain plaintext credential patterns.

This intentionally checks the small set of files that must not be published
with real values. It does not print matched values.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

TARGET_FILES = (
    "Nymeria/.env",
    "Nymeria/.env.docker",
    "Nymeria/firebase-service-account.json",
    "Nymeria/google_credentials.json",
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "OpenAI/Anthropic/OpenRouter-style API key",
        re.compile(r"\bsk-(?:ant-|proj-|or-v1-)?[A-Za-z0-9_-]{16,}"),
    ),
    ("Cartesia-style API key", re.compile(r"\bsk_car_[A-Za-z0-9_-]{16,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")),
    ("Google OAuth client secret", re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{10,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    ("Telegram bot token", re.compile(r"\b(?:bot)?\d{6,12}:[A-Za-z0-9_-]{20,}")),
    ("Discord bot token", re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}")),
    ("Firebase/GCP private key", re.compile(r"-----BEGIN PRIVATE KEY-----")),
    ("Nymeria service token", re.compile(r"\bnym_[A-Za-z0-9_-]{24,}")),
    ("Fernet key assignment", re.compile(r"(?m)^NYMERIA_SECRETS_KEY=[A-Za-z0-9_-]{43}=$")),
)


def scan_file(path: Path) -> list[str]:
    if not path.exists():
        return []

    text = path.read_bytes().decode("utf-8", errors="ignore")
    findings: list[str] = []
    for label, pattern in PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        line_no = text.count("\n", 0, match.start()) + 1
        findings.append(f"{path.relative_to(ROOT)}:{line_no}: {label}")
    return findings


def main() -> int:
    findings: list[str] = []
    for relative in TARGET_FILES:
        findings.extend(scan_file(ROOT / relative))

    if not findings:
        print("No plaintext runtime secrets detected in guarded files.")
        return 0

    print("Plaintext secret-looking values were found in guarded runtime files:", file=sys.stderr)
    for finding in findings:
        print(f"  - {finding}", file=sys.stderr)
    print(
        "Replace real values with placeholders, keep local secrets untracked, "
        "or keep private deployments encrypted outside the public release path.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
