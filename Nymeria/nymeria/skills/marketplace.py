"""Marketplace fetchers for Agent Skills.

Phase 1 ships AnthropicSkillsFetcher (github.com/anthropics/skills). ClawHub
and arbitrary-git-URL fetchers are stubbed — their public interface is frozen
so the UI dropdowns and REST endpoints can target them unchanged when Phase 2
lands.

All fetchers conform to the SkillMarketplaceFetcher protocol:
    - list(query) -> List[MarketplaceSkillEntry]
    - fetch(name, target_dir) -> Skill  (the loaded skill from disk)
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

import yaml

from . import Skill, SkillParseError, load_skill_directory

logger = logging.getLogger(__name__)

ANTHROPIC_SKILLS_OWNER = "anthropics"
ANTHROPIC_SKILLS_REPO = "skills"
ANTHROPIC_SKILLS_REF = "main"
ANTHROPIC_SKILLS_PATH = "skills"  # subdirectory in the repo where individual skills live
GITHUB_API_BASE = "https://api.github.com"
GITHUB_CODELOAD_BASE = "https://codeload.github.com"

LIST_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes, per research-doc recommendation
TARBALL_CACHE_TTL_SECONDS = 15 * 60

# Basic security red flags we scan for before writing to disk.
SUSPICIOUS_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*(bash|sh)\b", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*(bash|sh)\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*($|[^a-zA-Z])"),
    re.compile(r"\beval\s*\(\s*base64\.", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\|:"),  # fork bomb
]


@dataclass
class MarketplaceSkillEntry:
    """A skill visible in a marketplace listing."""

    name: str
    description: str
    source: str  # "anthropic", "clawhub", "git", ...
    repo_url: Optional[str] = None


class MarketplaceError(Exception):
    """Raised when a marketplace operation fails."""


class SkillMarketplaceFetcher(Protocol):
    source_name: str

    def list(self, query: Optional[str] = None) -> List[MarketplaceSkillEntry]: ...
    def fetch(self, name: str, target_dir: Path) -> Skill: ...


def scan_for_suspicious_patterns(text: str) -> List[str]:
    """Return a list of suspicious pattern descriptions found in *text*."""
    hits: List[str] = []
    for pat in SUSPICIOUS_PATTERNS:
        if pat.search(text):
            hits.append(pat.pattern)
    return hits


class AnthropicSkillsFetcher:
    """Fetches skills from anthropics/skills on GitHub via a cached tarball.

    One HTTP GET to GitHub's codeload tarball endpoint pulls the entire
    repository at ~a few MB. We cache the bytes for 15 minutes and walk the
    archive in-memory for both `list()` (extract all SKILL.md frontmatter)
    and `fetch()` (extract a single skill's subtree to disk). This replaces
    an N+1 per-skill fetch pattern.
    """

    source_name = "anthropic"

    def __init__(self, http_session=None):
        # Lazy import so a test that doesn't hit the network doesn't need requests.
        if http_session is None:
            import requests
            self._http = requests.Session()
            self._http.headers.update({"Accept": "application/vnd.github+json"})
        else:
            self._http = http_session

        self._tarball_cache: Optional[Tuple[float, bytes]] = None
        self._list_cache: Optional[Tuple[float, List[MarketplaceSkillEntry]]] = None
        self._cache_lock = threading.Lock()

    # ----- internals --------------------------------------------------

    def _tarball_url(self) -> str:
        return (
            f"{GITHUB_CODELOAD_BASE}/{ANTHROPIC_SKILLS_OWNER}/"
            f"{ANTHROPIC_SKILLS_REPO}/tar.gz/refs/heads/{ANTHROPIC_SKILLS_REF}"
        )

    def _get_tarball(self) -> bytes:
        """Return the anthropics/skills:main tarball bytes, downloading if stale."""
        now = time.time()
        with self._cache_lock:
            if self._tarball_cache and now - self._tarball_cache[0] < TARBALL_CACHE_TTL_SECONDS:
                return self._tarball_cache[1]

        url = self._tarball_url()
        logger.info("downloading anthropics/skills tarball from %s", url)
        try:
            resp = self._http.get(url, timeout=30, stream=False)
        except Exception as e:
            raise MarketplaceError(f"tarball download failed: {type(e).__name__}: {e}") from e
        if resp.status_code == 429:
            raise MarketplaceError("GitHub rate-limited the tarball download — try again shortly")
        if resp.status_code == 404:
            raise MarketplaceError(f"tarball not found (branch moved?): {url}")
        try:
            resp.raise_for_status()
        except Exception as e:
            raise MarketplaceError(f"tarball download HTTP {resp.status_code}: {e}") from e

        data = resp.content
        with self._cache_lock:
            self._tarball_cache = (now, data)
        return data

    @staticmethod
    def _open_tar(data: bytes) -> tarfile.TarFile:
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")

    @staticmethod
    def _archive_prefix(tf: tarfile.TarFile) -> str:
        """GitHub tarballs wrap everything in a single top-level directory
        named <repo>-<sha>/. Return that prefix with trailing slash."""
        for member in tf:
            top = member.name.split("/", 1)[0]
            if top:
                return top + "/"
        raise MarketplaceError("empty tarball")

    def _extract_description(self, skill_md_text: str) -> str:
        if not skill_md_text.startswith("---"):
            return ""
        parts = skill_md_text.split("---", 2)
        if len(parts) < 3:
            return ""
        try:
            data = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return ""
        return str(data.get("description", "")).strip()

    def _parse_index(self, data: bytes) -> List[MarketplaceSkillEntry]:
        """Walk the tarball, pluck out SKILL.md files under skills/*/SKILL.md."""
        entries: List[MarketplaceSkillEntry] = []
        seen_names: set[str] = set()
        with self._open_tar(data) as tf:
            prefix = self._archive_prefix(tf)
            skills_prefix = f"{prefix}{ANTHROPIC_SKILLS_PATH}/"
            for member in tf:
                if not member.isfile():
                    continue
                if not member.name.startswith(skills_prefix):
                    continue
                tail = member.name[len(skills_prefix):]
                # tail looks like "<skill-name>/SKILL.md" or "<skill-name>/subdir/..."
                pieces = tail.split("/")
                if len(pieces) != 2 or pieces[1] != "SKILL.md":
                    continue
                skill_name = pieces[0]
                if not skill_name or skill_name.startswith(".") or skill_name in seen_names:
                    continue
                seen_names.add(skill_name)
                f = tf.extractfile(member)
                if f is None:
                    continue
                try:
                    text = f.read().decode("utf-8", errors="replace")
                except Exception:
                    continue
                desc = self._extract_description(text)
                entries.append(
                    MarketplaceSkillEntry(
                        name=skill_name,
                        description=desc or f"(no description found for {skill_name})",
                        source=self.source_name,
                        repo_url=(
                            f"https://github.com/{ANTHROPIC_SKILLS_OWNER}/"
                            f"{ANTHROPIC_SKILLS_REPO}/tree/{ANTHROPIC_SKILLS_REF}/"
                            f"{ANTHROPIC_SKILLS_PATH}/{skill_name}"
                        ),
                    )
                )
        entries.sort(key=lambda e: e.name)
        return entries

    # ----- public API -------------------------------------------------

    def list(self, query: Optional[str] = None) -> List[MarketplaceSkillEntry]:
        """List skills available in anthropics/skills (tarball-backed, 15-min cache)."""
        now = time.time()
        with self._cache_lock:
            cached_ok = (
                self._list_cache
                and now - self._list_cache[0] < LIST_CACHE_TTL_SECONDS
            )
            entries = self._list_cache[1] if cached_ok else None

        if entries is None:
            data = self._get_tarball()
            entries = self._parse_index(data)
            with self._cache_lock:
                self._list_cache = (now, entries)

        if query:
            q = query.strip().lower()
            entries = [
                e for e in entries
                if q in e.name.lower() or q in e.description.lower()
            ]
        return entries

    def fetch(self, name: str, target_dir: Path) -> Skill:
        """Extract a single skill's subtree from the cached tarball onto disk.

        Reuses the 15-min-cached tarball if it's still fresh — a typical
        "search → install" flow hits the network exactly once.
        """
        skill_dir = target_dir / name
        if skill_dir.exists():
            raise MarketplaceError(f"skill directory already exists: {skill_dir}")

        data = self._get_tarball()

        staged: List[Tuple[Path, bytes]] = []
        warnings: List[str] = []
        matched_any = False

        with self._open_tar(data) as tf:
            prefix = self._archive_prefix(tf)
            skill_prefix = f"{prefix}{ANTHROPIC_SKILLS_PATH}/{name}/"
            for member in tf:
                if not member.isfile():
                    continue
                if not member.name.startswith(skill_prefix):
                    continue
                matched_any = True
                rel_path = member.name[len(skill_prefix):]
                if not rel_path:
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                content_bytes = f.read()

                # Security scan on textual files (best effort).
                if rel_path.endswith((".md", ".sh", ".py", ".bash", ".txt")):
                    try:
                        text = content_bytes.decode("utf-8", errors="replace")
                        hits = scan_for_suspicious_patterns(text)
                        if hits:
                            warnings.append(f"{rel_path}: {hits}")
                    except Exception:
                        pass

                staged.append((skill_dir / rel_path, content_bytes))

        if not matched_any:
            raise MarketplaceError(f"no files found for skill {name!r} in anthropics/skills")

        if warnings:
            logger.warning(
                "installing %s with %d suspicious-pattern warnings: %s",
                name, len(warnings), warnings,
            )

        skill_dir.mkdir(parents=True, exist_ok=False)
        try:
            for path, content in staged:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except Exception:
            import shutil
            shutil.rmtree(skill_dir, ignore_errors=True)
            raise

        scope = "user" if "users/" in str(target_dir.resolve()) else "global"
        user_id = None
        if scope == "user":
            user_id = target_dir.name

        loaded = load_skill_directory(skill_dir, scope=scope, user_id=user_id)
        if loaded is None:
            raise MarketplaceError(
                f"installed {name} but its SKILL.md failed to parse after download"
            )
        return loaded


class ClawHubFetcher:
    """Stub for the ClawHub registry (openclaw.io). Phase 2."""

    source_name = "clawhub"

    def list(self, query: Optional[str] = None) -> List[MarketplaceSkillEntry]:
        raise NotImplementedError("ClawHub fetcher is not yet implemented")

    def fetch(self, name: str, target_dir: Path) -> Skill:
        raise NotImplementedError("ClawHub fetcher is not yet implemented")


class GitUrlFetcher:
    """Stub for arbitrary-git-URL installs. Phase 2."""

    source_name = "git"

    def list(self, query: Optional[str] = None) -> List[MarketplaceSkillEntry]:
        raise NotImplementedError("Git URL fetcher is not yet implemented")

    def fetch(self, name: str, target_dir: Path) -> Skill:
        raise NotImplementedError("Git URL fetcher is not yet implemented")


# Module-level registry so callers can resolve by source string.
_FETCHERS: Dict[str, SkillMarketplaceFetcher] = {}


def get_fetcher(source: str) -> SkillMarketplaceFetcher:
    """Return a cached fetcher for the given source."""
    if source in _FETCHERS:
        return _FETCHERS[source]
    if source == "anthropic":
        _FETCHERS[source] = AnthropicSkillsFetcher()
    elif source == "clawhub":
        _FETCHERS[source] = ClawHubFetcher()
    elif source == "git":
        _FETCHERS[source] = GitUrlFetcher()
    else:
        raise MarketplaceError(f"unknown marketplace source: {source!r}")
    return _FETCHERS[source]


__all__ = [
    "AnthropicSkillsFetcher",
    "ClawHubFetcher",
    "GitUrlFetcher",
    "MarketplaceError",
    "MarketplaceSkillEntry",
    "SkillMarketplaceFetcher",
    "get_fetcher",
    "scan_for_suspicious_patterns",
]
