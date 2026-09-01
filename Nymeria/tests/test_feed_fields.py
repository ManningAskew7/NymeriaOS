"""Feed date fields agree across every reader (#309).

Atom REQUIRES `<updated>` and makes `<published>` optional, and feedparser
does not alias one onto the other. Every GitHub releases/commits/tags feed
takes that shape, so an rss trigger watching one delivered an empty
`{published}` into its notification template and nothing errored: a blank
where a date belongs.

Three surfaces read feed entries (the rss trigger source, the
`fetch_url_nymeria` preview, and the `fetch_rss_feed` public-info tool) and
they had three readers and two answers about dates, while `web_fetch`'s
docstring promised the preview showed exactly what a trigger would receive.
They now share `core/feed_fields`, so that promise is a property of the code.
The parity test below is the one that fails if a future edit re-splits them.
"""

from __future__ import annotations

import feedparser

from nymeria.core.feed_fields import feed_entry_field, feed_entry_published
from nymeria.triggers.sources.rss_source import RSSSource

_ATOM_UPDATED_ONLY = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Project Releases</title>
  <entry>
    <title>v2.4.0</title>
    <id>tag:example.com,2026:Repository/1/v2.4.0</id>
    <link href="https://example.com/releases/v2.4.0"/>
    <updated>2026-08-31T11:00:00Z</updated>
    <summary>Release notes.</summary>
  </entry>
</feed>
"""


def _entry(raw: bytes):
    return feedparser.parse(raw).entries[0]


# --- the shared reader ------------------------------------------------------


def test_feedparser_really_does_not_alias_updated_onto_published():
    """The premise. If feedparser ever starts aliasing these, the fallback
    becomes dead code and this test says so rather than leaving it to rot."""
    entry = _entry(_ATOM_UPDATED_ONLY)
    assert "published" not in entry
    assert entry.get("updated") == "2026-08-31T11:00:00Z"


def test_published_falls_back_to_updated_then_created():
    assert feed_entry_published({"published": "P", "updated": "U"}) == "P"
    assert feed_entry_published({"updated": "U", "created": "C"}) == "U"
    assert feed_entry_published({"created": "C"}) == "C"
    assert feed_entry_published({}) == ""
    # An empty string is not a date: keep falling back past it.
    assert feed_entry_published({"published": "", "updated": "U"}) == "U"


def test_a_none_valued_field_reads_as_empty_not_the_string_none():
    """The previous rss reader was `str(entry.get(key, default))`, which put
    the literal "None" in a delivered trigger event."""
    assert feed_entry_field({"author": None}, "author") == ""
    assert feed_entry_field({}, "author") == ""
    assert feed_entry_field({"author": "Bob"}, "author") == "Bob"


# --- the rss trigger source -------------------------------------------------


def _events_from(monkeypatch, raw: bytes) -> list[dict]:
    """Run RSSSource.check past its first-poll baseline and return events."""
    from nymeria.core import http_policy

    class _Resp:
        text = raw.decode("utf-8")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        http_policy,
        "httpx_request_with_policy",
        lambda *a, **k: (_Resp(), [], None),
    )

    source = RSSSource()
    state: dict = {}
    # The first poll deliberately baselines without flooding, so the events
    # under test come from the second, with the seen-id set emptied.
    source.check({"url": "https://example.com/feed.atom"}, state)
    state["seen_ids"] = []
    return source.check({"url": "https://example.com/feed.atom"}, state)


def test_an_atom_entry_with_only_updated_delivers_a_real_date(monkeypatch):
    events = _events_from(monkeypatch, _ATOM_UPDATED_ONLY)

    assert len(events) == 1
    assert events[0]["published"] == "2026-08-31T11:00:00Z"
    assert events[0]["title"] == "v2.4.0"


def test_an_explicit_published_still_wins(monkeypatch):
    raw = _ATOM_UPDATED_ONLY.replace(
        b"<updated>2026-08-31T11:00:00Z</updated>",
        b"<updated>2026-08-31T11:00:00Z</updated>"
        b"<published>2026-01-02T03:04:05Z</published>",
    )
    events = _events_from(monkeypatch, raw)

    assert events[0]["published"] == "2026-01-02T03:04:05Z"


def test_an_entry_with_no_date_at_all_stays_empty(monkeypatch):
    raw = _ATOM_UPDATED_ONLY.replace(
        b"<updated>2026-08-31T11:00:00Z</updated>", b""
    )
    events = _events_from(monkeypatch, raw)

    assert events[0]["published"] == ""


def test_the_event_shape_is_still_the_advertised_six_fields(monkeypatch):
    """The fallback must not smuggle a seventh variable into delivered events:
    `template_variables` advertises exactly these, and both GUI wizards and
    the docs list them."""
    events = _events_from(monkeypatch, _ATOM_UPDATED_ONLY)

    assert set(events[0]) == set(RSSSource.template_variables)


# --- the parity that the preview's docstring promises -----------------------


def test_the_preview_and_the_trigger_report_the_same_date(monkeypatch):
    """`fetch_url_nymeria`'s docstring says a previewed feed shows exactly
    what a trigger watching it will receive. Two independent readers used to
    hold that up by agreement; this is the test that keeps it true."""
    from nymeria.tools import web_fetch

    events = _events_from(monkeypatch, _ATOM_UPDATED_ONLY)
    body, _title = web_fetch._render_feed(_ATOM_UPDATED_ONLY)

    delivered = events[0]["published"]
    assert delivered
    published_rows = [
        line.split("published:", 1)[1].strip()
        for line in body.splitlines()
        if "published:" in line
    ]
    assert published_rows == [delivered]
