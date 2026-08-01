"""Generated on-disk map of the Nymeria resource stores.

Slice 1 of the resource-filesystem-layout plan (backlog #75): the runtime
data dir is the agent's resource root, and this module writes the live
per-deployment index into it at API startup: ``README.md`` plus JSON Schemas
for the JSON-backed stores under ``schema/``. Writes are write-if-changed so
boots do not churn mtimes; the artifacts are Nymeria-owned, so manual edits
to them are regenerated (every other file under the root is live state and
is never touched here). The durable generic knowledge lives in the bundled
``nymeria-resources`` skill, which points at these artifacts for live paths.

``_STORE_ROWS`` doubles as the security register for those stores: every row
declares which control governs writes that did not come through an authoring
surface (see ``StoreControl``). Co-located with the doc columns on purpose, so
that adding a store without classifying it is a ``TypeError`` rather than an
omission nobody notices.

That alone would only bind a developer already editing this list, which is the
one who was never going to forget. So ``tests/test_resource_layout.py`` walks
the package for every ``data_dir / "<name>"`` and fails on any child that is in
neither this list, ``_OPERATIONAL_DIRS``, nor its own reasoned non-store set.
It also reconciles the refusal columns against live ``file_write`` behavior and
ratchets the uncontrolled set so it can only shrink. The reasoning behind each
verdict, and what a gate is and is not worth, is in
``docs/private/security/control-store-matrix.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import KW_ONLY, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


class StoreControl(str, Enum):
    """Verdict on how a store is protected from writes that bypass authoring.

    The data dir is deliberately a management surface (backlog #75), so its
    stores are editable by hand on purpose. That makes some of them *inputs to
    execution*, reachable by ``file_write``/``file_edit``, which are seed tools.
    The question each row answers is which of three answers applies, and the
    choice is made by one rule rather than per store:

    1. Does Nymeria itself consume this file without the agent choosing to look
       at it (loaded into a prompt, dispatched as a tool, launched as a process,
       or used to resolve a destination or a credential)? If not, the file is
       data and the general untrusted-content posture covers it: ``EXEMPT``.
    2. If it is consumed that way, does the file route reach an effect that the
       sanctioned surface would not have granted this caller? Then the file
       tools refuse, at the same width as the thing being bypassed: outright
       (``denylisted``) when there is no legitimate hand-editing workflow at
       all, so nothing is lost by refusing everyone, or for non-admins only
       (``admin_only``) when the workflow is legitimate but the sanctioned
       surface is itself role-gated, so what the file route bypassed was the
       role check rather than the workflow. Refusing more widely than the
       surface does would buy nothing and cost the management surface. Both are
       the rare case.
    3. Otherwise the write is allowed to land and the *artifact* is made inert
       until it is re-approved through the sanctioned surface (``gates``). This
       is the default, because it preserves the management surface: it stops a
       bad artifact rather than stopping the agent from acting.

    ``UNCONTROLLED`` is not a fourth answer. It records a store that rule 2 or 3
    should cover and where no control covers the whole store yet, with the
    finding or sweep that established it. Note "whole": two of the eight do
    carry a gate, over part of their content only, and are classified at their
    weakest variant. New stores may not land here; the test pins the set.

    None of these three is declared. Each is derived from ``_StoreRow`` (see
    its ``control`` property) so that a verdict cannot be asserted independently
    of the facts that decide it.
    """

    PROTECTED = "protected"
    EXEMPT = "exempt"
    UNCONTROLLED = "uncontrolled"


@dataclass(frozen=True)
class _StoreRow:
    path: str
    what: str
    scope: str
    hot_load: str
    posture: str
    _: KW_ONLY
    # Keyword-only, and the two below have no default at all: a new store
    # cannot be added without answering rule 1 and saying why. The optional
    # fields default to the safe reading, "no control here".
    # Rule 1, and the only judgement call on the row: does Nymeria itself
    # consume this file without the agent choosing to look at it?
    drives_execution: bool
    control_note: str
    # Does a control cover the WHOLE store? False on a store where one variant
    # is gated and another is not, which is the weakest-variant convention.
    covered: bool = False
    # Declared, not derived from tools/filesystem.py's denylist. Deriving would
    # make this column a mirror, and a mirror cannot notice that somebody
    # removed the entry: it would simply report the new reality as intended.
    # Declaring it makes it a claim, which the test then reconciles against
    # live file_write behavior in both directions.
    denylisted: bool = False
    # The narrower sibling of ``denylisted``: the file tools refuse the write
    # for a non-admin and allow it for an admin. Kept as its own column rather
    # than folded into ``denylisted`` because the two make different claims and
    # the reconcile test proves different things: a denylisted row must be
    # refused for EVERYONE, and asserting that of this one would be false.
    admin_only: bool = False
    # Dotted paths, resolved by the test rather than imported here: the gate
    # modules pull in heavy dependencies and some import back into core, which
    # is why every store import in this module is function-local.
    gates: tuple[str, ...] = ()

    @property
    def control(self) -> StoreControl:
        """The verdict, DERIVED rather than declared.

        Declaring it was a mistake worth recording: it let the verdict be
        asserted independently of the facts that decide it, so the cheapest way
        to shrink the list of unprotected stores was to relabel one EXEMPT and
        edit the pin, which costs exactly one line and lands no control. Derived,
        that move does not exist. Reclassifying a store now means writing
        ``drives_execution=False`` next to a note explaining what it drives,
        which is a conspicuous, checkable, factual lie rather than a quiet
        change of opinion.
        """
        if not self.drives_execution:
            return StoreControl.EXEMPT
        return StoreControl.PROTECTED if self.covered else StoreControl.UNCONTROLLED


_STORE_ROWS: tuple[_StoreRow, ...] = (
    _StoreRow(
        "custom_tools/<id>.json",
        "Custom tool and workflow definitions (http, mcp, python, workflow types)",
        "global",
        "yes",
        "publish via tool_create; raw edits are inert until re-approval, for "
        "every implementation type",
        gates=(
            "nymeria.core.python_custom_tools.python_execution_gate",
            "nymeria.core.workflows.authoring.workflow_execution_gate",
            "nymeria.core.custom_tool_gate.custom_tool_execution_gate",
        ),
        covered=True,
        drives_execution=True,
        control_note=(
            "P4-01, CLOSED. All four implementation types now recompute an "
            "approval hash on every call, so the store is no longer classified "
            "at its weakest variant. Every type stamps at the AUTHORING layer "
            "with the acting user, never at save time: the persist path cannot "
            "tell a config that came from the request from one it just read off "
            "disk, so stamping there would let a name-only PUT approve a "
            "planted launch command. For http the stamp separates 'came through "
            "publish' from 'appeared in the directory' (authoring is "
            "agent-reachable by design and stays that way); for mcp, which has "
            "always been admin-only to author, it records a real admin "
            "decision. Residual, shared with the other three: the hash lives in "
            "the file it protects, so a writer who replicates the canonical "
            "form can forge it. The control is against a file-write primitive, "
            "not against a shell."
        ),
    ),
    _StoreRow(
        "custom_tools/revisions/<tool_id>/<hash>.py",
        "Python/workflow source revisions (last 10 kept)",
        "global",
        "n/a",
        "publish via tool_create; raw edits are inert until re-approval",
        drives_execution=False,
        control_note=(
            "Archive kept for diffing, and exempt for exactly one reason: "
            "nothing reads it back. Execution and the admin review view both "
            "take source_code off the definition JSON. Worth knowing how thin "
            "that is: retain_source_revision is not the only writer (snapshot "
            "restore walks this subtree), it skips a path that already exists "
            "so a file pre-planted at a valid hash name survives under an "
            "approved name, and the prune sorts by mtime so planted files can "
            "evict real ones. All harmless while nothing reads it, and all live "
            "the day the diffing this exists for is actually implemented."
        ),
    ),
    _StoreRow(
        "hooks/<user>.json",
        "Lifecycle hook definitions (schema/hook_store.schema.json)",
        "per user",
        "yes",
        "files-as-truth",
        drives_execution=True,
        control_note=(
            "D3-01 (re-scoped to file-tool confinement, so the remediation is "
            "not hook-shaped). Hook logic fires on Nymeria's own dispatch path "
            "with no approval hash. Not nothing, though: run_workflow and "
            "run_command re-gate at fire time, and hooks/approvals/ is "
            "deliberately not files-as-truth, so a planted approval cannot "
            "forge consent (the waiter is an in-process Future). External "
            "edits are audited, but only against a .sig sidecar a sanctioned "
            "write left behind, so a store planted where the user never "
            "authored one is silent (D3-02)."
        ),
    ),
    _StoreRow(
        "triggers/<user>.json",
        "Event trigger definitions (schema/trigger_store.schema.json)",
        "per user",
        "yes",
        "files-as-truth",
        gates=("nymeria.core.workflows.authoring.workflow_execution_gate",),
        drives_execution=True,
        control_note=(
            "D7-01, re-scoped to persistence: a planted trigger is scheduled "
            "autonomous re-execution that outlives the turn and the process. "
            "Of four action types, run_workflow re-gates its payload at every "
            "fire (the one store that inherits a trust gate), notify cannot "
            "execute, and two reach autonomous execution ungated, the second "
            "being create_todo with scheduled_for (D7-06). Structural fields "
            "are constrained by closed Literal allowlists and MAX_TRIGGERS, "
            "and external edits are audited: shape and volume limits and "
            "detection, none of which stop a well-formed planted trigger."
        ),
    ),
    _StoreRow(
        "skills/global/<name>/SKILL.md; skills/users/<id>/<name>/SKILL.md",
        "Skills and Skill Kits (kit = skill with required_tools); same-name "
        "user/global skills shadow bundled ones",
        "global + per user",
        "yes",
        "files-as-truth",
        drives_execution=True,
        control_note=(
            "C11-01. A planted SKILL.md is injected instruction plus a tool "
            "grant (a kit binds tools with a TTL and can declare "
            "thread_templates). It has to be selected to take effect, which is "
            "why it rates below hooks and triggers, but the planter writes the "
            "description, so the agent's own semantic search can surface it "
            "unprompted. Global scope, and a same-name skill shadows a bundled "
            "one. Skill-bound tools do still pass the role gate."
        ),
    ),
    _StoreRow(
        "mcp_servers/<id>.json",
        "MCP server definitions (schema/mcp_server.schema.json)",
        "global",
        "yes",
        "files-as-truth",
        denylisted=True,
        gates=("nymeria.core.mcp_execution_gate.mcp_execution_gate",),
        drives_execution=True,
        covered=True,
        control_note=(
            "The only store carrying both controls, and the model the others "
            "are measured against. There is no legitimate hand-editing workflow "
            "(the sanctioned surface is manage_mcp), so rule 2 applies and the "
            "file tools refuse the path; the launch-surface hash then makes a "
            "write that arrived some other way inert. The hash deliberately "
            "excludes env_vars and headers, so an env-based launch hijack that "
            "leaves the command alone is caught by the denylist, not the gate "
            "(C7-02), and the gate's guarantee starts at the backfill marker "
            "rather than at install (C7-03). Both residuals need a writer the "
            "denylist does not reach, which is the point of holding both."
        ),
    ),
    _StoreRow(
        "thread_configs/<thread_id>.json",
        "Per-thread config: model, tools, skills, dreaming "
        "(schema/thread_config.schema.json)",
        "per thread",
        "yes",
        "files-as-truth",
        gates=("nymeria.core.llm_provider_utils.destination_redirects_away_from_config",),
        drives_execution=True,
        control_note=(
            "P4-03. Carries base_url, so a write redirects the model call to a "
            "caller-named host: provider key, full prompt, and the model's "
            "replies (E10-01). Validating the PATCH route's schema does not "
            "reach this path, which is why the check lives where the config is "
            "CONSUMED: llm_provider_utils.destination_redirects_away_from_config "
            "refuses a base_url the deployment is not configured for, falls "
            "back to the configured destination and latches a one-shot notice, "
            "so a planted file cannot walk off with the operator credential "
            "unless it also supplies a key of its own. "
            "No role exemption, precisely because a planted file's author is "
            "not the thread's owner. Residual: a caller who supplies their OWN "
            "api_key may still name any address (deliberate, it is the "
            "bring-your-own-endpoint capability, and ownership of that key is "
            "checked by provenance, not presence, along the same precedence "
            "the key resolution itself uses), and this file still selects "
            "tools and skills."
        ),
    ),
    _StoreRow(
        "teams/<user>.json",
        "Callable-team entities: name, description, team memory "
        "(schema/team_store.schema.json); membership lives on thread configs "
        "as callable_team_id",
        "per user",
        "yes",
        "files-as-truth",
        drives_execution=True,
        control_note=(
            "P4. Team memory is seeded into every member thread's context "
            "(agent_memory_seed.read_team_memory), so a write is injected prose "
            "the agent never chose to read: the system_prompt.md shape, scoped "
            "to one team. Membership itself lives on thread configs, and "
            "cross-team reach is enforced at graph build, not here."
        ),
    ),
    _StoreRow(
        "dream_prompt.md, dream_kickoff.md",
        "Dream prompt overrides; file absent = built-in default",
        "global",
        "yes",
        "readable; writes are admin-only, as at PUT /settings/dream-prompts",
        admin_only=True,
        covered=True,
        drives_execution=True,
        control_note=(
            "P4-02, CLOSED. Unlike system_prompt.md these hot-load. They drive "
            "dream turns, which run unattended on a strict tool allowlist that "
            "nonetheless includes authoring memory, the parent's instructions, "
            "skills, triggers and tools. Rule 2, at role width: the file tools "
            "apply the same admin check PUT /settings/dream-prompts carries "
            "(tools/filesystem.py::admin_only_write_error). Reads unaffected. "
            "Residual: bash_execute is not path-checkable."
        ),
    ),
    _StoreRow(
        "system_prompt.md",
        "System prompt override; file absent = built-in default",
        "global",
        "no (read at startup; apply via settings update or restart)",
        "readable; writes are admin-only, as at PUT /settings/system-prompt",
        admin_only=True,
        covered=True,
        drives_execution=True,
        control_note=(
            "P4-02, CLOSED. Replaces the whole system prompt, for every user of "
            "the deployment, and is the highest-leverage persistence primitive "
            "here: it survives compaction, pruning and thread deletion by not "
            "being in the conversation. The startup-only read was a delay and "
            "not a control, since restarts happen and POST /restart exists. "
            "Rule 2, at role width: the file tools apply the same admin check "
            "PUT /settings/system-prompt carries "
            "(tools/filesystem.py::admin_only_write_error). Reads unaffected. "
            "Residual: bash_execute is not path-checkable."
        ),
    ),
    _StoreRow(
        "workflows/state/",
        "Workflow scratch state (nym.state)",
        "per workflow + user",
        "yes",
        "treat as read-only: workflows do not expect concurrent external edits",
        drives_execution=False,
        control_note=(
            "C10 walked all fourteen nym.* verbs and found none that decides "
            "anything from state content. It parameterizes a run the workflow "
            "gate already approved. Not quite the way that run's arguments do, "
            "though: state is keyed only by (workflow_id, user_id), outlives "
            "approval and is not purged on delete, so a value planted before "
            "approval is read by the approved revision afterwards. Arguments "
            "come from the caller per call; this comes from the last writer."
        ),
    ),
    _StoreRow(
        "hooks/<user>_executions.json, triggers/<user>_executions.json",
        "Execution logs",
        "per user",
        "n/a",
        "generated, read-only",
        drives_execution=False,
        control_note=(
            "Nymeria does read these back unbidden, on every fire, but only to "
            "append and re-cap them. Nothing decides from them: cooldowns and "
            "fire counts live on the definition store, the reaction debounce is "
            "process-local, and the hook recursion bound is a turn-loop local. "
            "So a planted entry is content someone had to go and fetch, which "
            "is the untrusted-content posture rather than a store control."
        ),
    ),
    _StoreRow(
        "service_token_warnings.json",
        "Service-token expiry-warning dedupe state (hourly sweep)",
        "global",
        "n/a",
        "generated; safe to delete (worst case: one duplicate warning)",
        drives_execution=False,
        control_note=(
            "Dedupe bookkeeping for the expiry sweep. Understating it would be "
            "easy: phases descend toward expiry and the sweep re-persists what "
            "it reads, so a planted terminal phase suppresses EVERY remaining "
            "notification for that token, permanently, not one. Still exempt "
            "because it cannot extend the token and the per-pass log warning is "
            "unconditional, but this is the closest an exempt row comes."
        ),
    ),
    _StoreRow(
        "README.md, schema/",
        "This map (generated at startup, write-if-changed)",
        "global",
        "n/a",
        "generated, manual edits are overwritten",
        drives_execution=False,
        control_note=(
            "Nymeria-owned output, rewritten at startup whenever it differs "
            "from what this module renders. Nothing consumes it; it exists to "
            "be read."
        ),
    ),
)

_OPERATIONAL_DIRS = (
    "activity/", "auth_tokens/", "backups/", "cli_history/", "flags/",
    "goals/", "hooks/approvals/", "logs/", "mcp_install_previews/",
    "mcp_runtimes/", "notifications/", "thread_metadata/", "thread_notes/",
    "todos/", "tool_drafts/", "users/", "voice/", "workflows/pending/",
    "workflows/runs/",
)


def render_resource_readme() -> str:
    """Render the root README.md content (deterministic, no timestamps)."""
    lines: list[str] = [
        "# Nymeria resource root",
        "",
        "Generated by Nymeria at startup (write-if-changed); manual edits to",
        "this file and to `schema/` are overwritten. Everything else under",
        "this directory is live runtime state: every agent-owned store",
        "(custom tools, workflows, hooks, triggers, skills and kits, MCP",
        "server configs, thread configs, prompt overrides) lives here as",
        "plain files, editable with the generic file tools. A few carry a",
        "narrower edit posture, named per row in the table below. The bundled",
        "`nymeria-resources` skill carries the editing rules; this file is",
        "the live per-deployment index it points at.",
        "",
        "## Resource stores",
        "",
        "| Path | What | Scope | Hot-load | Edit posture |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in _STORE_ROWS:
        lines.append(
            f"| `{row.path}` | {row.what} | {row.scope} | {row.hot_load} "
            f"| {row.posture} |"
        )
    lines += [
        "",
        'Hot-load "yes": a direct file edit is picked up on next use (checks',
        "are debounced to about two seconds), no restart needed. Same-instant",
        "rewrites that keep the byte size identical can be missed; when in",
        "doubt, touch the file again or use the matching authoring tool.",
        "",
        "`<store>.json.sig` sidecar files beside the hook and trigger stores",
        "are Nymeria bookkeeping (they let loaders tell tool-driven writes",
        "from raw edits, for the activity log). Leave them alone; deleting",
        "one is harmless beyond a missed or duplicate audit line.",
        "",
        "## Approval-gated content",
        "",
        "Python custom tools and workflow definitions run behind",
        "execution-time approval gates keyed to a content hash. Editing",
        "their source on disk is allowed but makes them inert: every call",
        "re-checks the hash and refuses until an admin re-approves",
        "(tool_create publish, or the workflow approval surfaces).",
        "",
        "## Off limits: credentials",
        "",
        "Anything holding credentials (the encrypted account vault and OAuth",
        "token caches) is not part of this interface. Use auth_write and",
        "auth_test (see the credential-management skill) instead.",
        "",
        "## Operational state (not yours to edit)",
        "",
        "Readable for debugging, but edits can corrupt runtime state:",
        "",
        "  " + ", ".join(f"`{d}`" for d in _OPERATIONAL_DIRS) + ",",
        "  plus the SQLite databases and token files at this root.",
        "",
        "## Etiquette for raw edits",
        "",
        "- Prefer the purpose-built tools (tool_create, hook_config,",
        "  trigger_config, skill_write, mcp_manage, workflow_info) when one",
        "  fits: they validate input and keep connected clients current.",
        "- Do not raw-edit a store that a running turn is also mutating via",
        "  tools: writes are whole-file last-writer-wins.",
        "- A malformed edit to a JSON store file is quarantined on next load:",
        "  the file moves into a `quarantine/` subdirectory beside the store",
        "  (timestamped, bytes preserved) and the store loads without it. Fix",
        "  the quarantined copy and move it back. A corrupt SKILL.md is",
        "  skipped (or served from cache) instead of quarantined.",
        "- Raw edits emit no UI events; open client panels stay stale until",
        "  refreshed.",
        "",
        "## Schemas",
        "",
        "`schema/*.schema.json` (generated) describe the JSON store file",
        "formats. Skills are markdown with YAML frontmatter; see the",
        "skill-management skill.",
        "",
    ]
    return "\n".join(lines)


def _render_schemas() -> Iterable[tuple[str, str]]:
    """Yield (filename, content) pairs for the JSON-backed store schemas.

    Imports are function-local: the store modules pull in heavier deps and
    some import back into core. Guarded so an import failure skips the
    schemas instead of aborting the whole map write (the caller's except
    clause only covers OSError, and the README should land regardless).
    """
    try:
        from .hook_manager import HookStore
        from .team_manager import TeamStore
        from .trigger_manager import TriggerStore
        from .thread_config import ThreadConfig
        from ..tools.definitions.custom_tool_schema import CustomToolDefinition
        from ..tools.definitions.mcp_schema import MCPServerDefinition
    except Exception:  # noqa: BLE001 - schemas are best-effort extras
        logger.exception("Store schema imports failed; skipping schema render")
        return

    models = (
        ("hook_store", HookStore),
        ("team_store", TeamStore),
        ("trigger_store", TriggerStore),
        ("thread_config", ThreadConfig),
        ("custom_tool", CustomToolDefinition),
        ("mcp_server", MCPServerDefinition),
    )
    for name, model in models:
        try:
            schema = model.model_json_schema()
        except Exception:  # noqa: BLE001 - one bad schema must not sink the rest
            logger.exception("Failed to render %s schema", name)
            continue
        yield (
            f"{name}.schema.json",
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
        )


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only when it differs; report a write."""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
    except OSError:
        pass  # Unreadable existing file: fall through and rewrite it.
    path.write_text(content, encoding="utf-8")
    return True


def write_resource_map(root: Path | None = None) -> list[Path]:
    """Write README.md + schema/ into the resource root; return written paths.

    Best-effort: never raises (startup must not fail on a map write), logs
    per-file failures, skips everything if the root cannot be created.
    """
    from ..tools.execution_environment import resource_root

    base = root or resource_root()
    written: list[Path] = []
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Resource root %s not writable; skipping map", base)
        return written

    targets: list[tuple[Path, str]] = [(base / "README.md", render_resource_readme())]
    schema_dir = base / "schema"
    try:
        schema_dir.mkdir(parents=True, exist_ok=True)
        targets += [(schema_dir / name, content) for name, content in _render_schemas()]
    except OSError:
        logger.exception("Schema dir %s not writable; writing README only", schema_dir)

    for path, content in targets:
        try:
            if _write_if_changed(path, content):
                written.append(path)
        except OSError:
            logger.exception("Failed to write resource map file %s", path)
    return written
