"""Compact args-schema rendering for deferred tool loading.

Shared by ``tool_search(include_schemas=True)``, ``tool_invoke``'s
validation-error echo, ``Skill(defer=True)``, and ``workflow_info(show)``. The
goal is a small, model-readable rendering of a tool's CALL arguments so the
model can format a deferred call (through ``tool_invoke``) without the tool
being bound to the graph.

Fidelity matters: deferred args are validated server-side but not
grammar-constrained, so the rendering preserves enums, ``anyOf``, nested
objects, and numeric/length constraints (exactly the shapes MCP servers emit).
It reads a tool's ``tool_call_schema`` (which already excludes injected args
like ``config`` and ``tool_call_id``) and falls back to ``args_schema``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Keep the rendering bounded so a pathological schema cannot flood context.
DEFAULT_SCHEMA_MAX_CHARS = 1500


def _strip_noise(node: Any) -> Any:
    """Recursively drop ``title`` keys (pydantic noise) from a JSON-schema node.

    ``title`` is auto-generated from field names and adds tokens without
    telling the model anything the property key does not already say.
    """
    if isinstance(node, dict):
        return {k: _strip_noise(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_noise(v) for v in node]
    return node


def tool_args_json_schema(tool: Any) -> Optional[dict]:
    """Return a tool's call-args JSON schema dict, or None if unavailable.

    Prefers ``tool_call_schema`` (injected args already removed) and falls back
    to ``args_schema``. Best-effort: any failure returns None rather than raise,
    since schema rendering is always an optional enrichment.
    """
    model = getattr(tool, "tool_call_schema", None) or getattr(tool, "args_schema", None)
    if model is None:
        return None
    try:
        return model.model_json_schema()
    except Exception:  # noqa: BLE001 - rendering is best-effort enrichment
        logger.debug("Failed to derive JSON schema for tool %r", getattr(tool, "name", tool))
        return None


def render_tool_args_schema(
    tool: Any, *, max_chars: int = DEFAULT_SCHEMA_MAX_CHARS
) -> str:
    """Render a tool's call arguments as compact, model-readable JSON.

    Returns ``"(no arguments)"`` when the tool takes no non-injected args, or
    ``""`` when a schema cannot be derived at all (caller decides whether to
    omit the line). Output is minified JSON of ``{properties, required, $defs}``
    with ``title`` noise stripped, capped at ``max_chars`` with a truncation
    marker so it is safe to inline into a tool result.
    """
    js = tool_args_json_schema(tool)
    if js is None:
        return ""
    props = js.get("properties") or {}
    defs = js.get("$defs") or js.get("definitions") or {}
    if not props and not defs:
        return "(no arguments)"

    compact: dict[str, Any] = {"properties": _strip_noise(props)}
    required = js.get("required")
    if required:
        compact["required"] = required
    if defs:
        compact["$defs"] = _strip_noise(defs)

    text = json.dumps(compact, separators=(",", ":"), default=str)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text
