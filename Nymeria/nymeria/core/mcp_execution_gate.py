"""Execution-time trust gate for managed MCP servers.

MCP server definitions live as raw JSON under ``data/mcp_servers/`` and are
hot-loaded on external edit (the resource-filesystem surface). Without an
execution-time check, a definition that appeared on disk without going through
Nymeria's admin-gated install path could launch an arbitrary command. This
module closes that gap the same way ``core/python_custom_tools.py`` closed it
for subprocess-backed Python tools (backlog #75 Gap 1) and the workflow gate
did for agent-authored workflows.

The mechanism: every persist through ``MCPServerRegistry.save_server`` stamps
``approved_revision`` with a content hash over the *launch surface* (the fields
that decide what process runs). At execution and at tool-wrap time the hash is
recomputed from the live definition and compared; a raw on-disk edit (which
never calls ``save_server``) leaves the hash stale and the server inert until
it is re-installed through the sanctioned path.

Scope, stated honestly (mirrors the Python gate's own note): the hash lives in
the same file it protects and uses a plain digest, so a writer who replicates
this canonical form could forge a matching approval. The hard boundary is the
file-tool denylist (``tools/filesystem.py``) plus the stdio launch allowlist;
this gate makes naive raw edits inert and keeps parity with the other
managed-code surfaces. A ``bash_execute``-capable caller already has code
execution regardless.

The hash covers the *launch command* surface only: ``transport``/
``server_command``/``server_args``/``url``/``working_directory``. It excludes
``env_vars``/``headers`` for two reasons: credential vaulting rewrites those
into ``${credential:}`` refs after install (hashing them would invalidate a
freshly-approved server), and the ``migrate_mcp_encrypted_env_vars`` startup
migration rewrites ``env_vars`` with a direct file write that never re-stamps.
Consequence, stated so the guarantee is not overclaimed: an edit that injects
an env-based launch hijack (``LD_PRELOAD``, ``NODE_OPTIONS``, ``BASH_ENV``,
``PYTHONSTARTUP``) WITHOUT touching the command is NOT made inert by this hash.
That vector is covered instead by the file-tool denylist (``mcp_servers/`` is
refused by ``secrets_path_error``); the residual is the same ``bash_execute`` /
external-edit path that already implies code execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .time_utils import utc_now

logger = logging.getLogger(__name__)

# One-shot marker (a non-JSON file, so it is never loaded as a definition) that
# records the pre-gate grandfathering has run. See backfill_mcp_gate_approvals.
_BACKFILL_MARKER = ".gate_backfill.done"


def _launch_field(defn: Any, name: str) -> Any:
    """Read a launch field from either a definition object or a raw dict."""
    if isinstance(defn, Mapping):
        return defn.get(name)
    return getattr(defn, name, None)


def compute_mcp_revision_hash(defn: Any) -> str:
    """Canonical content hash over an MCP server's launch surface.

    Accepts either an ``MCPServerDefinition`` or the raw JSON dict (the startup
    backfill hashes the on-disk dict directly).
    """
    canonical = json.dumps(
        {
            "transport": _launch_field(defn, "transport") or "",
            "server_command": _launch_field(defn, "server_command") or "",
            "server_args": [str(a) for a in (_launch_field(defn, "server_args") or [])],
            "url": _launch_field(defn, "url") or "",
            "working_directory": _launch_field(defn, "working_directory") or "",
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamp_mcp_approval(defn: Any, *, approved_by: str = "system") -> Any:
    """Approve the CURRENT launch revision (recomputed), mutating ``defn``.

    Called from ``save_server`` so any definition persisted through Nymeria's
    (admin-gated) code paths is approved, while a raw-disk/hot-loaded file that
    never reaches ``save_server`` keeps whatever approval it shipped with.
    """
    defn.approved_revision = compute_mcp_revision_hash(defn)
    defn.approved_at = utc_now()
    if not getattr(defn, "approved_by", ""):
        defn.approved_by = approved_by
    return defn


def mcp_execution_gate(defn: Any) -> Optional[str]:
    """None when this server's tools may run, else the refusal copy (fail closed).

    Recomputes the launch hash from the live definition so a stale stored
    ``approved_revision`` (edited launch fields) can never satisfy the gate.
    """
    current = compute_mcp_revision_hash(defn)
    approved = getattr(defn, "approved_revision", "") or ""
    if approved and approved == current:
        return None
    if approved:
        return (
            "this MCP server changed since it was approved (its launch command "
            "or URL differs from the approved revision; raw on-disk edits count "
            "and leave it inert by design). Re-run the install via manage_mcp or "
            "the MCP admin UI to approve the new revision."
        )
    return (
        "this MCP server is not approved; install it via manage_mcp or the MCP "
        "admin UI before its tools can run."
    )


def backfill_mcp_gate_approvals(mcp_servers_dir: Any) -> list[str]:
    """Grandfather servers installed before the execution gate existed (once).

    A server persisted before this feature has an empty ``approved_revision``
    and would otherwise be filtered as unapproved, silently hiding its tools on
    upgrade. This stamps the set present at the upgrade boundary exactly once,
    guarded by a marker file: a definition that appears on disk AFTER the
    marker is written stays inert, so the gate remains live across restarts (a
    plain every-startup stamp would defeat it). Direct dict writes, so it does
    not depend on the registry being up; runs at startup beside the other MCP
    migrations. Env fields are irrelevant to the launch hash, so ordering vs
    the encrypted-env migration does not matter.
    """
    logs: list[str] = []
    servers_dir = Path(mcp_servers_dir)
    marker = servers_dir / _BACKFILL_MARKER
    if marker.exists():
        return logs
    servers_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(servers_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - skip unreadable, keep migrating
            logs.append(f"Skipped MCP gate backfill for {path.name}: {exc}")
            continue
        if not isinstance(data, dict) or data.get("approved_revision"):
            continue
        if data.get("install_status") not in {"ready", "discovering"}:
            continue
        data["approved_revision"] = compute_mcp_revision_hash(data)
        data["approved_at"] = utc_now().isoformat()
        if not data.get("approved_by"):
            data["approved_by"] = "migration"
        try:
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            logs.append(
                f"Grandfathered pre-gate MCP server '{data.get('id') or path.stem}'."
            )
        except Exception as exc:  # noqa: BLE001
            logs.append(f"Failed MCP gate backfill for {path.name}: {exc}")
    try:
        marker.write_text("", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"Failed to write MCP gate backfill marker: {exc}")
    return logs
