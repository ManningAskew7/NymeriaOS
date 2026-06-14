"""Auto-reconnect watcher for the Rich REPL.

Covers the background loop that re-probes a saved-but-unreachable backend (CLI
started before the API) and swaps in a live client once it comes up, plus the
``_apply_reconnected_client`` swap it drives. The transport-level retry itself is
covered in ``test_cli_transport_api.py``; here we exercise the REPL glue.
"""

from __future__ import annotations

import asyncio

from cli_fixtures import FakeTerminalCapabilities

from nymeria.triggers.cli import app as app_module
from nymeria.triggers.cli.app import CLIApp, _RichReplRuntime, _disconnected_notice_text
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.transport import api as transport_api
from nymeria.triggers.cli.transport.api import APITransportStartupError
from nymeria.triggers.cli.transport.disconnected import DisconnectedAgentClient


def _make_app_and_runtime() -> tuple[CLIApp, _RichReplRuntime]:
    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    renderer = RichReplRenderer(capabilities=capabilities, width=100)
    runtime = _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )
    return cli_app, runtime


def _reconnectable_placeholder() -> DisconnectedAgentClient:
    return DisconnectedAgentClient(
        default_user_id="alice",
        reconnect_api_url="http://saved",
        reconnect_api_key="saved-token",
        reconnect_user_id="alice",
    )


class FakeLiveClient:
    connection_label = "api http://saved"

    def __init__(self) -> None:
        self.base_url = "http://saved"
        self.default_user_id = "alice"
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_apply_reconnected_client_swaps_in_live_client() -> None:
    cli_app, _ = _make_app_and_runtime()
    placeholder = _reconnectable_placeholder()
    cli_app._client = placeholder
    live = FakeLiveClient()

    # runtime=None keeps it to the pure client swap (no header render / notice).
    asyncio.run(cli_app._apply_reconnected_client(live, runtime=None))

    assert cli_app._client is live
    assert cli_app.state.user_id == "alice"


def test_reconnect_watcher_applies_client_when_backend_recovers(monkeypatch) -> None:
    cli_app, runtime = _make_app_and_runtime()
    cli_app._client = _reconnectable_placeholder()
    live = FakeLiveClient()
    applied: list[object] = []

    async def fake_apply(client, *, runtime=None) -> None:
        applied.append(client)
        cli_app._client = client  # now connected -> loop must not re-enter

    monkeypatch.setattr(cli_app, "_apply_reconnected_client", fake_apply)
    monkeypatch.setattr(app_module, "_RECONNECT_POLL_INTERVAL_SECONDS", 0)

    async def fake_attempt(client, **kwargs):
        return live

    monkeypatch.setattr(transport_api, "attempt_saved_reconnect", fake_attempt)

    asyncio.run(runtime._run_reconnect_watcher())

    assert applied == [live]


def test_reconnect_watcher_retries_until_backend_is_up(monkeypatch) -> None:
    cli_app, runtime = _make_app_and_runtime()
    cli_app._client = _reconnectable_placeholder()
    live = FakeLiveClient()
    applied: list[object] = []
    attempts = {"n": 0}

    async def fake_apply(client, *, runtime=None) -> None:
        applied.append(client)
        cli_app._client = client

    monkeypatch.setattr(cli_app, "_apply_reconnected_client", fake_apply)
    monkeypatch.setattr(app_module, "_RECONNECT_POLL_INTERVAL_SECONDS", 0)

    async def fake_attempt(client, **kwargs):
        attempts["n"] += 1
        return None if attempts["n"] < 3 else live  # down, down, then up

    monkeypatch.setattr(transport_api, "attempt_saved_reconnect", fake_attempt)

    asyncio.run(runtime._run_reconnect_watcher())

    assert attempts["n"] == 3
    assert applied == [live]


def test_reconnect_watcher_does_not_clobber_a_concurrent_connection_change(
    monkeypatch,
) -> None:
    cli_app, runtime = _make_app_and_runtime()
    cli_app._client = _reconnectable_placeholder()
    live = FakeLiveClient()
    intervening = FakeLiveClient()  # e.g. the client from a /login the user ran
    applied: list[object] = []

    async def fake_apply(client, *, runtime=None) -> None:
        applied.append(client)

    monkeypatch.setattr(cli_app, "_apply_reconnected_client", fake_apply)
    monkeypatch.setattr(app_module, "_RECONNECT_POLL_INTERVAL_SECONDS", 0)

    async def fake_attempt(client, **kwargs):
        # Simulate the user changing the active connection while the probe awaits
        # network I/O (the realistic race: /login, /logout, or a switch).
        cli_app._client = intervening
        return live

    monkeypatch.setattr(transport_api, "attempt_saved_reconnect", fake_attempt)

    asyncio.run(runtime._run_reconnect_watcher())

    # The watcher's result is stale: it must not apply it over the user's change,
    # and must close the live client it would otherwise have leaked.
    assert applied == []
    assert cli_app._client is intervening
    assert live.closed is True


def test_reconnect_watcher_stops_and_warns_on_auth_error(monkeypatch) -> None:
    cli_app, runtime = _make_app_and_runtime()
    placeholder = _reconnectable_placeholder()
    cli_app._client = placeholder
    monkeypatch.setattr(app_module, "_RECONNECT_POLL_INTERVAL_SECONDS", 0)

    async def fake_attempt(client, **kwargs):
        raise APITransportStartupError(
            "Saved token was revoked.",
            code="api_auth_error",
            api_url="http://saved",
        )

    monkeypatch.setattr(transport_api, "attempt_saved_reconnect", fake_attempt)

    asyncio.run(runtime._run_reconnect_watcher())

    # A revoked token can't be retried into success: stop, keep the placeholder,
    # and tell the user to /login.
    assert cli_app._client is placeholder
    assert runtime._status_notice is not None
    assert runtime._status_notice.level == "error"
    assert "/login" in runtime._status_notice.message


def test_reconnect_watcher_is_noop_for_plain_disconnected(monkeypatch) -> None:
    cli_app, runtime = _make_app_and_runtime()
    cli_app._client = DisconnectedAgentClient(default_user_id="alice")
    monkeypatch.setattr(app_module, "_RECONNECT_POLL_INTERVAL_SECONDS", 0)
    called: list[object] = []

    async def fake_attempt(client, **kwargs):
        called.append(client)
        return None

    monkeypatch.setattr(transport_api, "attempt_saved_reconnect", fake_attempt)

    asyncio.run(runtime._run_reconnect_watcher())

    assert called == []  # nothing to retry, so the backend is never probed


def test_disconnected_notice_prefers_suggested_url() -> None:
    client = DisconnectedAgentClient(
        default_user_id="default",
        reconnect_api_url="http://127.0.0.1:8098",
        reconnect_api_key="tok",
        suggested_url="http://127.0.0.1:8000",
    )

    msg = _disconnected_notice_text(client, auto_reconnect=True)

    # A detected alternate backend wins over the auto-reconnect promise.
    assert "http://127.0.0.1:8000" in msg
    assert "/login http://127.0.0.1:8000" in msg
    assert "reconnecting automatically" not in msg


def test_disconnected_notice_auto_reconnect_without_suggestion() -> None:
    client = DisconnectedAgentClient(
        default_user_id="default",
        reconnect_api_url="http://127.0.0.1:8098",
        reconnect_api_key="tok",
    )

    msg = _disconnected_notice_text(client, auto_reconnect=True)

    assert "reconnecting automatically" in msg
    assert "8098" in msg


def test_start_reconnect_watcher_skips_when_not_reconnectable() -> None:
    async def go():
        cli_app, runtime = _make_app_and_runtime()
        cli_app._client = DisconnectedAgentClient(default_user_id="alice")
        runtime.start_reconnect_watcher()
        return runtime._reconnect_task

    assert asyncio.run(go()) is None


def test_start_reconnect_watcher_starts_when_reconnectable() -> None:
    async def go():
        cli_app, runtime = _make_app_and_runtime()
        cli_app._client = _reconnectable_placeholder()
        runtime.start_reconnect_watcher()
        task = runtime._reconnect_task
        await runtime.stop_reconnect_watcher_async()
        return task

    task = asyncio.run(go())
    assert task is not None
    assert task.cancelled()
