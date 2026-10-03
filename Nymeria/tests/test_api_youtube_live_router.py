"""POST /youtube/live-chat/poll (api/routers/youtube_live.py): the Twitch
bot's YouTube reader. Plan behaviors 13 and 20 at the HTTP surface: the poll
reads with the CALLER's own vault grants and logs under the caller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from test_youtube_live import ALICE_CH, STREAMER_CH, _broadcast, _text_item, fake  # noqa: F401


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _token(agent: FakeAgent, user_id: str) -> str:
    agent.accounts_repo.create_user(user_id, f"{user_id}@example.com", user_id.title(), role="user")
    return agent.accounts_repo.issue_token(user_id)


def test_poll_reads_with_the_callers_grant_and_logs_under_the_caller(
    tmp_path: Path, fake, api_client_builder  # noqa: F811
):
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, api_client_builder.settings(tmp_path))
    alice = _token(agent, "alice")
    bob = _token(agent, "bob")
    now = datetime.now(timezone.utc)
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH, "Silk live")]
    fake.pages.append(
        {
            "items": [_text_item("m1", ALICE_CH, "alice [mod]", "hello", now - timedelta(seconds=5))],
            "nextPageToken": "p1",
            "pollingIntervalMillis": 6000,
        }
    )

    resp = client.post("/youtube/live-chat/poll", json={}, headers=api_client_builder.auth(alice))
    assert resp.status_code == 200
    body = resp.json()
    assert (body["state"], body["video_id"], body["title"], body["attached_via"]) == (
        "live", "vid00000001", "Silk live", "bot"
    )
    assert body["next_page_token"] == "p1" and body["poll_after_seconds"] == 6.0
    [line] = body["messages"]
    assert (line["id"], line["kind"], line["author_channel_id"], line["text"]) == ("m1", "chat", ALICE_CH, "hello")
    assert line["author_name"] == "alice mod" and line["is_self"] is False
    assert (tmp_path / "users" / "alice" / "youtube_chatlog").is_dir()

    # Bob holds no grant: his poll is unauthorized, calls YouTube not at all,
    # and never sees alice's attachment.
    calls = len(fake.calls)
    theirs = client.post("/youtube/live-chat/poll", json={}, headers=api_client_builder.auth(bob))
    assert theirs.status_code == 200
    assert theirs.json()["state"] == "unauthorized" and theirs.json()["video_id"] is None
    assert len(fake.calls) == calls
    assert not (tmp_path / "users" / "bob").exists()


def test_poll_requires_auth_and_validates_video_ids(tmp_path: Path, fake, api_client_builder):  # noqa: F811
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, api_client_builder.settings(tmp_path))
    alice = _token(agent, "alice")

    assert client.post("/youtube/live-chat/poll", json={}).status_code == 401
    for bad in ({"pinned_video_id": "../../etc"}, {"video_id": "x" * 12}):
        resp = client.post("/youtube/live-chat/poll", json=bad, headers=api_client_builder.auth(alice))
        assert resp.status_code == 422
    assert fake.calls == []
