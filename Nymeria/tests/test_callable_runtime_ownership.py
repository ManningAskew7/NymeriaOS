from __future__ import annotations

import logging
from types import SimpleNamespace

import nymeria.core.thread_agent_executor as thread_agent_executor
from nymeria.agents.tool_factory import create_callable_thread_tool
from nymeria.core.agent import set_current_agent
from nymeria.core.thread_config import ThreadConfig


class FakeOwnershipRepo:
    def __init__(self, owner: str | None, roles: dict[str, str] | None = None):
        self.owner = owner
        self.roles = roles or {}

    def get_thread_owner(self, thread_id: str) -> str | None:
        return self.owner

    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(role=self.roles.get(user_id, "user"))


class FakeOwnershipAgent:
    def __init__(self, owner: str | None, roles: dict[str, str] | None = None):
        self.accounts_repo = FakeOwnershipRepo(owner, roles)


def _invoke_callable_tool(monkeypatch, *, owner: str | None, user_id: str):
    calls: list[tuple[str, str, str, str]] = []

    def fake_invoke(thread_id, task, resolved_user_id, callable_name, **_kwargs):
        calls.append((thread_id, task, resolved_user_id, callable_name))
        return "[OK]: invoked"

    monkeypatch.setattr(thread_agent_executor, "invoke", fake_invoke)
    tool = create_callable_thread_tool(
        ThreadConfig(
            thread_id="target-thread",
            callable=True,
            callable_name="Helper",
        )
    )
    set_current_agent(
        FakeOwnershipAgent(
            owner,
            roles={
                "admin": "admin",
                "owner": "user",
                "other": "user",
            },
        )
    )
    try:
        result = tool.invoke(
            {"task": "do the work"},
            config={"configurable": {"user_id": user_id}},
        )
    finally:
        set_current_agent(None)
    return result, calls


def test_callable_tool_blocks_regular_user_from_other_users_callable(monkeypatch):
    result, calls = _invoke_callable_tool(monkeypatch, owner="owner", user_id="other")

    assert result.startswith("[Error]:")
    assert "not available to this user" in result
    assert calls == []


def test_callable_tool_blocks_admin_as_self_from_other_users_callable(monkeypatch):
    result, calls = _invoke_callable_tool(monkeypatch, owner="owner", user_id="admin")

    assert result.startswith("[Error]:")
    assert "not available to this user" in result
    assert calls == []


def test_callable_tool_allows_admin_act_as_owner_effective_user(monkeypatch):
    # The API resolves X-Nymeria-Act-As before graph execution, so the runtime
    # gate only sees the effective owner user_id in RunnableConfig.
    result, calls = _invoke_callable_tool(monkeypatch, owner="owner", user_id="owner")

    assert result == "[OK]: invoked"
    assert calls == [("target-thread", "do the work", "owner", "Helper")]


def test_callable_tool_keeps_ownerless_legacy_callable_admin_only(
    monkeypatch,
    caplog,
):
    regular_result, regular_calls = _invoke_callable_tool(
        monkeypatch,
        owner=None,
        user_id="other",
    )

    caplog.set_level(logging.WARNING, logger="nymeria.agents.tool_factory")
    admin_result, admin_calls = _invoke_callable_tool(
        monkeypatch,
        owner=None,
        user_id="admin",
    )

    assert regular_result.startswith("[Error]:")
    assert regular_calls == []
    assert admin_result == "[OK]: invoked"
    assert admin_calls == [("target-thread", "do the work", "admin", "Helper")]
    assert "legacy unowned callable thread target-thread" in caplog.text
