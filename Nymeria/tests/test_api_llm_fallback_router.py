"""Tests for the LLM fallback-consent REST router (/llm/fallback-approvals).

Mirrors the hooks-approvals router coverage: owner scoping, admin visibility,
resolve waking the parked waiter with the chosen hold, 404 for missing and
foreign records, and 409 stale cleanup when nothing is waiting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")

    def invalidate_thread_config_cache(self, thread_id: str):
        pass

    def sync_agent_tools(self):
        pass


@pytest.fixture
def approvals_env(tmp_path, api_client_builder, monkeypatch):
    """Client + isolated approval store + reset coordinator."""
    import nymeria.core.fallback_approvals as fa

    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="owner", email="owner@example.com"
    )
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(fa, "_coordinator", None)
    headers = api_client_builder.auth(token)
    return client, agent, headers, api_client_builder


@pytest.fixture
def waiter_loop():
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _mint_pending(loop, user_id="owner", **kw):
    from nymeria.core.fallback_approvals import create_pending_approval

    async def _mint():
        return create_pending_approval(
            kind=kw.get("kind", "transport"),
            user_id=user_id,
            thread_id=kw.get("thread_id", "t1"),
            payload={
                "from_provider": "anthropic",
                "from_model": "claude-fable-5",
                "to_provider": "anthropic",
                "to_model": "claude-opus-4-8",
                "reason": kw.get("reason", "server_error"),
            },
            window_seconds=60.0,
            default_hold_seconds=7200,
        )

    return loop.run_until_complete(_mint())


def _await_result(loop, future, timeout=2.0):
    import asyncio

    return loop.run_until_complete(asyncio.wait_for(future, timeout))


def test_list_scopes_to_owner(approvals_env, waiter_loop):
    client, agent, headers, builder = approvals_env
    record, _ = _mint_pending(waiter_loop, user_id="owner")
    _mint_pending(waiter_loop, user_id="somebody-else")

    resp = client.get("/llm/fallback-approvals", headers=headers)
    assert resp.status_code == 200
    entries = resp.json()["approvals"]
    assert [e["record_id"] for e in entries] == [record["record_id"]]
    assert entries[0]["kind"] == "transport"
    assert entries[0]["to_model"] == "claude-opus-4-8"
    assert entries[0]["hold_options"]

    agent.accounts_repo.create_user("boss", "boss@example.com", "Boss", role="admin")
    admin_headers = builder.auth(agent.accounts_repo.issue_token("boss"))
    all_entries = client.get(
        "/llm/fallback-approvals", headers=admin_headers
    ).json()["approvals"]
    assert len(all_entries) == 2


def test_resolve_wakes_waiter_with_hold(approvals_env, waiter_loop):
    client, _agent, headers, _b = approvals_env
    record, future = _mint_pending(waiter_loop)
    resp = client.post(
        f"/llm/fallback-approvals/{record['record_id']}/resolve",
        headers=headers,
        json={"approved": True, "hold_seconds": 600},
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "approved"
    result = _await_result(waiter_loop, future)
    assert result["approved"] is True
    assert result["hold_seconds"] == 600
    assert result["resolved_by"] == "owner"


def test_resolve_permanent_hold(approvals_env, waiter_loop):
    client, _agent, headers, _b = approvals_env
    record, future = _mint_pending(waiter_loop)
    resp = client.post(
        f"/llm/fallback-approvals/{record['record_id']}/resolve",
        headers=headers,
        json={"approved": True, "hold_permanent": True},
    )
    assert resp.status_code == 200
    result = _await_result(waiter_loop, future)
    assert result["hold_permanent"] is True


def test_resolve_404_for_missing_and_foreign_records(approvals_env, waiter_loop):
    client, _agent, headers, _b = approvals_env
    resp = client.post(
        "/llm/fallback-approvals/nope/resolve",
        headers=headers,
        json={"approved": True},
    )
    assert resp.status_code == 404

    foreign, _ = _mint_pending(waiter_loop, user_id="somebody-else")
    resp = client.post(
        f"/llm/fallback-approvals/{foreign['record_id']}/resolve",
        headers=headers,
        json={"approved": True},
    )
    assert resp.status_code == 404


def test_resolve_409_cleans_stale_record(approvals_env, waiter_loop):
    from nymeria.core.fallback_approvals import (
        get_fallback_approval_coordinator,
        load_record,
    )

    client, _agent, headers, _b = approvals_env
    record, _future = _mint_pending(waiter_loop)
    # Simulate the waiter being gone: the coordinator entry is drained but the
    # durable record was left behind (crash shape).
    get_fallback_approval_coordinator().abort_thread("t1")

    resp = client.post(
        f"/llm/fallback-approvals/{record['record_id']}/resolve",
        headers=headers,
        json={"approved": False},
    )
    assert resp.status_code == 409
    assert load_record(record["record_id"]) is None
