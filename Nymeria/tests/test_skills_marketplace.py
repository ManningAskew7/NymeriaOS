"""Tests for the skills marketplace fetchers.

Focus: the AnthropicSkillsFetcher tarball-extraction path guard. The network
GET is monkeypatched out (``_get_tarball``), so these run with no network.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from nymeria.skills import marketplace
from nymeria.skills.marketplace import AnthropicSkillsFetcher, MarketplaceError


def _make_tar(members: dict[str, bytes]) -> bytes:
    """Build an in-memory gzipped tar from {name: content} in insertion order."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _fetcher_with_tar(data: bytes, monkeypatch: pytest.MonkeyPatch) -> AnthropicSkillsFetcher:
    # http_session is never used because _get_tarball is stubbed.
    fetcher = AnthropicSkillsFetcher(http_session=object())
    monkeypatch.setattr(fetcher, "_get_tarball", lambda: data)
    return fetcher


def test_fetch_rejects_path_traversal_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A crafted member escapes the skill dir via ".." segments while still
    # starting with the expected "<prefix>/skills/<name>/" prefix.
    data = _make_tar(
        {
            # First member establishes the GitHub "<repo>-<ref>/" prefix.
            "skills-main/README.md": b"top-level",
            "skills-main/skills/evil/SKILL.md": b"---\nname: evil\n---\n",
            "skills-main/skills/evil/../../../../pwned.txt": b"owned",
        }
    )
    fetcher = _fetcher_with_tar(data, monkeypatch)

    with pytest.raises(MarketplaceError, match="unsafe path"):
        fetcher.fetch("evil", tmp_path)

    # The whole install aborts: nothing is written inside or outside the dest.
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "pwned.txt").exists()


def test_fetch_accepts_legitimate_nested_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A normal skill with a nested subdirectory installs cleanly; the guard
    # must not reject legitimate paths that stay under the destination.
    data = _make_tar(
        {
            "skills-main/README.md": b"top-level",
            "skills-main/skills/good/SKILL.md": (
                b"---\nname: good\ndescription: A good skill.\n---\n\n# Good\n"
            ),
            "skills-main/skills/good/scripts/helper.py": b"print('hi')\n",
        }
    )
    fetcher = _fetcher_with_tar(data, monkeypatch)
    # Isolate the extraction-path guard from SKILL.md parsing rules: the only
    # post-extraction contract fetch() requires is a non-None load result.
    monkeypatch.setattr(marketplace, "load_skill_directory", lambda *a, **k: object())

    fetcher.fetch("good", tmp_path)

    assert (tmp_path / "good" / "SKILL.md").exists()
    assert (tmp_path / "good" / "scripts" / "helper.py").read_text() == "print('hi')\n"
