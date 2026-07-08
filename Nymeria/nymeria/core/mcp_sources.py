"""Shared helpers for MCP install source text.

This module owns source-string normalization and classification only.
`mcp_installer` turns ready sources into MCPServerDefinition objects, while
`mcp_runtime` turns classified sources into previews, plans, and prepared
local runtimes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import unquote, urlparse

from ..tools.definitions.mcp_schema import MCPServerDefinition

logger = logging.getLogger(__name__)


class MCPInstallError(ValueError):
    """Raised when an MCP source string cannot be parsed, planned, or resolved."""


@dataclass(frozen=True)
class MCPSourceClassification:
    """Normalized install source and its high-level source kind."""

    kind: str
    source: str
    value: str = ""


# Matches "io.github.owner/name" style registry IDs. Conservative: no scheme,
# no whitespace, one slash separating namespace from name.
REGISTRY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9._-]+$")

# Matches a slug fragment we can safely derive from a user-visible name.
SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9]+")

SAFE_STDIO_COMMANDS = {
    "uvx",
    "uv",
    "npx",
    "npm",
    "node",
    "python",
    "python3",
    "deno",
    "bun",
}

ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _clean_mcp_slug(slug: str) -> str:
    """Drop redundant ``mcp``/``mcp-server`` boilerplate so a server named
    ``mcp-server-github`` slugs to ``github``, and never leave a bare ``mcp``.

    Readability, and defense-in-depth against a future non-``mcp__`` naming
    scheme: the tool name always keeps the tested-clean ``mcp__`` prefix (guarded
    in ``format_mcp_tool_name``), but a clean, non-``mcp``-leading id keeps that
    invariant obvious.
    """
    for prefix in ("mcp-server-", "mcp-"):
        if slug.startswith(prefix) and len(slug) > len(prefix):
            return slug[len(prefix):].strip("-")
    if slug == "mcp":
        return "server"
    return slug


def slugify_mcp_name(raw: str) -> str:
    slug = SLUG_PATTERN.sub("-", raw.strip().lower()).strip("-")
    return _clean_mcp_slug(slug) or "server"


def new_mcp_server_id(name: str, existing_ids: Optional[set[str]] = None) -> str:
    """Return a clean, collision-free server id derived from *name*.

    Drops the old ``-<uuid6>`` suffix in favor of a readable slug
    (``mcp__notion__search`` reads for the model and the user; the internal id is
    the shared, single name). Collisions are disambiguated deterministically
    (``notion``, ``notion-2``, ...). ``existing_ids`` defaults to the current
    registry so callers need not thread it through.
    """
    if existing_ids is None:
        try:
            from .mcp_servers import get_mcp_server_registry

            existing_ids = {s.id for s in get_mcp_server_registry().get_all_servers()}
        except Exception:  # noqa: BLE001 - registry unavailable at some call sites
            # Can't check collisions; fall back to a uuid-suffixed id so we never
            # silently overwrite another server's definition.
            import uuid

            return f"{slugify_mcp_name(name)}-{uuid.uuid4().hex[:6]}"
    base = slugify_mcp_name(name)
    if base not in existing_ids:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base}-{suffix}"


def extract_install_source(source: str) -> str:
    """Strip common prose/Markdown wrappers around a pasted install source."""
    s = source.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            body = "\n".join(lines[1:-1]).strip()
            return first_install_candidate(body) or body
    if not s.startswith("{") and "{" in s and "}" in s:
        start = s.find("{")
        end = s.rfind("}")
        candidate = s[start : end + 1].strip()
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            logger.debug("Candidate substring is not valid JSON")
    fence = re.search(r"```[^\n]*\n(.*?)```", s, re.S)
    if fence:
        body = fence.group(1).strip()
        return first_install_candidate(body) or body
    line_candidate = first_install_candidate(s)
    if line_candidate:
        return line_candidate
    return s


def first_install_candidate(text: str) -> Optional[str]:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):
            line = line[1:].strip()
        tokens = line.split()
        while tokens and ENV_ASSIGNMENT_PATTERN.match(tokens[0]):
            tokens.pop(0)
        first = tokens[0] if tokens else ""
        if first in SAFE_STDIO_COMMANDS:
            return line
        if line.startswith(("http://", "https://")):
            return line
        if REGISTRY_ID_PATTERN.match(line):
            return line
    return None


def classify_url_source(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    lower_path = unquote(parsed.path).lower()
    host = (parsed.hostname or "").lower()
    if lower_path.endswith((".mcpb", ".dxt", ".zip")):
        return "bundle_url", url
    if host == "www.npmjs.com" and "/package/" in lower_path:
        pkg = unquote(lower_path.split("/package/", 1)[1].strip("/"))
        return "npm", pkg
    if host == "pypi.org" and lower_path.startswith("/project/"):
        pkg = unquote(lower_path.split("/project/", 1)[1].strip("/").split("/")[0])
        return "pypi", pkg
    if host in {"github.com", "gitlab.com", "bitbucket.org"}:
        return "git", url
    if lower_path.endswith(".git"):
        return "git", url
    return "http", url


def classify_mcp_source(source: str) -> MCPSourceClassification:
    stripped = source.strip()
    if not stripped:
        raise MCPInstallError("source is empty")
    if stripped.startswith("{"):
        return MCPSourceClassification(kind="json", source=stripped, value=stripped)
    if stripped.startswith(("http://", "https://")):
        kind, value = classify_url_source(stripped)
        return MCPSourceClassification(kind=kind, source=stripped, value=value)
    if REGISTRY_ID_PATTERN.match(stripped):
        return MCPSourceClassification(kind="registry", source=stripped, value=stripped)
    return MCPSourceClassification(kind="stdio", source=stripped, value=stripped)


def package_command_source(classification: MCPSourceClassification) -> str:
    if classification.kind == "npm":
        return f"npx -y {classification.value}"
    if classification.kind == "pypi":
        return f"uvx {classification.value}"
    raise MCPInstallError(f"source kind '{classification.kind}' is not a package source")


def describe_definition(defn: MCPServerDefinition) -> str:
    """Return a human-readable summary for confirmation dialogs and chat echoes."""
    if defn.transport == "http":
        return f"HTTP MCP server '{defn.name}' at {defn.url}"
    cmd = " ".join([defn.server_command, *defn.server_args])
    return f"stdio MCP server '{defn.name}' running: {cmd}"
