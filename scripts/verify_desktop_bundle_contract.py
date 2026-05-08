#!/usr/bin/env python3
"""Verify the Windows desktop backend bundle contract.

The Tauri installer must bundle the PyInstaller backend at the same resource
path that the Rust process manager probes at runtime.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EXPECTED_RESOURCE_SOURCE = "../../Nymeria/dist/nymeria-backend.exe"
EXPECTED_RESOURCE_TARGET = "Nymeria/dist/nymeria-backend.exe"
EXPECTED_BACKEND_SEGMENTS = ("Nymeria", "dist", "nymeria-backend.exe")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _error(message: str) -> str:
    return f"[desktop-bundle-contract] {message}"


def verify_contract(root: Path, *, require_built_backend: bool) -> list[str]:
    root = root.resolve()
    tauri_dir = root / "nymeria-desktop" / "src-tauri"
    tauri_config_path = tauri_dir / "tauri.conf.json"
    process_manager_path = tauri_dir / "src" / "process_manager.rs"

    errors: list[str] = []
    notes: list[str] = []

    try:
        config = json.loads(tauri_config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(_error(f"missing {tauri_config_path}"))
        return errors

    resources = config.get("bundle", {}).get("resources")
    if not isinstance(resources, dict):
        errors.append(_error("tauri.conf.json bundle.resources must be an object mapping"))
        return errors

    actual_target = resources.get(EXPECTED_RESOURCE_SOURCE)
    if actual_target != EXPECTED_RESOURCE_TARGET:
        errors.append(
            _error(
                "expected Tauri to map "
                f"{EXPECTED_RESOURCE_SOURCE!r} to {EXPECTED_RESOURCE_TARGET!r}; "
                f"got {actual_target!r}"
            )
        )
    else:
        notes.append(f"Tauri resource target: {EXPECTED_RESOURCE_TARGET}")

    backend_source = (tauri_dir / EXPECTED_RESOURCE_SOURCE).resolve()
    if require_built_backend:
        if not backend_source.is_file():
            errors.append(_error(f"built backend exe not found at {backend_source}"))
        else:
            notes.append(f"Built backend source exists: {backend_source}")
    else:
        notes.append(f"Built backend source check skipped: {backend_source}")

    try:
        process_manager = process_manager_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append(_error(f"missing {process_manager_path}"))
        return errors

    missing_segments = [
        segment
        for segment in EXPECTED_BACKEND_SEGMENTS
        if f'join("{segment}")' not in process_manager
    ]
    if missing_segments:
        errors.append(
            _error(
                "process_manager.rs does not probe the expected backend path "
                f"segments: {', '.join(missing_segments)}"
            )
        )
    else:
        notes.append(
            "Process manager probes resource root path: "
            + "/".join(EXPECTED_BACKEND_SEGMENTS)
        )

    if 'exe_parent.join("resources")' not in process_manager:
        errors.append(
            _error('process_manager.rs does not check the installed app "resources" directory')
        )
    else:
        notes.append('Process manager checks the installed app "resources" directory')

    return errors or notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-built-backend",
        action="store_true",
        help="Fail unless Nymeria/dist/nymeria-backend.exe exists for Tauri to bundle.",
    )
    args = parser.parse_args(argv)

    results = verify_contract(_repo_root(), require_built_backend=args.require_built_backend)
    failed = any(line.startswith("[desktop-bundle-contract]") for line in results)

    if failed:
        for line in results:
            print(line, file=sys.stderr)
        return 1

    print("Desktop bundle contract verified:")
    for line in results:
        print(f"- {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
