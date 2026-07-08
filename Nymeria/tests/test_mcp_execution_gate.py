"""Tests for the MCP execution-trust gate (core/mcp_execution_gate.py) and its
wiring into the registry: save_server stamping, get_all_tools filtering, the
runtime re-check in the wrapped tool, and the file-tool denylist."""

from __future__ import annotations

from pathlib import Path

from nymeria.config import get_settings
from nymeria.core import mcp_servers as mcp_servers_module
from nymeria.core.mcp_execution_gate import (
    compute_mcp_revision_hash,
    mcp_execution_gate,
    stamp_mcp_approval,
)
from nymeria.core.mcp_servers import MCPServerRegistry
from nymeria.tools.definitions.mcp_schema import (
    MCPDiscoveredTool,
    MCPServerDefinition,
)
from nymeria.tools.filesystem import secrets_path_error


def _ready_stdio_defn(server_id: str = "demo") -> MCPServerDefinition:
    return MCPServerDefinition(
        id=server_id,
        name="Demo",
        transport="stdio",
        server_command="npx",
        server_args=["-y", "@example/demo"],
        enabled=True,
        install_status="ready",
        discovered_tools=[MCPDiscoveredTool(name="do_thing", description="", input_schema={})],
    )


# ---- hash / gate unit behavior ----


def test_hash_is_stable_and_launch_sensitive():
    a = _ready_stdio_defn()
    b = _ready_stdio_defn()
    assert compute_mcp_revision_hash(a) == compute_mcp_revision_hash(b)

    b.server_command = "uvx"
    assert compute_mcp_revision_hash(a) != compute_mcp_revision_hash(b)


def test_hash_ignores_credentials_so_vaulting_does_not_invalidate():
    a = _ready_stdio_defn()
    before = compute_mcp_revision_hash(a)
    a.env_vars = {"API_KEY": "${credential:demo:api_key}"}
    a.headers = {"Authorization": "Bearer x"}
    assert compute_mcp_revision_hash(a) == before


def test_gate_rejects_unapproved_then_passes_after_stamp():
    defn = _ready_stdio_defn()
    reason = mcp_execution_gate(defn)
    assert reason and "not approved" in reason

    stamp_mcp_approval(defn)
    assert mcp_execution_gate(defn) is None


def test_gate_rejects_launch_edit_after_approval():
    defn = _ready_stdio_defn()
    stamp_mcp_approval(defn)
    assert mcp_execution_gate(defn) is None

    defn.server_command = "malware"  # raw edit that never re-stamps
    reason = mcp_execution_gate(defn)
    assert reason and "changed since" in reason


# ---- registry wiring ----


def test_save_server_stamps_and_get_all_tools_wraps(tmp_path: Path):
    registry = MCPServerRegistry(servers_dir=tmp_path)
    registry.save_server(_ready_stdio_defn())

    stored = registry.get_server("demo")
    assert stored.approved_revision == compute_mcp_revision_hash(stored)

    tools = registry.get_all_tools()
    assert [t.name for t in tools] == ["mcp__demo__do_thing"]


def test_get_all_tools_filters_unapproved_planted_server(tmp_path: Path):
    registry = MCPServerRegistry(servers_dir=tmp_path)
    # Inject a ready+enabled server WITHOUT going through save_server, as a
    # hot-loaded / disk-planted definition would appear.
    planted = _ready_stdio_defn("planted")
    registry._definitions[planted.id] = planted

    assert registry.get_all_tools() == []


def test_runtime_gate_blocks_tool_edited_after_wrap(tmp_path: Path, monkeypatch):
    calls: list = []

    class _FakeManager:
        def call_tool_sync(self, config, kwargs):
            calls.append((config.server_id, kwargs))
            return "OK"

        async def call_tool(self, config, kwargs):  # pragma: no cover - unused here
            calls.append((config.server_id, kwargs))
            return "OK"

    registry = MCPServerRegistry(servers_dir=tmp_path)
    monkeypatch.setattr(mcp_servers_module, "get_mcp_manager", lambda: _FakeManager())
    monkeypatch.setattr(mcp_servers_module, "get_mcp_server_registry", lambda: registry)

    registry.save_server(_ready_stdio_defn())
    tool = registry.get_all_tools()[0]

    # Approved at wrap time: dispatch reaches the manager.
    assert tool.func() == "OK"
    assert calls == [("demo", {})]

    # Simulate a post-wrap raw edit of the live definition's launch surface.
    registry._definitions["demo"].server_command = "malware"
    result = tool.func()
    assert result.startswith("[Error]:") and "changed since" in result
    # Manager was NOT invoked a second time.
    assert len(calls) == 1


# ---- startup backfill (grandfathering pre-gate servers) ----


def test_compute_hash_accepts_dict_and_object():
    defn = _ready_stdio_defn()
    as_dict = defn.model_dump(mode="json")
    assert compute_mcp_revision_hash(defn) == compute_mcp_revision_hash(as_dict)


def test_backfill_grandfathers_existing_then_is_one_shot(tmp_path: Path):
    from nymeria.core.mcp_execution_gate import backfill_mcp_gate_approvals

    # A legacy server persisted before the gate: ready, empty approved_revision.
    legacy = _ready_stdio_defn("legacy-a")
    assert legacy.approved_revision == ""
    (tmp_path / "legacy-a.json").write_text(legacy.model_dump_json(indent=2), encoding="utf-8")

    logs = backfill_mcp_gate_approvals(tmp_path)
    assert any("legacy-a" in line for line in logs)

    reg = MCPServerRegistry(servers_dir=tmp_path)
    assert [t.name for t in reg.get_all_tools()] == ["mcp__legacy-a__do_thing"]

    # A server planted AFTER the marker must NOT be grandfathered on re-run.
    planted = _ready_stdio_defn("planted-b")
    (tmp_path / "planted-b.json").write_text(planted.model_dump_json(indent=2), encoding="utf-8")
    assert backfill_mcp_gate_approvals(tmp_path) == []  # marker present -> no-op

    reg2 = MCPServerRegistry(servers_dir=tmp_path)
    assert [t.name for t in reg2.get_all_tools()] == ["mcp__legacy-a__do_thing"]


# ---- file-tool denylist ----


def test_file_tools_refuse_mcp_servers_dir():
    data_dir = get_settings().data_dir.resolve()
    target = data_dir / "mcp_servers" / "evil.json"
    err = secrets_path_error(target)
    assert err is not None and "mcp_servers" in err
