"""Tests for hardened third-party MCP schema/name/description handling (T5).

Untrusted server input must not kill a turn: dotted/over-long tool names are
sanitized, descriptions are capped and framed as external text, and JSON-Schema
edge cases ($ref/$defs, Draft-4 boolean bounds, type-mismatched constraints)
keep structure or degrade a single field instead of dropping the whole tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from nymeria.core.mcp_servers import (
    MCP_TOOL_DESCRIPTION_MAX_CHARS,
    MCPServerRegistry,
    _format_mcp_tool_description,
)
from nymeria.tools.definitions.mcp_schema import (
    MCPDiscoveredTool,
    MCPServerDefinition,
)


def _registry(tmp_path: Path) -> MCPServerRegistry:
    return MCPServerRegistry(servers_dir=tmp_path)


def _inner_type(annotation):
    """Return the non-None member of an Optional[...] annotation."""
    if get_origin(annotation) is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


# --------------------------------------------------------------------------
# $ref / $defs resolution
# --------------------------------------------------------------------------


def test_ref_to_defs_keeps_object_structure(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {"addr": {"$ref": "#/$defs/Address"}},
        "$defs": {
            "Address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            }
        },
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    inner = _inner_type(model.model_fields["addr"].annotation)
    # Resolved to a real nested model, not degraded to dict[str, Any].
    assert isinstance(inner, type) and issubclass(inner, BaseModel)
    assert model(addr={"city": "NYC"}).addr.city == "NYC"


def test_definitions_alias_is_also_resolved(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {"pt": {"$ref": "#/definitions/Point"}},
        "definitions": {
            "Point": {"type": "object", "properties": {"x": {"type": "integer"}}}
        },
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    inner = _inner_type(model.model_fields["pt"].annotation)
    assert isinstance(inner, type) and issubclass(inner, BaseModel)


def test_cyclic_ref_terminates_and_degrades(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {"root": {"$ref": "#/$defs/Node"}},
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    # Must not hang or overflow; the recursive branch degrades to a mapping.
    model = reg._create_args_schema("mcp__s__t", schema)
    assert model(root={"next": {}}) is not None


def test_external_or_unknown_ref_degrades_without_crashing(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {
            "a": {"$ref": "https://example.com/schema.json"},
            "b": {"$ref": "#/$defs/Missing"},
        },
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    # Both unresolved refs degrade to an untyped mapping; the tool still builds.
    assert model(a={"x": 1}, b={"y": 2}) is not None


def test_malformed_ref_degrades_only_its_own_field(tmp_path: Path):
    reg = _registry(tmp_path)
    # A non-string (unhashable) $ref must not escalate the whole tool to the
    # permissive fallback; the sibling good field keeps its type.
    schema = {
        "type": "object",
        "properties": {
            "good": {"type": "string"},
            "bad": {"$ref": {"unhashable": 1}},
        },
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    # If the whole tool had degraded to extra="allow", model_fields would be
    # empty; the good field surviving proves only the bad field degraded.
    assert "good" in model.model_fields
    assert _inner_type(model.model_fields["good"].annotation) is str


# --------------------------------------------------------------------------
# Draft-4 vs Draft-6 numeric constraints
# --------------------------------------------------------------------------


def test_draft4_boolean_exclusive_minimum_is_ignored_not_fatal(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 5, "exclusiveMinimum": True}},
        "required": ["n"],
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    # Tool survives; the boolean exclusive flag is dropped, minimum stays (ge=5).
    assert model(n=5).n == 5
    with pytest.raises(ValidationError):
        model(n=4)


def test_draft6_numeric_exclusive_minimum_is_applied(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "exclusiveMinimum": 5}},
        "required": ["n"],
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    assert model(n=6).n == 6
    with pytest.raises(ValidationError):
        model(n=5)


def test_type_mismatched_constraint_is_dropped(tmp_path: Path):
    reg = _registry(tmp_path)
    # A bogus numeric bound on a string field must not reach pydantic (it would
    # raise at model creation and drop the tool).
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "minimum": 3}},
        "required": ["s"],
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    assert model(s="hi").s == "hi"


def test_string_constraints_apply_only_to_strings(tmp_path: Path):
    reg = _registry(tmp_path)
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "minLength": 2}},
        "required": ["s"],
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    assert model(s="ok").s == "ok"
    with pytest.raises(ValidationError):
        model(s="x")


def test_malformed_constraint_falls_back_without_dropping_tool(tmp_path: Path):
    reg = _registry(tmp_path)
    # An invalid regex pattern would blow up model creation; the tool must still
    # come back usable (constraints dropped) rather than vanish.
    schema = {
        "type": "object",
        "properties": {"s": {"type": "string", "pattern": "([a-z"}},
        "required": ["s"],
    }
    model = reg._create_args_schema("mcp__s__t", schema)
    assert model(s="anything at all").s == "anything at all"


def test_empty_schema_builds_empty_model(tmp_path: Path):
    reg = _registry(tmp_path)
    assert reg._create_args_schema("mcp__s__t", {}) is not None


# --------------------------------------------------------------------------
# Description framing
# --------------------------------------------------------------------------


def test_description_is_framed_as_untrusted_third_party_text():
    out = _format_mcp_tool_description("Notion", "Search your workspace docs.", "search")
    assert out == "MCP server 'Notion' describes this tool as: Search your workspace docs."


def test_description_is_capped():
    huge = "A" * (MCP_TOOL_DESCRIPTION_MAX_CHARS + 500)
    out = _format_mcp_tool_description("Srv", huge, "tool")
    assert "[truncated]" in out
    # Bounded by the cap plus the lead-in and marker, not the raw 2000+ chars.
    assert len(out) < MCP_TOOL_DESCRIPTION_MAX_CHARS + 200


def test_empty_description_names_the_server():
    out = _format_mcp_tool_description("Notion", "", "search")
    assert out == "Tool 'search' provided by Notion (no description supplied)."


def test_empty_description_caps_a_hostile_tool_name():
    # The empty-description branch interpolates the server-controlled tool name;
    # a huge name must not flood the prompt just because there is no description.
    out = _format_mcp_tool_description("Srv", "", "A" * 100000)
    assert len(out) < 500


def test_hostile_server_label_is_capped():
    out = _format_mcp_tool_description("L" * 100000, "hi", "t")
    assert len(out) < MCP_TOOL_DESCRIPTION_MAX_CHARS + 300


# --------------------------------------------------------------------------
# End-to-end tool-name sanitization via get_all_tools
# --------------------------------------------------------------------------


def _server(server_id: str, tools: list[MCPDiscoveredTool]) -> MCPServerDefinition:
    return MCPServerDefinition(
        id=server_id,
        name="Test Server",
        transport="stdio",
        server_command="npx",
        server_args=["-y", "@example/x"],
        enabled=True,
        install_status="ready",
        discovered_tools=tools,
    )


def test_dotted_tool_name_is_sanitized_when_wrapped(tmp_path: Path):
    reg = _registry(tmp_path)
    reg.save_server(
        _server(
            "srv",
            [MCPDiscoveredTool(name="search.docs", description="", input_schema={})],
        )
    )
    names = [t.name for t in reg.get_all_tools()]
    assert names == ["mcp__srv__search_docs"]


def test_colliding_sanitized_names_keep_first_and_skip_rest(tmp_path: Path):
    reg = _registry(tmp_path)
    reg.save_server(
        _server(
            "srv",
            [
                MCPDiscoveredTool(name="a.b", description="", input_schema={}),
                MCPDiscoveredTool(name="a b", description="", input_schema={}),
            ],
        )
    )
    names = [t.name for t in reg.get_all_tools()]
    # Both sanitize to mcp__srv__a_b; only the first is wrapped so the runtime
    # never dispatches the wrong raw tool name under a shared internal name.
    assert names == ["mcp__srv__a_b"]


def test_empty_tool_name_is_skipped(tmp_path: Path):
    reg = _registry(tmp_path)
    reg.save_server(
        _server(
            "srv",
            [
                MCPDiscoveredTool(name="", description="", input_schema={}),
                MCPDiscoveredTool(name="real", description="", input_schema={}),
            ],
        )
    )
    names = [t.name for t in reg.get_all_tools()]
    assert names == ["mcp__srv__real"]
