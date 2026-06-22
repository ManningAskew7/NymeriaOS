#!/usr/bin/env python3
"""Synchronize release versions across Nymeria backend and desktop manifests."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

INIT_VERSION_RE = re.compile(r'(?m)^__version__ = "([^"]+)"$')

VERSION_FILES = {
    "backend": Path("Nymeria/nymeria/__init__.py"),
    "desktop package": Path("nymeria-desktop/package.json"),
    "desktop Cargo": Path("nymeria-desktop/src-tauri/Cargo.toml"),
    "desktop Tauri": Path("nymeria-desktop/src-tauri/tauri.conf.json"),
    "desktop Cargo lock": Path("nymeria-desktop/src-tauri/Cargo.lock"),
}


class VersionManagementError(RuntimeError):
    """Raised when release version files cannot be read, updated, or validated."""


def default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def validate_version(version: str) -> None:
    if not VERSION_RE.fullmatch(version):
        raise VersionManagementError(
            "version must be SemVer-compatible, for example 0.2.0 or 0.2.0-beta.1"
        )

    try:
        from packaging.version import Version
    except ImportError:
        return

    try:
        Version(version)
    except Exception as exc:  # pragma: no cover - exact exception varies by packaging release
        raise VersionManagementError(
            f"version {version!r} is not accepted by Python packaging"
        ) from exc


def _read_text(root: Path, path: Path) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VersionManagementError(f"missing version file: {path}") from exc


def _write_text(root: Path, path: Path, text: str) -> None:
    (root / path).write_text(text, encoding="utf-8")


def _read_json(root: Path, path: Path) -> dict[str, object]:
    try:
        return json.loads(_read_text(root, path))
    except json.JSONDecodeError as exc:
        raise VersionManagementError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(root: Path, path: Path, data: dict[str, object]) -> None:
    _write_text(root, path, json.dumps(data, indent=2) + "\n")


def _extract_init_version(text: str) -> str:
    match = INIT_VERSION_RE.search(text)
    if not match:
        raise VersionManagementError('could not find __version__ = "..." in backend init')
    return match.group(1)


def _replace_init_version(text: str, version: str) -> str:
    updated, count = INIT_VERSION_RE.subn(f'__version__ = "{version}"', text, count=1)
    if count != 1:
        raise VersionManagementError('could not update __version__ = "..." in backend init')
    return updated


def _read_json_version(root: Path, path: Path) -> str:
    data = _read_json(root, path)
    version = data.get("version")
    if not isinstance(version, str):
        raise VersionManagementError(f"{path} does not contain a string version field")
    return version


def _update_json_version(root: Path, path: Path, version: str) -> None:
    data = _read_json(root, path)
    if not isinstance(data.get("version"), str):
        raise VersionManagementError(f"{path} does not contain a string version field")
    data["version"] = version
    _write_json(root, path, data)


_TOML_PACKAGE_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')


def _find_package_version_line(lines: list[str]) -> int | None:
    """Index of the ``version = "..."`` line in the ``[package]`` section.

    Returns None if there is no ``[package]`` section or it has no version line
    before the next ``[`` section header. Lines are stripped for matching, so
    callers may pass either ``splitlines()`` or ``splitlines(keepends=True)``
    output (the indices line up either way).
    """
    in_package = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[package]":
            in_package = True
            continue
        if in_package and stripped.startswith("["):
            return None
        if in_package and _TOML_PACKAGE_VERSION_RE.match(stripped):
            return index
    return None


def _read_package_version_from_toml(text: str, label: str) -> str:
    lines = text.splitlines()
    index = _find_package_version_line(lines)
    if index is None:
        raise VersionManagementError(f"could not find [package] version in {label}")
    match = _TOML_PACKAGE_VERSION_RE.match(lines[index].strip())
    assert match is not None  # guaranteed by _find_package_version_line
    return match.group(1)


def _replace_package_version_in_toml(text: str, version: str, label: str) -> str:
    lines = text.splitlines(keepends=True)
    index = _find_package_version_line(lines)
    if index is None:
        raise VersionManagementError(f"could not update [package] version in {label}")
    lines[index] = re.sub(r'"[^"]+"', f'"{version}"', lines[index], count=1)
    return "".join(lines)


def _read_cargo_lock_version(text: str) -> str:
    match = re.search(
        r'(?ms)^\[\[package\]\]\nname = "nymeria-desktop"\nversion = "([^"]+)"',
        text,
    )
    if not match:
        raise VersionManagementError("could not find nymeria-desktop package in Cargo.lock")
    return match.group(1)


def _replace_cargo_lock_version(text: str, version: str) -> str:
    pattern = re.compile(
        r'(?ms)(^\[\[package\]\]\nname = "nymeria-desktop"\nversion = ")([^"]+)(")'
    )
    updated, count = pattern.subn(rf"\g<1>{version}\3", text, count=1)
    if count != 1:
        raise VersionManagementError("could not update nymeria-desktop package in Cargo.lock")
    return updated


def read_versions(root: Path | None = None) -> dict[str, str]:
    root = root or default_root()
    backend_text = _read_text(root, VERSION_FILES["backend"])
    cargo_text = _read_text(root, VERSION_FILES["desktop Cargo"])
    cargo_lock_text = _read_text(root, VERSION_FILES["desktop Cargo lock"])

    return {
        "backend": _extract_init_version(backend_text),
        "desktop package": _read_json_version(root, VERSION_FILES["desktop package"]),
        "desktop Cargo": _read_package_version_from_toml(cargo_text, "Cargo.toml"),
        "desktop Tauri": _read_json_version(root, VERSION_FILES["desktop Tauri"]),
        "desktop Cargo lock": _read_cargo_lock_version(cargo_lock_text),
    }


def update_versions(version: str, root: Path | None = None) -> None:
    validate_version(version)
    root = root or default_root()

    init_path = VERSION_FILES["backend"]
    cargo_path = VERSION_FILES["desktop Cargo"]
    cargo_lock_path = VERSION_FILES["desktop Cargo lock"]

    _write_text(
        root,
        init_path,
        _replace_init_version(_read_text(root, init_path), version),
    )
    _update_json_version(root, VERSION_FILES["desktop package"], version)
    _write_text(
        root,
        cargo_path,
        _replace_package_version_in_toml(_read_text(root, cargo_path), version, "Cargo.toml"),
    )
    _update_json_version(root, VERSION_FILES["desktop Tauri"], version)
    _write_text(
        root,
        cargo_lock_path,
        _replace_cargo_lock_version(_read_text(root, cargo_lock_path), version),
    )


def ensure_versions_synced(root: Path | None = None) -> tuple[str, dict[str, str]]:
    versions = read_versions(root)
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        lines = ["version files are not in sync:"]
        lines.extend(f"  {name}: {version}" for name, version in versions.items())
        raise VersionManagementError("\n".join(lines))
    version = next(iter(unique_versions))
    validate_version(version)
    return version, versions


def normalize_tag(tag: str) -> str:
    tag_name = tag.rsplit("/", 1)[-1]
    return tag_name[1:] if tag_name.startswith("v") else tag_name


def verify_tag_matches_version(tag: str, version: str) -> None:
    tag_version = normalize_tag(tag)
    if tag_version != version:
        raise VersionManagementError(
            f"release tag {tag!r} does not match synchronized version {version!r}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check or update Nymeria release versions across package manifests."
    )
    parser.add_argument(
        "--set",
        metavar="VERSION",
        help="update all version files to VERSION before checking consistency",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that all version files match; this is the default action",
    )
    parser.add_argument(
        "--tag",
        metavar="TAG",
        help="also verify that TAG, with an optional leading v, matches the version",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root(),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    try:
        if args.set:
            update_versions(args.set, args.root)
            print(f"Updated version files to {args.set}.")

        version, versions = ensure_versions_synced(args.root)
        if args.tag:
            verify_tag_matches_version(args.tag, version)

        if not args.set:
            print(f"Version files are in sync: {version}")
        for name, value in versions.items():
            print(f"  {name}: {value}")
    except VersionManagementError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
