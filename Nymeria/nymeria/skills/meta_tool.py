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
from typing import Annotated, List, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool as tool_decorator
from langchain_core.tools import InjectedToolArg, InjectedToolCallId
from langgraph.types import Command

from ..core.tool_reload import tool_reload_command
from . import AVAILABLE_SKILLS_CHAR_BUDGET, Skill, SkillManager

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

Call this tool at most once per distinct skill per turn. If no skill applies,
do not call it — proceed with your regular tools.
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
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS

        names.update(t.name for t in ALL_TOOLS)
        names.update(OPTIONAL_TOOLS.keys())
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

    thread_tools_set = set(thread_tool_names or [])

    @tool_decorator("Skill", return_direct=False)
    def skill_meta_tool(
        name: str,
        *,
        tool_call_id: Annotated[str, InjectedToolCallId],
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> Union[str, Command]:
        """Load the full body of the named skill (see tool description for the list).

        Args:
            name: The exact name of one of the skills in <available_skills>.
        """
        # Re-fetch from disk when possible so SKILL.md edits are live.
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

        if skill is None:
            available = ", ".join(sorted(active_names)) or "(none)"
            return (
                f"[skill not found] No active skill named {name!r} on this thread. "
                f"Active skills: {available}"
            )

        logger.info("skill activated: %s (scope=%s)", skill.name, skill.scope)
        body = _render_skill_body(skill)

        binding_text = ""
        binding_reload_queued = False
        binding_cap_hit = False
        if skill.required_tools:
            from ..tools.tool_search import bind_tools_for_thread
            from ..tools.utils import get_thread_id, get_user_id

            binding = bind_tools_for_thread(
                skill.required_tools,
                "",
                get_thread_id(config),
                get_user_id(config),
                ttl=skill.tool_ttl,
                strict=True,
                source="skill_kit",
                skill_name=skill.name,
                reason="Skill Kit required_tools activation",
            )
            if not binding.ok:
                return (
                    f"[Skill Kit activation failed: {skill.name}]\n"
                    f"{binding.text}\n\n"
                    "No required tools were bound. Do not follow this skill's "
                    "instructions until the dependency problem is fixed."
                )

            binding_text = binding.text
            binding_reload_queued = bool(binding.reload_tools and not binding.cap_hit)
            binding_cap_hit = bool(binding.cap_hit)
            body += (
                "\n\n---\n"
                f"Skill Kit binding result for {skill.name}:\n"
                f"{binding_text}"
            )

        # Advisory: warn the model only when portable allowed-tools entries
        # correspond to real Nymeria tool names missing from this thread.
        if thread_tools_set and skill.allowed_tools:
            known_nymeria_tools = _known_nymeria_tool_names()
            declared = [_allowed_tool_name(t) for t in skill.allowed_tools if t]
            missing = [
                t
                for t in declared
                if t and t in known_nymeria_tools and t not in thread_tools_set
            ]
            if missing:
                body += (
                    "\n\n---\n"
                    "[notice] This skill's allowed-tools declaration references "
                    "tools that are NOT enabled on the current thread: "
                    + ", ".join(sorted(set(missing)))
                    + ". If the skill's instructions require them, ask the user "
                    "to enable those tools on this thread before proceeding."
                )

        if binding_reload_queued:
            body += (
                "\n\n---\n"
                "[Skill Kit reload queued - STOP NOW]\n"
                "This Skill Kit's required tools were just persisted, but they "
                "are not callable in the current graph invocation. Stop after "
                "this tool result. The system will automatically resume you "
                "after rebuilding the tool list; continue the user's task only "
                "after that resume."
            )
            return tool_reload_command(body, tool_call_id)

        if binding_cap_hit:
            body += (
                "\n\n---\n"
                "[notice] This Skill Kit's required tools were persisted, but "
                "the turn already hit the in-turn reload cap. Do not call those "
                "new tools until the next user turn."
            )

        return body

    skill_meta_tool.description = description
    return skill_meta_tool
