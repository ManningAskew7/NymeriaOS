"""Paste-friendly MCP server installer.

Parses whatever the user hands us (Claude Desktop config snippet, bare stdio
command, HTTP URL, or registry ID) into a concrete MCPServerDefinition that
MCPServerRegistry can save and discover.

Registry ID resolution needs a live lookup, so a resolver callable is accepted
(defaults to the real registry client). Everything else is pure.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import uuid
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from ..tools.definitions.schema import MCPServerDefinition
from ..skills.marketplace import scan_for_suspicious_patterns

logger = logging.getLogger(__name__)

# Matches "io.github.owner/name" style registry IDs. Conservative — no scheme,
# no whitespace, one slash separating namespace from name.
_REGISTRY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9._-]+$")

# Matches a slug fragment we can safely derive from a user-visible name.
_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9]+")


class MCPInstallError(ValueError):
    """Raised when an MCP source string cannot be parsed or resolved."""


def _slugify(raw: str) -> str:
    slug = _SLUG_PATTERN.sub("-", raw.strip().lower()).strip("-")
    return slug or "mcp"


def _new_id(name: str) -> str:
    return f"{_slugify(name)}-{uuid.uuid4().hex[:6]}"


def _classify(source: str) -> str:
    stripped = source.strip()
    if not stripped:
        raise MCPInstallError("source is empty")
    if stripped.startswith("{"):
        return "json"
    if stripped.startswith(("http://", "https://")):
        return "url"
    if _REGISTRY_ID_PATTERN.match(stripped):
        return "registry"
    return "stdio"


def parse_mcp_source(
    source: str,
    *,
    name: Optional[str] = None,
    resolver: Optional[Callable[[str], MCPServerDefinition]] = None,
) -> MCPServerDefinition:
    """Parse a user-provided MCP install source into a server definition.

    Accepted forms:
      1. Claude-Desktop-style JSON: `{"mcpServers": {"fs": {"command": "npx", "args": [...]}}}`
      2. Bare stdio command string: `npx -y @modelcontextprotocol/server-filesystem /tmp`
      3. HTTP/SSE URL: `http://localhost:8811/mcp`
      4. Registry ID (namespace/name): `io.github.modelcontextprotocol/server-filesystem`

    The optional `resolver` is called for registry IDs and must return a ready
    MCPServerDefinition. When omitted, the default MCP registry client is used.
    """
    kind = _classify(source)
    stripped = source.strip()

    if kind == "json":
        return _parse_json_blob(stripped, name=name)
    if kind == "url":
        return _parse_url(stripped, name=name)
    if kind == "registry":
        if resolver is None:
            from .mcp_registry_client import resolve_registry_id
            resolver = resolve_registry_id
        return resolver(stripped)
    # stdio
    return _parse_stdio_command(stripped, name=name)


def _parse_json_blob(blob: str, *, name: Optional[str]) -> MCPServerDefinition:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        raise MCPInstallError(f"not valid JSON: {e}") from e

    # Accept either `{"mcpServers": {"<key>": {...}}}` or a single server entry.
    servers: Dict[str, Dict] = data.get("mcpServers") if isinstance(data, dict) else None
    if servers is None and isinstance(data, dict) and ("command" in data or "url" in data):
        servers = {name or "pasted": data}
    if not servers or not isinstance(servers, dict):
        raise MCPInstallError(
            "expected {\"mcpServers\": {...}} or a single-server config object"
        )
    if len(servers) > 1:
        raise MCPInstallError(
            f"JSON blob contains {len(servers)} servers; paste one at a time"
        )

    server_name, entry = next(iter(servers.items()))
    if not isinstance(entry, dict):
        raise MCPInstallError(f"server '{server_name}' is not an object")

    display_name = name or server_name
    env_vars = entry.get("env") or {}

    # HTTP entry
    if "url" in entry:
        url = entry["url"]
        if not isinstance(url, str) or not url:
            raise MCPInstallError("'url' must be a non-empty string")
        return MCPServerDefinition(
            id=_new_id(display_name),
            name=display_name,
            description=entry.get("description", ""),
            transport="http",
            url=url,
            headers=entry.get("headers", {}) or {},
            env_vars=env_vars,
        )

    # Stdio entry
    command = entry.get("command")
    if not command or not isinstance(command, str):
        raise MCPInstallError("stdio server entry requires a 'command' string")
    args = entry.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise MCPInstallError("'args' must be a list of strings")

    _reject_suspicious(command, args)

    return MCPServerDefinition(
        id=_new_id(display_name),
        name=display_name,
        description=entry.get("description", ""),
        transport="stdio",
        server_command=command,
        server_args=args,
        env_vars=env_vars,
    )


def _parse_url(url: str, *, name: Optional[str]) -> MCPServerDefinition:
    parsed = urlparse(url)
    if not parsed.netloc:
        raise MCPInstallError(f"URL has no host: {url}")
    display_name = name or parsed.hostname or "mcp-http"
    return MCPServerDefinition(
        id=_new_id(display_name),
        name=display_name,
        transport="http",
        url=url,
    )


def _parse_stdio_command(command_str: str, *, name: Optional[str]) -> MCPServerDefinition:
    try:
        parts = shlex.split(command_str)
    except ValueError as e:
        raise MCPInstallError(f"could not parse command: {e}") from e
    if not parts:
        raise MCPInstallError("empty command string")

    command, args = parts[0], parts[1:]
    _reject_suspicious(command, args)

    # Derive a display name from the package-ish last token when we can.
    derived = name
    if derived is None:
        derived = next((a for a in reversed(args) if not a.startswith("-")), command)
    return MCPServerDefinition(
        id=_new_id(derived),
        name=derived,
        transport="stdio",
        server_command=command,
        server_args=args,
    )


def _reject_suspicious(command: str, args) -> None:
    joined = " ".join([command, *args])
    hits = scan_for_suspicious_patterns(joined)
    if hits:
        raise MCPInstallError(
            f"refusing to install: command contains suspicious patterns ({', '.join(hits)})"
        )


def describe_definition(defn: MCPServerDefinition) -> str:
    """Return a human-readable summary for confirmation dialogs / chat echoes."""
    if defn.transport == "http":
        return f"HTTP MCP server '{defn.name}' at {defn.url}"
    cmd = " ".join([defn.server_command, *defn.server_args])
    return f"stdio MCP server '{defn.name}' running: {cmd}"
