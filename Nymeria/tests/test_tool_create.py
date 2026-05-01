"""Tests for the agent-facing tool_create workflow."""

from __future__ import annotations

import asyncio
import json

import pytest

from nymeria.core.custom_tools import CustomToolLoader
from nymeria.tools import ALL_TOOLS, OPTIONAL_TOOLS
from nymeria.tools.definitions.schema import CustomToolDefinition, HTTPToolConfig, ToolParameter
from nymeria.tools.metadata import SecurityLevel, ToolCategory, clear_custom_tool_metadata, get_all_tool_metadata
from nymeria.tools.tool_create import (
    ToolDraftStore,
    _publish_draft,
    create_draft_definition,
    test_draft as run_draft_test,
)


def _http_config(**overrides):
    config = {
        "method": "GET",
        "url": "https://api.example.com/prices/${symbol}",
        "response_format": "json",
    }
    config.update(overrides)
    return config


def test_tool_create_rejects_inline_secret_headers():
    with pytest.raises(ValueError, match="Authorization"):
        create_draft_definition(
            user_id="user-1",
            tool_id="crypto_price",
            name="Crypto Price",
            description="Fetch a cryptocurrency spot price",
            parameters={
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol",
                    "required": True,
                }
            },
            http_config=_http_config(headers={"Authorization": "Bearer ${api_key}"}),
        )


def test_tool_create_rejects_env_secret_references():
    with pytest.raises(ValueError, match="env secret reference"):
        create_draft_definition(
            user_id="user-1",
            tool_id="crypto_price",
            name="Crypto Price",
            description="Fetch a cryptocurrency spot price",
            parameters={},
            http_config=_http_config(url="https://api.example.com/${env:API_TOKEN}"),
        )


def test_tool_create_draft_and_failed_test_are_persisted(tmp_path):
    store = ToolDraftStore(tmp_path / "drafts")
    draft = create_draft_definition(
        user_id="user-1",
        tool_id="blocked_probe",
        name="Blocked Probe",
        description="Probe a blocked URL for test coverage",
        parameters={},
        http_config=_http_config(url="http://127.0.0.1:8000/admin", response_format="auto"),
    )
    store.save("user-1", draft)

    result = asyncio.run(run_draft_test(store, "user-1", "blocked_probe", {}))

    assert result["ok"] is False
    assert "blocked_network_target" in result["response_preview"]
    saved = store.get("user-1", "blocked_probe")
    assert saved is not None
    assert saved.last_test_ok is False
    assert saved.last_test_error is not None


def test_tool_create_publish_requires_successful_test(tmp_path):
    store = ToolDraftStore(tmp_path / "drafts")
    draft = create_draft_definition(
        user_id="user-1",
        tool_id="untested_tool",
        name="Untested Tool",
        description="Should not publish before test success",
        parameters={},
        http_config=_http_config(),
    )
    store.save("user-1", draft)

    result = _publish_draft(
        store=store,
        user_id="user-1",
        draft_id="untested_tool",
        thread_id="thread-1",
        ttl="2h",
        tool_call_id="call-1",
    )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "untested_draft"


def test_custom_tool_loader_registers_search_metadata(tmp_path):
    clear_custom_tool_metadata()
    loader = CustomToolLoader(tmp_path / "custom_tools")
    definition = CustomToolDefinition(
        id="price_lookup",
        name="Price Lookup",
        description="Look up a public market price",
        parameters={
            "symbol": ToolParameter(
                type="string",
                description="Ticker symbol",
                required=True,
            )
        },
        implementation_type="http",
        http_config=HTTPToolConfig(
            method="GET",
            url="https://api.example.com/prices/${symbol}",
        ),
        enabled=True,
    )

    loader.save_definition(definition)

    meta = get_all_tool_metadata("price_lookup")
    assert meta is not None
    assert meta.category == ToolCategory.CUSTOM
    assert meta.security_level == SecurityLevel.MODERATE
    assert meta.description == "Look up a public market price"

    assert loader.delete_definition("price_lookup") is True
    assert get_all_tool_metadata("price_lookup") is None


def test_tool_create_is_optional_with_metadata():
    core_names = {tool.name for tool in ALL_TOOLS}

    assert "tool_create" not in core_names
    assert "tool_create" in OPTIONAL_TOOLS
    meta = get_all_tool_metadata("tool_create")
    assert meta is not None
    assert meta.category == ToolCategory.CUSTOM
    assert meta.security_level == SecurityLevel.MODERATE
    assert meta.default_enabled is False
