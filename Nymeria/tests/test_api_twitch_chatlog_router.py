"""Twitch chat-log routes (tmp/twitch-chatlog-plan.md behaviors 4, 5, 8)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from nymeria.core import twitch_chatlog as chatlog_module


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, monkeypatch, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    monkeypatch.setattr(chatlog_module, "_STORES", {})
    agent = FakeAgent(tmp_path)
    # ApiTestSettings carries data_dir and twitch_chatlog_retention_days.
    return api_client_builder.client(agent, settings), agent


def _token(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(user_id, f"{user_id}@example.com", user_id.title(), role=role)
    return agent.accounts_repo.issue_token(user_id)


def _line(mid, login, text):
    return {
        "message_id": mid,
        "user_login": login,
        "display_name": login.title(),
        "user_id": "5",
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "badges": [],
    }


def test_push_is_bound_to_the_caller_and_lookup_is_per_account(
    tmp_path: Path, monkeypatch, api_client_builder
):
    client, agent = _client(tmp_path, monkeypatch, api_client_builder)
    alice = _token(agent, "alice")
    bob = _token(agent, "bob")

    pushed = client.post(
        "/twitch/chat-log",
        json={
            "channel": "#Silk",
            "user_id": "bob",  # ignored: the log belongs to the caller
            "messages": [_line("m1", "kid", "hello"), _line("m1", "kid", "dup"), {"text": "no login"}],
        },
        headers=api_client_builder.auth(alice),
    )
    assert pushed.status_code == 200 and pushed.json() == {"stored": 1, "dropped": 2}
    assert (tmp_path / "users" / "alice" / "twitch_chatlog" / "silk").is_dir()
    assert not (tmp_path / "users" / "bob").exists()

    mine = client.get(
        "/twitch/chat-log",
        params={"channel": "silk", "login": "Kid", "hours": 2},
        headers=api_client_builder.auth(alice),
    )
    assert mine.status_code == 200
    body = mine.json()
    assert body["channel"] == "silk" and body["login"] == "kid" and body["count"] == 1
    assert body["entries"][0]["text"] == "hello" and body["entries"][0]["message_id"] == "m1"

    theirs = client.get(
        "/twitch/chat-log",
        params={"channel": "silk", "login": "kid"},
        headers=api_client_builder.auth(bob),
    )
    assert theirs.status_code == 200 and theirs.json()["count"] == 0


def test_push_rejects_a_malformed_batch_and_anonymous_calls(
    tmp_path: Path, monkeypatch, api_client_builder
):
    client, agent = _client(tmp_path, monkeypatch, api_client_builder)
    alice = _token(agent, "alice")

    assert client.post("/twitch/chat-log", json={"channel": "silk", "messages": []}).status_code == 401
    bad = client.post(
        "/twitch/chat-log",
        json={"channel": "", "messages": [_line("m1", "kid", "x")]},
        headers=api_client_builder.auth(alice),
    )
    assert bad.status_code == 422
    not_a_list = client.post(
        "/twitch/chat-log",
        json={"channel": "silk", "messages": {"m": 1}},
        headers=api_client_builder.auth(alice),
    )
    assert not_a_list.status_code == 422
    from nymeria.core.twitch_chatlog import CHATLOG_BATCH_MAX

    too_many = client.post(
        "/twitch/chat-log",
        json={"channel": "silk", "messages": [_line(f"m{i}", "kid", "x") for i in range(CHATLOG_BATCH_MAX + 1)]},
        headers=api_client_builder.auth(alice),
    )
    assert too_many.status_code == 422
    assert client.get("/twitch/chat-log", params={"channel": "silk"}, headers=api_client_builder.auth(alice)).status_code == 422
    assert not (tmp_path / "users" / "alice" / "twitch_chatlog").exists()
