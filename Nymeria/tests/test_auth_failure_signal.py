"""Tests for the structured [Auth check] signal on auth-shaped tool failures
(dev-todo #44): the module-level ``_AUTH_FAILURE_RE`` / ``_auth_failure_guidance``
and ``SafeToolNode._augment_auth_failure``.

Reuses the tmp-vault fixture pattern from tests/test_credential_registry.py:
NYMERIA_SECRETS_KEY set, config.get_settings monkeypatched to a data_dir
dataclass, an AccountsRepo user "alice" created FIRST (FK requirement), then a
CredentialVaultRepo on the same db bound onto credential_vault._vault_repo.
``import nymeria.tools.productivity_service_integrations`` registers the todoist
spec so ``spec_for_tool("todoist_list_tasks")`` resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from nymeria.vendor.react_agent.nodes import (
    _AUTH_FAILURE_RE,
    SafeToolNode,
    _auth_failure_guidance,
)


@dataclass
class _Settings:
    data_dir: Path


def _vault(tmp_path, monkeypatch):
    from nymeria.core.accounts import AccountsRepo
    from nymeria.core.credential_vault import CredentialVaultRepo

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = _Settings(data_dir=tmp_path)

    import nymeria.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)

    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")

    import nymeria.core.credential_vault as vault_mod

    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(vault_mod, "_vault_repo", repo)
    return repo


@tool
def _dummy(query: str) -> str:
    """Stub tool; never executed (only used to build a SafeToolNode)."""
    return query


def _node() -> SafeToolNode:
    return SafeToolNode([_dummy])


def _err_msg(content: str, name: str = "todoist_list_tasks") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="t1", name=name, status="error")


def _config():
    return {"configurable": {"user_id": "alice"}}


def _call(name: str = "todoist_list_tasks") -> dict:
    return {"name": name, "id": "t1"}


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------


def test_auth_failure_regex_positives():
    positives = [
        "[Error]: HTTP 401: Unauthorized",
        "[Error]: No Todoist credential found. Set TODOIST_API_KEY...",
        "Error: RuntimeError('HTTP 403: Forbidden')",
        "[Error]: invalid api key provided",
    ]
    for text in positives:
        assert _AUTH_FAILURE_RE.search(text) is not None, text


def test_auth_failure_regex_negatives():
    # A bare "403" without HTTP/status wording must not match.
    assert _AUTH_FAILURE_RE.search("Found 403 results for your query") is None


# ---------------------------------------------------------------------------
# _guidance helper
# ---------------------------------------------------------------------------


def test_guidance_needs_setup_mentions_request_credential():
    block = _auth_failure_guidance("todoist_list_tasks", "todoist", "needs_setup")
    assert "[Auth check]:" in block
    assert 'Provider "todoist"' in block
    assert "needs_setup" in block
    assert "request_credential" in block


def test_guidance_connected_points_to_auth_test():
    block = _auth_failure_guidance("todoist_list_tasks", "todoist", "connected")
    assert 'auth_test(tool_name="todoist_list_tasks")' in block


# ---------------------------------------------------------------------------
# _augment_auth_failure end-to-end
# ---------------------------------------------------------------------------


def test_augment_appends_auth_check_needs_setup(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    out = _node()._augment_auth_failure(
        _err_msg("[Error]: HTTP 401: Unauthorized"), _call(), _config()
    )
    assert "[Auth check]:" in out.content
    assert 'Provider "todoist"' in out.content
    assert "needs_setup" in out.content
    assert "request_credential" in out.content


def test_augment_connected_points_to_auth_test(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _vault(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="todoist key",
        provider="todoist",
        kind="api_key",
        secret_fields={"api_key": "sk-x"},
    )
    out = _node()._augment_auth_failure(
        _err_msg("[Error]: HTTP 401: Unauthorized"), _call(), _config()
    )
    assert "[Auth check]:" in out.content
    assert 'auth_test(tool_name="todoist_list_tasks")' in out.content


# ---------------------------------------------------------------------------
# Passthroughs
# ---------------------------------------------------------------------------


def test_passthrough_success_content_with_auth_words(tmp_path, monkeypatch):
    """Auth wording in a NON-error result (this matches the regex) is excluded
    by the error-shape gate, not the regex."""
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    # Success-shaped: no [Error]/Error prefix, default "success" status.
    msg = ToolMessage(
        content="The word unauthorized appears in this article",
        tool_call_id="t1",
        name="todoist_list_tasks",
    )
    assert _AUTH_FAILURE_RE.search(msg.content) is not None  # regex would match
    out = _node()._augment_auth_failure(msg, _call(), _config())
    assert out is msg
    assert "[Auth check]:" not in out.content


def test_passthrough_tool_without_spec(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    msg = _err_msg("[Error]: HTTP 401: Unauthorized", name="definitely_not_integration")
    out = _node()._augment_auth_failure(
        msg, _call("definitely_not_integration"), _config()
    )
    assert out is msg
    assert "[Auth check]:" not in out.content


def test_passthrough_non_toolmessage():
    out = _node()._augment_auth_failure("just a string", _call(), _config())
    assert out == "just a string"


def test_idempotent_when_already_augmented(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    node = _node()
    once = node._augment_auth_failure(
        _err_msg("[Error]: HTTP 401: Unauthorized"), _call(), _config()
    )
    assert "[Auth check]:" in once.content
    twice = node._augment_auth_failure(once, _call(), _config())
    assert twice is once


# ---------------------------------------------------------------------------
# Behavior update: bare "forbidden" dropped; 403/access/permission still match.
# ---------------------------------------------------------------------------


def test_regex_positives_access_permission_403():
    for text in ("access denied", "permission denied", "403 Forbidden"):
        assert _AUTH_FAILURE_RE.search(text) is not None, text


def test_regex_negatives_404_400_and_forbidden_prose():
    # 404/400 are not auth codes; bare "forbidden" in prose no longer matches.
    for text in (
        "[Error]: HTTP 404: Not Found",
        "[Error]: HTTP 400: bad request",
        "this action is forbidden for free-tier accounts",
        "label 'forbidden' does not exist",
    ):
        assert _AUTH_FAILURE_RE.search(text) is None, text


def test_augment_passthrough_404_400_on_spec_mapped_tool(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    node = _node()
    for content in ("[Error]: HTTP 404: Not Found", "[Error]: HTTP 400: bad request"):
        msg = _err_msg(content)
        out = node._augment_auth_failure(msg, _call(), _config())
        assert out is msg
        assert "[Auth check]:" not in out.content


# ---------------------------------------------------------------------------
# Status branches: pending + the new optional branch.
# ---------------------------------------------------------------------------


def test_augment_pending_branch(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    repo = _vault(tmp_path, monkeypatch)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="pending todoist",
        provider="todoist",
        kind="api_key",
        status="pending_setup",
    )
    out = _node()._augment_auth_failure(
        _err_msg("[Error]: HTTP 401: Unauthorized"), _call(), _config()
    )
    assert "[Auth check]:" in out.content
    assert 'Provider "todoist"' in out.content
    assert "pending" in out.content


def test_augment_optional_branch(tmp_path, monkeypatch):
    # searxng has no required credential groups -> "optional"; the new branch
    # says it works without a credential but this failure suggests one is needed.
    import nymeria.tools.web_search_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    call = {"name": "web_search_searxng", "id": "t1"}
    msg = ToolMessage(
        content="[Error]: HTTP 401: Unauthorized",
        tool_call_id="t1",
        name="web_search_searxng",
        status="error",
    )
    out = _node()._augment_auth_failure(msg, call, _config())
    assert "[Auth check]:" in out.content
    assert 'Provider "searxng"' in out.content
    assert "optional" in out.content
    assert "works without a credential" in out.content


# ---------------------------------------------------------------------------
# model_copy preservation + the cheap candidate gate + async wrapper.
# ---------------------------------------------------------------------------


def test_augment_preserves_message_fields(tmp_path, monkeypatch):
    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    msg = ToolMessage(
        content="[Error]: HTTP 401: Unauthorized",
        tool_call_id="tc-9",
        name="todoist_list_tasks",
        status="error",
        artifact={"raw": 1},
    )
    out = _node()._augment_auth_failure(msg, _call(), _config())
    assert out is not msg
    assert out.tool_call_id == "tc-9"
    assert out.status == "error"
    assert out.name == "todoist_list_tasks"
    assert out.artifact == {"raw": 1}
    assert out.content.startswith("[Error]: HTTP 401: Unauthorized")
    assert "[Auth check]:" in out.content


def test_auth_failure_candidate_gate():
    node = _node()
    err = ToolMessage(
        content="[Error]: HTTP 401: Unauthorized",
        tool_call_id="t1",
        name="todoist_list_tasks",
        status="error",
    )
    assert node._auth_failure_candidate(err) is True

    ok = ToolMessage(content="all good, 200", tool_call_id="t1", name="todoist_list_tasks")
    assert node._auth_failure_candidate(ok) is False

    already = ToolMessage(
        content="[Error]: HTTP 401: Unauthorized\n\n[Auth check]: already here",
        tool_call_id="t1",
        name="todoist_list_tasks",
        status="error",
    )
    assert node._auth_failure_candidate(already) is False

    assert node._auth_failure_candidate("plain string") is False


def test_a_augment_auth_failure_async(tmp_path, monkeypatch):
    import asyncio

    import nymeria.tools.productivity_service_integrations  # noqa: F401

    _vault(tmp_path, monkeypatch)
    node = _node()

    # Non-candidate (success-shaped): returns the same object, no thread hop.
    ok = ToolMessage(content="fine", tool_call_id="t1", name="todoist_list_tasks")
    out_ok = asyncio.run(node._a_augment_auth_failure(ok, _call(), _config()))
    assert out_ok is ok
    assert "[Auth check]:" not in out_ok.content

    # Candidate: augmented via the thread-offloaded vault read.
    err = _err_msg("[Error]: HTTP 401: Unauthorized")
    out_err = asyncio.run(node._a_augment_auth_failure(err, _call(), _config()))
    assert "[Auth check]:" in out_err.content
    assert 'Provider "todoist"' in out_err.content
