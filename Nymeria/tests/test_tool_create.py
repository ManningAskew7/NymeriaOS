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
    approve_python_revision,
    config_python_revision_hash,
    python_execution_gate,
    run_python_tool_subprocess,
    validate_python_tool_static,
)
from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS
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
        # Subprocess validation runs the drafted tool in a fresh interpreter,
        # which takes several seconds even idle; keep a generous budget so the
        # test stays green under CPU contention (e.g. parallel `pytest -n` runs).
        validation_timeout_seconds=60,
    )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["published"] is True
    assert payload["tool"]["implementation_type"] == "python"
    published = loader.get_definition("python_publish_echo")
    assert published is not None
    assert published.python_config is not None
    # Publish stamps admin approval, so the execution gate admits the revision.
    assert published.python_config.approved_revision == config_python_revision_hash(
        published.python_config, published.parameters
    )
    assert python_execution_gate(published.python_config, published.parameters) is None
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
    core_names = {tool.name for tool in SEED_TOOLS}

    assert "tool_create" not in core_names
    assert "tool_create" in CATALOG_TOOLS
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


def test_python_tool_subprocess_stdlib_import_not_shadowed():
    # The runner launches by file path, so its own directory (nymeria/core)
    # would otherwise sit on sys.path[0] and shadow a stdlib module a user tool
    # imports (``import secrets`` -> nymeria/core/secrets.py, whose token_hex
    # does not exist). Regression guard for that fix.
    config = PythonToolConfig(
        source_code=(
            "def run() -> str:\n"
            "    import secrets\n"
            "    return secrets.token_hex(4)\n"
        )
    )

    result = run_python_tool_subprocess(
        tool_id="stdlib_secrets",
        config=config,
        params={},
        timeout_seconds=10,
    )

    assert result.ok is True, result.error_message
    assert len(result.result) == 8
    int(result.result, 16)  # valid hex, i.e. the real stdlib module ran


def test_custom_tool_loader_registers_python_wrapper_without_importing_user_code(tmp_path):
    loader = CustomToolLoader(tmp_path / "custom_tools")
    parameters = {
        "value": ToolParameter(type="string", description="Value to echo", required=True),
    }
    # The execution gate requires an admin-stamped approval, so a legitimately
    # published tool carries one (mirrors what _publish_draft / the REST create
    # paths stamp).
    python_config = approve_python_revision(
        PythonToolConfig(
            source_code=("def run(value: str) -> str:\n    return value.upper()\n")
        ),
        parameters,
        approved_by="admin-1",
    )
    definition = CustomToolDefinition(
        id="python_echo",
        name="Python Echo",
        description="Echo a value through a subprocess-backed Python tool",
        parameters=parameters,
        implementation_type="python",
        python_config=python_config,
        enabled=True,
    )

    loader.save_definition(definition)
    tool_obj = loader._tools["python_echo"]

    assert tool_obj.invoke({"value": "nymeria"}) == "NYMERIA"


# --- Execution-time approval gate for Python tools (backlog #75 Gap 1) --------


def _py_params():
    return {"value": ToolParameter(type="string", description="Value", required=True)}


def _py_config(source="def run(value: str) -> str:\n    return value.upper()\n"):
    return PythonToolConfig(source_code=source)


def test_python_execution_gate_lifecycle():
    # Fresh (unapproved) -> refused; approve -> allowed; edit source -> refused.
    params = _py_params()
    config = _py_config()
    refusal = python_execution_gate(config, params)
    assert refusal is not None and "not approved" in refusal

    approved = approve_python_revision(config, params, approved_by="admin-1")
    assert python_execution_gate(approved, params) is None
    assert approved.approved_revision == config_python_revision_hash(approved, params)
    assert approved.approved_by == "admin-1"

    edited = approved.model_copy(
        update={"source_code": "def run(value: str) -> str:\n    return value.lower()\n"}
    )
    changed = python_execution_gate(edited, params)
    assert changed is not None and "changed since its approval" in changed


def test_python_execution_gate_ignores_stale_stored_hash():
    # A hand-set revision_hash must not satisfy the gate; only a recomputed
    # match against approved_revision does.
    params = _py_params()
    config = _py_config().model_copy(update={"revision_hash": "stale"})
    approved = approve_python_revision(config, params, approved_by="admin-1")
    assert approved.revision_hash != "stale"
    assert python_execution_gate(approved, params) is None


def test_python_execution_gate_reflects_param_change():
    # The hash covers parameters, so changing the schema without re-approving
    # fails closed (this is why the REST update path re-stamps on a params edit).
    params = _py_params()
    approved = approve_python_revision(_py_config(), params, approved_by="admin-1")
    assert python_execution_gate(approved, params) is None
    more_params = dict(params)
    more_params["extra"] = ToolParameter(type="string", description="x", required=False)
    refusal = python_execution_gate(approved, more_params)
    assert refusal is not None and "changed since its approval" in refusal


def test_loader_python_tool_fails_closed_without_approval(tmp_path):
    # The core bypass: a definition planted on disk without an admin approval
    # must NOT execute; the loader binds the tool but the gate refuses (no
    # subprocess is spawned).
    loader = CustomToolLoader(tmp_path / "custom_tools")
    params = _py_params()
    definition = CustomToolDefinition(
        id="planted",
        name="Planted",
        description="Unapproved python tool planted on disk",
        parameters=params,
        implementation_type="python",
        python_config=_py_config(),  # no approval fields
        enabled=True,
    )
    loader.save_definition(definition)
    tool_obj = loader._tools["planted"]
    result = tool_obj.invoke({"value": "nymeria"})
    assert isinstance(result, str)
    assert result.startswith("[Error]: approval_required")


def test_loader_python_tool_revocation_takes_effect_next_call(tmp_path):
    # Revoking approval on the loader's cached definition fails closed on the
    # NEXT call with no reload (mirrors the workflow lifecycle test).
    loader = CustomToolLoader(tmp_path / "custom_tools")
    params = _py_params()
    approved = approve_python_revision(_py_config(), params, approved_by="admin-1")
    definition = CustomToolDefinition(
        id="revocable",
        name="Revocable",
        description="Approved then revoked",
        parameters=params,
        implementation_type="python",
        python_config=approved,
        enabled=True,
    )
    loader.save_definition(definition)
    tool_obj = loader._tools["revocable"]
    assert tool_obj.invoke({"value": "nymeria"}) == "NYMERIA"

    live = loader.get_definition("revocable")
    assert live is not None and live.python_config is not None
    live.python_config = live.python_config.model_copy(update={"approved_revision": None})
    result = tool_obj.invoke({"value": "nymeria"})
    assert isinstance(result, str)
    assert result.startswith("[Error]: approval_required")


# --- draft-test sits behind the same vault gate as the published tool ------


class _RecordingVault:
    """Captures the (actor, target) the credential vault is asked to resolve under."""

    def __init__(self):
        self.calls = []

    def resolve_references(
        self,
        value,
        *,
        actor,
        target_type=None,
        target_id=None,
        used_credentials=None,
        redact_values=None,
    ):
        self.calls.append(
            {"actor": actor, "target_type": target_type, "target_id": target_id}
        )
        if used_credentials is not None:
            used_credentials.add("cred1")
        if redact_values is not None:
            redact_values.add("SEKRET")
        return value.replace("${credential:cred1.token}", "SEKRET")


def test_draft_test_resolves_credentials_under_the_published_target(tmp_path, monkeypatch):
    """Testing a draft must not reach a credential the published tool cannot.

    Draft-test is a real execution with real credentials, so it has to present
    the same ``(target_type, target_id)`` to the vault that the published tool
    will. It presented neither: with no target at all, a credential scoped to
    any specific tool was unreachable, and while an empty ``allowed_targets``
    still meant "any target may read", every other credential was reachable.

    The ``custom_tool`` literal is checked here, not just "some target", because
    the vault matches it as a string: the HTTP entry points defaulted to
    ``custom_http_tool`` while every other caller passed ``custom_tool``, which
    no test caught while the gate was vacuous.
    """
    from nymeria.core import credential_vault

    vault = _RecordingVault()
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: vault)
    monkeypatch.setattr(
        "nymeria.tools.http_api._http_request_impl",
        lambda **kwargs: {"ok": True, "body": {"done": True}},
    )

    store = ToolDraftStore(tmp_path / "drafts")
    draft = create_draft_definition(
        user_id="user-1",
        tool_id="priced",
        name="Priced",
        description="Fetch a price behind an authenticated endpoint",
        parameters={},
        http_config=_http_config(
            headers={"X-Token": "${credential:cred1.token}"},
            url="https://api.example.com/prices",
        ),
    )
    store.save("user-1", draft)

    result = asyncio.run(run_draft_test(store, "user-1", "priced", {}))

    assert result["ok"] is True
    assert vault.calls, "the draft test never consulted the vault"
    for call in vault.calls:
        assert call["actor"] == "user-1"
        assert call["target_type"] == "custom_tool"
        assert call["target_id"] == "priced"

    # The other half of the invariant: the PUBLISHED tool, which reaches the
    # vault through a different entry point, presents the same target string.
    # ``_publish_draft`` sets ``definition.id = draft.tool_id``, so a divergence
    # here is a divergence between the two spellings, not between two tools.
    vault.calls.clear()
    loader = CustomToolLoader(tmp_path / "custom_tools")
    loader.save_definition(
        CustomToolDefinition(
            id="priced",
            name="Priced",
            description="Fetch a price behind an authenticated endpoint",
            parameters={},
            implementation_type="http",
            http_config=HTTPToolConfig(
                method="GET",
                url="https://api.example.com/prices",
                headers={"X-Token": "${credential:cred1.token}"},
            ),
            enabled=True,
        )
    )
    tool_obj = loader.get_tool("priced")
    assert tool_obj is not None
    tool_obj.invoke({}, config={"configurable": {"user_id": "user-1"}})

    assert vault.calls
    for call in vault.calls:
        assert call["actor"] == "user-1"
        assert call["target_type"] == "custom_tool"
        assert call["target_id"] == "priced"
