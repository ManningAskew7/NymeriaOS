"""Agent Skills module — Anthropic SKILL.md compatible progressive-disclosure system.

A skill is a directory containing:
    SKILL.md        required — YAML frontmatter + markdown body
    scripts/        optional — scripts invoked via the existing bash_execute tool
    references/     optional — markdown files the agent reads on demand
    assets/         optional — binary/template files, referenced by path only

Scope model (precedence for name collisions, highest wins):
    thread > user > global > bundled

A skill is "active" on a thread if its name appears in the union of:
    user_profile.enabled_global_skills + ThreadConfig.enabled_skills
    minus ThreadConfig.disabled_skills.

Only the (name, description) of active skills are always-present (injected into
the Skill meta-tool's description). The full body loads only when the agent
calls Skill(name).
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from functools import cached_property
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from ..core.storage_paths import (
    FileFingerprint,
    compare_fingerprint,
    scan_fingerprint_map,
)
from ..core.time_utils import parse_tool_ttl

logger = logging.getLogger(__name__)

KEBAB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")

# Kit-declared thread-template TOOL names (normalized: lowercase, hyphens
# become underscores). Must be a valid provider tool-name shape.
TEMPLATE_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

SkillScope = Literal["bundled", "global", "user"]

# Anthropic's documented upper bound on the <available_skills> block.
AVAILABLE_SKILLS_CHAR_BUDGET = 15_000
DEFAULT_SKILL_KIT_TOOL_TTL = "2h"

# spawn_thread's instructions cap; template instructions honor the same bound.
TEMPLATE_INSTRUCTIONS_MAX_CHARS = 5000


class SkillParseError(Exception):
    """Raised when a SKILL.md file cannot be parsed."""


class ThreadTemplate(BaseModel):
    """A kit-declared callable-thread template (backlog #26).

    Declared under ``metadata.nymeria.thread_templates``. Surfaces as a
    callable-thread TOOL when the kit is active; the thread itself is
    materialized lazily on the tool's first call via the spawn_thread
    machinery (so spawn depth/rate caps and role gates apply to the caller).
    ``extra="forbid"`` so a typo'd key fails authoring validation instead of
    being silently ignored.
    """

    model_config = {"extra": "forbid"}

    name: str
    description: str = Field(..., min_length=1, max_length=1024)
    title: Optional[str] = None
    instructions: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    tools: List[str] = Field(default_factory=list)
    kit: Optional[str] = None
    ttl_hours: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _tool_name(cls, v: str) -> str:
        normalized = str(v).strip().lower().replace("-", "_")
        if not TEMPLATE_TOOL_NAME_RE.match(normalized):
            raise ValueError(
                "template name must normalize to a valid tool name "
                f"(lowercase letters, digits, underscores, max 64 chars): {v!r}"
            )
        return normalized

    @field_validator("instructions")
    @classmethod
    def _instructions_cap(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > TEMPLATE_INSTRUCTIONS_MAX_CHARS:
            raise ValueError(
                f"template instructions exceed {TEMPLATE_INSTRUCTIONS_MAX_CHARS} chars"
            )
        return v

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, list):
            raise ValueError("template tools must be a list of tool names")
        out: List[str] = []
        seen = set()
        for item in v:
            name = str(item).strip()
            if not name or name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @field_validator("ttl_hours")
    @classmethod
    def _ttl_floor(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and int(v) < 1:
            raise ValueError("template ttl_hours must be >= 1 (or omitted for permanent)")
        return v

    @property
    def thread_title(self) -> str:
        """The materialized thread's title (declared, or derived from name)."""
        if self.title and self.title.strip():
            return self.title.strip()[:80]
        return self.name.replace("_", " ").title()[:80]


class SkillFrontmatter(BaseModel):
    """YAML frontmatter of a SKILL.md file.

    Matches Anthropic's strict spec: only name, description, license,
    allowed-tools, metadata are accepted. Extra keys trigger a warning at
    load time but do not reject the skill (forward-compat).
    """

    model_config = {"populate_by_name": True, "extra": "allow"}

    name: str
    description: str = Field(..., min_length=1, max_length=1024)
    license: Optional[str] = None
    allowed_tools: List[str] = Field(default_factory=list)
    metadata: Dict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _kebab_case(cls, v: str) -> str:
        if not KEBAB_NAME_RE.match(v):
            raise ValueError(
                f"skill name must be kebab-case (lowercase, digits, hyphens): {v!r}"
            )
        return v


class Skill(BaseModel):
    """A loaded skill — frontmatter + body + on-disk location."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    license: Optional[str] = None
    allowed_tools: List[str] = Field(default_factory=list)
    metadata: Dict = Field(default_factory=dict)
    body: str
    path: Path
    scope: SkillScope
    user_id: Optional[str] = None

    @property
    def _nymeria_metadata(self) -> Dict:
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        nymeria = metadata.get("nymeria", {})
        return nymeria if isinstance(nymeria, dict) else {}

    def _nymeria_name_list(self, key: str) -> List[str]:
        """Normalize a ``metadata.nymeria`` string-or-list field to names."""
        raw = self._nymeria_metadata.get(key, [])
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list):
            values = raw
        else:
            values = []

        out: List[str] = []
        seen = set()
        for value in values:
            name = str(value).strip()
            if not name or name in seen:
                continue
            out.append(name)
            seen.add(name)
        return out

    @property
    def required_tools(self) -> List[str]:
        """Nymeria tools this skill must bind when activated.

        This intentionally reads from ``metadata.nymeria.required_tools`` so
        portable ``allowed-tools`` remains advisory Agent Skills metadata.
        """
        return self._nymeria_name_list("required_tools")

    @property
    def required_skills(self) -> List[str]:
        """Skills or kits this kit pulls in on activation (one level deep).

        Reads ``metadata.nymeria.required_skills``. With defer=false a nested
        skill fully activates (its body is loaded and, if it is a kit, its
        required_tools bind too); with defer=true nested entries are listed as
        name + description only, loadable on demand via Skill().
        """
        return self._nymeria_name_list("required_skills")

    @cached_property
    def thread_templates(self) -> List["ThreadTemplate"]:
        """Kit-declared callable-thread templates (lenient at read time).

        Reads ``metadata.nymeria.thread_templates``. A malformed entry is
        logged and skipped here (matching the ``tool_ttl`` fallback posture for
        hand-edited files); ``skill_write``/``skill_edit`` reject the same
        input strictly. Duplicate template names keep the first entry.
        Parsed once per Skill instance (the property is hit several times per
        turn: graph build, fingerprint, payload surfaces), so a malformed
        hand-edited entry warns once per load, not once per access; skill
        refreshes build fresh instances, so edits still take effect.
        """
        raw = self._nymeria_metadata.get("thread_templates", [])
        if not isinstance(raw, list):
            if raw:
                logger.warning(
                    "skill %s: metadata.nymeria.thread_templates must be a "
                    "list; ignoring", self.name,
                )
            return []
        out: List[ThreadTemplate] = []
        seen: set = set()
        for entry in raw:
            try:
                template = ThreadTemplate.model_validate(entry)
            except ValidationError as exc:
                logger.warning(
                    "skill %s: skipping invalid thread template %r: %s",
                    self.name, entry, exc,
                )
                continue
            if template.name in seen:
                logger.warning(
                    "skill %s: duplicate thread template name %r; keeping first",
                    self.name, template.name,
                )
                continue
            seen.add(template.name)
            out.append(template)
        return out

    @property
    def tool_ttl(self) -> str:
        """TTL for Skill Kit tool bindings, defaulting to 2h."""
        value = str(
            self._nymeria_metadata.get("tool_ttl", DEFAULT_SKILL_KIT_TOOL_TTL)
        ).strip().lower()
        try:
            ttl_key, _ = parse_tool_ttl(value)
            return ttl_key
        except ValueError:
            logger.warning(
                "skill %s: invalid metadata.nymeria.tool_ttl %r; using %s",
                self.name,
                value,
                DEFAULT_SKILL_KIT_TOOL_TTL,
            )
            return DEFAULT_SKILL_KIT_TOOL_TTL

    @property
    def is_skill_kit(self) -> bool:
        """A skill with ANY declared dependency or template.

        All composition features (required tools, nested skills, thread
        templates) route through the kit surfaces (/kit, activation binding,
        UI "Kit" labels), so a tools-less skill that only nests other skills
        or declares templates still counts as a kit.
        """
        return bool(
            self.required_tools or self.required_skills or self.thread_templates
        )

    @property
    def is_internal(self) -> bool:
        return bool(self._nymeria_metadata.get("internal", False))

    @property
    def has_scripts(self) -> bool:
        scripts = self.path / "scripts"
        return scripts.is_dir() and any(scripts.iterdir())

    @property
    def has_references(self) -> bool:
        refs = self.path / "references"
        return refs.is_dir() and any(refs.iterdir())

    @property
    def has_assets(self) -> bool:
        assets = self.path / "assets"
        return assets.is_dir() and any(assets.iterdir())

    def list_scripts(self) -> List[str]:
        scripts = self.path / "scripts"
        if not scripts.is_dir():
            return []
        return sorted(
            str(p.relative_to(self.path))
            for p in scripts.rglob("*")
            if p.is_file()
        )

    def list_references(self) -> List[str]:
        refs = self.path / "references"
        if not refs.is_dir():
            return []
        return sorted(
            str(p.relative_to(self.path))
            for p in refs.rglob("*")
            if p.is_file()
        )


def _parse_allowed_tools(value) -> List[str]:
    """Normalize 'allowed-tools' from YAML (string or list) to a list of strings.

    Anthropic uses a comma-separated string with optional bash glob patterns
    like 'Read,Write,Bash(pdftotext:*)'. We store the raw tokens; matching is
    advisory, not enforced at tool-registration time.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def parse_skill_file(skill_md_path: Path) -> tuple[SkillFrontmatter, str]:
    """Parse a SKILL.md file into (frontmatter, body).

    Raises SkillParseError on any structural failure.
    """
    try:
        raw = skill_md_path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillParseError(f"cannot read {skill_md_path}: {e}") from e

    if not raw.startswith("---"):
        raise SkillParseError(
            f"{skill_md_path}: missing YAML frontmatter (file must start with '---')"
        )

    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise SkillParseError(f"{skill_md_path}: frontmatter not terminated by '---'")

    _, yaml_text, body = parts

    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"{skill_md_path}: invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise SkillParseError(f"{skill_md_path}: frontmatter is not a YAML mapping")

    # Normalize hyphenated key to underscore for pydantic.
    if "allowed-tools" in data and "allowed_tools" not in data:
        data["allowed_tools"] = _parse_allowed_tools(data.pop("allowed-tools"))
    elif "allowed_tools" in data:
        data["allowed_tools"] = _parse_allowed_tools(data["allowed_tools"])

    try:
        fm = SkillFrontmatter(**data)
    except ValidationError as e:
        raise SkillParseError(f"{skill_md_path}: frontmatter validation: {e}") from e

    # Warn on unknown keys (forward-compat but visible).
    known = {"name", "description", "license", "allowed_tools", "metadata"}
    extras = set(data.keys()) - known
    if extras:
        logger.warning(
            "skill %s: ignoring unknown frontmatter keys: %s", fm.name, sorted(extras)
        )

    return fm, body.lstrip("\n")


def load_skill_directory(
    skill_dir: Path,
    scope: SkillScope,
    user_id: Optional[str] = None,
) -> Optional[Skill]:
    """Load a skill from its directory. Returns None and logs on failure."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        logger.debug("skill dir %s has no SKILL.md, skipping", skill_dir)
        return None

    try:
        fm, body = parse_skill_file(skill_md)
    except SkillParseError as e:
        logger.warning("skipping skill at %s: %s", skill_dir, e)
        return None

    # Enforce convention: directory name == frontmatter name.
    if skill_dir.name != fm.name:
        logger.warning(
            "skill directory %s does not match frontmatter name %r; using frontmatter name",
            skill_dir, fm.name,
        )

    return Skill(
        name=fm.name,
        description=fm.description,
        license=fm.license,
        allowed_tools=fm.allowed_tools,
        metadata=fm.metadata,
        body=body,
        path=skill_dir,
        scope=scope,
        user_id=user_id,
    )


def resolve_nested_skills(
    skill: Skill,
    skill_manager: Optional["SkillManager"],
    user_id: Optional[str] = None,
    snapshot_by_name: Optional[Dict[str, Skill]] = None,
) -> Tuple[List[Skill], List[str]]:
    """Resolve a kit's ``required_skills`` one level deep.

    Returns ``(nested_skills, missing_names)``. Resolution prefers the live
    ``skill_manager`` (user > global > bundled precedence) and falls back to
    ``snapshot_by_name`` (a graph-build snapshot) when no manager is available.
    Self-references and duplicate names are skipped. Nesting is deliberately
    ONE level deep: a nested kit's own ``required_skills`` are never expanded
    here (callers list them instead), which also makes cycles a non-issue.
    """
    nested: List[Skill] = []
    missing: List[str] = []
    seen = {skill.name}
    for name in skill.required_skills:
        if name in seen:
            continue
        seen.add(name)
        resolved: Optional[Skill] = None
        if skill_manager is not None:
            try:
                resolved = skill_manager.get(name, user_id=user_id)
            except Exception:  # noqa: BLE001 - resolution is best-effort per name
                logger.warning(
                    "nested skill lookup failed for %r (required by %r)",
                    name, skill.name, exc_info=True,
                )
        if resolved is None and snapshot_by_name:
            resolved = snapshot_by_name.get(name)
        if resolved is None:
            missing.append(name)
            continue
        nested.append(resolved)
    return nested, missing


def expanded_required_tools(skill: Skill, nested_skills: List[Skill]) -> List[str]:
    """The deduped, order-preserving tool union a kit activation must bind.

    The outer kit's ``required_tools`` first, then each nested kit's, in
    declaration order. This is the one list a defer=false activation binds in
    a single strict call (atomic no-partial-activation).
    """
    out: List[str] = []
    seen: set = set()
    for source in [skill, *nested_skills]:
        for name in source.required_tools:
            if name in seen:
                continue
            out.append(name)
            seen.add(name)
    return out


class SkillManager:
    """Discovers, caches, and resolves skills across the four scope layers.

    Instantiated once by the agent on startup; the agent calls reload() after
    install/uninstall operations.

    Thread-safety: discovery is guarded by a lock. Read methods read from the
    in-memory cache which is only swapped atomically.

    An optional :class:`SkillEmbeddingIndex` gets refreshed on every reload so
    the ``search_skills`` agent-facing tool has an up-to-date semantic + BM25
    index of the installed pool.
    """

    # Minimum seconds between external-edit stat scans (hot-load debounce).
    # Class attribute so tests can zero it.
    EXTERNAL_REFRESH_INTERVAL_SECONDS: float = 2.0

    def __init__(
        self,
        bundled_dir: Path,
        data_skills_dir: Path,
        embedding_index=None,
    ):
        self._bundled_dir = bundled_dir
        self._data_dir = data_skills_dir
        self._global_dir = data_skills_dir / "global"
        self._users_dir = data_skills_dir / "users"

        # Per-user-and-scope caches keyed to avoid cross-user leakage.
        self._lock = threading.RLock()
        self._bundled: Dict[str, Skill] = {}
        self._global: Dict[str, Skill] = {}
        self._per_user: Dict[str, Dict[str, Skill]] = {}
        self._embedding_index = embedding_index

        # Hot-load bookkeeping (resource-filesystem-layout plan, slice 2):
        # SKILL.md path str -> FileFingerprint captured at scan time, so raw
        # file edits are detected without an explicit reload().
        self._scan_sigs: Dict[str, FileFingerprint] = {}
        self._last_freshness_check = 0.0
        self._embed_rebuild_running = False
        self._embed_rebuild_dirty = False

        self._ensure_dirs()
        self.reload()

    @property
    def embedding_index(self):
        """Return the attached SkillEmbeddingIndex, or None if search is keyword-only."""
        return self._embedding_index

    def _ensure_dirs(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._global_dir.mkdir(parents=True, exist_ok=True)
        self._users_dir.mkdir(parents=True, exist_ok=True)
        # Bundled dir may not exist on fresh checkouts — that's fine.

    @staticmethod
    def _scan_dir(scope_dir: Path, scope: SkillScope, user_id: Optional[str] = None) -> Dict[str, Skill]:
        out: Dict[str, Skill] = {}
        if not scope_dir.is_dir():
            return out
        for entry in sorted(scope_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill = load_skill_directory(entry, scope=scope, user_id=user_id)
            if skill is None:
                continue
            if skill.name in out:
                logger.warning(
                    "duplicate skill name %r in %s — keeping first",
                    skill.name, scope_dir,
                )
                continue
            out[skill.name] = skill
        return out

    def reload(self) -> None:
        """Rescan all four scope layers from disk."""
        self._rescan_from_disk()
        # Refresh the installed embedding index if one is attached. Inline on
        # purpose: reload() callers are the explicit install/uninstall/write
        # paths and tests, which expect the index current on return. The
        # hot-load refresh path uses the background variant instead.
        self._rebuild_embedding_index_now()

    def _collect_scan_sigs(
        self, previous: Optional[Dict[str, FileFingerprint]] = None
    ) -> Tuple[Dict[str, FileFingerprint], List[str]]:
        """Fingerprint every SKILL.md across the scope roots (no parsing).

        Returns the fresh map plus the keys that changed (edited, added, or
        removed). The KEYS, not just a bool, because the audit must name the
        same files this verdict was derived from rather than re-deriving them
        by comparing the two maps, which is a comparison the fingerprint type
        does not support (see ``storage_paths.FileFingerprint``).

        Keyed by full path because skills live one directory deep under
        several scope roots, unlike the flat JSON stores that key by filename.
        """
        entries: List[Tuple[str, Path]] = []
        roots: List[Path] = [self._bundled_dir, self._global_dir]
        try:
            if self._users_dir.is_dir():
                roots.extend(p for p in self._users_dir.iterdir() if p.is_dir())
        except OSError:
            pass  # Unreadable users dir: scan the fixed roots only.
        for root in roots:
            if not root.is_dir():
                continue
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for entry in children:
                skill_md = entry / "SKILL.md"
                entries.append((str(skill_md), skill_md))
        sigs, changed, removed = scan_fingerprint_map(entries, previous)
        return sigs, changed + removed

    def _rescan_from_disk(self) -> None:
        """Rebuild the in-memory caches (and scan fingerprints) from disk."""
        # Collect fingerprints BEFORE parsing, so a file changing mid-scan is
        # read as newer content against an older fingerprint and the next
        # freshness check re-detects it. That ordering only pays off because
        # the fingerprint is content-exact for recently written files: mtime
        # is stamped from a coarse (~1ms) clock, so a same-size write landing
        # in the scan's own tick would otherwise leave (mtime, size) untouched
        # and be missed permanently rather than re-detected.
        with self._lock:
            previous = dict(self._scan_sigs)
        sigs, _ = self._collect_scan_sigs(previous)
        with self._lock:
            self._bundled = self._scan_dir(self._bundled_dir, scope="bundled")
            self._global = self._scan_dir(self._global_dir, scope="global")
            self._per_user = {}
            if self._users_dir.is_dir():
                for user_dir in self._users_dir.iterdir():
                    if user_dir.is_dir():
                        self._per_user[user_dir.name] = self._scan_dir(
                            user_dir, scope="user", user_id=user_dir.name,
                        )
            self._scan_sigs = sigs
        total = len(self._bundled) + len(self._global) + sum(len(v) for v in self._per_user.values())
        logger.info(
            "SkillManager loaded %d skills (bundled=%d global=%d users=%d)",
            total, len(self._bundled), len(self._global),
            sum(len(v) for v in self._per_user.values()),
        )

    def refresh_if_stale(self, force: bool = False) -> bool:
        """Pick up external (non-manager) SKILL.md edits, adds, and removals.

        Debounced stat scan across the scope roots; on any change, rescans
        the caches and schedules a background embedding rebuild (never
        inline: a raw edit must not pay the embed cost on the turn path).
        Returns True when a change was applied.

        The scan itself runs outside the lock (it stats every skill file and
        may hash a few), so two callers that both get past the debounce can
        scan the same edit and each audit it. The debounce closes that window
        for ordinary calls by stamping under the lock BEFORE scanning; only
        concurrent ``force=True`` callers can still overlap, and the cost is a
        duplicate audit line, never a wrong cache value.
        """
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_freshness_check < self.EXTERNAL_REFRESH_INTERVAL_SECONDS:
                return False
            self._last_freshness_check = now

        with self._lock:
            previous = dict(self._scan_sigs)
        sigs, changed_keys = self._collect_scan_sigs(previous)
        if not changed_keys:
            return False
        logger.info("Skill store: external edit detected; rescanning")
        self._rescan_from_disk()
        self._schedule_embedding_rebuild()
        self._audit_external_change(changed_keys)
        return True

    def _audit_external_change(self, changed: "Iterable[str]") -> None:
        """User-attributed audit line(s) for raw skill-file changes.

        Takes the keys the scan itself judged changed, rather than re-deriving
        them. Seeing an edit and AUDITING it are separate contracts, and a
        second derivation is a second chance to disagree with the first: an
        earlier version did exactly that and silently dropped the audit line
        for the same-tick edits this machinery exists to detect, while the
        hot-load kept working and the tests kept passing.
        """
        changed = set(changed)
        if not changed:
            return
        by_user: Dict[str, List[str]] = {}
        users_root = str(self._users_dir)
        for key in changed:
            path = Path(key)
            user = "default"
            try:
                rel = path.relative_to(users_root)
                user = rel.parts[0] if rel.parts else "default"
            except ValueError:
                pass  # Not under users/: a global or bundled skill.
            by_user.setdefault(user, []).append(path.parent.name)
        try:
            from ..core.activity_log import log_external_edit

            for user, names in by_user.items():
                log_external_edit("skills", f"skill dirs {sorted(names)}", user_id=user)
        except Exception:  # noqa: BLE001 - audit must never break a scan
            logger.debug("Failed to record skills external-edit audit", exc_info=True)

    def _rebuild_embedding_index_now(self) -> None:
        """Rebuild the installed-skills embedding index inline (best-effort)."""
        if self._embedding_index is None:
            return
        try:
            # De-dupe by name with user > global > bundled precedence so the
            # index matches what `get()` would resolve for any user.
            with self._lock:
                merged: Dict[str, Skill] = {}
                merged.update(self._bundled)
                merged.update(self._global)
                for user_map in self._per_user.values():
                    merged.update(user_map)
                items = list(merged.values())
            summary = self._embedding_index.rebuild(
                namespace="installed",
                items=items,
            )
            logger.info(
                "skills index rebuilt: fts=%d semantic=%d%s",
                summary.get("fts_indexed", 0),
                summary.get("semantic_indexed", 0),
                f" warning={summary.get('warning')!r}" if summary.get("warning") else "",
            )
        except Exception as e:
            logger.warning("skills embedding index rebuild failed: %s", e)

    def _schedule_embedding_rebuild(self) -> None:
        """Rebuild the embedding index on a background thread, coalescing.

        A rebuild embeds every installed skill, so the hot-load path must not
        run it inline. The dirty flag coalesces bursts: changes that land
        while a rebuild is running trigger exactly one follow-up pass.
        """
        if self._embedding_index is None:
            return
        with self._lock:
            self._embed_rebuild_dirty = True
            if self._embed_rebuild_running:
                return
            self._embed_rebuild_running = True

        def _run() -> None:
            try:
                while True:
                    with self._lock:
                        if not self._embed_rebuild_dirty:
                            self._embed_rebuild_running = False
                            return
                        self._embed_rebuild_dirty = False
                    self._rebuild_embedding_index_now()
            except Exception:  # noqa: BLE001 - daemon thread must not die noisily
                with self._lock:
                    self._embed_rebuild_running = False
                logger.exception("Background skills index rebuild failed")

        threading.Thread(
            target=_run, name="skills-embed-rebuild", daemon=True
        ).start()

    def _user_scope(self, user_id: Optional[str]) -> Dict[str, Skill]:
        if not user_id:
            return {}
        return self._per_user.get(user_id, {})

    def list_installed(self, user_id: Optional[str] = None) -> List[Skill]:
        """All skills visible to this user across all four layers.

        Precedence for name collisions: user > global > bundled.
        """
        self.refresh_if_stale()
        with self._lock:
            merged: Dict[str, Skill] = {}
            merged.update(self._bundled)
            merged.update(self._global)
            merged.update(self._user_scope(user_id))
            return sorted(merged.values(), key=lambda s: s.name)

    def _resolve_cached(self, name: str, user_id: Optional[str]) -> Optional[Skill]:
        """Precedence resolution against the in-memory caches (caller locks)."""
        user_skills = self._user_scope(user_id)
        if name in user_skills:
            return user_skills[name]
        if name in self._global:
            return self._global[name]
        if name in self._bundled:
            return self._bundled[name]
        return None

    def get(self, name: str, user_id: Optional[str] = None) -> Optional[Skill]:
        """Resolve a skill by name, honoring precedence.

        Reads through to disk: the debounced scan runs first so newly created
        directories appear (including a higher-precedence same-name skill
        shadowing the cached resolution), then a raw edit to the resolved
        skill's SKILL.md is reparsed on the spot (one stat when unchanged).
        """
        self.refresh_if_stale()
        with self._lock:
            skill = self._resolve_cached(name, user_id)
        if skill is None:
            return None
        return self._fresh_skill(skill, user_id)

    def _fresh_skill(self, skill: Skill, user_id: Optional[str]) -> Optional[Skill]:
        """Return ``skill``, reparsing its directory if the file changed."""
        skill_md = skill.path / "SKILL.md"
        key = str(skill_md)
        with self._lock:
            previous = self._scan_sigs.get(key)
        sig, changed = compare_fingerprint(skill_md, previous)
        if sig is None:
            # Deleted or unreadable out-of-band: rescan so a lower-precedence
            # skill of the same name (or None) resolves.
            self.refresh_if_stale(force=True)
            with self._lock:
                return self._resolve_cached(skill.name, user_id)
        if not changed:
            return skill

        reloaded = load_skill_directory(
            skill.path, scope=skill.scope, user_id=skill.user_id
        )
        if reloaded is None:
            # The edited file no longer parses: keep serving the cached copy
            # (matching load-time skip-on-bad-file behavior) but record the
            # fingerprint so every call does not re-parse the broken file.
            with self._lock:
                self._scan_sigs[key] = sig
            logger.warning(
                "skill %r changed on disk but no longer parses; serving the "
                "cached copy", skill.name,
            )
            return skill
        if reloaded.name != skill.name:
            # Renamed in frontmatter: the cheap in-place swap would leave the
            # old name dangling, so do a full rescan.
            self._rescan_from_disk()
            self._schedule_embedding_rebuild()
            with self._lock:
                return self._resolve_cached(skill.name, user_id)

        with self._lock:
            self._scan_sigs[key] = sig
            if skill.scope == "bundled":
                self._bundled[skill.name] = reloaded
            elif skill.scope == "global":
                self._global[skill.name] = reloaded
            elif skill.user_id:
                self._per_user.setdefault(skill.user_id, {})[skill.name] = reloaded
        self._schedule_embedding_rebuild()
        try:
            from ..core.activity_log import log_external_edit

            log_external_edit(
                "skills",
                f"skill {skill.name!r} reparsed after a raw edit",
                user_id=skill.user_id or "default",
            )
        except Exception:  # noqa: BLE001 - audit must never break a read
            logger.debug("Failed to record skill external-edit audit", exc_info=True)
        return reloaded

    def list_for_thread(
        self,
        user_id: str,
        enabled_global_skills: List[str],
        thread_enabled_skills: List[str],
        thread_disabled_skills: List[str],
    ) -> List[Skill]:
        """Resolve the set of skills active for a given thread.

        Active set = (enabled_global_skills ∪ thread_enabled_skills) −
        thread_disabled_skills, with each name resolved via get()
        (user > global > bundled precedence).
        """
        self.refresh_if_stale()
        active_names: List[str] = []
        seen = set()
        configured_names = list(enabled_global_skills) + list(thread_enabled_skills)
        for name in configured_names:
            if name in seen or name in thread_disabled_skills:
                continue
            seen.add(name)
            active_names.append(name)

        out: List[Skill] = []
        for name in active_names:
            skill = self.get(name, user_id=user_id)
            if skill is None:
                logger.debug(
                    "thread references skill %r which is not installed — skipping",
                    name,
                )
                continue
            out.append(skill)
        return out

    # ------------------------------------------------------------------
    # Install / uninstall (disk operations)
    # ------------------------------------------------------------------

    def target_dir(self, scope: SkillScope, user_id: Optional[str] = None) -> Path:
        """Return the on-disk directory where a skill of the given scope lives."""
        if scope == "user":
            if not user_id:
                raise ValueError("user scope requires user_id")
            return self._users_dir / user_id
        if scope == "global":
            return self._global_dir
        if scope == "bundled":
            return self._bundled_dir
        raise ValueError(f"unknown scope: {scope}")

    def uninstall(self, name: str, scope: SkillScope, user_id: Optional[str] = None) -> bool:
        """Remove a skill from disk. Returns True if it existed and was deleted."""
        if scope == "bundled":
            raise PermissionError("bundled skills cannot be uninstalled at runtime")
        parent = self.target_dir(scope, user_id=user_id)
        skill_path = parent / name
        if not skill_path.is_dir():
            return False
        # Basic safety: must be under our managed tree.
        if not skill_path.resolve().is_relative_to(self._data_dir.resolve()):
            raise PermissionError(f"refusing to delete path outside data dir: {skill_path}")
        shutil.rmtree(skill_path)
        self.reload()
        return True


__all__ = [
    "Skill",
    "SkillFrontmatter",
    "SkillManager",
    "SkillParseError",
    "SkillScope",
    "ThreadTemplate",
    "AVAILABLE_SKILLS_CHAR_BUDGET",
    "TEMPLATE_TOOL_NAME_RE",
    "expanded_required_tools",
    "load_skill_directory",
    "parse_skill_file",
    "resolve_nested_skills",
]
