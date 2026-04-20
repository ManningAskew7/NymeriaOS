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
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

KEBAB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")

SkillScope = Literal["bundled", "global", "user"]

# Anthropic's documented upper bound on the <available_skills> block.
AVAILABLE_SKILLS_CHAR_BUDGET = 15_000


class SkillParseError(Exception):
    """Raised when a SKILL.md file cannot be parsed."""


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


class SkillManager:
    """Discovers, caches, and resolves skills across the four scope layers.

    Instantiated once by the agent on startup; the agent calls reload() after
    install/uninstall operations.

    Thread-safety: discovery is guarded by a lock. Read methods read from the
    in-memory cache which is only swapped atomically.
    """

    def __init__(self, bundled_dir: Path, data_skills_dir: Path):
        self._bundled_dir = bundled_dir
        self._data_dir = data_skills_dir
        self._global_dir = data_skills_dir / "global"
        self._users_dir = data_skills_dir / "users"

        # Per-user-and-scope caches keyed to avoid cross-user leakage.
        self._lock = threading.RLock()
        self._bundled: Dict[str, Skill] = {}
        self._global: Dict[str, Skill] = {}
        self._per_user: Dict[str, Dict[str, Skill]] = {}

        self._ensure_dirs()
        self.reload()

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
        total = len(self._bundled) + len(self._global) + sum(len(v) for v in self._per_user.values())
        logger.info(
            "SkillManager loaded %d skills (bundled=%d global=%d users=%d)",
            total, len(self._bundled), len(self._global),
            sum(len(v) for v in self._per_user.values()),
        )

    def _user_scope(self, user_id: Optional[str]) -> Dict[str, Skill]:
        if not user_id:
            return {}
        return self._per_user.get(user_id, {})

    def list_installed(self, user_id: Optional[str] = None) -> List[Skill]:
        """All skills visible to this user across all four layers.

        Precedence for name collisions: user > global > bundled.
        """
        with self._lock:
            merged: Dict[str, Skill] = {}
            merged.update(self._bundled)
            merged.update(self._global)
            merged.update(self._user_scope(user_id))
            return sorted(merged.values(), key=lambda s: s.name)

    def get(self, name: str, user_id: Optional[str] = None) -> Optional[Skill]:
        """Resolve a skill by name, honoring precedence."""
        with self._lock:
            user_skills = self._user_scope(user_id)
            if name in user_skills:
                return user_skills[name]
            if name in self._global:
                return self._global[name]
            if name in self._bundled:
                return self._bundled[name]
            return None

    def list_for_thread(
        self,
        user_id: str,
        enabled_global_skills: List[str],
        thread_enabled_skills: List[str],
        thread_disabled_skills: List[str],
    ) -> List[Skill]:
        """Resolve the set of skills active for a given thread.

        Active set = (enabled_global_skills ∪ thread_enabled_skills) − thread_disabled_skills,
        with each name resolved via get() (user > global > bundled precedence).
        """
        active_names: List[str] = []
        seen = set()
        for name in list(enabled_global_skills) + list(thread_enabled_skills):
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
    "AVAILABLE_SKILLS_CHAR_BUDGET",
    "load_skill_directory",
    "parse_skill_file",
]
