"""Skill meta-tool — the single `Skill` tool that carries the progressive-disclosure index.

The tool's description lists every skill active on the thread as
<available_skills>…</available_skills>. Calling `Skill(name="…")` returns the
full SKILL.md body of that skill as a ToolMessage, which the model then uses
as procedural knowledge for the rest of its turn(s).

No skill content lives in the system prompt. The index lives in the tool's
description (sent with every request alongside the tool schema), and the body
lives in a ToolMessage (conversation history).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, List, NamedTuple, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool as tool_decorator
from langchain_core.tools import InjectedToolArg, InjectedToolCallId
from langgraph.types import Command

from ..core.time_utils import parse_tool_ttl
from ..core.tool_reload import should_emit_reload_command, tool_reload_command
from . import AVAILABLE_SKILLS_CHAR_BUDGET, Skill, SkillManager

if TYPE_CHECKING:
    # Annotation-only; imported function-locally at runtime in
    # _bind_skill_kit_tools to avoid the tools.tool_search import cycle.
    from ..tools.tool_search import ToolBindingResult

logger = logging.getLogger(__name__)


_SKILL_TOOL_PREAMBLE = """Load the full instructions for a named skill.

A "skill" is a bundle of procedural knowledge (a markdown playbook plus
optional scripts and reference files on disk). The list of skills currently
available to you is in <available_skills> below — each has a name and a short
description explaining when to use it.

When you judge that a skill applies to the current task, call this tool with
that skill's name. The return value is the skill's full body — read it
carefully and follow its instructions. Skills may instruct you to read
reference files or run scripts using your existing tools (file_read,
bash_execute, etc.); the skill's directory path is included in the returned
body so you can resolve those references.

Optional `ttl` argument (Skill Kits only): some skills are "Skill Kits" that
bind extra tools onto this thread when you activate them. By default those
tools stay bound for the kit's own declared lifetime; pass `ttl` to set a
one-off lifetime for THIS activation instead, e.g. ttl="30m", "2h", "24h",
"7d", or "permanent". Important: `ttl` governs ONLY the kit's bound tools
(and, once nested skills are supported, any skills a kit pulls in the same way
it pulls in tools). It does NOT change how long the skill's instructions stay
with you: the body text this tool returns remains in your context until the
conversation is compacted, cleared, or scrolls out of the sliding window, no
matter what `ttl` you pass. Note that ttl="permanent" (or "never") makes the
kit's tools persist on the thread beyond this turn rather than expiring; use a
finite value like "2h" unless you intend a lasting change. `ttl` has no effect
on skills that bind no tools.

Call this tool at most once per distinct skill per turn. If no skill applies,
do not call it; proceed with your regular tools.
"""


def _render_available_skills(skills: List[Skill]) -> str:
    """Render the <available_skills> XML block for the tool description.

    Enforces the 15k char budget by truncating descriptions if the block would
    exceed it. In practice 20-40 typical skills fit comfortably.
    """
    lines: List[str] = ["<available_skills>"]
    for s in skills:
        desc = s.description.strip().replace("\n", " ")
        lines.append(f'  <skill name="{s.name}">{desc}</skill>')
    lines.append("</available_skills>")
    block = "\n".join(lines)

    if len(block) <= AVAILABLE_SKILLS_CHAR_BUDGET:
        return block

    logger.warning(
        "available_skills block (%d chars, %d skills) exceeds budget %d — truncating descriptions",
        len(block), len(skills), AVAILABLE_SKILLS_CHAR_BUDGET,
    )
    overhead_per_skill = len('  <skill name="">{}</skill>'.format(""))
    available = AVAILABLE_SKILLS_CHAR_BUDGET - len("<available_skills>\n</available_skills>\n")
    if not skills:
        return "<available_skills></available_skills>"
    budget_per = max(40, (available // len(skills)) - overhead_per_skill - 80)

    lines = ["<available_skills>"]
    for s in skills:
        desc = s.description.strip().replace("\n", " ")
        if len(desc) > budget_per:
            desc = desc[: budget_per - 1].rstrip() + "…"
        lines.append(f'  <skill name="{s.name}">{desc}</skill>')
    lines.append("</available_skills>")
    return "\n".join(lines)


def _render_skill_body(skill: Skill) -> str:
    """Render the full SKILL.md body with a small header that tells the model
    what auxiliary files exist and where they live."""
    parts = [f"# Skill: {skill.name}", "", skill.body.rstrip(), ""]

    aux_lines: List[str] = []
    if skill.has_references:
        aux_lines.append("References (read on demand with file_read):")
        for ref in skill.list_references():
            aux_lines.append(f"  - {skill.path}/{ref}")
    if skill.has_scripts:
        if aux_lines:
            aux_lines.append("")
        aux_lines.append("Scripts (invoke via bash_execute):")
        for script in skill.list_scripts():
            aux_lines.append(f"  - {skill.path}/{script}")
    if skill.has_assets:
        if aux_lines:
            aux_lines.append("")
        aux_lines.append(f"Assets directory (path only, do not auto-read): {skill.path}/assets/")

    if skill.allowed_tools:
        aux_lines.append("")
        aux_lines.append(
            "This skill's allowed-tools declaration: "
            + ", ".join(skill.allowed_tools)
        )

    if skill.required_tools:
        aux_lines.append("")
        aux_lines.append(
            "Skill Kit required Nymeria tools: "
            + ", ".join(skill.required_tools)
            + f" (ttl: {skill.tool_ttl})"
        )

    if aux_lines:
        parts.append("---")
        parts.extend(aux_lines)

    return "\n".join(parts)


def _allowed_tool_name(value: str) -> str:
    """Return the leading tool name from portable allowed-tools syntax."""
    return str(value).split("(", 1)[0].strip()


def _known_nymeria_tool_names() -> set[str]:
    """Best-effort set of Nymeria tool names for advisory allowed-tools checks.

    Agent Skills commonly use portable names such as Read, Write, and Bash.
    Those are not Nymeria tool names, so they should not produce missing-tool
    warnings unless a Nymeria tool with that exact name exists.
    """
    names: set[str] = set()
    try:
        from ..tools import SEED_TOOLS, CATALOG_TOOLS

        names.update(t.name for t in SEED_TOOLS)
        names.update(CATALOG_TOOLS.keys())
    except Exception:
        logger.warning("Failed to import tool names from tools module", exc_info=True)

    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        registry = getattr(agent, "tool_registry", None) if agent else None
        if registry is not None:
            names.update(
                str(item.get("name"))
                for item in registry.list_tools()
                if item.get("name")
            )
    except Exception:
        logger.debug("Failed to query dynamic tool registry")

    return names


class _SkillKitOutcome(NamedTuple):
    """Result of attempting to bind a skill's Skill Kit tools.

    ``failure_text`` is non-None only when binding failed and the caller must
    return it directly. ``result_suffix`` is the body block to append on the
    success path (empty when the skill binds no tools or binding failed).
    """

    binding: Optional["ToolBindingResult"]
    failure_text: Optional[str]
    result_suffix: str
    reload_queued: bool
    cap_hit: bool


def _resolve_active_skill(
    name: str,
    active_names: List[str],
    snapshot_by_name: dict[str, Skill],
    skill_manager: Optional[SkillManager],
    user_id: Optional[str],
) -> Optional[Skill]:
    """Resolve the named skill, re-reading from disk when possible.

    Prefers a live ``skill_manager`` re-read (so SKILL.md edits take effect
    without rebuilding the graph) and falls back to the graph-build snapshot. A
    name absent from the snapshot is still resolved when the manager has it
    (skills created or enabled since the graph was built, e.g. after a mid-turn
    reload_all). Returns None when no active skill matches.
    """
    skill: Optional[Skill] = None
    if name in set(active_names):
        if skill_manager is not None:
            skill = skill_manager.get(name, user_id=user_id)
        if skill is None:
            skill = snapshot_by_name.get(name)
    elif skill_manager is not None:
        # Not in the graph-build snapshot — check if installed/enabled
        # after a mid-turn reload_all (skills created or enabled since
        # the graph was built).
        skill = skill_manager.get(name, user_id=user_id)
        if skill is not None:
            logger.info("skill resolved dynamically (post-reload): %s", skill.name)
    return skill


def _resolve_effective_ttl(skill: Skill, ttl: Optional[str]) -> tuple[str, str]:
    """Resolve the effective Skill Kit tool TTL and any model-facing notice.

    A model-supplied ``ttl`` overrides the kit's declared ``tool_ttl``, but ONLY
    for the bound tools: the skill body is not governed by any TTL (it persists
    in conversation history until compaction, /clear, or sliding-window
    eviction). Returns ``(effective_ttl, ttl_notice)``; ``ttl_notice`` is empty
    when there is nothing to tell the model.
    """
    effective_ttl = skill.tool_ttl
    ttl_notice = ""
    if ttl is not None and str(ttl).strip():
        if not skill.required_tools:
            ttl_notice = (
                "[note] ttl was provided but this skill binds no Skill Kit "
                "tools, so it has no effect. TTL applies only to a kit's "
                "bound tools, not to the instructions above."
            )
        else:
            try:
                ttl_key, _ = parse_tool_ttl(ttl)
                effective_ttl = ttl_key
            except ValueError as exc:
                # Deliberately divergent from the /kit slash path: there an
                # invalid leading token is reinterpreted as the prompt
                # (command_service.activate_skill_kit / prepare_skill_slash_command).
                # Here the typed `ttl` arg means the model intended a TTL, so
                # fall back to the kit default and tell it the value was bad
                # rather than aborting activation. Do not "align" the two.
                ttl_notice = (
                    f"[note] Ignored ttl={ttl!r}: {exc} Bound this kit's "
                    f"tools with its default TTL ({skill.tool_ttl}) instead."
                )
    return effective_ttl, ttl_notice


def _bind_skill_kit_tools(
    skill: Skill, effective_ttl: str, config: RunnableConfig
) -> _SkillKitOutcome:
    """Bind a Skill Kit's required tools and report the outcome to the caller.

    Skills with no ``required_tools`` short-circuit to an empty outcome (no bind
    call). On a strict binding failure the outcome carries ``failure_text`` for
    the caller to return directly; on success it carries the body block to
    append plus the reload/cap-hit flags.
    """
    if not skill.required_tools:
        return _SkillKitOutcome(
            binding=None,
            failure_text=None,
            result_suffix="",
            reload_queued=False,
            cap_hit=False,
        )

    from ..tools.tool_search import bind_tools_for_thread
    from ..tools.utils import get_thread_id, get_user_id

    # Delegate the whole required set to bind_tools_for_thread and let
    # IT decide what is missing. It diffs against this thread's REAL
    # config (default-bound ∪ enabled_tools ∪ live temporary_tools −
    # disabled_tools), binds only the genuinely-missing tools, reports
    # already-present ones as no-ops, and queues a reload only when a
    # new binding was actually written.
    #
    # We must NOT pre-filter the required set against `thread_tools_set`
    # here. In dynamic-binding mode the meta-tool that executes Skill()
    # is built from the executor SUPERSET (every registered tool — see
    # compute_tool_superset), so `thread_tools_set` is "every tool that
    # exists," not "tools bound on this thread." A pre-gate keyed on it
    # classified every required tool as already-bound and skipped
    # binding entirely, so Skill Kit tools were never persisted.
    binding = bind_tools_for_thread(
        skill.required_tools,
        "",
        get_thread_id(config),
        get_user_id(config),
        ttl=effective_ttl,
        strict=True,
        source="skill_kit",
        skill_name=skill.name,
        reason="Skill Kit required_tools activation",
    )
    if not binding.ok:
        return _SkillKitOutcome(
            binding=binding,
            failure_text=(
                f"[Skill Kit activation failed: {skill.name}]\n"
                f"{binding.text}\n\n"
                "No required tools were bound. Do not follow this skill's "
                "instructions until the dependency problem is fixed."
            ),
            result_suffix="",
            reload_queued=False,
            cap_hit=False,
        )

    result_suffix = (
        "\n\n---\n"
        f"Skill Kit binding result for {skill.name}:\n"
        f"{binding.text}"
    )
    return _SkillKitOutcome(
        binding=binding,
        failure_text=None,
        result_suffix=result_suffix,
        reload_queued=bool(binding.reload_tools and not binding.cap_hit),
        cap_hit=bool(binding.cap_hit),
    )


def _allowed_tools_advisory(skill: Skill, thread_tools_set: set[str]) -> str:
    """Advisory body block warning when a skill's portable allowed-tools entries
    name real Nymeria tools missing from this thread.

    Returns an empty string when there is nothing to warn about (no thread-tool
    snapshot, no declared allowed-tools, or nothing missing).
    """
    if not (thread_tools_set and skill.allowed_tools):
        return ""
    known_nymeria_tools = _known_nymeria_tool_names()
    declared = [_allowed_tool_name(t) for t in skill.allowed_tools if t]
    missing = [
        t
        for t in declared
        if t and t in known_nymeria_tools and t not in thread_tools_set
    ]
    if not missing:
        return ""
    return (
        "\n\n---\n"
        "[notice] This skill's allowed-tools declaration references "
        "tools that are NOT enabled on the current thread: "
        + ", ".join(sorted(set(missing)))
        + ". If the skill's instructions require them, ask the user "
        "to enable those tools on this thread before proceeding."
    )


def _skill_kit_reload_notice(
    binding: "ToolBindingResult", config: RunnableConfig
) -> tuple[str, bool]:
    """Compose the post-binding reload / dynamic-bind body block.

    Returns ``(notice, emit_command)``. When ``emit_command`` is True the caller
    must wrap the accumulated body in ``tool_reload_command`` and return it (the
    new tools are not callable in the current graph invocation, so the model
    must stop for the rebuild/resume). When False the tools are callable on the
    next model step without a rebuild.
    """
    try:
        configurable = (config or {}).get("configurable") or {}
        thread_id = str(configurable.get("thread_id") or "")
    except Exception:
        thread_id = ""
    if should_emit_reload_command(binding.reload_tools, thread_id=thread_id):
        return (
            "\n\n---\n"
            "[Skill Kit reload queued - STOP NOW]\n"
            "This Skill Kit's required tools were just persisted, but they "
            "are not callable in the current graph invocation. Stop after "
            "this tool result. The system will automatically resume you "
            "after rebuilding the tool list; continue the user's task only "
            "after that resume.",
            True,
        )
    # Dynamic mode skips the rebuild round-trip. The next model step
    # resolves the updated tools, and SafeToolNode can dispatch
    # post-build tools from the live resolver.
    return (
        "\n\n---\n"
        "[Skill Kit tools bound; callable on the next model step]\n"
        "These tools were NOT bound before this Skill Kit activation — "
        "earlier turns of this conversation did not have access to them. "
        "They become callable immediately after this tool result without "
        "a graph rebuild or resume. "
        "See the binding-result block above for the exact bound-list "
        "delta; trust those counts over any assumption that the tools "
        "were already available.",
        False,
    )


def create_skill_meta_tool(
    active_skills: List[Skill],
    skill_manager: Optional[SkillManager] = None,
    user_id: Optional[str] = None,
    thread_tool_names: Optional[List[str]] = None,
) -> BaseTool:
    """Build the single `Skill` meta-tool for a given set of active skills.

    The tool's description is dynamically composed so that every request
    carries the (name, description) index. The body only loads when the
    model calls Skill(name=...).

    If skill_manager is provided, bodies are re-read from disk at activation
    time — edits to SKILL.md take effect without rebuilding the graph. If the
    thread's current tool_names are provided, the returned body will also
    include an advisory warning when the skill's declared allowed-tools name
    actual Nymeria tools that are missing from the thread.
    """
    # Snapshot the active-skill set by name for the description and lookup.
    active_names = [s.name for s in active_skills]
    snapshot_by_name: dict[str, Skill] = {s.name: s for s in active_skills}

    description = (
        _SKILL_TOOL_PREAMBLE
        + "\n"
        + _render_available_skills(active_skills)
        + "\n"
    )

    # NOTE: in dynamic-binding mode this set is the executor SUPERSET (every
    # registered tool), not the tools bound on this thread. It is only safe to
    # use for the advisory allowed-tools notice below — never as a gate that
    # decides whether to bind a Skill Kit's required_tools.
    thread_tools_set = set(thread_tool_names or [])

    @tool_decorator("Skill", return_direct=False)
    def skill_meta_tool(
        name: str,
        ttl: Annotated[
            Optional[str],
            "Optional one-off lifetime for a Skill Kit's bound tools, e.g. "
            "'30m', '2h', '7d', or 'permanent'. Overrides the kit's declared "
            "tool_ttl for this activation only. Applies ONLY to the bound "
            "tools, never to the skill's instruction text (which persists "
            "until compaction/clear/sliding-window). 'permanent'/'never' make "
            "the tools persist beyond this turn. Ignored for skills that bind "
            "no tools.",
        ] = None,
        *,
        tool_call_id: Annotated[str, InjectedToolCallId],
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> Union[str, Command]:
        """Load the full body of the named skill (see tool description for the list).

        Args:
            name: The exact name of one of the skills in <available_skills>.
            ttl: Optional. For Skill Kits only, a one-off lifetime for the
                tools the kit binds (e.g. "30m", "2h", "7d", "permanent"),
                overriding the kit's declared tool_ttl for this activation.
                Governs the bound tools only, not the returned instructions,
                which stay in context until compaction/clear/sliding-window.
                No effect for skills that bind no tools.
        """
        # Re-fetch from disk when possible so SKILL.md edits are live.
        skill = _resolve_active_skill(
            name, active_names, snapshot_by_name, skill_manager, user_id
        )
        if skill is None:
            available = ", ".join(sorted(active_names)) or "(none)"
            return (
                f"[skill not found] No active skill named {name!r} on this thread. "
                f"Active skills: {available}"
            )

        logger.info("skill activated: %s (scope=%s)", skill.name, skill.scope)
        body = _render_skill_body(skill)

        # Resolve the effective TTL for any Skill Kit tools this activation
        # binds. A model-supplied `ttl` overrides the kit's declared tool_ttl,
        # but ONLY for the bound tools: the skill body returned here is not
        # governed by any TTL (it persists in conversation history until
        # compaction, /clear, or the sliding window evicts it).
        effective_ttl, ttl_notice = _resolve_effective_ttl(skill, ttl)

        outcome = _bind_skill_kit_tools(skill, effective_ttl, config)
        if outcome.failure_text is not None:
            return outcome.failure_text
        body += outcome.result_suffix

        if ttl_notice:
            body += "\n\n---\n" + ttl_notice

        # Advisory: warn the model only when portable allowed-tools entries
        # correspond to real Nymeria tool names missing from this thread.
        body += _allowed_tools_advisory(skill, thread_tools_set)

        if outcome.reload_queued and outcome.binding is not None:
            notice, emit_command = _skill_kit_reload_notice(outcome.binding, config)
            body += notice
            if emit_command:
                return tool_reload_command(body, tool_call_id)

        if outcome.cap_hit:
            body += (
                "\n\n---\n"
                "[notice] This Skill Kit's required tools were persisted, but "
                "the turn already hit the in-turn reload cap. Do not call those "
                "new tools until the next user turn."
            )

        return body

    skill_meta_tool.description = description
    return skill_meta_tool
