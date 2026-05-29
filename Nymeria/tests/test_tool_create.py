"""Tests for the agent-facing tool_create workflow."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nymeria.core.custom_tools import CustomToolLoader
from nymeria.core.python_custom_tools import (
    run_python_tool_subprocess,
    validate_python_tool_static,
)
from nymeria.tools import ALL_TOOLS, OPTIONAL_TOOLS
from nymeria.tools.definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    PythonToolConfig,
    ToolParameter,
)
from nymeria.tools.metadata import SecurityLevel, ToolCategory, clear_custom_tool_metadata, get_all_tool_metadata
from nymeria.tools.tool_create import (
    ToolDraftStore,
    _publish_draft,
    _user_is_admin,
    create_draft_definition,
    test_draft as run_draft_test,
    tool_create as tool_create_tool,
)


def _fake_agent_with_role(role: str):
    user = SimpleNamespace(id="u1", role=role, disabled=False)
    repo = SimpleNamespace(
        get_user_by_id=lambda uid: user if uid == "u1" else None,
    )
    return SimpleNamespace(accounts_repo=repo)


def test_user_is_admin_resolves_role_fails_closed():
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("admin")):
        assert _user_is_admin("u1") is True
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("user")):
        assert _user_is_admin("u1") is False
    # Unresolvable agent / account -> fail closed (deny Python path).
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        assert _user_is_admin("u1") is False
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("admin")):
        assert _user_is_admin("ghost") is False


def test_python_draft_denied_for_non_admin():
    # H-3 regression: a non-admin user must not be able to create a Python
    # custom tool via the agent path (it runs in-process with full secrets).
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("user")):
        result = asyncio.run(
            tool_create_tool.coroutine(
                action="draft",
                implementation_type="python",
                tool_id="evil_tool",
                name="Evil",
                description="reads os.environ",
                python_code="def run():\n    import os\n    return dict(os.environ)\n",
                tool_call_id="call-1",
                config={"configurable": {"user_id": "u1", "thread_id": "t1"}},
            )
        )
    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "admin_required"


def test_http_draft_allowed_for_non_admin(tmp_path):
    # Minimal-UX guarantee: HTTP custom tools remain available to non-admins;
    # only the Python (in-process code execution) path is gated.
    store = ToolDraftStore(tmp_path / "drafts")
    with patch("nymeria.tools.tool_create._draft_store", return_value=store), \
         patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("user")):
        result = asyncio.run(
            tool_create_tool.coroutine(
                action="draft",
                implementation_type="http",
                tool_id="price_lookup",
                name="Price Lookup",
                description="Fetch a price",
                http_config=_http_config(),
                tool_call_id="call-1",
                config={"configurable": {"user_id": "u1", "thread_id": "t1"}},
            )
        )
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["action"] == "draft"


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


def test_python_tool_publish_can_validate_with_sample_params(tmp_path, monkeypatch):
    loader = CustomToolLoader(tmp_path / "custom_tools")
    tool_create_module = importlib.import_module("nymeria.tools.tool_create")
    monkeypatch.setattr(tool_create_module, "get_custom_tool_loader", lambda: loader)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: None)

    store = ToolDraftStore(tmp_path / "drafts")
    draft = create_draft_definition(
        user_id="user-1",
        tool_id="python_publish_echo",
        name="Python Publish Echo",
        description="Echo text during publish validation",
        parameters={
            "value": {
                "type": "string",
                "description": "Value to echo",
                "required": True,
            }
        },
        http_config=None,
        implementation_type="python",
        python_code=(
            "def run(value: str) -> str:\n"
            "    return value.upper()\n"
        ),
    )
    store.save("user-1", draft)

    result = _publish_draft(
        store=store,
        user_id="user-1",
        draft_id="python_publish_echo",
        thread_id="thread-1",
        ttl="2h",
        tool_call_id="call-1",
        sample_params={"value": "nymeria"},
        validation_timeout_seconds=10,
    )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["published"] is True
    assert payload["tool"]["implementation_type"] == "python"
    assert loader.get_definition("python_publish_echo") is not None
    saved = store.get("user-1", "python_publish_echo")
    assert saved is not None
    assert saved.last_test_ok is True


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
    assert meta.default_enabled is False

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


def test_python_tool_static_validation_rejects_top_level_calls():
    config = PythonToolConfig(
        source_code=(
            "print('side effect')\n\n"
            "def run(value: str) -> str:\n"
            "    return value\n"
        )
    )

    errors = validate_python_tool_static(
        config=config,
        parameters={
            "value": ToolParameter(
                type="string",
                description="Value to echo",
                required=True,
            )
        },
    )

    assert any("top-level Expr" in error for error in errors)


def test_python_tool_subprocess_executes_successfully():
    config = PythonToolConfig(
        source_code=(
            "def run(name: str) -> str:\n"
            "    return f'hello {name}'\n"
        )
    )

    result = run_python_tool_subprocess(
        tool_id="hello_python",
        config=config,
        params={"name": "Ada"},
        timeout_seconds=10,
    )

    assert result.ok is True
    assert result.result == "hello Ada"


def test_python_tool_subprocess_contains_process_exit():
    config = PythonToolConfig(
        source_code=(
            "def run() -> str:\n"
            "    import os\n"
            "    os._exit(7)\n"
        )
    )

    result = run_python_tool_subprocess(
        tool_id="crashy_python",
        config=config,
        params={},
        timeout_seconds=10,
    )

    assert result.ok is False
    assert result.error_type == "process_exit"
    assert "7" in result.error_message


def test_custom_tool_loader_registers_python_wrapper_without_importing_user_code(tmp_path):
    loader = CustomToolLoader(tmp_path / "custom_tools")
    definition = CustomToolDefinition(
        id="python_echo",
        name="Python Echo",
        description="Echo a value through a subprocess-backed Python tool",
        parameters={
            "value": ToolParameter(
                type="string",
                description="Value to echo",
                required=True,
            )
        },
        implementation_type="python",
        python_config=PythonToolConfig(
            source_code=(
                "def run(value: str) -> str:\n"
                "    return value.upper()\n"
            )
        ),
        enabled=True,
    )

    loader.save_definition(definition)
    tool_obj = loader._tools["python_echo"]

    assert tool_obj.invoke({"value": "nymeria"}) == "NYMERIA"
