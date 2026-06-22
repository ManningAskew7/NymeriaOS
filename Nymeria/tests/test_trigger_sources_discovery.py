"""Tests for trigger-source discovery dedup (slice 23 F12) and the Slack
source author field after the ``user_id`` shadow rename (slice 23 F11)."""

from __future__ import annotations

from nymeria.triggers import sources
from nymeria.triggers.sources.slack_source import SlackSource


# ---------------------------------------------------------------------------
# F12: _iter_source_module_names is the single glob+filter both discovery
# paths (reload_sources, _auto_load_sources) now share.
# ---------------------------------------------------------------------------


def test_iter_source_module_names_filters_and_sorts():
    names = sources._iter_source_module_names()
    assert names == sorted(names), "module names must be sorted for deterministic load order"
    assert all(n.startswith("nymeria.triggers.sources.") for n in names)
    stems = {n.rsplit(".", 1)[-1] for n in names}
    assert {"slack_source", "webhook_source", "rss_source"} <= stems
    assert "base" not in stems, "base.py must be excluded"
    assert not any(stem.startswith("_") for stem in stems), "private files must be excluded"


def test_reload_sources_registers_all_discovered():
    count = sources.reload_sources()
    assert count == len(sources.AVAILABLE_SOURCES)
    assert count > 0
    assert "slack" in sources.AVAILABLE_SOURCES
    assert "webhook" in sources.AVAILABLE_SOURCES


# ---------------------------------------------------------------------------
# F11: contract guard for the Slack event ``author`` field. The rename
# (loop-local ``user_id`` -> ``author``) was cosmetic (the old shadowed name
# was already written straight into ``author`` and never read again), so this
# does not catch the rename itself; it pins the contract that ``author`` comes
# from the Slack message ``user`` field and never from the Nymeria ``user_id``
# parameter, which is the trap the shadow rename removes for future edits.
# ---------------------------------------------------------------------------


class _FakeSlackResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


def test_slack_source_event_author_is_message_poster_not_nymeria_user(monkeypatch):
    source = SlackSource()
    config = {"bot_token": "xoxb-test", "channel_id": "C1"}
    # Pre-initialized state so check() takes the poll path (no baseline call).
    state = {
        "initialized": True,
        "last_message_ts": "1.0",
        "_channel_name": "general",
    }

    def _fake_get(url, **kwargs):
        return _FakeSlackResponse(
            {
                "ok": True,
                "messages": [{"ts": "2.0", "user": "U_POSTER", "text": "hello"}],
            }
        )

    monkeypatch.setattr("httpx.get", _fake_get)

    events = source.check(config, state, user_id="nymeria-user-42")

    assert len(events) == 1
    event = events[0]
    # author comes from the Slack message ``user`` field, not the Nymeria
    # ``user_id`` parameter passed to check().
    assert event["author"] == "U_POSTER"
    assert event["author"] != "nymeria-user-42"
    assert event["channel_name"] == "general"
    assert event["content"] == "hello"
