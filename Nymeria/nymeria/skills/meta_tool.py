"""Skill meta-tool: the single `Skill` tool that carries the progressive-disclosure index.

The tool's description lists every skill active on the thread as
<available_skills>...</available_skills>. Calling `Skill(name="...", ttl="...")`
returns the full SKILL.md body of that skill as a ToolMessage, which the model
then uses as procedural knowledge for the rest of its turn(s); for a Skill Kit
it also binds the kit's tools for the required `ttl` window.

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
from . import (
    AVAILABLE_SKILLS_CHAR_BUDGET,
    Skill,
    SkillManager,
    expanded_required_tools,
    resolve_nested_skills,
)

if TYPE_CHECKING:
    # Annotation-only; imported function-locally at runtime in
    # _bind_skill_kit_tools to avoid the tools.tool_search import cycle.
    from ..tools.tool_search import ToolBindingResult

logger = logging.getLogger(__name__)


_SKILL_TOOL_PREAMBLE = """Load a skill's full instructions; for a Skill Kit, also bind its tools.

A skill is a playbook (markdown plus optional scripts and reference files).
A Skill Kit is a skill that also BINDS TOOLS you do not otherwise have: an
entry with binds="N tools" below is a kit, and those tools stay unavailable
until you activate it. The <available_skills> list is what is enabled on this
thread; any installed skill loads by exact name (find others with
search_skills; a skill that is not installed needs install_skill first).

RULE: when a task falls in a listed kit's area, activate that kit FIRST,
before attempting the task with generic tools (bash_execute, file tools, web
fetch) or telling the user it cannot be done. Read the returned body and
follow it; it names any reference files or scripts, which you resolve with
your existing tools. Call this tool at most once per distinct skill per turn
(exception: re-activating with defer=false a kit you first loaded with
defer=true). If no skill applies, do not call it.

`ttl` (required): how long the kit's tools stay bound. Nm/Nh/Nd/Nw (e.g.
"45m", "3d"; max one year) or "permanent". Size it to the task and prefer
the shortest window that covers it; suggested_ttl in the list is the kit
author's typical span, not a default. An invalid value refuses the
activation. A kit that requires other kits binds their tools too, under the
same window. The window governs only the bound tools: the instructions stay
in your context until compaction, /clear, or the sliding window, regardless.
For a skill without binds= the value is accepted and ignored; pass any valid
one. When the window lapses you are told once and a call to a lapsed tool is
refused; re-activate for a fresh window.

`defer` (kits only): pass defer=true for a one-off use of a kit's tools. The
instructions and the tools' argument schemas load without binding, and you
run them by name via tool_invoke (cache-safe; protected management tools
cannot run this way). For a multi-step task or future use, bind instead
(defer=false), so the schemas sit in your tool list and your arguments are
grammar-constrained. With defer=true, ttl is ignored.
"""


def _skill_entry_attrs(skill: Skill) -> str:
    """``binds="N tools" suggested_ttl="2h"`` for a kit; empty for a plain skill.

    The structural cue is what lets a cold model tell "instructions only"
    from "tools I do not have yet" without parsing prose; the suggested
    window is the author's typical span, quoted because the agent must pick
    its own ``ttl`` (there is no default on the agent call).
    """
    parts: List[str] = []
    if skill.required_tools:
        count = len(skill.required_tools)
        parts.append(f"{count} tool{'s' if count != 1 else ''}")
    if skill.required_skills:
        parts.append("nested kits")
    if skill.thread_templates:
        parts.append("thread templates")
    if not parts:
        # Same predicate as Skill.is_skill_kit: any declared dependency or
        # template makes it a kit, and every kind binds under the ttl.
        return ""
    return f' binds="{" + ".join(parts)}" suggested_ttl="{skill.tool_ttl}"'


def _render_available_skills(skills: List[Skill]) -> str:
    """Render the <available_skills> XML block for the tool description.

    Enforces the 15k char budget by truncating descriptions if the block would
    exceed it. In practice 20-40 typical skills fit comfortably.
    """
    lines: List[str] = ["<available_skills>"]
    for s in skills:
        desc = s.description.strip().replace("\n", " ")
        lines.append(f'  <skill name="{s.name}"{_skill_entry_attrs(s)}>{desc}</skill>')
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
        lines.append(f'  <skill name="{s.name}"{_skill_entry_attrs(s)}>{desc}</skill>')
    lines.append("</available_skills>")
    return "\n".join(lines)


def _render_skill_body(skill: Skill, *, effective_ttl: Optional[str] = None) -> str:
    """Render the full SKILL.md body with a small header that tells the model
    what auxiliary files exist and where they live. ``effective_ttl`` is the
    window this activation asked for (None on a deferred load and on a
    nested body, whose tools bind under the OUTER activation's window); the
    kit's own ``tool_ttl`` is always quoted as the author's suggestion. The
    binding-result block, not this line, says which tools actually got a
    fresh TTL entry (an already-bound default tool gets none)."""
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
        window = (
            f"activation ttl {effective_ttl}; " if effective_ttl else ""
        ) + f"kit suggests {skill.tool_ttl}"
        aux_lines.append(
            "Skill Kit required Nymeria tools: "
            + ", ".join(skill.required_tools)
            + f" ({window})"
        )

    if skill.required_skills:
        aux_lines.append("")
        aux_lines.append(
            "Skill Kit required skills: " + ", ".join(skill.required_skills)
        )

    templates = skill.thread_templates
    if templates:
        aux_lines.append("")
        aux_lines.append(
            "Thread templates (callable threads, created on first call): "
            + ", ".join(t.name for t in templates)
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


def _resolve_effective_ttl(
    skill: Skill, ttl: Optional[str], required_tools: Optional[List[str]] = None
) -> tuple[Optional[str], str]:
    """Resolve the window for the tools this activation binds, or refuse.

    ``ttl`` is required on the agent's ``Skill()`` call and has NO default:
    a kit activation with a blank or invalid value returns ``(None, refusal)``
    and the caller loads nothing. A skill that binds no tools accepts any
    value and reports it ignored. The window governs the bound tools only
    (``required_tools``, the nested-expansion union; defaults to the skill's
    own list); the skill body persists in history regardless.
    ``skill.tool_ttl`` is the author's SUGGESTION, quoted in the refusal; it
    is the default only for the human ``/kit`` command and programmatic
    activation (``command_service.activate_skill_kit``), never here. (The
    ``/kit`` slash path reinterprets a bad leading token as prompt text; here
    the typed argument means the model intended a TTL, so a bad one refuses
    rather than guesses. Do not "align" the two.)
    """
    effective_tools = (
        skill.required_tools if required_tools is None else required_tools
    )
    if not effective_tools:
        # One clause: the preamble already explains the required-but-ignored
        # contract, and this lands on every plain-skill activation.
        return skill.tool_ttl, "[note] ttl ignored: this skill binds no tools."
    try:
        ttl_key, _ = parse_tool_ttl("" if ttl is None else str(ttl))
    except ValueError as exc:
        return None, (
            f"[Skill activation refused: {skill.name}] {exc} Pass the window "
            "you expect to need this kit's tools for; the kit suggests "
            f'"{skill.tool_ttl}". Nothing was loaded or bound.'
        )
    return ttl_key, ""


def _bind_skill_kit_tools(
    skill: Skill,
    effective_ttl: str,
    config: RunnableConfig,
    required_tools: Optional[List[str]] = None,
) -> _SkillKitOutcome:
    """Bind a Skill Kit's required tools and report the outcome to the caller.

    ``required_tools`` is the tool set to bind, normally the nested-expansion
    union (``expanded_required_tools``); it defaults to the skill's own list.
    Activations with no tools to bind short-circuit to an empty outcome (no
    bind call). On a strict binding failure the outcome carries
    ``failure_text`` for the caller to return directly; on success it carries
    the body block to append plus the reload/cap-hit flags.
    """
    tools_to_bind = (
        skill.required_tools if required_tools is None else required_tools
    )
    if not tools_to_bind:
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
        tools_to_bind,
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


# One deliberate exception to "defer binds nothing": when the thread lacks the
# tool_invoke executor the deferred instructions rely on, it is bound with this
# self-cleaning TTL rather than permanently (developer decision, 2026-08-13;
# backlog #170).
_DEFER_EXECUTOR_TTL = "7d"


def _unbound_direct_calls_active() -> bool:
    """True when deferred tools are called DIRECTLY by name on this thread.

    Delegates to the graph-build strip predicate
    (``agent_graph.unbound_direct_calls_active``) so the two surfaces cannot
    drift: under it, ``tool_invoke`` is deliberately dropped from the bound
    schema (direct unbound calls make it redundant) and an explicit call to
    it is refused by the by-name gate. Deferred instructions must therefore
    point at direct calls there, and an auto-bind of ``tool_invoke`` would
    only be stripped again at the next build (backlog #170, Gap 1b).
    """
    from ..core.agent import get_current_agent
    from ..core.agent_graph import unbound_direct_calls_active

    agent = get_current_agent()
    return unbound_direct_calls_active(getattr(agent, "settings", None))


def _ensure_deferred_executor(
    skill: Skill, config: RunnableConfig, use_direct: bool
) -> tuple[str, bool, bool]:
    """Guarantee the executor a deferred activation tells the model to use.

    A deferred kit's tool schemas and thread templates are run via
    ``tool_invoke``, which the thread may not actually have (#164: seeding is
    first-time-only, so an account whose ``default_thread_tools`` predates the
    tool silently lacks it). Defer promising an unreachable executor reads to
    the model as its own failure. Resolution, in order:

    - Direct-call mode, or nothing in this activation points at
      ``tool_invoke``: nothing to ensure.
    - Reachable (``thread_tool_reachability``, which resolves from thread
      config, never the built tool list): nothing to ensure.
    - Disabled on the thread: honest note, NO un-disable. Calling the bind
      path would silently reverse an explicit per-thread decision
      (``bind_tools_for_thread`` un-disables first), so steer to ``ttl``
      binding instead, which needs no ``tool_invoke``.
    - Unreachable: bind ``tool_invoke`` with a self-cleaning 7-day TTL and say
      so. A bind failure never fails the activation: the instructions still
      return, with a steer note.

    Returns ``(note_block, emit_reload_command, executor_bound)``.
    ``emit_reload_command`` is True only when the auto-bind was persisted on
    the legacy rebuild path, so the caller must wrap the accumulated body in
    ``tool_reload_command``. ``executor_bound`` is True whenever the auto-bind
    persisted, so the deferred blocks can drop their cache-safe framing (the
    tools prefix DID change in that case).
    """
    if use_direct or not (skill.required_tools or skill.thread_templates):
        return "", False, False

    from ..tools.tool_search import bind_tools_for_thread, thread_tool_reachability
    from ..tools.utils import get_thread_id, get_user_id

    # Bare accessor calls, matching _bind_skill_kit_tools: both coerce a
    # missing value to "default" and never raise.
    thread_id = get_thread_id(config)
    user_id = get_user_id(config)

    state = thread_tool_reachability("tool_invoke", thread_id, user_id)
    if state == "bound":
        return "", False, False
    if state == "disabled":
        return (
            "\n\n---\n"
            "[tool_invoke disabled on this thread] The deferred instructions "
            "above rely on tool_invoke, which this thread has explicitly "
            "disabled (disabled_tools is authoritative; it was NOT "
            "overridden). To use this kit's tools, activate the kit again "
            f'with Skill(name="{skill.name}", ttl=...) to bind them '
            "first-class (no tool_invoke needed), choosing the ttl for how "
            'long you expect to need them (Nm/Nh/Nd/Nw or "permanent"), or '
            "ask the user to re-enable tool_invoke.",
            False,
            False,
        )

    binding = bind_tools_for_thread(
        ["tool_invoke"],
        "",
        thread_id,
        user_id,
        ttl=_DEFER_EXECUTOR_TTL,
        strict=True,
        source="skill_kit_defer",
        skill_name=skill.name,
        reason="deferred activation requires the tool_invoke executor",
    )
    if not binding.ok:
        return (
            "\n\n---\n"
            "[note] tool_invoke is not bound on this thread and auto-binding "
            f"it failed:\n{binding.text}\n"
            "The deferred instructions above cannot run as written. Activate "
            f'the kit again with Skill(name="{skill.name}", ttl=...) to bind '
            "its tools first-class instead, choosing the ttl for how long "
            'you expect to need them (Nm/Nh/Nd/Nw or "permanent").',
            False,
            False,
        )

    note = (
        "\n\n---\n"
        "[tool_invoke auto-bound] Deferred activation binds none of the "
        "kit's tools, with one exception made here: tool_invoke (the "
        "executor the instructions above rely on) was not bound on this "
        f"thread, so it was bound with a {_DEFER_EXECUTOR_TTL} TTL."
    )
    if binding.cap_hit:
        note += (
            " However, this turn already hit the in-turn reload cap, so "
            "tool_invoke becomes callable only on the next user turn; the "
            "deferred tools cannot run in this turn."
        )
        return note, False, True
    if binding.reload_tools:
        if should_emit_reload_command(binding.reload_tools, thread_id=thread_id):
            note += (
                "\n\n[Tool reload queued - STOP NOW]\n"
                "tool_invoke was persisted but is not callable in the current "
                "graph invocation. Stop after this tool result. The system "
                "will rebuild the tool list and resume you automatically; run "
                "the deferred tools via tool_invoke only after that resume."
            )
            return note, True, True
        note += " It is callable from your next step."
    return note, False, True


def _defer_kit_tools_block(
    skill: Skill, use_direct: bool, executor_bound: bool = False
) -> str:
    """Body block for a deferred Skill Kit activation (binds no kit tools).

    Loads each required tool's compact argument schema so the model can call
    it by name (via ``tool_invoke``, or directly in permissive dynamic mode)
    without the kit mutating the thread's tool list. Skills that bind no
    tools get a short no-op note instead. The repeat-use steer is the
    kit-level one, a second ``Skill()`` call with a ``ttl``, never a
    ``tool_manage`` detour (backlog #170). When ``executor_bound`` (the
    activation auto-bound ``tool_invoke``), the cache-safe framing is dropped:
    the tools prefix DID change, and the auto-bound note explains why.
    """
    if not skill.required_tools:
        return (
            "\n\n---\n"
            "[defer] This skill binds no Skill Kit tools, so defer had nothing "
            "to load; its instructions above are ready to use."
        )

    from ..core.agent import get_current_agent
    from ..tools.schema_render import render_tool_args_schema
    from ..tools.tool_search import _resolve_tool_object

    agent = get_current_agent()
    if use_direct:
        instruction = (
            "Call these tools DIRECTLY by name (this thread allows direct "
            "unbound calls, gated the same as any other tool call)."
        )
    elif executor_bound:
        instruction = "Run these tools by name with tool_invoke(name, arguments)."
    else:
        instruction = (
            "Run these tools by name with tool_invoke(name, arguments) "
            "(cache-safe)."
        )
    lines = [
        "\n\n---\n"
        "[Skill Kit deferred] None of this kit's tools were bound to this "
        f"thread. {instruction} "
        "If this becomes a multi-step task or you will use the kit again "
        'later, activate it again with Skill(name="' + skill.name + '", '
        "ttl=...) to bind its tools first-class instead, choosing the ttl "
        'for how long you expect to need them (Nm/Nh/Nd/Nw or "permanent").',
    ]
    for name in skill.required_tools:
        tool_obj = _resolve_tool_object(name, agent)
        if tool_obj is None:
            lines.append(
                f"  - {name}: schema unavailable (its backing source may be "
                "installed but disabled)"
            )
            continue
        schema = render_tool_args_schema(tool_obj)
        lines.append(f"  - {name} args: {schema or '(no arguments)'}")
    return "\n".join(lines)


def _nested_skill_bodies_block(skill: Skill, nested_skills: List[Skill]) -> str:
    """Full-activation body sections for a kit's nested skills (defer=false).

    Each nested skill's full body is appended (that is what activation means
    for a plain skill; its tools, if any, were bound by the caller's single
    union bind). Nesting is one level deep: a nested kit's OWN required_skills
    are listed, never expanded.
    """
    if not nested_skills:
        return ""
    parts: List[str] = []
    for nested in nested_skills:
        logger.info(
            "nested skill activated: %s (required by %s)", nested.name, skill.name
        )
        parts.append(
            f"\n\n---\n[Nested skill: {nested.name} (required by {skill.name})]\n\n"
            + _render_skill_body(nested)
        )
        if nested.required_skills:
            parts.append(
                "\n\n[note] Nested kit "
                f"{nested.name!r} declares further required skills that were "
                "NOT auto-activated (nesting is one level deep): "
                + ", ".join(nested.required_skills)
                + ". Load any of them on demand with Skill(name=..., ttl=...)."
            )
    return "".join(parts)


def _defer_required_skills_block(
    skill: Skill,
    skill_manager: Optional[SkillManager],
    user_id: Optional[str],
    snapshot_by_name: dict[str, Skill],
) -> str:
    """Deferred listing of a kit's nested skills: name + description only.

    Nothing activates; each entry is loadable on demand via Skill(). A name
    that does not resolve is listed as not installed rather than failing,
    because defer binds nothing so there is no partial state to protect.
    """
    if not skill.required_skills:
        return ""
    nested, missing = resolve_nested_skills(
        skill, skill_manager, user_id, snapshot_by_name=snapshot_by_name
    )
    lines = [
        "\n\n---\n"
        "[Required skills (deferred)] This kit pulls in the skills below. "
        "None were activated; load one on demand with Skill(name=..., "
        "ttl=...) (add defer=true to keep that load cache-safe too).",
    ]
    for nested_skill in nested:
        kind = "kit" if nested_skill.is_skill_kit else "skill"
        desc = nested_skill.description.strip().replace("\n", " ")
        lines.append(f"  - {nested_skill.name} ({kind}): {desc}")
    for name in missing:
        lines.append(
            f"  - {name}: (not installed; install_skill can fetch it if needed)"
        )
    return "\n".join(lines)


def _defer_thread_templates_block(skill: Skill, use_direct: bool) -> str:
    """Deferred listing of a kit's thread templates: name + description + schema.

    Nothing is registered on the thread; each template is runnable by name
    (the dispatch superset carries templates from every installed skill), and
    its thread materializes on the first call.
    """
    templates = skill.thread_templates
    if not templates:
        return ""
    from ..agents.tool_factory import create_template_thread_tool
    from ..tools.schema_render import render_tool_args_schema

    run_how = (
        "run one directly by name (this thread allows direct unbound calls)"
        if use_direct
        else "run one via tool_invoke(name, arguments)"
    )
    lines = [
        "\n\n---\n"
        "[Thread templates (deferred)] This kit declares callable-thread "
        f"templates. Nothing was registered on this thread; {run_how}. Its "
        "thread is created on the first call (with that call's task) and "
        "reused afterwards.",
    ]
    for template in templates:
        desc = template.description.strip().replace("\n", " ")
        lines.append(f"  - {template.name}: {desc}")
        try:
            schema = render_tool_args_schema(
                create_template_thread_tool(skill.name, template)
            )
        except Exception:  # noqa: BLE001 - schema rendering is enrichment
            logger.debug(
                "template schema render failed for %r", template.name,
                exc_info=True,
            )
            schema = ""
        if schema:
            lines.append(f"    args: {schema}")
    return "\n".join(lines)


def _register_thread_templates(
    skill: Skill, config: RunnableConfig, *, reload_already_queued: bool
) -> tuple[str, bool, bool]:
    """Register a kit's thread templates on the current thread (defer=false).

    Registration = enabling the kit on the thread (idempotent): template
    tools are DERIVED from the active skill set at graph build, so this is
    the persistent step that surfaces them, and deactivation is the single
    removal path. Returns ``(note, reload_queued, cap_hit)``; the reload is
    only queued when the activation actually changed the thread AND the
    caller's tool binding did not already queue one (a second queue would
    clobber the pending reload's tool list).
    """
    templates = skill.thread_templates
    if not templates:
        return "", False, False

    from ..core.agent import get_current_agent
    from ..tools.utils import get_thread_id

    agent = get_current_agent()
    try:
        thread_id = get_thread_id(config)
    except Exception:  # noqa: BLE001
        thread_id = ""
    if agent is None or not thread_id:
        return (
            "\n\n---\n[note] This kit declares thread templates, but no "
            "active thread was available to register them on.",
            False,
            False,
        )

    from ..tools.skill_config import _activate_skill_on_thread, _queue_skill_reload

    try:
        changed = _activate_skill_on_thread(agent, thread_id, skill.name)
    except Exception as exc:  # noqa: BLE001
        return (
            f"\n\n---\n[note] Thread template registration failed: {exc}",
            False,
            False,
        )

    queued = cap_hit = False
    if changed and not reload_already_queued:
        queued, cap_hit = _queue_skill_reload(
            agent,
            thread_id,
            skill.name,
            source="skill_kit",
            reason="thread_templates_registered",
        )

    names = ", ".join(t.name for t in templates)
    note = (
        "\n\n---\n"
        f"[Thread templates registered]: {names}. Each is a callable-thread "
        "tool; its thread is created on the FIRST call (with that call's "
        "task) and reused afterwards. Registration enabled this kit on the "
        "thread; deactivating the kit removes the template tools (already "
        "materialized threads live on as ordinary spawned threads)."
    )
    return note, queued, cap_hit


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
        "These tools were NOT bound before this Skill Kit activation: "
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
    model calls Skill(name=..., ttl=...).

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
            str,
            "Required. How long this kit's tools stay bound: Nm/Nh/Nd/Nw "
            "(e.g. '45m', '3d'; max one year) or 'permanent'. Size it to the "
            "task and prefer the shortest window that covers it; the list's "
            "suggested_ttl is the author's typical span, not a default. An "
            "invalid value refuses the activation. Governs only the bound "
            "tools, never the returned instructions. Accepted and ignored "
            "for a skill that binds no tools, and ignored when defer=true "
            "(pass any valid value).",
        ],
        defer: Annotated[
            Optional[bool],
            "Skill Kits only. Decide by usage: one-off use of the kit's tools "
            "= defer=true; multi-step or future use = bind with ttl instead. "
            "When true, load the kit's instructions and its tools' argument "
            "schemas WITHOUT binding the kit's tools; call them by name via "
            "tool_invoke (cache-safe). A bound schema sits in your tool list "
            "and is called more reliably (grammar-constrained); a deferred "
            "one is prose in history. ttl is still required by the schema "
            "and is ignored when defer=true.",
        ] = False,
        *,
        tool_call_id: Annotated[str, InjectedToolCallId],
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> Union[str, Command]:
        """Load the full body of the named skill (see tool description for the list).

        Args:
            name: The exact name of an installed skill (one listed in
                <available_skills>, or any other installed skill by exact name).
            ttl: Required. How long the tools a Skill Kit binds stay bound,
                sized to how long you expect to need them (e.g. "45m", "3d",
                "permanent"); an invalid value refuses the activation.
                Governs the bound tools only, not the returned instructions,
                which stay in context until compaction/clear/sliding-window.
                Accepted and ignored for skills that bind no tools, and
                ignored when defer=true.
            defer: Optional. For Skill Kits only. When true, load the kit's
                instructions plus its tools' schemas without binding the
                kit's tools; call them via tool_invoke. Cache-safe, best for
                one-off use; a multi-step task or future use binds instead
                (defer=false). ttl stays required by the schema and is
                ignored when defer=true.
        """
        # Re-fetch from disk when possible so SKILL.md edits are live.
        skill = _resolve_active_skill(
            name, active_names, snapshot_by_name, skill_manager, user_id
        )
        if skill is None:
            available = ", ".join(sorted(active_names)) or "(none)"
            return (
                f"[skill not found] No installed skill named {name!r} is visible "
                f"to you. Skills enabled on this thread: {available}. If you "
                "found this skill via search_skills and it is not installed yet, "
                "install it first with install_skill."
            )

        logger.info("skill activated: %s (scope=%s)", skill.name, skill.scope)

        # Deferred activation: load the kit's tool schemas and list its nested
        # skills but bind nothing, so the model runs the tools by name (via
        # tool_invoke, or directly in permissive dynamic mode) with the prompt
        # cache preserved. One exception: when the thread lacks the
        # tool_invoke executor those instructions rely on,
        # _ensure_deferred_executor binds it on a self-cleaning TTL (or says
        # honestly why it will not, when the thread disabled it).
        if defer:
            body = _render_skill_body(skill)
            use_direct = _unbound_direct_calls_active()
            executor_note, emit_reload, executor_bound = _ensure_deferred_executor(
                skill, config, use_direct
            )
            body += _defer_kit_tools_block(
                skill, use_direct, executor_bound=executor_bound
            )
            body += _defer_required_skills_block(
                skill, skill_manager, user_id, snapshot_by_name
            )
            body += _defer_thread_templates_block(skill, use_direct)
            body += executor_note
            if skill.required_tools:
                body += (
                    "\n\n---\n[note] ttl ignored: defer=true binds no tools; "
                    "ttl governs a bound kit's tool lifetime."
                )
            body += _allowed_tools_advisory(skill, thread_tools_set)
            if emit_reload:
                return tool_reload_command(body, tool_call_id)
            return body

        # Nested required skills resolve first (one level deep) and are
        # strict: a missing nested skill aborts before anything binds, so
        # there is never a partial activation.
        nested_skills: List[Skill] = []
        if skill.required_skills:
            nested_skills, missing_nested = resolve_nested_skills(
                skill, skill_manager, user_id, snapshot_by_name=snapshot_by_name
            )
            if missing_nested:
                return (
                    f"[Skill Kit activation failed: {skill.name}]\n"
                    "This kit requires skills that are not installed: "
                    f"{', '.join(missing_nested)}.\n"
                    "Nothing was bound or activated. Install the missing "
                    "skills first (install_skill) or fix the kit's "
                    "required_skills, then activate again."
                )
        union_tools = expanded_required_tools(skill, nested_skills)

        # Resolve the window for any Skill Kit tools this activation binds,
        # BEFORE rendering the body: a blank or invalid `ttl` on a kit
        # refuses the whole activation (nothing loaded, nothing bound; the
        # agent call has no default). The one window governs the whole union
        # (outer + nested kits' tools) and never the body, which persists in
        # history until compaction, /clear, or the sliding window.
        effective_ttl, ttl_notice = _resolve_effective_ttl(
            skill, ttl, required_tools=union_tools
        )
        if effective_ttl is None:
            return ttl_notice
        body = _render_skill_body(
            skill, effective_ttl=effective_ttl if union_tools else None
        )

        outcome = _bind_skill_kit_tools(
            skill, effective_ttl, config, required_tools=union_tools
        )
        if outcome.failure_text is not None:
            return outcome.failure_text
        body += _nested_skill_bodies_block(skill, nested_skills)
        body += outcome.result_suffix

        # Kit-declared thread templates register AFTER a successful bind so a
        # binding failure never half-activates the kit.
        template_note, template_reload_queued, template_cap_hit = (
            _register_thread_templates(
                skill, config, reload_already_queued=outcome.reload_queued
            )
        )
        body += template_note

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

        if template_reload_queued:
            # Legacy rebuild mode, skill-only change (no tool bind queued a
            # reload): the template tools cannot bind in this invocation.
            body += (
                "\n\n---\n"
                "[Skill reload queued - STOP NOW]\n"
                "This kit's thread templates were registered, but the current "
                "graph invocation cannot bind them. Stop after this tool "
                "result; the system will rebuild the tool list and resume "
                "you automatically."
            )
            return tool_reload_command(body, tool_call_id)

        if outcome.cap_hit:
            body += (
                "\n\n---\n"
                "[notice] This Skill Kit's required tools were persisted, but "
                "the turn already hit the in-turn reload cap. Do not call those "
                "new tools until the next user turn."
            )
        if template_cap_hit:
            body += (
                "\n\n---\n"
                "[notice] Thread templates were registered, but the turn "
                "already hit the in-turn reload cap; the template tools become "
                "callable on the next user turn."
            )

        return body

    skill_meta_tool.description = description
    return skill_meta_tool
