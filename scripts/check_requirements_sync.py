#!/usr/bin/env python3
"""Guard against drift between pyproject.toml and the requirements*.txt files.

The two dependency declarations serve different consumers: pyproject.toml drives
wheel/uv/pip installs, while the requirements*.txt files drive the Docker image
builds. They are easy to let drift apart (that is how pypdf/python-docx ended up
in requirements.txt but missing from pyproject, breaking PDF/DOCX extraction in
wheel installs).

This script compares the *set of distribution names* (PEP 503 normalized, version
specifiers and extras markers ignored) on each side. It deliberately does not
compare version specifiers, because the two formats pin differently on purpose;
the high-value invariant is that no package is present in one place and absent
from the other.

Run from the repo root or anywhere:  python3 scripts/check_requirements_sync.py
Exit code 0 == in sync, 1 == drift found.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "Nymeria"
PYPROJECT = BACKEND / "pyproject.toml"

# (label, requirements file relative to BACKEND, pyproject dotted location).
# "core" maps to [project.dependencies]; the rest map to optional-dependencies.
CHECKS = [
    ("core", "requirements.txt", None),
    ("bots", "requirements-bots.txt", "bots"),
    ("postgres", "requirements-postgres.txt", "postgres"),
]

_NAME_SPLIT = re.compile(r"[<>=!~;\[\s]")


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def requirement_name(line: str) -> str | None:
    """Extract the normalized distribution name from a requirements line."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    head = _NAME_SPLIT.split(line, 1)[0]
    return normalize(head) if head else None


def parse_requirements(path: Path, _seen: set[Path] | None = None) -> set[str]:
    """Parse a requirements file, following `-r other.txt` includes."""
    seen = _seen if _seen is not None else set()
    path = path.resolve()
    if path in seen:
        return set()
    seen.add(path)
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if stripped.startswith(("-r ", "--requirement ")):
            include = stripped.split(None, 1)[1].strip()
            names |= parse_requirements(path.parent / include, seen)
            continue
        name = requirement_name(raw)
        if name:
            names.add(name)
    return names


def pyproject_names(data: dict, extra: str | None) -> set[str]:
    project = data["project"]
    specs = (
        project["dependencies"]
        if extra is None
        else project["optional-dependencies"][extra]
    )
    return {n for spec in specs if (n := requirement_name(spec))}


def main() -> int:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    ok = True
    for label, req_file, extra in CHECKS:
        req_names = parse_requirements(BACKEND / req_file)
        proj_names = pyproject_names(data, extra)
        only_req = sorted(req_names - proj_names)
        only_proj = sorted(proj_names - req_names)
        if only_req or only_proj:
            ok = False
            print(f"[DRIFT] {label}: {req_file} vs pyproject")
            if only_req:
                target = "[project.dependencies]" if extra is None else f"[{extra}] extra"
                print(f"  in {req_file} but missing from pyproject {target}: {only_req}")
            if only_proj:
                print(f"  in pyproject but missing from {req_file}: {only_proj}")
        else:
            print(f"[OK] {label}: {len(req_names)} packages in sync")
    if not ok:
        print("\nDependency drift detected. Reconcile pyproject.toml with the "
              "requirements*.txt files (names only; version specs may differ).")
        return 1
    print("\nAll dependency sets in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
