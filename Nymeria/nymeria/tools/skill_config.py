"""Agent-facing validated Skill and Skill Kit authoring workflow.

V1 writes only SKILL.md files in Nymeria's managed skill directories. It does
not write scripts, assets, references, or arbitrary paths.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, List, Optional, Union

import yaml
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field

from ..config import get_settings
from ..core.time_utils import parse_tool_ttl, utc_now
from ..core.tool_reload import should_emit_reload_command, tool_reload_command
from ..core.thread_config import ThreadConfig
from ..skills import (
    DEFAULT_SKILL_KIT_TOOL_TTL,
    KEBAB_NAME_RE,
    Skill,
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
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

SKILL_CONFIG_VERSION = "2026-05-01.1"


class SkillDraft(BaseModel):
    """Persisted draft for an agent-authored Skill or Skill Kit."""

    draft_id: str
    name: str
    description: str
    body: str
    allowed_tools: List[str] = Field(default_factory=list)
    required_tools: List[str] = Field(default_factory=list)
    tool_ttl: str = DEFAULT_SKILL_KIT_TOOL_TTL
    created_by_user_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_validated_at: Optional[datetime] = None

    def public_summary(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "name": self.name,
            "description": self.description,
            "allowed_tools": self.allowed_tools,
            "required_tools": self.required_tools,
            "tool_ttl": self.tool_ttl,
            "is_skill_kit": bool(self.required_tools),
            "last_validated_at": self.last_validated_at.isoformat() if self.last_validated_at else None,
            "updated_at": self.updated_at.isoformat(),
        }


class SkillDraftStore:
    """Filesystem store for per-user Skill drafts."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _safe_segment(self, value: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
        return safe.strip("._") or "default"

    def _user_dir(self, user_id: str) -> Path:
        path = self.base_dir / self._safe_segment(user_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _draft_path(self, user_id: str, draft_id: str) -> Path:
        return self._user_dir(user_id) / f"{self._safe_segment(draft_id)}.json"

    def save(self, user_id: str, draft: SkillDraft) -> Path:
        draft.updated_at = utc_now()
        path = self._draft_path(user_id, draft.draft_id)
        path.write_text(draft.model_dump_json(indent=2), encoding="utf-8")
        return path

    def get(self, user_id: str, draft_id: str) -> Optional[SkillDraft]:
        path = self._draft_path(user_id, draft_id)
        if not path.exists():
            return None
        return SkillDraft.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, user_id: str, draft_id: str) -> bool:
        path = self._draft_path(user_id, draft_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self, user_id: str) -> list[SkillDraft]:
        drafts: list[SkillDraft] = []
        user_dir = self._user_dir(user_id)
        for path in sorted(user_dir.glob("*.json")):
            try:
                drafts.append(SkillDraft.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Skipping invalid skill draft %s: %s", path, exc)
        return drafts


def _draft_store() -> SkillDraftStore:
    return SkillDraftStore(get_settings().data_dir / "skill_drafts")


def _json_result(**payload: Any) -> str:
    return json.dumps({"tool_version": SKILL_CONFIG_VERSION, **payload}, indent=2, default=str)


def _normalize_skill_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    if not KEBAB_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "name must be kebab-case: lowercase letters, numbers, and hyphens; "
            "it must start with a letter and end with a letter or number"
        )
    return normalized


def _normalize_draft_id(draft_id: str, name: str = "") -> str:
    candidate = (draft_id or name or "").strip().lower()
    return _normalize_skill_name(candidate)


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


def _coerce_scope(scope: str) -> SkillScope:
    scope_key = (scope or "user").strip().lower()
    if scope_key not in ("user", "global"):
        raise ValueError("scope must be 'user' or 'global'")
    return scope_key  # type: ignore[return-value]


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

    try:
        user = agent.accounts_repo.get_user_by_id(user_id) if user_id else None
        role = user.role if user else "user"
    except Exception:
        role = "user"
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


def create_skill_draft(
    *,
    user_id: str,
    name: str,
    description: str,
    body: str,
    allowed_tools: Optional[list[str] | str] = None,
    required_tools: Optional[list[str] | str] = None,
    tool_ttl: str = DEFAULT_SKILL_KIT_TOOL_TTL,
    draft_id: str = "",
) -> SkillDraft:
    normalized_name = _normalize_skill_name(name)
    normalized_draft_id = _normalize_draft_id(draft_id, normalized_name)
    desc = (description or "").strip()
    if not desc:
        raise ValueError("description is required")
    body_text = (body or "").strip()
    if not body_text:
        raise ValueError("body is required")
    if body_text.startswith("---"):
        raise ValueError("body must not include YAML frontmatter; pass frontmatter fields separately")

    normalized_allowed = _normalize_tool_list(allowed_tools, field_name="allowed_tools")
    normalized_required = _normalize_tool_list(required_tools, field_name="required_tools")
    normalized_ttl = _normalize_ttl(tool_ttl)

    # Validate portable frontmatter fields before dependency checks.
    SkillFrontmatter(
        name=normalized_name,
        description=desc,
        allowed_tools=normalized_allowed,
        metadata=(
            {
                "nymeria": {
                    "required_tools": normalized_required,
                    "tool_ttl": normalized_ttl,
                }
            }
            if normalized_required
            else {}
        ),
    )
    _validate_required_tools(normalized_required, user_id)

    return SkillDraft(
        draft_id=normalized_draft_id,
        name=normalized_name,
        description=desc,
        body=body_text,
        allowed_tools=normalized_allowed,
        required_tools=normalized_required,
        tool_ttl=normalized_ttl,
        created_by_user_id=user_id,
        last_validated_at=utc_now(),
    )


def _render_skill_markdown(draft: SkillDraft) -> str:
    frontmatter: dict[str, Any] = {
        "name": draft.name,
        "description": draft.description,
    }
    if draft.allowed_tools:
        frontmatter["allowed-tools"] = draft.allowed_tools
    if draft.required_tools:
        frontmatter["metadata"] = {
            "nymeria": {
                "required_tools": draft.required_tools,
                "tool_ttl": draft.tool_ttl,
            }
        }
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{yaml_text}\n---\n\n{draft.body.rstrip()}\n"


def _skill_dir_has_only_skill_md(path: Path) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    entries = [p.name for p in path.iterdir()]
    return set(entries).issubset({"SKILL.md"})


def _write_skill_md_atomic(target_dir: Path, markdown: str) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "SKILL.md"
    tmp = target_dir / ".SKILL.md.tmp"
    tmp.write_text(markdown, encoding="utf-8")
    tmp.replace(target)


def _invalidate_graph_caches(agent) -> None:
    with agent._graph_cache_lock:
        agent._user_graphs.clear()
    try:
        agent._async_user_graphs.clear()
    except Exception:
        logger.debug("Failed to clear async graph cache")


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
    source: str = "skill_config",
    reason: str = "skill_published",
) -> tuple[bool, bool]:
    """Queue a same-turn graph rebuild after a skill list change.

    Returns (queued, cap_hit).
    """
    if not thread_id:
        return False, False
    reload_cap = getattr(agent, "MAX_TOOL_RELOADS_PER_TURN", 1)
    current_reloads = getattr(agent, "_turn_reload_count", {}).get(thread_id, 0)
    cap_hit = current_reloads >= reload_cap
    if cap_hit:
        return False, True
    # Skill-only changes pass empty new_tools; in dynamic mode the next
    # agent step's resolver rebuilds the skill meta-tool naturally, so
    # the rebuild round-trip is unnecessary. Skip the _pending write.
    if not should_emit_reload_command([]):
        return False, False
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


def _load_draft_or_inline(
    *,
    store: SkillDraftStore,
    user_id: str,
    draft_id: str,
    name: str,
    description: str,
    body: str,
    allowed_tools: Optional[list[str] | str],
    required_tools: Optional[list[str] | str],
    tool_ttl: str,
) -> SkillDraft:
    if draft_id:
        normalized_draft_id = _normalize_draft_id(draft_id)
        draft = store.get(user_id, normalized_draft_id)
        if draft is None:
            raise ValueError(f"Draft not found: {draft_id}")
        _validate_required_tools(draft.required_tools, user_id)
        draft.tool_ttl = _normalize_ttl(draft.tool_ttl)
        draft.last_validated_at = utc_now()
        return draft
    return create_skill_draft(
        user_id=user_id,
        name=name,
        description=description,
        body=body,
        allowed_tools=allowed_tools,
        required_tools=required_tools,
        tool_ttl=tool_ttl,
    )


def _assert_publish_allowed(agent, draft: SkillDraft, scope: SkillScope, user_id: str, overwrite: bool) -> None:
    target_parent = agent.skill_manager.target_dir(scope, user_id=user_id if scope == "user" else None)
    target_dir = target_parent / draft.name
    if target_dir.exists():
        if not overwrite:
            raise ValueError(
                f"skill {draft.name!r} already exists in {scope} scope; "
                "set overwrite=true to replace its SKILL.md"
            )
        if not _skill_dir_has_only_skill_md(target_dir):
            raise ValueError(
                f"refusing to overwrite {draft.name!r}: existing skill has auxiliary files. "
                "Delete it explicitly before replacing it with a generated SKILL.md-only skill."
            )
        return

    existing = agent.skill_manager.get(draft.name, user_id=user_id)
    if existing is not None:
        raise ValueError(
            f"publishing {draft.name!r} in {scope} scope would shadow an existing "
            f"{existing.scope} skill. Pick a different name."
        )


def _publish_skill(
    *,
    draft: SkillDraft,
    scope: SkillScope,
    user_id: str,
    thread_id: str,
    overwrite: bool,
    activate_current_thread: bool,
    reload_source: str = "skill_config",
    reload_reason: str = "skill_published",
) -> tuple[str, bool, bool, Skill]:
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    if agent is None or getattr(agent, "skill_manager", None) is None:
        raise ValueError("skills subsystem not initialized")

    if scope == "global":
        try:
            caller = agent.accounts_repo.get_user_by_id(user_id)
            if not caller or caller.role != "admin":
                raise ValueError("Global skill publish requires admin role")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"caller verification failed: {exc}") from exc

    _assert_publish_allowed(agent, draft, scope, user_id, overwrite)

    target_parent = agent.skill_manager.target_dir(scope, user_id=user_id if scope == "user" else None)
    target_dir = target_parent / draft.name
    skill_md = target_dir / "SKILL.md"
    old_text = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
    markdown = _render_skill_markdown(draft)

    try:
        _write_skill_md_atomic(target_dir, markdown)
        loaded = load_skill_directory(
            target_dir,
            scope=scope,
            user_id=user_id if scope == "user" else None,
        )
        if loaded is None:
            raise SkillParseError(f"failed to load generated skill at {target_dir}")
    except Exception:
        if old_text is not None:
            _write_skill_md_atomic(target_dir, old_text)
        elif skill_md.exists():
            skill_md.unlink()
            try:
                target_dir.rmdir()
            except OSError:
                pass  # directory may not be empty or already removed
        raise

    agent.skill_manager.reload()
    activated = False
    queued_reload = False
    cap_hit = False
    if activate_current_thread:
        activated = _activate_skill_on_thread(agent, thread_id, draft.name)
        queued_reload, cap_hit = _queue_skill_reload(
            agent,
            thread_id,
            draft.name,
            source=reload_source,
            reason=reload_reason,
        )

    _invalidate_graph_caches(agent)
    return str(skill_md), activated, cap_hit if not queued_reload else False, loaded


def _command_or_text(
    text: str,
    queued_reload: bool,
    tool_call_id: str,
    new_tool_names: Optional[list[str]] = None,
) -> Union[str, Command]:
    """Decide whether to emit Command(goto=END) or a plain string.

    ``new_tool_names`` lets dynamic-binding mode short-circuit the rebuild
    when the tools are already in the graph's superset (i.e., the next
    agent step will rebind them automatically). Default empty list is
    correct for skill-only changes (meta-tool description update), which
    are always rebind-safe in dynamic mode.
    """
    if queued_reload and tool_call_id and should_emit_reload_command(new_tool_names or []):
        return tool_reload_command(text, tool_call_id)
    return text


@tool
def skill_config(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    allowed_tools: Optional[list[str] | str] = None,
    required_tools: Optional[list[str] | str] = None,
    tool_ttl: str = DEFAULT_SKILL_KIT_TOOL_TTL,
    draft_id: str = "",
    scope: str = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Draft, validate, publish, list, or delete Nymeria Skills and Skill Kits.

    Use this after you have written concise SKILL.md body instructions. A
    plain Skill is instructions only. A Skill Kit is a Skill with
    required_tools, so activating it binds those Nymeria tools for the thread.
    V1 writes only SKILL.md files in managed user/global skill directories; it
    cannot create scripts, assets, references, or arbitrary paths.

    Actions:
      draft:    Validate and save a per-user skill draft.
      validate: Validate inline fields or a saved draft without publishing.
      publish: Write a validated SKILL.md to user/global scope, reload skills,
               and by default enable it on the current thread.
      list:     Show this user's drafts and visible installed skills.
      delete:   Delete a draft when scope="draft", otherwise uninstall a
               user/global skill. Global delete requires admin.

    Args:
      tool_ttl: TTL for required tools when the skill is activated. Format:
               Nm/Nh/Nd/Nw or "never"/"permanent". Default "2h".

    Returns:
        JSON {tool_version, ok, action, ...}. draft/validate return
        {draft: {draft_id, name, description, ...}}. publish returns
        {published, skill, activated_current_thread, reload_queued};
        when reload_queued=true, includes "[Skill reload queued - STOP
        NOW]" and the graph is force-ended for rebuild — do not respond
        after this directive. list returns {drafts, installed}. delete
        returns {deleted}. Errors: {ok: false, error: {type, message}}.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    store = _draft_store()
    action_key = (action or "").strip().lower()

    try:
        if action_key == "draft":
            draft = create_skill_draft(
                user_id=user_id,
                name=name,
                description=description,
                body=body,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
                tool_ttl=tool_ttl,
                draft_id=draft_id,
            )
            store.save(user_id, draft)
            return _json_result(ok=True, action="draft", draft=draft.public_summary())

        if action_key == "validate":
            draft = _load_draft_or_inline(
                store=store,
                user_id=user_id,
                draft_id=draft_id,
                name=name,
                description=description,
                body=body,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
                tool_ttl=tool_ttl,
            )
            return _json_result(ok=True, action="validate", draft=draft.public_summary())

        if action_key == "publish":
            from ..core.agent import get_current_agent

            agent = get_current_agent()
            if agent is None or getattr(agent, "skill_manager", None) is None:
                return _json_result(ok=False, error={"type": "unavailable", "message": "skills subsystem not initialized"})
            publish_scope = _coerce_scope(scope)
            draft = _load_draft_or_inline(
                store=store,
                user_id=user_id,
                draft_id=draft_id,
                name=name,
                description=description,
                body=body,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
                tool_ttl=tool_ttl,
            )
            saved_path, activated, cap_hit, skill = _publish_skill(
                draft=draft,
                scope=publish_scope,
                user_id=user_id,
                thread_id=thread_id,
                overwrite=overwrite,
                activate_current_thread=activate_current_thread,
            )
            queued_reload = bool(
                activate_current_thread
                and not cap_hit
                and getattr(agent, "_pending_tool_reload", {}).get(thread_id)
            )
            # In dynamic-binding mode, a pure-skill change (no new tool names)
            # is rebind-safe — the next agent step's resolver rebuilds the
            # skill meta-tool from fresh ThreadConfig.
            will_reload = queued_reload and should_emit_reload_command([])
            payload = _json_result(
                ok=True,
                action="publish",
                published=True,
                saved_path=saved_path,
                activated_current_thread=activated,
                reload_queued=will_reload,
                reload_cap_hit=cap_hit,
                skill={
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope,
                    "required_tools": skill.required_tools,
                    "tool_ttl": skill.tool_ttl,
                    "is_skill_kit": skill.is_skill_kit,
                },
            )
            if will_reload:
                payload = (
                    payload
                    + "\n\n[Skill reload queued - STOP NOW]\n"
                    "The skill list changed for this thread, but the current "
                    "graph invocation cannot see the new Skill meta-tool index. "
                    "Do not write a final answer or call another tool now. The "
                    "system will automatically resume you after rebuilding."
                )
            elif cap_hit:
                payload = (
                    payload
                    + "\n\n[Reload cap hit]: the skill was published and enabled "
                    "on this thread, but it will not be visible to the model "
                    "until the next user message."
                )
            return _command_or_text(payload, will_reload, tool_call_id, [])

        if action_key == "list":
            from ..core.agent import get_current_agent

            agent = get_current_agent()
            installed = []
            if agent is not None and getattr(agent, "skill_manager", None) is not None:
                installed = [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "scope": skill.scope,
                        "required_tools": skill.required_tools,
                        "tool_ttl": skill.tool_ttl,
                        "is_skill_kit": skill.is_skill_kit,
                    }
                    for skill in agent.skill_manager.list_installed(user_id=user_id)
                ]
            return _json_result(
                ok=True,
                action="list",
                drafts=[draft.public_summary() for draft in store.list(user_id)],
                installed=installed,
            )

        if action_key == "delete":
            scope_key = (scope or "user").strip().lower()
            if scope_key == "draft":
                target = _normalize_draft_id(draft_id or name)
                deleted = store.delete(user_id, target)
                return _json_result(ok=deleted, action="delete", deleted=deleted, draft_id=target)

            from ..core.agent import get_current_agent

            agent = get_current_agent()
            if agent is None or getattr(agent, "skill_manager", None) is None:
                return _json_result(ok=False, error={"type": "unavailable", "message": "skills subsystem not initialized"})
            delete_scope = _coerce_scope(scope_key)
            if delete_scope == "global":
                caller = agent.accounts_repo.get_user_by_id(user_id)
                if not caller or caller.role != "admin":
                    return _json_result(
                        ok=False,
                        error={"type": "permission_error", "message": "Global skill delete requires admin role"},
                    )
            target_name = _normalize_skill_name(name or draft_id)
            deleted = agent.skill_manager.uninstall(
                target_name,
                scope=delete_scope,
                user_id=user_id if delete_scope == "user" else None,
            )
            _invalidate_graph_caches(agent)
            return _json_result(
                ok=deleted,
                action="delete",
                deleted=deleted,
                name=target_name,
                scope=delete_scope,
            )

        return _json_result(
            ok=False,
            error={
                "type": "validation_error",
                "message": "action must be one of: draft, validate, publish, list, delete",
            },
        )
    except ValueError as exc:
        return _json_result(ok=False, error={"type": "validation_error", "message": str(exc)})
    except Exception as exc:
        logger.error("skill_config failed", exc_info=True)
        return _json_result(ok=False, error={"type": type(exc).__name__, "message": str(exc)})


@tool
async def skill_kit_create(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    allowed_tools: Optional[list[str] | str] = None,
    required_tools: Optional[list[str] | str] = None,
    tool_ttl: str = DEFAULT_SKILL_KIT_TOOL_TTL,
    draft_id: str = "",
    scope: str = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
    tool_id: str = "",
    parameters: Optional[dict[str, Any]] = None,
    http_config: Optional[dict[str, Any]] = None,
    sample_params: Optional[dict[str, Any]] = None,
    ttl: str = DEFAULT_SKILL_KIT_TOOL_TTL,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Create durable Skills or Skill Kits, optionally drafting HTTP tools first.

    Use a plain Skill when the reusable value is instructions only. Use a
    Skill Kit when future threads should receive both the instructions and
    exact Nymeria tools via required_tools.

    Actions:
      draft:             Save a Skill Kit draft.
      validate:          Validate a draft or inline Skill Kit fields.
      publish/package:   Publish a Skill Kit and optionally activate it here.
      list:              List Skill Kit drafts, installed skills, and HTTP drafts.
      draft_http_tool:   Draft an HTTP custom tool.
      test_http_tool:    Test an HTTP custom tool draft.
      publish_http_tool: Publish a tested HTTP tool and enable it on this thread.

    Args:
      tool_ttl: TTL for required tools when the skill is activated. Format:
               Nm/Nh/Nd/Nw or "never"/"permanent". Default "2h".

    Returns:
        JSON {tool_version, ok, action, ...}. Same schema as skill_config
        for draft/validate/publish (publish may trigger "[Skill reload
        queued - STOP NOW]" graph force-end). list adds
        {http_tool_drafts, published_http_tools}. publish_http_tool may
        also trigger "[Tool reload queued - STOP NOW]" when the new
        tool is bound. Errors: {ok: false, error: {type, message}}.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    store = _draft_store()
    action_key = (action or "").strip().lower()

    try:
        if action_key == "draft":
            draft = create_skill_draft(
                user_id=user_id,
                name=name,
                description=description,
                body=body,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
                tool_ttl=tool_ttl,
                draft_id=draft_id,
            )
            store.save(user_id, draft)
            return _json_result(ok=True, action="draft", draft=draft.public_summary())

        if action_key == "validate":
            draft = _load_draft_or_inline(
                store=store,
                user_id=user_id,
                draft_id=draft_id,
                name=name,
                description=description,
                body=body,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
                tool_ttl=tool_ttl,
            )
            return _json_result(ok=True, action="validate", draft=draft.public_summary())

        if action_key in {"publish", "package"}:
            from ..core.agent import get_current_agent

            agent = get_current_agent()
            if agent is None or getattr(agent, "skill_manager", None) is None:
                return _json_result(ok=False, error={"type": "unavailable", "message": "skills subsystem not initialized"})
            publish_scope = _coerce_scope(scope)
            draft = _load_draft_or_inline(
                store=store,
                user_id=user_id,
                draft_id=draft_id,
                name=name,
                description=description,
                body=body,
                allowed_tools=allowed_tools,
                required_tools=required_tools,
                tool_ttl=tool_ttl,
            )
            saved_path, activated, cap_hit, skill = _publish_skill(
                draft=draft,
                scope=publish_scope,
                user_id=user_id,
                thread_id=thread_id,
                overwrite=overwrite,
                activate_current_thread=activate_current_thread,
                reload_source="skill_kit_create",
                reload_reason="skill_kit_created",
            )
            queued_reload = bool(
                activate_current_thread
                and not cap_hit
                and getattr(agent, "_pending_tool_reload", {}).get(thread_id)
            )
            # Pure-skill change: rebind-safe in dynamic mode.
            will_reload = queued_reload and should_emit_reload_command([])
            payload = _json_result(
                ok=True,
                action="publish",
                published=True,
                saved_path=saved_path,
                activated_current_thread=activated,
                reload_queued=will_reload,
                reload_cap_hit=cap_hit,
                skill={
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope,
                    "required_tools": skill.required_tools,
                    "tool_ttl": skill.tool_ttl,
                    "is_skill_kit": skill.is_skill_kit,
                },
            )
            if will_reload:
                payload += (
                    "\n\n[Skill Kit reload queued - STOP NOW]\n"
                    "The Skill Kit was created and enabled on this thread, but "
                    "the current graph invocation cannot see the updated Skill "
                    "meta-tool index. Do not write a final answer or call "
                    "another tool now. The system will automatically resume "
                    "you after rebuilding."
                )
            elif cap_hit:
                payload += (
                    "\n\n[Reload cap hit]: the Skill Kit was created and enabled "
                    "on this thread, but it will not be visible until the next "
                    "user message."
                )
            return _command_or_text(payload, will_reload, tool_call_id, [])

        if action_key == "list":
            from .tool_create import _draft_store as tool_draft_store
            from .tool_create import _published_summary
            from ..core.custom_tools import get_custom_tool_loader
            from ..core.agent import get_current_agent

            agent = get_current_agent()
            installed = []
            if agent is not None and getattr(agent, "skill_manager", None) is not None:
                installed = [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "scope": skill.scope,
                        "required_tools": skill.required_tools,
                        "tool_ttl": skill.tool_ttl,
                        "is_skill_kit": skill.is_skill_kit,
                    }
                    for skill in agent.skill_manager.list_installed(user_id=user_id)
                ]
            loader = get_custom_tool_loader()
            return _json_result(
                ok=True,
                action="list",
                skill_drafts=[draft.public_summary() for draft in store.list(user_id)],
                installed=installed,
                http_tool_drafts=[
                    draft.public_summary() for draft in tool_draft_store().list(user_id)
                ],
                published_http_tools=[
                    _published_summary(defn)
                    for defn in sorted(loader.get_all_definitions(), key=lambda item: item.id)
                ],
            )

        if action_key == "draft_http_tool":
            from .tool_create import _draft_store as tool_draft_store
            from .tool_create import _json_result as tool_json_result
            from .tool_create import create_draft_definition
            from ..core.agent import get_current_agent

            draft = create_draft_definition(
                user_id=user_id,
                tool_id=tool_id,
                name=name,
                description=description,
                parameters=parameters,
                http_config=http_config,
                draft_id=draft_id,
                agent=get_current_agent(),
            )
            tool_draft_store().save(user_id, draft)
            return tool_json_result(ok=True, action="draft_http_tool", draft=draft.public_summary())

        if action_key == "test_http_tool":
            from .tool_create import _draft_store as tool_draft_store
            from .tool_create import _json_result as tool_json_result
            from .tool_create import _normalize_draft_id as normalize_tool_draft_id
            from .tool_create import test_draft

            target_draft_id = normalize_tool_draft_id(draft_id or tool_id)
            result = await test_draft(tool_draft_store(), user_id, target_draft_id, sample_params)
            return tool_json_result(action="test_http_tool", **result)

        if action_key == "publish_http_tool":
            from .tool_create import _draft_store as tool_draft_store
            from .tool_create import _normalize_draft_id as normalize_tool_draft_id
            from .tool_create import _publish_draft

            target_draft_id = normalize_tool_draft_id(draft_id or tool_id)
            return _publish_draft(
                store=tool_draft_store(),
                user_id=user_id,
                draft_id=target_draft_id,
                thread_id=thread_id,
                ttl=ttl,
                tool_call_id=tool_call_id,
                reload_source="skill_kit_create",
                reload_reason="http_tool_published_for_skill_kit",
            )

        return _json_result(
            ok=False,
            error={
                "type": "validation_error",
                "message": (
                    "action must be one of: draft, validate, publish, package, "
                    "list, draft_http_tool, test_http_tool, publish_http_tool"
                ),
            },
        )
    except ValueError as exc:
        return _json_result(ok=False, error={"type": "validation_error", "message": str(exc)})
    except Exception as exc:
        logger.error("skill_kit_create failed", exc_info=True)
        return _json_result(ok=False, error={"type": type(exc).__name__, "message": str(exc)})


SKILL_CONFIG_TOOLS = [skill_config]
SKILL_KIT_CREATE_TOOLS = [skill_kit_create]
