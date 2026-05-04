from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_FILES = (
    ROOT / "requirements.txt",
    ROOT / "requirements-dev.txt",
    ROOT / "requirements-docker.txt",
    ROOT / "requirements-postgres.txt",
    ROOT / "requirements-sqlite.txt",
)


def _requirement_name(line: str) -> str | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped or stripped.startswith("-r "):
        return None
    match = re.match(r"([A-Za-z0-9_.-]+)", stripped)
    if match is None:
        return None
    return match.group(1).replace("_", "-").lower()


def test_requirement_files_do_not_duplicate_package_entries() -> None:
    packages: dict[str, list[str]] = defaultdict(list)

    for path in REQUIREMENT_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            name = _requirement_name(line)
            if name is not None:
                packages[name].append(path.name)

    duplicates = {
        package: sorted(files)
        for package, files in packages.items()
        if len(files) > 1
    }

    assert duplicates == {}


def test_requirement_includes_preserve_backend_ownership() -> None:
    base = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    docker = (ROOT / "requirements-docker.txt").read_text(encoding="utf-8")
    full_dockerfile = (ROOT / "Dockerfile.full").read_text(encoding="utf-8")
    slim_dockerfile = (ROOT / "Dockerfile.slim").read_text(encoding="utf-8")

    assert "-r requirements-sqlite.txt" in base
    assert "pytest" in dev
    assert "pytest-cov" in dev
    assert "ruff" in dev
    assert "pytest" not in base
    assert "pytest-cov" not in base
    assert "ruff" not in base
    assert "pytest" not in docker
    assert "pytest-cov" not in docker
    assert "ruff" not in docker
    assert "-r requirements-postgres.txt" in docker
    assert "COPY requirements*.txt ./" in full_dockerfile
    assert "COPY requirements*.txt ./" in slim_dockerfile
    assert "requirements-dev.txt" not in full_dockerfile
    assert "requirements-dev.txt" not in slim_dockerfile
