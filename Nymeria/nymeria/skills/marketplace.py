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

import base64
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol

import yaml

from . import Skill, SkillParseError, load_skill_directory

logger = logging.getLogger(__name__)

ANTHROPIC_SKILLS_OWNER = "anthropics"
ANTHROPIC_SKILLS_REPO = "skills"
ANTHROPIC_SKILLS_PATH = "skills"  # subdirectory in the repo where individual skills live
GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"

LIST_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes, per research-doc recommendation

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
    """Fetches skills from anthropics/skills on GitHub via the public API.

    Uses the GitHub Contents API (anonymous, 60 req/hr unauthenticated limit).
    Lists are cached 15 minutes; per-skill description is fetched lazily.
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

        self._list_cache: Optional[tuple[float, List[MarketplaceSkillEntry]]] = None
        self._list_lock = threading.Lock()
        self._desc_cache: Dict[str, str] = {}

    # ----- internals --------------------------------------------------

    def _contents_url(self, subpath: str = "") -> str:
        tail = f"/{subpath}" if subpath else ""
        return (
            f"{GITHUB_API_BASE}/repos/{ANTHROPIC_SKILLS_OWNER}/"
            f"{ANTHROPIC_SKILLS_REPO}/contents/{ANTHROPIC_SKILLS_PATH}{tail}"
        )

    def _raw_url(self, subpath: str) -> str:
        return (
            f"{GITHUB_RAW_BASE}/{ANTHROPIC_SKILLS_OWNER}/"
            f"{ANTHROPIC_SKILLS_REPO}/main/{ANTHROPIC_SKILLS_PATH}/{subpath}"
        )

    def _get_json(self, url: str):
        resp = self._http.get(url, timeout=15)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise MarketplaceError(
                "GitHub API rate limit hit for anonymous requests — try again later"
            )
        if resp.status_code == 404:
            raise MarketplaceError(f"not found: {url}")
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, url: str) -> str:
        resp = self._http.get(url, timeout=15)
        if resp.status_code == 404:
            raise MarketplaceError(f"not found: {url}")
        resp.raise_for_status()
        return resp.text

    def _extract_description(self, skill_md_text: str) -> str:
        """Quick-and-dirty parse of just the description out of a SKILL.md."""
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

    # ----- public API -------------------------------------------------

    def list(self, query: Optional[str] = None) -> List[MarketplaceSkillEntry]:
        """List skills available in anthropics/skills.

        Returns cached result if last fetch was < 15 min ago. Descriptions are
        fetched on demand the first time a skill's entry is rendered, and
        cached thereafter.
        """
        now = time.time()
        with self._list_lock:
            if self._list_cache and now - self._list_cache[0] < LIST_CACHE_TTL_SECONDS:
                entries = self._list_cache[1]
            else:
                entries = self._fetch_list()
                self._list_cache = (now, entries)

        if query:
            q = query.strip().lower()
            entries = [
                e for e in entries
                if q in e.name.lower() or q in e.description.lower()
            ]
        return entries

    def _fetch_list(self) -> List[MarketplaceSkillEntry]:
        logger.info("fetching Anthropic skills index from %s", self._contents_url())
        items = self._get_json(self._contents_url())
        if not isinstance(items, list):
            raise MarketplaceError(
                f"unexpected GitHub response shape: {type(items).__name__}"
            )

        entries: List[MarketplaceSkillEntry] = []
        for item in items:
            if item.get("type") != "dir":
                continue
            name = item.get("name", "")
            if not name or name.startswith("."):
                continue
            # Cheap first pass: use the directory name as a fallback description
            # so the initial list returns fast. Descriptions are filled in lazily.
            desc = self._desc_cache.get(name, "")
            if not desc:
                try:
                    text = self._get_text(self._raw_url(f"{name}/SKILL.md"))
                    desc = self._extract_description(text)
                    self._desc_cache[name] = desc
                except MarketplaceError:
                    desc = ""
            entries.append(MarketplaceSkillEntry(
                name=name,
                description=desc or f"(no description found for {name})",
                source=self.source_name,
                repo_url=item.get("html_url"),
            ))
        return entries

    def fetch(self, name: str, target_dir: Path) -> Skill:
        """Download skill directory contents into *target_dir*/<name>/.

        Walks the GitHub Contents API tree (recursive), writes files via the
        raw-content endpoint. Runs a security scan on SKILL.md and every
        script/reference before committing to disk.

        Returns the loaded Skill (parsed from the just-written directory).
        Raises MarketplaceError on any failure.
        """
        skill_dir = target_dir / name
        if skill_dir.exists():
            raise MarketplaceError(f"skill directory already exists: {skill_dir}")

        # Use the Trees API to list all files under the skill's subtree in one call.
        tree_url = (
            f"{GITHUB_API_BASE}/repos/{ANTHROPIC_SKILLS_OWNER}/"
            f"{ANTHROPIC_SKILLS_REPO}/git/trees/main?recursive=1"
        )
        tree = self._get_json(tree_url)
        if tree.get("truncated"):
            logger.warning("GitHub tree response was truncated — skill files may be missing")

        prefix = f"{ANTHROPIC_SKILLS_PATH}/{name}/"
        files = [
            item for item in tree.get("tree", [])
            if item.get("type") == "blob" and item.get("path", "").startswith(prefix)
        ]
        if not files:
            raise MarketplaceError(f"no files found for skill {name!r} in anthropics/skills")

        staged: List[tuple[Path, bytes]] = []
        warnings: List[str] = []

        for item in files:
            rel_path = item["path"][len(prefix):]
            if not rel_path:
                continue
            # Fetch blob contents via the blob SHA (handles large files and binaries).
            blob = self._get_json(
                f"{GITHUB_API_BASE}/repos/{ANTHROPIC_SKILLS_OWNER}/"
                f"{ANTHROPIC_SKILLS_REPO}/git/blobs/{item['sha']}"
            )
            if blob.get("encoding") == "base64":
                content_bytes = base64.b64decode(blob.get("content", ""))
            else:
                content_bytes = blob.get("content", "").encode("utf-8")

            # Security scan on textual files only (best effort).
            if rel_path.endswith((".md", ".sh", ".py", ".bash", ".txt")):
                try:
                    text = content_bytes.decode("utf-8", errors="replace")
                    hits = scan_for_suspicious_patterns(text)
                    if hits:
                        warnings.append(f"{rel_path}: {hits}")
                except Exception:
                    pass

            staged.append((skill_dir / rel_path, content_bytes))

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
            # Roll back partial write.
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
