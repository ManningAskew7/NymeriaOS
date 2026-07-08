"""Tests for the clean MCP server-id slug scheme (T3): boilerplate stripping,
collision-safe id allocation, and the one-shot re-slug migration."""

from __future__ import annotations

from pathlib import Path

from nymeria.core import mcp_servers as mcp_servers_module
from nymeria.core.mcp_servers import MCPServerRegistry, reslug_legacy_mcp_server_ids
from nymeria.core.mcp_sources import new_mcp_server_id, slugify_mcp_name
from nymeria.tools.definitions.mcp_schema import (
    MCPDiscoveredTool,
    MCPServerDefinition,
)


def test_slugify_strips_mcp_boilerplate_and_never_bare_mcp():
    assert slugify_mcp_name("mcp-server-github") == "github"
    assert slugify_mcp_name("MCP Obsidian") == "obsidian"
    assert slugify_mcp_name("mcp") == "server"
    assert slugify_mcp_name("Notion") == "notion"
    assert slugify_mcp_name("") == "server"
    # 'mcp' not followed by a separator is part of the name, left intact.
    assert slugify_mcp_name("mcpfoo") == "mcpfoo"


def test_new_mcp_server_id_is_clean_and_collision_free():
    assert new_mcp_server_id("Notion", existing_ids=set()) == "notion"
    assert new_mcp_server_id("Notion", existing_ids={"notion"}) == "notion-2"
    assert new_mcp_server_id("Notion", existing_ids={"notion", "notion-2"}) == "notion-3"
    assert new_mcp_server_id("mcp-server-github", existing_ids=set()) == "github"


def _legacy_server(server_id: str, name: str) -> MCPServerDefinition:
    return MCPServerDefinition(
        id=server_id,
        name=name,
        transport="stdio",
        server_command="npx",
        server_args=["-y", "@example/x"],
        enabled=True,
        install_status="ready",
        discovered_tools=[MCPDiscoveredTool(name="search", description="", input_schema={})],
    )


def test_reslug_migration_renames_and_is_one_shot(tmp_path: Path, monkeypatch):
    reg = MCPServerRegistry(servers_dir=tmp_path)
    monkeypatch.setattr(mcp_servers_module, "get_mcp_server_registry", lambda: reg)
    reg.save_server(_legacy_server("notion-a1b2c3", "Notion"))

    logs = reslug_legacy_mcp_server_ids()
    assert any("notion-a1b2c3" in line and "notion" in line for line in logs)

    assert reg.get_server("notion-a1b2c3") is None
    renamed = reg.get_server("notion")
    assert renamed is not None
    assert renamed.registered_tool_names == ["mcp__notion__search"]
    assert (tmp_path / "notion.json").exists()
    assert not (tmp_path / "notion-a1b2c3.json").exists()

    # The re-slugged server is still approved (save_server re-stamped it).
    assert [t.name for t in reg.get_all_tools()] == ["mcp__notion__search"]

    # One-shot: a second run does nothing.
    assert reslug_legacy_mcp_server_ids() == []


def test_reslug_disambiguates_same_named_servers(tmp_path: Path, monkeypatch):
    reg = MCPServerRegistry(servers_dir=tmp_path)
    monkeypatch.setattr(mcp_servers_module, "get_mcp_server_registry", lambda: reg)
    reg.save_server(_legacy_server("notion-aaa111", "Notion"))
    reg.save_server(_legacy_server("notion-bbb222", "Notion"))

    reslug_legacy_mcp_server_ids()

    ids = {s.id for s in reg.get_all_servers()}
    assert ids == {"notion", "notion-2"}


def test_reslug_rerun_after_lost_marker_does_not_delete(tmp_path: Path, monkeypatch):
    reg = MCPServerRegistry(servers_dir=tmp_path)
    monkeypatch.setattr(mcp_servers_module, "get_mcp_server_registry", lambda: reg)
    reg.save_server(_legacy_server("notion-aaa111", "Notion"))
    reg.save_server(_legacy_server("notion-bbb222", "Notion"))

    reslug_legacy_mcp_server_ids()
    assert {s.id for s in reg.get_all_servers()} == {"notion", "notion-2"}

    # Marker lost (read-only FS, host move that kept *.json but dropped the
    # dotfile); a re-run must be a no-op, not delete the 'notion-2' server.
    (tmp_path / ".reslug.done").unlink()
    reslug_legacy_mcp_server_ids()
    assert {s.id for s in reg.get_all_servers()} == {"notion", "notion-2"}
    assert (tmp_path / "notion-2.json").exists()
