"""Characterization tests for CLIApp startup thread resolution (slice 19 F14).

`_resolve_startup_thread_ref` and `_resolve_most_recent_thread` had no prior
direct coverage. These lock their return values, the exact startup-error
strings, and the capability expression passed to `_render_startup_error`, so the
F14 extraction (a `_startup_capabilities()` helper + a shared
`_load_threads_for_startup` preamble) stays behavior-preserving. They pass
against the pre- and post-refactor code.
"""

from __future__ import annotations

from typing import Any

from cli_fixtures import FakeAgentClient, FakeTerminalCapabilities, run

from nymeria.triggers.cli import app as app_module
from nymeria.triggers.cli.app import CLIApp


def _make_app(client: Any) -> CLIApp:
    app = CLIApp(agent=None, thread_id="thread-1", user_id="alice")
    app._client = client
    return app


def _capture_startup_errors(monkeypatch, app: CLIApp) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        app,
        "_render_startup_error",
        lambda message, capabilities: calls.append((message, capabilities)),
    )
    return calls


class _ClientNoMethods:
    """A client exposing neither list_threads nor an ``api`` attribute."""


class _RaisingClient:
    def list_threads(self, user_id: str = "default"):
        raise RuntimeError("boom")


class _EmptyListClient:
    """Returns a genuinely empty thread list (FakeAgentClient synthesizes a
    default thread when given threads=[], so it can't exercise the empty path)."""

    async def list_threads(self, user_id: str = "default"):
        return []


# --- _resolve_most_recent_thread -------------------------------------------

def test_most_recent_picks_latest_by_updated_at():
    client = FakeAgentClient(
        threads=[
            {"thread_id": "t1", "title": "Old", "updated_at": "2026-01-01"},
            {"thread_id": "t2", "title": "New", "updated_at": "2026-02-01"},
        ]
    )
    app = _make_app(client)
    result = run(app._resolve_most_recent_thread())
    assert result == {"thread_id": "t2", "title": "New"}


def test_most_recent_empty_threads_renders_error(monkeypatch):
    app = _make_app(_EmptyListClient())
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_most_recent_thread())
    assert result is None
    assert [m for m, _ in calls] == ["No threads found to continue."]


def test_most_recent_thread_without_id_renders_error(monkeypatch):
    app = _make_app(FakeAgentClient(threads=[{"title": "NoId", "updated_at": "2026-01-01"}]))
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_most_recent_thread())
    assert result is None
    assert [m for m, _ in calls] == ["No threads found to continue."]


def test_most_recent_missing_client_method(monkeypatch):
    app = _make_app(_ClientNoMethods())
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_most_recent_thread())
    assert result is None
    assert [m for m, _ in calls] == ["Cannot continue: missing client method list_threads."]


def test_most_recent_generic_exception(monkeypatch):
    app = _make_app(_RaisingClient())
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_most_recent_thread())
    assert result is None
    assert [m for m, _ in calls] == ["Cannot continue: boom"]


# --- _resolve_startup_thread_ref -------------------------------------------

def test_resolve_ref_exact_match():
    client = FakeAgentClient(threads=[{"thread_id": "abc", "title": "Hello"}])
    app = _make_app(client)
    result = run(app._resolve_startup_thread_ref("abc"))
    assert result == {"thread_id": "abc", "title": "Hello"}


def test_resolve_ref_no_match_renders_error(monkeypatch):
    client = FakeAgentClient(threads=[{"thread_id": "abc", "title": "Hello"}])
    app = _make_app(client)
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_startup_thread_ref("zzz"))
    assert result is None
    assert [m for m, _ in calls] == ["No thread matching 'zzz'."]


def test_resolve_ref_ambiguous_renders_error(monkeypatch):
    client = FakeAgentClient(
        threads=[
            {"thread_id": "t1", "title": "Project A"},
            {"thread_id": "t2", "title": "Project B"},
        ]
    )
    app = _make_app(client)
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_startup_thread_ref("Project"))
    assert result is None
    # Ambiguity message content is owned by _format_thread_resolution_ambiguity;
    # we only lock that exactly one startup error is rendered for this branch.
    assert len(calls) == 1


def test_resolve_ref_missing_client_method(monkeypatch):
    app = _make_app(_ClientNoMethods())
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_startup_thread_ref("abc"))
    assert result is None
    assert [m for m, _ in calls] == ["Cannot open thread 'abc': missing client method list_threads."]


def test_resolve_ref_generic_exception(monkeypatch):
    app = _make_app(_RaisingClient())
    calls = _capture_startup_errors(monkeypatch, app)
    result = run(app._resolve_startup_thread_ref("abc"))
    assert result is None
    assert [m for m, _ in calls] == ["Cannot open thread 'abc': boom"]


# --- capability expression preservation ------------------------------------

def test_active_capabilities_passed_to_startup_error(monkeypatch):
    app = _make_app(_EmptyListClient())
    sentinel = FakeTerminalCapabilities(width=80)
    app._active_capabilities = sentinel  # type: ignore[assignment]
    calls = _capture_startup_errors(monkeypatch, app)
    run(app._resolve_most_recent_thread())
    assert calls[-1][1] is sentinel


def test_detect_capabilities_fallback_when_active_is_none(monkeypatch):
    app = _make_app(_EmptyListClient())
    app._active_capabilities = None
    detected = FakeTerminalCapabilities(width=42)
    monkeypatch.setattr(app_module, "detect_terminal_capabilities", lambda cfg: detected)
    calls = _capture_startup_errors(monkeypatch, app)
    run(app._resolve_most_recent_thread())
    assert calls[-1][1] is detected
