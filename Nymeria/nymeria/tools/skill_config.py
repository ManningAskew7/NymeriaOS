"""Agent-facing validated Skill and Skill Kit authoring workflow."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import logging
import shutil
import stat
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union

import yaml
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel

from ..core.http_policy import SECRET_PATTERNS
from ..core.storage_paths import write_text_atomic
from ..core.time_utils import parse_tool_ttl
from ..core.tool_reload import command_or_text, should_emit_reload_command
from ..core.thread_config import ThreadConfig
from ..skills import (
    DEFAULT_SKILL_KIT_TOOL_TTL,
    KEBAB_NAME_RE,
    SkillFrontmatter,
    SkillParseError,
    SkillScope,
    load_skill_directory,
)
from .tool_search import (
    _build_catalog,
    _format_unloadable_error,
    _resolve_tool_object,
)
from .utils import caller_role, get_thread_id, get_user_id, versioned_json_result

logger = logging.getLogger(__name__)

SKILL_CONFIG_VERSION = "2026-05-01.1"


def _json_result(**payload: Any) -> str:
    return versioned_json_result(SKILL_CONFIG_VERSION, **payload)


def _normalize_skill_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    if not KEBAB_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "name must be kebab-case: lowercase letters, numbers, and hyphens; "
            "it must start with a letter and end with a letter or number"
        )
    return normalized


def _normalize_tool_list(values: Optional[list[str] | str], *, field_name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = values.split(",")
    elif isinstance(values, list):
        raw_values = values
    else:
        raise ValueError(f"{field_name} must be a list of tool names or a comma-separated string")

    out: list[str] = []
    seen = set()
    for value in raw_values:
        name = str(value).strip()
        if not name or name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


def _normalize_ttl(tool_ttl: str) -> str:
    ttl = DEFAULT_SKILL_KIT_TOOL_TTL if tool_ttl is None else tool_ttl
    try:
        ttl_key, _ = parse_tool_ttl(ttl)
        return ttl_key
    except ValueError as exc:
        raise ValueError(f"tool_ttl is invalid. {exc}") from exc


def _normalize_optional_ttl(value: Any) -> str:
    return _normalize_ttl(str(value or DEFAULT_SKILL_KIT_TOOL_TTL))


def _coerce_scope(scope: str) -> SkillScope:
    scope_key = (scope or "user").strip().lower()
    if scope_key not in ("user", "global"):
        raise ValueError("scope must be 'user' or 'global'")
    return scope_key  # type: ignore[return-value]


class SkillScriptFile(BaseModel):
    """A script file bundled into a generated skill."""

    path: str
    content: str
    executable: bool = False


def _normalize_tools_arg(values: Optional[list[str] | str]) -> Optional[list[str]]:
    if values is None:
        return None
    if isinstance(values, str):
        raw_values = values.replace("|", ",").split(",")
    elif isinstance(values, list):
        raw_values = values
    else:
        raise ValueError("tools must be a list, comma-separated string, or pipe-separated string")

    out: list[str] = []
    seen = set()
    for value in raw_values:
        name = str(value).strip()
        if not name or name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


def _parse_allowed_tools_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _parse_skill_markdown(markdown: str) -> tuple[SkillFrontmatter, dict[str, Any], str]:
    text = (markdown or "").strip()
    if not text.startswith("---"):
        raise ValueError("markdown must be a full SKILL.md starting with YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("markdown frontmatter must be terminated by '---'")
    _, yaml_text, body = parts
    try:
        raw_frontmatter = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(raw_frontmatter, dict):
        raise ValueError("frontmatter must be a YAML mapping")

    data = dict(raw_frontmatter)
    if "allowed-tools" in data and "allowed_tools" not in data:
        data["allowed_tools"] = _parse_allowed_tools_value(data.pop("allowed-tools"))
    elif "allowed_tools" in data:
        data["allowed_tools"] = _parse_allowed_tools_value(data["allowed_tools"])
    try:
        frontmatter = SkillFrontmatter(**data)
    except Exception as exc:
        raise ValueError(f"frontmatter validation failed: {exc}") from exc
    clean_body = body.lstrip("\n").rstrip()
    if not clean_body:
        raise ValueError("markdown body is required")
    _validate_no_raw_secrets(text, "markdown")
    return frontmatter, data, clean_body


def _validate_no_raw_secrets(text: str, label: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"{label} appears to contain a raw secret")


def _set_required_tools_metadata(
    frontmatter_data: dict[str, Any],
    required_tools: Optional[list[str]],
    tool_ttl: Optional[str],
) -> dict[str, Any]:
    data = dict(frontmatter_data)
    raw_metadata = data.get("metadata")
    metadata: dict[str, Any]
    if not isinstance(raw_metadata, dict):
        metadata = {}
    else:
        metadata = dict(raw_metadata)

    if required_tools is not None:
        if required_tools:
            raw_nymeria = metadata.get("nymeria")
            nymeria: dict[str, Any] = dict(raw_nymeria) if isinstance(raw_nymeria, dict) else {}
            nymeria["required_tools"] = required_tools
            nymeria["tool_ttl"] = _normalize_optional_ttl(tool_ttl or nymeria.get("tool_ttl"))
            metadata["nymeria"] = nymeria
        else:
            raw_nymeria = metadata.get("nymeria")
            if isinstance(raw_nymeria, dict):
                nymeria = dict(raw_nymeria)
                nymeria.pop("required_tools", None)
                nymeria.pop("tool_ttl", None)
                if nymeria:
                    metadata["nymeria"] = nymeria
                else:
                    metadata.pop("nymeria", None)

    if tool_ttl is not None:
        raw_nymeria = metadata.get("nymeria")
        nymeria: dict[str, Any] = dict(raw_nymeria) if isinstance(raw_nymeria, dict) else {}
        if required_tools is None:
            existing = nymeria.get("required_tools") or []
            if existing:
                nymeria["required_tools"] = existing
                nymeria["tool_ttl"] = _normalize_ttl(tool_ttl)
                metadata["nymeria"] = nymeria
        elif required_tools:
            nymeria["tool_ttl"] = _normalize_ttl(tool_ttl)
            metadata["nymeria"] = nymeria

    if metadata:
        data["metadata"] = metadata
    else:
        data.pop("metadata", None)
    return data


def _nymeria_frontmatter(frontmatter_data: dict[str, Any]) -> dict[str, Any]:
    metadata = frontmatter_data.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    nymeria = metadata.get("nymeria")
    return nymeria if isinstance(nymeria, dict) else {}


def _required_tools_from_frontmatter(frontmatter_data: dict[str, Any]) -> list[str]:
    nymeria = _nymeria_frontmatter(frontmatter_data)
    if not nymeria:
        return []
    return _normalize_tool_list(nymeria.get("required_tools"), field_name="required_tools")


def _required_skills_from_frontmatter(frontmatter_data: dict[str, Any]) -> list[str]:
    nymeria = _nymeria_frontmatter(frontmatter_data)
    if not nymeria:
        return []
    return _normalize_tool_list(
        nymeria.get("required_skills"), field_name="required_skills"
    )


def _thread_templates_from_frontmatter(frontmatter_data: dict[str, Any]) -> list:
    nymeria = _nymeria_frontmatter(frontmatter_data)
    raw = nymeria.get("thread_templates")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            "metadata.nymeria.thread_templates must be a list of template objects"
        )
    return raw


def _tool_ttl_from_frontmatter(frontmatter_data: dict[str, Any]) -> str:
    metadata = frontmatter_data.get("metadata")
    if isinstance(metadata, dict):
        nymeria = metadata.get("nymeria")
        if isinstance(nymeria, dict) and nymeria.get("tool_ttl"):
            return _normalize_ttl(str(nymeria["tool_ttl"]))
    return DEFAULT_SKILL_KIT_TOOL_TTL


def _render_full_skill_markdown(frontmatter_data: dict[str, Any], body: str) -> str:
    data = dict(frontmatter_data)
    if "allowed_tools" in data:
        allowed = _parse_allowed_tools_value(data.pop("allowed_tools"))
        if allowed:
            data["allowed-tools"] = allowed
    yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"


def _coerce_script_files(raw_scripts: Optional[list[dict[str, Any]]]) -> list[SkillScriptFile]:
    if raw_scripts is None:
        return []
    if not isinstance(raw_scripts, list):
        raise ValueError("scripts must be a list of {path, content, executable?} objects")
    if len(raw_scripts) > 20:
        raise ValueError("scripts may contain at most 20 files")
    scripts: list[SkillScriptFile] = []
    for raw in raw_scripts:
        if not isinstance(raw, dict):
            raise ValueError("each script entry must be an object")
        scripts.append(SkillScriptFile.model_validate(raw))
    return scripts


def _validate_script_file(script: SkillScriptFile) -> Path:
    rel = Path(script.path)
    if rel.is_absolute():
        raise ValueError(f"script path must be relative: {script.path}")
    parts = rel.parts
    if not parts or parts[0] != "scripts":
        raise ValueError(f"script path must live under scripts/: {script.path}")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError(f"script path contains an unsafe segment: {script.path}")
    if len(script.content.encode("utf-8")) > 500_000:
        raise ValueError(f"script file is too large: {script.path}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(script.content):
            raise ValueError(f"script {script.path} appears to contain a raw secret")
    if rel.suffix == ".py":
        try:
            compile(script.content, script.path, "exec")
        except SyntaxError as exc:
            raise ValueError(f"script {script.path} has Python syntax error at line {exc.lineno}: {exc.msg}") from exc
    return rel


def _write_script_files(root: Path, scripts: list[SkillScriptFile]) -> list[str]:
    written: list[str] = []
    for script in scripts:
        rel = _validate_script_file(script)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script.content, encoding="utf-8")
        mode = 0o755 if script.executable else 0o644
        target.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
        written.append(str(rel))
    return written


def _validate_skill_directory_candidate(
    *,
    target_parent: Path,
    scope: SkillScope,
    user_id: str,
    skill_name: str,
    markdown: str,
    scripts: list[SkillScriptFile],
) -> None:
    validate_root = target_parent / f".{skill_name}.validate-{uuid.uuid4().hex}"
    skill_dir = validate_root / skill_name
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(markdown, encoding="utf-8")
        _write_script_files(skill_dir, scripts)
        loaded = load_skill_directory(
            skill_dir,
            scope=scope,
            user_id=user_id if scope == "user" else None,
        )
        if loaded is None:
            raise ValueError("generated skill failed loader validation")
    finally:
        shutil.rmtree(validate_root, ignore_errors=True)


def _validate_required_tools(required_tools: list[str], user_id: str) -> None:
    if not required_tools:
        return

    from ..core.agent import get_current_agent
    from . import filter_admin_only_tools, filter_developer_only_tools

    agent = get_current_agent()
    if agent is None:
        raise ValueError("No active agent. Cannot validate Skill Kit required_tools.")

    catalog = _build_catalog()
    known = set(catalog.keys())
    registry = getattr(agent, "tool_registry", None)
    valid: list[str] = []
    invalid: list[str] = []
    unloadable: list[str] = []

    for name in required_tools:
        in_catalog = name in known
        in_registry = bool(registry and registry.get_tool(name))
        if not (in_catalog or in_registry):
            invalid.append(name)
            continue
        if _resolve_tool_object(name, agent) is None:
            unloadable.append(name)
            continue
        valid.append(name)

    errors: list[str] = []
    if invalid:
        errors.append(f"[Not found]: {', '.join(invalid)}")
    if unloadable:
        errors.append(_format_unloadable_error(unloadable))

    role = caller_role(user_id, agent=agent)
    _, blocked = filter_admin_only_tools(valid, role)
    if blocked:
        errors.append(
            "Admin-only tools cannot be declared as required_tools by this user: "
            + ", ".join(sorted(blocked))
        )
    _, blocked = filter_developer_only_tools(valid, role)
    if blocked:
        errors.append(
            "Developer-only diagnostic tools cannot be declared as required_tools by this user: "
            + ", ".join(sorted(blocked))
        )

    if errors:
        raise ValueError("Tool dependency validation failed; no skill was written. " + " ".join(errors))


def _validate_required_skills(
    required_skills: list[str], skill_name: str, user_id: str
) -> None:
    """Strict-existence validation for ``metadata.nymeria.required_skills``.

    Mirrors the ``required_tools`` posture: every named skill must be
    installed and visible to the author, names must be kebab-case, and a kit
    cannot require itself. Nesting stays one level deep at activation, so a
    nested kit's own required_skills are allowed here without cycle checks.
    """
    if not required_skills:
        return

    from ..core.agent import get_current_agent

    agent = get_current_agent()
    skill_manager = getattr(agent, "skill_manager", None) if agent else None
    if skill_manager is None:
        raise ValueError("No active agent. Cannot validate required_skills.")

    errors: list[str] = []
    missing: list[str] = []
    for name in required_skills:
        if not KEBAB_NAME_RE.fullmatch(name):
            errors.append(f"required_skills entry is not kebab-case: {name!r}")
            continue
        if name == skill_name:
            errors.append(f"a kit cannot require itself: {name!r}")
            continue
        try:
            resolved = skill_manager.get(name, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - lookup failure = unresolvable
            errors.append(f"required_skills lookup failed for {name!r}: {exc}")
            continue
        if resolved is None:
            missing.append(name)
    if missing:
        errors.append(f"[Not installed]: {', '.join(missing)}")
    if errors:
        raise ValueError(
            "Skill dependency validation failed; no skill was written. "
            + " ".join(errors)
        )


def _validate_thread_templates(raw_templates: list, user_id: str) -> list[dict]:
    """Strict authoring validation for ``metadata.nymeria.thread_templates``.

    The loader is lenient (a hand-edited bad template is skipped with a
    warning), but the authoring surface rejects: malformed entries (the
    ThreadTemplate model, ``extra="forbid"``), duplicate/colliding tool names,
    unknown or author-role-gated template tools, and an uninstalled ``kit``.
    Returns ``[{name, description}, ...]`` summaries for the result payload.
    """
    if not raw_templates:
        return []

    from ..core.agent import get_current_agent
    from ..skills import ThreadTemplate
    from . import filter_admin_only_tools, filter_developer_only_tools

    agent = get_current_agent()
    if agent is None:
        raise ValueError("No active agent. Cannot validate thread_templates.")

    catalog = _build_catalog()
    known = set(catalog.keys())
    registry = getattr(agent, "tool_registry", None)
    role = caller_role(user_id, agent=agent)
    skill_manager = getattr(agent, "skill_manager", None)

    errors: list[str] = []
    validated: list[dict] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_templates):
        label = f"thread_templates[{index}]"
        try:
            template = ThreadTemplate.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - surfaced as a validation error
            errors.append(f"{label}: {exc}")
            continue
        if template.name in seen_names:
            errors.append(f"{label}: duplicate template name {template.name!r}")
            continue
        seen_names.add(template.name)
        if template.name in known or (registry and registry.get_tool(template.name)):
            errors.append(
                f"{label}: name {template.name!r} collides with an existing tool"
            )
        unknown = [
            name
            for name in template.tools
            if name not in known and not (registry and registry.get_tool(name))
        ]
        if unknown:
            errors.append(f"{label}: unknown template tools: {', '.join(unknown)}")
        gated = [name for name in template.tools if name not in unknown]
        _, blocked = filter_admin_only_tools(gated, role)
        if blocked:
            errors.append(
                f"{label}: admin-only tools cannot be declared by this user: "
                + ", ".join(sorted(blocked))
            )
        _, blocked = filter_developer_only_tools(gated, role)
        if blocked:
            errors.append(
                f"{label}: developer-only diagnostic tools cannot be declared "
                "by this user: " + ", ".join(sorted(blocked))
            )
        if template.kit and skill_manager is not None:
            try:
                kit_skill = skill_manager.get(template.kit, user_id=user_id)
            except Exception:  # noqa: BLE001
                kit_skill = None
            if kit_skill is None:
                errors.append(f"{label}: kit {template.kit!r} is not installed")
        validated.append({"name": template.name, "description": template.description})

    if errors:
        raise ValueError(
            "Thread template validation failed; no skill was written. "
            + " ".join(errors)
        )
    return validated


def _write_skill_md_atomic(target_dir: Path, markdown: str) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "SKILL.md"
    write_text_atomic(target, markdown)


def _invalidate_graph_caches(agent) -> None:
    # Canonical lock-correct evict-and-rebuild; best-effort so a skill-config
    # tool call never fails on a graph rebuild error.
    try:
        agent._rebuild_default_graphs()
    except Exception:
        logger.debug("Failed to rebuild graphs after skill change", exc_info=True)


def _activate_skill_on_thread(agent, thread_id: str, skill_name: str) -> bool:
    if not thread_id:
        return False
    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    changed = False
    if skill_name not in tc.enabled_skills:
        tc.enabled_skills = [*tc.enabled_skills, skill_name]
        changed = True
    if skill_name in tc.disabled_skills:
        tc.disabled_skills = [name for name in tc.disabled_skills if name != skill_name]
        changed = True
    if changed:
        if not agent.thread_config_manager.save_config(tc):
            raise ValueError("failed to save thread config while activating skill")
        if hasattr(agent, "invalidate_thread_config_cache"):
            agent.invalidate_thread_config_cache(thread_id)
    return changed


def _queue_skill_reload(
    agent,
    thread_id: str,
    skill_name: str,
    *,
    source: str = "skill_write",
    reason: str = "skill_published",
) -> tuple[bool, bool]:
    """Queue a same-turn graph rebuild after a skill list change when needed.

    Returns (queued, cap_hit).
    """
    if not thread_id:
        return False, False
    # Skill-only changes pass empty new_tools; in dynamic mode the next
    # agent step's resolver rebuilds the skill meta-tool naturally, so
    # the rebuild round-trip and reload cap are both irrelevant.
    if not should_emit_reload_command([], thread_id=thread_id):
        return False, False
    reload_cap = getattr(agent, "MAX_TOOL_RELOADS_PER_TURN", 1)
    current_reloads = getattr(agent, "_turn_reload_count", {}).get(thread_id, 0)
    cap_hit = current_reloads >= reload_cap
    if cap_hit:
        return False, True
    if not hasattr(agent, "_pending_tool_reload"):
        agent._pending_tool_reload = {}
    agent._pending_tool_reload[thread_id] = {
        "new_tools": [],
        "ttl": "",
        "ttl_seconds": None,
        "source": source,
        "skill_name": skill_name,
        "reason": reason,
    }
    return True, False


def _ensure_global_scope_allowed(agent: Any, scope: SkillScope, user_id: str) -> None:
    if scope != "global":
        return
    try:
        caller = agent.accounts_repo.get_user_by_id(user_id)
        if not caller or caller.role != "admin":
            raise ValueError("Global skill publish requires admin role")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"caller verification failed: {exc}") from exc


def _assert_skill_write_allowed(
    *,
    agent: Any,
    skill_name: str,
    target_dir: Path,
    scope: SkillScope,
    user_id: str,
    overwrite: bool,
    old_name: str = "",
) -> None:
    if scope == "bundled":
        raise ValueError("bundled skills are read-only")
    if target_dir.exists() and not overwrite and skill_name != old_name:
        raise ValueError(f"skill {skill_name!r} already exists in {scope} scope; set overwrite=true to replace it")
    skill_manager = agent.skill_manager
    existing = skill_manager.get(skill_name, user_id=user_id)
    if existing is not None and existing.path != target_dir:
        if not overwrite:
            raise ValueError(
                f"publishing {skill_name!r} in {scope} scope would shadow an existing "
                f"{existing.scope} skill. Pick a different name or set overwrite=true."
            )
        if existing.scope == "bundled":
            raise ValueError(f"refusing to overwrite bundled skill {skill_name!r}")


def _replace_skill_reference_on_thread(agent: Any, thread_id: str, old_name: str, new_name: str) -> None:
    if not thread_id or old_name == new_name:
        return
    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return
    changed = False
    if old_name in tc.enabled_skills:
        tc.enabled_skills = [new_name if name == old_name else name for name in tc.enabled_skills]
        changed = True
    if old_name in tc.disabled_skills:
        tc.disabled_skills = [new_name if name == old_name else name for name in tc.disabled_skills]
        changed = True
    if changed:
        agent.thread_config_manager.save_config(tc)
        if hasattr(agent, "invalidate_thread_config_cache"):
            agent.invalidate_thread_config_cache(thread_id)


def _write_skill_package(
    *,
    markdown: str,
    scripts: list[SkillScriptFile],
    scope: SkillScope,
    user_id: str,
    thread_id: str,
    overwrite: bool,
    activate_current_thread: bool,
    tool_call_id: str,
    reload_source: str,
    reload_reason: str,
    old_name: str = "",
    dry_run: bool = False,
) -> Union[str, Command]:
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    skill_manager = getattr(agent, "skill_manager", None) if agent is not None else None
    if agent is None or skill_manager is None:
        return _json_result(ok=False, error={"type": "unavailable", "message": "skills subsystem not initialized"})

    _ensure_global_scope_allowed(agent, scope, user_id)
    frontmatter, frontmatter_data, _body = _parse_skill_markdown(markdown)
    required_tools = _required_tools_from_frontmatter(frontmatter_data)
    _validate_required_tools(required_tools, user_id)
    required_skills = _required_skills_from_frontmatter(frontmatter_data)
    _validate_required_skills(required_skills, frontmatter.name, user_id)
    thread_templates = _validate_thread_templates(
        _thread_templates_from_frontmatter(frontmatter_data), user_id
    )

    target_parent = skill_manager.target_dir(scope, user_id=user_id if scope == "user" else None)
    target_dir = target_parent / frontmatter.name
    _assert_skill_write_allowed(
        agent=agent,
        skill_name=frontmatter.name,
        target_dir=target_dir,
        scope=scope,
        user_id=user_id,
        overwrite=overwrite,
        old_name=old_name,
    )
    _validate_skill_directory_candidate(
        target_parent=target_parent,
        scope=scope,
        user_id=user_id,
        skill_name=frontmatter.name,
        markdown=markdown,
        scripts=scripts,
    )

    payload = {
        "ok": True,
        "action": "write",
        "dry_run": dry_run,
        "published": not dry_run,
        "activated_current_thread": False,
        "reload_queued": False,
        "reload_cap_hit": False,
        "skill": {
            "name": frontmatter.name,
            "description": frontmatter.description,
            "scope": scope,
            "required_tools": required_tools,
            "required_skills": required_skills,
            "thread_templates": thread_templates,
            "tool_ttl": _tool_ttl_from_frontmatter(frontmatter_data),
            "is_skill_kit": bool(
                required_tools or required_skills or thread_templates
            ),
        },
        "scripts": [script.path for script in scripts],
    }
    if dry_run:
        return _json_result(**payload)

    old_dir = target_parent / old_name if old_name else None
    backup_dir: Optional[Path] = None
    if old_dir is not None and old_dir.exists() and old_dir != target_dir:
        backup_dir = target_parent / f".{old_name}.backup-{uuid.uuid4().hex}"
        old_dir.rename(backup_dir)

    target_existed_before_write = target_dir.exists()
    old_skill_text = (target_dir / "SKILL.md").read_text(encoding="utf-8") if (target_dir / "SKILL.md").exists() else None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(target_dir / "SKILL.md", markdown)
        written_scripts = _write_script_files(target_dir, scripts)

        loaded = load_skill_directory(
            target_dir,
            scope=scope,
            user_id=user_id if scope == "user" else None,
        )
        if loaded is None:
            raise SkillParseError(f"failed to load generated skill at {target_dir}")

        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)
        skill_manager.reload()
        if old_name:
            _replace_skill_reference_on_thread(agent, thread_id, old_name, loaded.name)

        activated = False
        queued_reload = False
        cap_hit = False
        if activate_current_thread:
            activated = _activate_skill_on_thread(agent, thread_id, loaded.name)
            queued_reload, cap_hit = _queue_skill_reload(
                agent,
                thread_id,
                loaded.name,
                source=reload_source,
                reason=reload_reason,
            )
        _invalidate_graph_caches(agent)
    except Exception:
        if old_skill_text is not None:
            _write_skill_md_atomic(target_dir, old_skill_text)
        elif (target_dir / "SKILL.md").exists():
            (target_dir / "SKILL.md").unlink()
        if backup_dir is not None and backup_dir.exists():
            if target_dir.exists() and not target_existed_before_write and target_dir != backup_dir:
                shutil.rmtree(target_dir, ignore_errors=True)
            if old_dir is not None:
                backup_dir.rename(old_dir)
        raise

    will_reload = bool(
        activate_current_thread
        and queued_reload
        and not cap_hit
        and should_emit_reload_command([], thread_id=thread_id)
    )
    payload.update(
        saved_path=str(target_dir / "SKILL.md"),
        activated_current_thread=activated,
        reload_queued=will_reload,
        reload_cap_hit=cap_hit,
        scripts=written_scripts,
    )
    text = _json_result(**payload)
    if will_reload:
        text += (
            "\n\n[Skill reload queued - STOP NOW]\n"
            "The skill list changed for this thread, but the current graph "
            "invocation cannot see the updated Skill meta-tool index. Do not "
            "write a final answer or call another tool now. The system will "
            "automatically resume you after rebuilding."
        )
    elif cap_hit:
        text += (
            "\n\n[Reload cap hit]: the skill was published and enabled on this "
            "thread, but it will not be visible to the model until the next "
            "user message."
        )
    return command_or_text(text, will_reload, tool_call_id, [], thread_id=thread_id)


@tool
def skill_write(
    markdown: str,
    tools: Optional[list[str] | str] = None,
    scripts: Optional[list[dict[str, Any]]] = None,
    tool_ttl: Optional[str] = None,
    scope: Literal["user", "global"] = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
    dry_run: bool = False,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Write a Skill or Skill Kit from full SKILL.md markdown.

    Provide full markdown with YAML frontmatter containing name and
    description. If tools is provided, it replaces
    metadata.nymeria.required_tools; an empty tools value creates a plain
    instruction-only Skill. Optional scripts are written under scripts/.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    try:
        frontmatter, frontmatter_data, body = _parse_skill_markdown(markdown)
        requested_tools = _normalize_tools_arg(tools)
        merged_frontmatter = _set_required_tools_metadata(
            frontmatter_data,
            requested_tools,
            tool_ttl,
        )
        if requested_tools is not None:
            _validate_required_tools(requested_tools, user_id)
        merged_markdown = _render_full_skill_markdown(merged_frontmatter, body)
        script_files = _coerce_script_files(scripts)
        return _write_skill_package(
            markdown=merged_markdown,
            scripts=script_files,
            scope=_coerce_scope(scope),
            user_id=user_id,
            thread_id=thread_id,
            overwrite=overwrite,
            activate_current_thread=activate_current_thread,
            tool_call_id=tool_call_id,
            reload_source="skill_write",
            reload_reason="skill_written",
            dry_run=dry_run,
        )
    except ValueError as exc:
        return _json_result(ok=False, error={"type": "validation_error", "message": str(exc)})
    except Exception as exc:
        logger.error("skill_write failed", exc_info=True)
        return _json_result(ok=False, error={"type": type(exc).__name__, "message": str(exc)})


@tool
def skill_edit(
    name: str,
    markdown: str = "",
    new_name: str = "",
    description: str = "",
    body: str = "",
    set_tools: Optional[list[str] | str] = None,
    add_tools: Optional[list[str] | str] = None,
    remove_tools: Optional[list[str] | str] = None,
    tool_ttl: Optional[str] = None,
    scope: Literal["user", "global"] = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
    dry_run: bool = False,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Edit an existing generated Skill or Skill Kit.

    V1 edits SKILL.md only. Existing scripts/references/assets are preserved.
    Use markdown to replace the full SKILL.md, or pass structured fields to
    patch the name, description, body, required tools, and tool_ttl.
    """
    from ..core.agent import get_current_agent

    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    try:
        current_name = _normalize_skill_name(name)
        edit_scope = _coerce_scope(scope)
        agent = get_current_agent()
        skill_manager = getattr(agent, "skill_manager", None) if agent is not None else None
        if agent is None or skill_manager is None:
            return _json_result(ok=False, error={"type": "unavailable", "message": "skills subsystem not initialized"})
        _ensure_global_scope_allowed(agent, edit_scope, user_id)
        target_parent = skill_manager.target_dir(
            edit_scope,
            user_id=user_id if edit_scope == "user" else None,
        )
        current_dir = target_parent / current_name
        skill_md = current_dir / "SKILL.md"
        if not skill_md.exists():
            return _json_result(
                ok=False,
                error={
                    "type": "not_found",
                    "message": f"skill {current_name!r} does not exist in {edit_scope} scope or is read-only in another scope",
                },
            )

        source_markdown = markdown.strip() if markdown.strip() else skill_md.read_text(encoding="utf-8")
        frontmatter, frontmatter_data, parsed_body = _parse_skill_markdown(source_markdown)
        patched = dict(frontmatter_data)
        patched_body = parsed_body

        if new_name:
            patched["name"] = _normalize_skill_name(new_name)
        if description:
            patched["description"] = description.strip()
        if body:
            patched_body = body.strip()

        current_tools = _required_tools_from_frontmatter(patched)
        replacement_tools = _normalize_tools_arg(set_tools)
        if replacement_tools is not None:
            current_tools = replacement_tools
        additions = _normalize_tools_arg(add_tools) or []
        removals = set(_normalize_tools_arg(remove_tools) or [])
        for tool_name in additions:
            if tool_name not in current_tools:
                current_tools.append(tool_name)
        if removals:
            current_tools = [tool_name for tool_name in current_tools if tool_name not in removals]

        tools_were_touched = (
            replacement_tools is not None
            or bool(additions)
            or bool(removals)
            or tool_ttl is not None
        )
        patched = _set_required_tools_metadata(
            patched,
            current_tools if tools_were_touched else None,
            tool_ttl,
        )

        # Re-validate frontmatter after structured patches.
        patched_markdown = _render_full_skill_markdown(patched, patched_body)
        _parse_skill_markdown(patched_markdown)
        if tools_were_touched:
            _validate_required_tools(current_tools, user_id)

        return _write_skill_package(
            markdown=patched_markdown,
            scripts=[],
            scope=edit_scope,
            user_id=user_id,
            thread_id=thread_id,
            overwrite=overwrite,
            activate_current_thread=activate_current_thread,
            tool_call_id=tool_call_id,
            reload_source="skill_edit",
            reload_reason="skill_edited",
            old_name=current_name,
            dry_run=dry_run,
        )
    except ValueError as exc:
        return _json_result(ok=False, error={"type": "validation_error", "message": str(exc)})
    except Exception as exc:
        logger.error("skill_edit failed", exc_info=True)
        return _json_result(ok=False, error={"type": type(exc).__name__, "message": str(exc)})


SKILL_CONFIG_TOOLS = [skill_write, skill_edit]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="skill_config", tools=tuple(SKILL_CONFIG_TOOLS)))
