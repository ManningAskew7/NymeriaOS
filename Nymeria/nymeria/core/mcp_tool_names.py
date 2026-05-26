"""Helpers for Nymeria's managed MCP tool identifiers.

The internal name stays provider-safe and collision-resistant. Human-facing
surfaces should use the display helpers instead of showing the raw identifier.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

MCP_TOOL_PREFIX = "mcp__"


def _field(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def format_mcp_tool_name(server_id: str, tool_name: str) -> str:
    """Return the internal Nymeria wrapper name for one MCP server tool."""

    return f"{MCP_TOOL_PREFIX}{server_id}__{tool_name}"


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
    """Build internal tool names for a server definition or lookalike."""

    discovered = tools if tools is not None else _field(server, "discovered_tools", [])
    server_id = str(_field(server, "id", ""))
    return [
        format_mcp_tool_name(server_id, str(_field(tool, "name", "")))
        for tool in discovered
        if server_id and str(_field(tool, "name", ""))
    ]


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
