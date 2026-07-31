"""Execution-time trust gate for ``http`` and ``mcp`` custom tools (P4-01).

``data/custom_tools/<id>.json`` is a hot-loading, GLOBAL-scope store the generic
file tools can write. Two of its four implementation types already refuse to run
from a record that never came through Nymeria's own code path: ``python`` via
``core/python_custom_tools.py`` and ``workflow`` via
``core/workflows/authoring.py``. The other two had no check at all, and the
resource map stated the asymmetry as if it were a decision.

It was not a decision, and the ``mcp`` half is the reason this module exists.
``MCPToolConfig`` carries ``server_command``/``server_args``/``working_directory``
DIRECTLY, so a planted definition of that type spawns a subprocess on
invocation. The identical launch surface reached through ``data/mcp_servers/``
IS gated (``core/mcp_execution_gate.py``). Same store class, same fields, one
gated and one not.

**Where the stamp is applied, and why it is not here.** Like its three siblings,
this gate stamps at the AUTHORING layer with the real actor id
(``build_custom_tool_definition`` / ``apply_custom_tool_update`` for the REST
routes, the import route, and ``tool_create`` publish for the agent's own http
authoring), never in ``CustomToolLoader.save_definition``. Stamping at the
storage chokepoint is tempting because it cannot be forgotten, and it was the
first shape of this module, but it is wrong in a way that matters: a name-only
``PUT /tools/custom/{id}`` reads the record off disk, changes a label, and
re-persists, so the storage path would approve a launch command nobody authored.
Authoring-layer stamping costs a missed call site becoming an inert tool, which
is the fail-safe direction and the cost the python gate already pays.

No capability is lost. ``tool_create`` accepts ``http`` (with ``python`` and
``workflow``), and the authoring actor self-approves, so an agent that could
publish an HTTP tool yesterday still can. ``mcp`` custom tools have always been
admin-only to author (``tool_create`` rejects the type, and every REST route
that creates one is ``require_admin_user``), so for that type the stamp records
a real admin decision, exactly as the Python gate's does.

What the hash covers is the surface that decides WHERE the request goes, WHAT
rides along, and WHAT the caller may leave unset:

* ``parameters``, for both types. This is not padding: ``_create_pydantic_schema``
  installs an optional parameter's ``default`` into the args schema, langchain
  fills it in whenever the model omits the argument, and ``interpolate_params``
  substitutes it into an HTTP tool's url, headers, query params and body. So an
  edit to a default redirects an approved, credential-bearing tool, and flipping
  ``required`` to false lets a planted default apply where the model used to be
  asked. Both sibling gates hash parameters for this reason.
* ``http``: method, url, headers, query params, body template. Headers are safe
  to include, unlike the MCP SERVER gate which must exclude them: there is no
  post-save credential-vaulting rewrite for custom tools, because agent-authored
  ones must already spell ``${credential:...}`` at authoring time
  (``tools/tool_create.py``). So a stamp cannot be invalidated behind the
  author's back.
* ``mcp``: transport, server command, args, url, headers, working directory,
  tool name, server id, and env vars. ``env_vars`` IS hashed here, where the
  sibling gate excludes it, and the difference is load-bearing rather than
  cosmetic: ``migrate_mcp_encrypted_env_vars`` rewrites ``data/mcp_servers/``
  only, so a custom tool's env block is stable after save and can be covered.
  That closes the launch-hijack residual the sibling still carries (an edit
  injecting ``LD_PRELOAD``/``NODE_OPTIONS``/``BASH_ENV`` without touching the
  command). ``server_id`` is hashed because it is the credential-vault target
  key (``mcp_manager``: ``target_id=config.server_id or config.server_command``),
  so editing it re-points which vault rows the tool satisfies.

Deliberately NOT hashed, so the omissions are choices rather than oversights:
the http ``timeout_seconds``/``response_path``/``response_format`` and the mcp
``idle_timeout_seconds``/``startup_timeout_seconds``/``call_timeout_seconds``.
These shape how long a call waits and how much of an answer comes back, not
where it goes or what rides along, and an edit to one cannot reach a new
destination or a new process.

Residual, stated the way the other three gates state theirs: the hash lives in
the file it protects and uses a plain digest, so a writer who replicates this
canonical form can forge a matching approval. The value is against a file-write
primitive, not against a shell; a ``bash_execute``-capable caller already has
code execution. Per ``SECURITY.md`` 2.2 that is the boundary, and this is not it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from .time_utils import utc_now

logger = logging.getLogger(__name__)

# The types this gate governs. ``python`` and ``workflow`` have their own gates
# and must not be double-stamped here.
GATED_IMPLEMENTATION_TYPES = frozenset({"http", "mcp"})

# One-shot marker (a non-JSON file, so the loader never reads it as a
# definition) recording that the pre-gate grandfathering has run.
_BACKFILL_MARKER = ".custom_tool_gate_backfill.done"


def _str_map(value: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in (value or {}).items()}


def _http_surface(config: Any) -> dict[str, Any]:
    return {
        "method": str(getattr(config, "method", "") or ""),
        "url": str(getattr(config, "url", "") or ""),
        "headers": _str_map(getattr(config, "headers", None)),
        "query_params": _str_map(getattr(config, "query_params", None)),
        "body_template": str(getattr(config, "body_template", "") or ""),
    }


def _mcp_surface(config: Any) -> dict[str, Any]:
    return {
        "transport": str(getattr(config, "transport", "") or ""),
        "server_command": str(getattr(config, "server_command", "") or ""),
        "server_args": [str(a) for a in (getattr(config, "server_args", None) or [])],
        "url": str(getattr(config, "url", "") or ""),
        "headers": _str_map(getattr(config, "headers", None)),
        "working_directory": str(getattr(config, "working_directory", "") or ""),
        "tool_name": str(getattr(config, "tool_name", "") or ""),
        "server_id": str(getattr(config, "server_id", "") or ""),
        "env_vars": _str_map(getattr(config, "env_vars", None)),
        "encrypted_env_vars": _str_map(getattr(config, "encrypted_env_vars", None)),
    }


def _parameter_surface(definition: Any) -> dict[str, Any]:
    """The parameter map, reduced to the fields that reach execution.

    ``default`` and ``required`` are the two that matter (see the module
    docstring); ``type``/``enum`` ride along because they constrain what the
    model may put in the same slots, and ``description`` because it is the only
    thing steering what the model puts there at all.
    """
    params = getattr(definition, "parameters", None) or {}
    surface: dict[str, Any] = {}
    for name, param in sorted(params.items()):
        surface[str(name)] = {
            "type": str(getattr(param, "type", "") or ""),
            "description": str(getattr(param, "description", "") or ""),
            "required": bool(getattr(param, "required", False)),
            "default": json.dumps(getattr(param, "default", None), sort_keys=True, default=str),
            "enum": [str(e) for e in (getattr(param, "enum", None) or [])],
        }
    return surface


def _config_for(definition: Any) -> tuple[str, Any]:
    """Return ``(implementation_type, config)`` for a definition."""
    impl = str(getattr(definition, "implementation_type", "") or "")
    return impl, getattr(definition, f"{impl}_config", None)


def compute_custom_tool_revision_hash(definition: Any) -> str:
    """Canonical content hash over a gated custom tool's execution surface.

    Takes a ``CustomToolDefinition``, and only that. An earlier draft also
    accepted the raw on-disk dict so the startup backfill could hash bytes
    directly, which was a quiet defect: attribute access over a dict yields
    ``None`` for an absent key, so a pydantic default (``MCPToolConfig.transport``
    is ``"stdio"``, ``HTTPToolConfig.method`` is ``"GET"``) was invisible on that
    path. A hand-written definition relying on either default would have been
    stamped with a hash execution could never reproduce, and refused with the
    misleading "changed since it was approved". The backfill parses to a model
    instead, so there is one hashing path and it is this one.

    Returns "" for the ungated types, so a caller can treat "no hash" and "not
    our business" as one case.
    """
    impl, config = _config_for(definition)
    if impl not in GATED_IMPLEMENTATION_TYPES or config is None:
        return ""
    surface = _http_surface(config) if impl == "http" else _mcp_surface(config)
    canonical = json.dumps(
        {
            "implementation_type": impl,
            "parameters": _parameter_surface(definition),
            **surface,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp_custom_tool_approval(definition: Any, *, approved_by: str) -> Any:
    """Approve the CURRENT execution revision, mutating ``definition``.

    Called from the authoring layer, never from storage: see the module
    docstring for why the storage chokepoint is the wrong home. A no-op for
    ``python``/``workflow``, which carry their own gates, so a caller may apply
    it without branching on the type.

    ``approved_by`` is assigned unconditionally. It is attribution rather than a
    control (nothing reads it back to decide anything), but an
    only-when-empty assignment let an imported record declare its own approver
    and keep the claim, and let a re-approval by a second admin keep the first
    one's name. The sibling gates assign unconditionally too.
    """
    impl, config = _config_for(definition)
    if impl not in GATED_IMPLEMENTATION_TYPES or config is None:
        return definition
    definition.approved_revision = compute_custom_tool_revision_hash(definition)
    definition.approved_at = utc_now()
    definition.approved_by = approved_by
    return definition


def custom_tool_execution_gate(definition: Any) -> Optional[str]:
    """None when this tool may run, else the refusal copy (fail closed).

    Recomputes from live content on every call, so an edited record or a
    revoked approval takes effect immediately with no reload.
    """
    impl, config = _config_for(definition)
    if impl not in GATED_IMPLEMENTATION_TYPES or config is None:
        return None
    current = compute_custom_tool_revision_hash(definition)
    approved = getattr(definition, "approved_revision", "") or ""
    if approved and approved == current:
        return None
    kind = "HTTP" if impl == "http" else "MCP"
    if approved:
        return (
            f"this {kind} custom tool changed since it was approved (its "
            "request target, parameters or launch command differ from the "
            "approved revision; raw on-disk edits count and leave it inert by "
            "design). Re-save it through tool_create or the tools admin UI to "
            "approve the new revision."
        )
    return (
        f"this {kind} custom tool is not approved; save it through tool_create "
        "or the tools admin UI before it can run."
    )


def definition_execution_gate_error(definition: Any) -> Optional[str]:
    """None when this definition may execute, else the refusal copy.

    The one answer to "may this custom tool run", across all four
    implementation types, so a caller that holds a whole definition does not
    have to know which of the four gate modules owns its type. ``http``/``mcp``
    are this module's own gate; ``python`` and ``workflow`` are dispatched to
    theirs. A type with no gate, or a definition missing its config, returns
    None: absence of a config is the loader's error to report, not this
    function's.
    """
    impl = str(getattr(definition, "implementation_type", "") or "")
    if impl in GATED_IMPLEMENTATION_TYPES:
        return custom_tool_execution_gate(definition)
    if impl == "python":
        from .python_custom_tools import python_execution_gate

        config = getattr(definition, "python_config", None)
        if config is None:
            return None
        return python_execution_gate(config, getattr(definition, "parameters", None) or {})
    if impl == "workflow":
        from .workflows.authoring import workflow_execution_gate

        config = getattr(definition, "workflow_config", None)
        if config is None:
            return None
        return workflow_execution_gate(config, getattr(definition, "parameters", None) or {})
    return None


def backfill_custom_tool_gate_approvals(tools_dir: Any) -> list[str]:
    """Grandfather tools created before this gate existed (exactly once).

    Without this, every existing ``http``/``mcp`` custom tool goes inert on
    upgrade. The marker file is what keeps the gate live afterwards: a
    definition that appears on disk AFTER the backfill stays unapproved, where
    a plain every-startup stamp would quietly approve planted files forever.

    The marker is therefore written FIRST, and a failure to write it aborts the
    pass. Stamping first and treating a failed marker as a warning (the shape
    this started with, inherited from the sibling) fails OPEN: with no marker
    the grandfather pass re-arms on every subsequent start, so anything planted
    between restarts is stamped as pre-existing and the control degrades to
    "approve everything at boot".
    """
    from ..tools.definitions.custom_tool_schema import CustomToolDefinition
    from .storage_paths import write_text_atomic

    logs: list[str] = []
    directory = Path(tools_dir)
    marker = directory / _BACKFILL_MARKER
    if marker.exists():
        return logs
    try:
        directory.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - see the docstring: abort, never proceed
        logs.append(
            f"Skipped custom-tool gate backfill: could not write {_BACKFILL_MARKER} "
            f"({exc}). Pre-existing http/mcp custom tools stay unapproved until "
            "they are re-saved."
        )
        return logs
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - skip unreadable, keep migrating
            logs.append(f"Skipped custom-tool gate backfill for {path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("implementation_type") or "") not in GATED_IMPLEMENTATION_TYPES:
            continue
        if data.get("approved_revision"):
            continue
        try:
            # Hash the MODEL, so the value stamped here is the one execution
            # recomputes. Parsing also skips a record the loader would
            # quarantine anyway.
            definition = CustomToolDefinition(**data)
        except Exception as exc:  # noqa: BLE001
            logs.append(f"Skipped custom-tool gate backfill for {path.name}: {exc}")
            continue
        data["approved_revision"] = compute_custom_tool_revision_hash(definition)
        data["approved_at"] = utc_now().isoformat()
        data["approved_by"] = "migration"
        try:
            # Atomic for the same reason save_definition is: `_load_tool_file`
            # QUARANTINES a file that no longer parses, so a torn write here
            # would move the prior good bytes aside rather than merely fail.
            write_text_atomic(path, json.dumps(data, indent=2, default=str))
            logs.append(
                f"Grandfathered pre-gate custom tool '{data.get('id') or path.stem}'."
            )
        except Exception as exc:  # noqa: BLE001
            logs.append(f"Failed custom-tool gate backfill for {path.name}: {exc}")
    return logs


__all__ = [
    "GATED_IMPLEMENTATION_TYPES",
    "backfill_custom_tool_gate_approvals",
    "compute_custom_tool_revision_hash",
    "custom_tool_execution_gate",
    "definition_execution_gate_error",
    "stamp_custom_tool_approval",
]
