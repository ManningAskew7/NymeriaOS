"""Helpers for Nymeria's managed MCP tool identifiers.

The internal name stays provider-safe and collision-resistant. Human-facing
surfaces should use the display helpers instead of showing the raw identifier.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

MCP_TOOL_PREFIX = "mcp__"

# Provider tool-name budget. Anthropic and the OpenAI-compatible gateways accept
# function names matching ^[A-Za-z0-9_-]{1,64}$. The internal
# ``mcp__<server>__<tool>`` name is what gets bound to the model, so a raw MCP
# tool name with dots, spaces, or unicode would reach the provider as a
# turn-killing 400 BEFORE the model runs. We sanitize the tool component into
# that charset and cap the whole name; the RAW tool name is carried separately
# (on the discovered tool / MCPToolConfig) for the actual ``tools/call``.
MAX_MCP_TOOL_NAME_LEN = 64
_TOOL_COMPONENT_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_tool_component(raw: str) -> str:
    """Map a raw MCP tool name onto the provider-safe ``[A-Za-z0-9_-]`` charset.

    Every disallowed character becomes ``_``. An all-invalid name collapses to
    underscores (representable), and only a truly empty name yields ``""`` (the
    caller treats that as unrepresentable and skips the tool).
    """

    return _TOOL_COMPONENT_DISALLOWED.sub("_", str(raw))

# Leading forms that Claude OAuth classifies as third-party MCP app usage and
# rejects with a 400 "Third-party apps now draw from your extra usage" BEFORE
# the model runs. Tested table: docs/private/cliproxy.md "Claude OAuth MCP
# tool-name classifier". The DOUBLE-underscore ``mcp__`` prefix is tested-clean,
# so a tool name must start with ``mcp__`` and never ``mcp`` + a single
# separator. This guard is what keeps a future naming refactor (or an
# "mcp"-leading server id) from silently breaking every Claude-OAuth thread
# that has any MCP tool bound.
_CLIPROXY_UNSAFE_PREFIXES = ("mcp.", "mcp/", "mcp-")


def is_cliproxy_unsafe(name: str) -> bool:
    """True when *name* would trip the Claude OAuth third-party-MCP classifier."""
    value = str(name)
    if value.startswith(_CLIPROXY_UNSAFE_PREFIXES):
        return True
    # ``mcp_x`` fails but ``mcp__x`` passes: a single underscore is the hazard.
    return value.startswith("mcp_") and not value.startswith("mcp__")


def assert_cliproxy_safe(name: str) -> str:
    """Return *name*, or raise if it would trip the Claude OAuth classifier."""
    if is_cliproxy_unsafe(name):
        raise ValueError(
            f"MCP tool name {name!r} would trip the Claude OAuth third-party-MCP "
            "classifier (leading 'mcp' + single separator). It must start with "
            "'mcp__'. See docs/private/cliproxy.md."
        )
    return name


def _field(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def format_mcp_tool_name(server_id: str, tool_name: str) -> str:
    """Return the internal Nymeria wrapper name for one MCP server tool.

    Always ``mcp__<server_id>__<tool_name>`` (the tested-clean double-underscore
    namespace), with the tool component sanitized to the provider charset and
    the whole name capped at ``MAX_MCP_TOOL_NAME_LEN`` (the tool component is
    truncated, never the server segment, so ``parse_mcp_tool_name`` still
    round-trips). The result is asserted CLIProxy-safe so a future change to
    this format, or an ``mcp``-leading server id, fails loudly instead of
    silently tripping the classifier at request time.
    """

    safe_tool = sanitize_tool_component(tool_name)
    prefix = f"{MCP_TOOL_PREFIX}{server_id}__"
    name = f"{prefix}{safe_tool}"
    if len(name) > MAX_MCP_TOOL_NAME_LEN:
        budget = MAX_MCP_TOOL_NAME_LEN - len(prefix)
        if budget > 0:
            name = f"{prefix}{safe_tool[:budget]}"
        else:
            # Pathological: the server id alone exhausts the budget (a re-slug
            # concern upstream). Hard-cap the whole name so it can never reach
            # the provider over-length; parse round-trip is best-effort past
            # this point, but the name stays mcp__-prefixed and CLIProxy-safe.
            name = name[:MAX_MCP_TOOL_NAME_LEN]
    return assert_cliproxy_safe(name)


# Map an MCP server's install status onto the shared credential-axis
# vocabulary (connected / pending / needs_setup / optional) that the tool
# surfaces already render. MCP has no credential provider; its "auth" is
# really "is the server configured and reachable", so a server missing
# required config reads as needs_setup and a live one as connected. Statuses
# absent here (draft/preparing/disabled/approved) contribute no axis signal.
_MCP_INSTALL_STATUS_AUTH = {
    "ready": "connected",
    "discovering": "connected",
    "needs_config": "needs_setup",
    "failed": "needs_setup",
}


def mcp_auth_status_for_install(install_status: str) -> str | None:
    """Return the credential-axis status for an MCP server's install status."""

    return _MCP_INSTALL_STATUS_AUTH.get(str(install_status or ""))


def is_mcp_tool_name(name: str) -> bool:
    """Return true when a tool name uses Nymeria's managed MCP namespace."""

    return str(name).startswith(MCP_TOOL_PREFIX)


def parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Parse an internal MCP tool name into ``(server_id, raw_tool_name)``."""

    value = str(name)
    if not value.startswith(MCP_TOOL_PREFIX):
        return None
    body = value[len(MCP_TOOL_PREFIX):]
    server_id, sep, tool_name = body.partition("__")
    if not sep or not server_id or not tool_name:
        return None
    return server_id, tool_name


def mcp_tool_display_label(
    *,
    server_id: str,
    tool_name: str,
    server_name: str = "",
) -> str:
    """Return the label shown to users for one managed MCP tool."""

    label = server_name.strip() or server_id
    if not label:
        return tool_name
    return f"{label} / {tool_name}"


def mcp_tool_display_name(
    internal_name: str,
    server_names: Mapping[str, str] | None = None,
) -> str:
    """Return a readable label for an internal MCP tool name."""

    parsed = parse_mcp_tool_name(internal_name)
    if parsed is None:
        return internal_name
    server_id, tool_name = parsed
    server_name = (server_names or {}).get(server_id, "")
    return mcp_tool_display_label(
        server_id=server_id,
        tool_name=tool_name,
        server_name=server_name,
    )


def registered_mcp_tool_names(server: Any, tools: Iterable[Any] | None = None) -> list[str]:
    """Build internal tool names for a server definition or lookalike.

    Mirrors the wrap-loop guards in ``MCPServerRegistry.get_all_tools`` so the
    registered list matches what is actually bound: whitespace-empty raw names
    are skipped and names that sanitize to the same internal name are deduped
    (first wins), avoiding a dead UI toggle or a duplicate entry.
    """

    discovered = tools if tools is not None else _field(server, "discovered_tools", [])
    server_id = str(_field(server, "id", ""))
    names: list[str] = []
    seen: set[str] = set()
    for tool in discovered:
        raw = str(_field(tool, "name", ""))
        if not server_id or not raw.strip():
            continue
        internal = format_mcp_tool_name(server_id, raw)
        if internal in seen:
            continue
        seen.add(internal)
        names.append(internal)
    return names


def display_mcp_tool_names(server: Any, tools: Iterable[Any] | None = None) -> list[str]:
    """Build human-readable labels for a server definition or lookalike."""

    discovered = tools if tools is not None else _field(server, "discovered_tools", [])
    server_id = str(_field(server, "id", ""))
    server_name = str(_field(server, "name", "") or server_id)
    return [
        mcp_tool_display_label(
            server_id=server_id,
            server_name=server_name,
            tool_name=str(_field(tool, "name", "")),
        )
        for tool in discovered
        if str(_field(tool, "name", ""))
    ]
