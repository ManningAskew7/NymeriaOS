"""Ready-source parser for MCP server installs.

Parses ready install sources (Claude Desktop config snippet, bare stdio command,
HTTP URL, package page, or registry ID) into a concrete MCPServerDefinition
that MCPServerRegistry can save and discover.

Registry ID resolution needs a live lookup, so a resolver callable is accepted
(defaults to the real registry client). Everything else is pure.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

from ..tools.definitions.schema import MCPServerDefinition
from ..skills.marketplace import scan_for_suspicious_patterns
from .mcp_sources import (
    MCPInstallError,
    classify_mcp_source,
    describe_definition,  # re-exported for older mcp_installer callers
    new_mcp_server_id,
    package_command_source,
)

logger = logging.getLogger(__name__)

_new_id = new_mcp_server_id


def _classify(source: str) -> str:
    return classify_mcp_source(source).kind


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
    classification = classify_mcp_source(source)
    kind = classification.kind
    stripped = classification.source

    if kind == "json":
        return _parse_json_blob(stripped, name=name)
    if kind in {"http", "git", "bundle_url"}:
        return _parse_url(stripped, name=name)
    if kind in {"npm", "pypi"}:
        return _parse_stdio_command(
            package_command_source(classification),
            name=name or classification.value,
        )
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
            id=new_mcp_server_id(display_name),
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
        id=new_mcp_server_id(display_name),
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
        id=new_mcp_server_id(display_name),
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
        id=new_mcp_server_id(derived),
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
