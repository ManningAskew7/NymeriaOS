from types import SimpleNamespace

from nymeria.core.mcp_tool_names import (
    display_mcp_tool_names,
    format_mcp_tool_name,
    mcp_tool_display_name,
    parse_mcp_tool_name,
    registered_mcp_tool_names,
)


def test_mcp_tool_name_helpers_keep_internal_and_display_names_separate():
    internal = format_mcp_tool_name("fetch-a1b2c3", "fetch")

    assert internal == "mcp__fetch-a1b2c3__fetch"
    assert parse_mcp_tool_name(internal) == ("fetch-a1b2c3", "fetch")
    assert mcp_tool_display_name(internal, {"fetch-a1b2c3": "Fetch"}) == "Fetch / fetch"


def test_mcp_tool_name_helpers_build_server_tool_lists():
    server = SimpleNamespace(
        id="filesystem-a1b2c3",
        name="Filesystem",
        discovered_tools=[
            SimpleNamespace(name="read_file"),
            SimpleNamespace(name="write_file"),
        ],
    )

    assert registered_mcp_tool_names(server) == [
        "mcp__filesystem-a1b2c3__read_file",
        "mcp__filesystem-a1b2c3__write_file",
    ]
    assert display_mcp_tool_names(server) == [
        "Filesystem / read_file",
        "Filesystem / write_file",
    ]


def test_mcp_tool_name_helpers_accept_mapping_shapes():
    server = {
        "id": "memory-a1b2c3",
        "name": "Memory",
        "discovered_tools": [{"name": "create_entities"}],
    }

    assert registered_mcp_tool_names(server) == [
        "mcp__memory-a1b2c3__create_entities",
    ]
    assert display_mcp_tool_names(server) == [
        "Memory / create_entities",
    ]


def test_mcp_tool_display_label_falls_back_to_tool_name_without_server_label():
    assert display_mcp_tool_names({}, [{"name": "ping"}]) == ["ping"]
