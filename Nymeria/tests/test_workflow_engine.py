"""Phase 1 engine tests: pump, runner, executor, budget, envelope, kill.

These drive real subprocesses over the real socket transport (the whole point
of phase 1 is that seam), with a test verb registered per test so no agent
runtime is required. Budgets are pinned small to keep the suite fast.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from nymeria.core.workflows import (
    WorkflowBudget,
    execute_workflow,
    register_verb,
)
from nymeria.core.workflows.registry import _REGISTRY, VerbError

pytestmark = pytest.mark.asyncio

WALL = 30.0  # generous outer cap; individual tests finish far sooner


@pytest.fixture(autouse=True)
def _clean_test_verbs():
    """Snapshot and restore the verb registry around every test."""
    before = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(before)


def _register_echo():
    @register_verb("test.echo", positional=("value",))
    async def _echo(ctx, verb, args):
        return {"echo": args.get("value"), "user": ctx.user_id}

    return _echo


async def _run(source: str, *, budget: WorkflowBudget | None = None, params=None):
    return await execute_workflow(
        source=source,
        entrypoint="run",
        params=params or {},
        user_id="tester",
        thread_id="wf-test-thread",
        budget=budget or WorkflowBudget(wall_clock_seconds=WALL),
        persist_record=False,
    )


async def test_round_trip_ok_envelope_and_trace():
    _register_echo()
    source = (
        "@workflow\n"
        "def run(name: str = 'world'):\n"
        "    first = nym.test.echo('hi')\n"
        "    second = nym.test.echo(value=name)\n"
        "    return {'first': first, 'second': second}\n"
    )
    result = await _run(source, params={"name": "nymeria"})
    env = result.envelope
    assert env.ok is True and env.status == "ok"
    assert env.output == {
        "first": {"echo": "hi", "user": "tester"},
        "second": {"echo": "nymeria", "user": "tester"},
    }
    assert env.budget["calls_used"] == 2
    assert [s.verb for s in result.trace.steps] == ["test.echo", "test.echo"]
    assert all(s.status == "ok" for s in result.trace.steps)


async def test_author_error_returns_traceback():
    source = "def run():\n    raise ValueError('boom at step zero')\n"
    result = await _run(source)
    env = result.envelope
    assert env.ok is False and env.status == "error"
    assert env.error is not None
    assert env.error.kind == "author_error"
    assert "boom at step zero" in env.error.message
    assert "ValueError" in (env.error.traceback or "")


async def test_budget_exceeded_keeps_taxonomy_when_uncaught():
    _register_echo()
    source = (
        "def run():\n"
        "    for i in range(10):\n"
        "        nym.test.echo(value=i)\n"
    )
    budget = WorkflowBudget(wall_clock_seconds=WALL, max_calls=3)
    result = await _run(source, budget=budget)
    env = result.envelope
    assert env.status == "error"
    assert env.error is not None and env.error.kind == "budget_exceeded"
    # Three charged calls succeeded; the fourth was refused before dispatch.
    assert env.budget["calls_used"] == 3


async def test_author_can_catch_verb_errors_and_continue():
    _register_echo()
    source = (
        "def run():\n"
        "    try:\n"
        "        nym.no.such.verb(x=1)\n"
        "    except Exception as exc:\n"
        "        caught = type(exc).__name__\n"
        "    return {'caught': caught, 'after': nym.test.echo(value='ok')['echo']}\n"
    )
    result = await _run(source)
    env = result.envelope
    assert env.status == "ok"
    assert env.output == {"caught": "NymVerbError", "after": "ok"}


async def test_unknown_verb_fails_fast_child_side():
    _register_echo()
    source = "def run():\n    return nym.definitely.unregistered()\n"
    result = await _run(source)
    env = result.envelope
    assert env.status == "error"
    assert env.error is not None and env.error.kind == "verb_error"
    assert "unknown verb" in env.error.message
    # Fail-fast means no frame was sent: nothing charged, nothing traced.
    assert env.budget["calls_used"] == 0


async def test_wall_clock_timeout_kills_child():
    source = "import time\ndef run():\n    time.sleep(60)\n"
    result = await _run(
        source, budget=WorkflowBudget(wall_clock_seconds=2.0)
    )
    env = result.envelope
    assert env.status == "timeout"
    assert env.ok is False
    assert env.error is not None
    assert "wall-clock" in env.error.message


async def test_cancellation_kills_process_group():
    _register_echo()

    @register_verb("test.block")
    async def _block(ctx, verb, args):
        await asyncio.sleep(60)

    source = "def run():\n    nym.test.block()\n"
    task = asyncio.create_task(_run(source))
    await asyncio.sleep(1.5)  # let the child spawn and block inside the verb
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # No straightforward pid handle out here; the invariant we can assert is
    # that cancellation propagated instead of hanging until the verb's 60s.


async def test_stdout_is_log_capture_not_protocol():
    _register_echo()
    source = (
        "def run():\n"
        "    print('hello from author code')\n"
        "    return nym.test.echo(value='clean')['echo']\n"
    )
    result = await _run(source)
    assert result.envelope.status == "ok"
    assert result.envelope.output == "clean"
    assert "hello from author code" in result.trace.stdout


async def test_verb_handler_exception_is_verb_error():
    @register_verb("test.broken")
    async def _broken(ctx, verb, args):
        raise RuntimeError("handler blew up")

    source = "def run():\n    nym.test.broken()\n"
    result = await _run(source)
    env = result.envelope
    assert env.status == "error"
    assert env.error is not None and env.error.kind == "verb_error"
    assert "handler blew up" in env.error.message


async def test_verb_error_kind_propagates():
    @register_verb("test.gated")
    async def _gated(ctx, verb, args):
        raise VerbError("no entry", kind="verb_error")

    source = "def run():\n    nym.test.gated()\n"
    result = await _run(source)
    assert result.envelope.error is not None
    assert result.envelope.error.kind == "verb_error"
    assert "no entry" in result.envelope.error.message


async def test_pump_rejects_bad_token():
    """A connection with a wrong hello token is closed before dispatch."""
    from nymeria.core.workflows.budget import BudgetUsage
    from nymeria.core.workflows.pump import VerbPump
    from nymeria.core.workflows.registry import VerbContext
    from nymeria.core.workflows.trace import StepTrace

    ctx = VerbContext(
        user_id="tester",
        thread_id="t",
        run_id="r",
        budget=WorkflowBudget(),
        usage=BudgetUsage(),
    )
    pump = VerbPump(
        ctx=ctx, trace=StepTrace(run_id="r", workflow_id="adhoc"), token="right"
    )
    server = await asyncio.start_server(pump.serve, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            json.dumps({"t": "hello", "token": "wrong"}).encode() + b"\n"
        )
        await writer.drain()
        assert await reader.readline() == b""  # closed without a welcome
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert pump.auth_failures == 1
    assert pump.finish is None


async def test_entrypoint_missing_is_author_error():
    source = "def other():\n    return 1\n"
    result = await _run(source)
    assert result.envelope.status == "error"
    assert result.envelope.error is not None
    assert result.envelope.error.kind == "author_error"
    assert "entrypoint" in result.envelope.error.message


async def test_positional_args_map_via_welcome_metadata():
    _register_echo()
    source = "def run():\n    return nym.test.echo('positional')['echo']\n"
    result = await _run(source)
    assert result.envelope.status == "ok"
    assert result.envelope.output == "positional"


async def test_run_record_persisted_and_pruned(tmp_path, monkeypatch):
    from nymeria.core.workflows import trace as trace_mod

    class _S:
        data_dir = tmp_path

    monkeypatch.setattr(
        "nymeria.config.get_settings", lambda: _S(), raising=True
    )
    _register_echo()
    monkeypatch.setattr(trace_mod, "MAX_RUN_RECORDS_PER_WORKFLOW", 2)
    source = "def run():\n    return nym.test.echo(value='x')['echo']\n"
    for _ in range(3):
        result = await execute_workflow(
            source=source,
            user_id="tester",
            thread_id="t",
            workflow_id="wf-keep",
            budget=WorkflowBudget(wall_clock_seconds=WALL),
            persist_record=True,
        )
        assert result.envelope.status == "ok"
    runs_dir = tmp_path / "workflows" / "runs" / "wf-keep"
    records = list(runs_dir.glob("*.json"))
    assert len(records) == 2
    payload = json.loads(records[0].read_text())
    assert payload["envelope"]["status"] == "ok"
    assert payload["trace"]["steps"][0]["verb"] == "test.echo"


async def test_child_env_is_scrubbed():
    marker = "NYMERIA_WF_SECRET_MARKER"
    os.environ[marker] = "should-not-leak"
    try:

        @register_verb("test.env")
        async def _env(ctx, verb, args):
            return None

        source = (
            "import os\n"
            "def run():\n"
            f"    return {{'leak': os.environ.get('{marker}'), "
            "'run': os.environ.get('NYMERIA_WORKFLOW_RUN_ID', '') != ''}\n"
        )
        result = await _run(source)
        assert result.envelope.status == "ok"
        assert result.envelope.output == {"leak": None, "run": True}
    finally:
        os.environ.pop(marker, None)


async def test_result_cap_truncates_large_strings():
    @register_verb("test.big")
    async def _big(ctx, verb, args):
        return "x" * 5000

    source = "def run():\n    return len(nym.test.big())\n"
    budget = WorkflowBudget(wall_clock_seconds=WALL, result_cap_chars=1000)
    result = await _run(source, budget=budget)
    assert result.envelope.status == "ok"
    assert result.envelope.output < 1100  # truncated, marker included


async def test_result_cap_truncates_large_structured_results():
    @register_verb("test.bigdict")
    async def _bigdict(ctx, verb, args):
        return {"rows": ["y" * 100 for _ in range(200)]}

    source = (
        "def run():\n"
        "    r = nym.test.bigdict()\n"
        "    return {'truncated': isinstance(r, dict) and r.get('_truncated')}\n"
    )
    budget = WorkflowBudget(wall_clock_seconds=WALL, result_cap_chars=1000)
    result = await _run(source, budget=budget)
    assert result.envelope.status == "ok"
    assert result.envelope.output == {"truncated": True}


async def test_per_verb_timeout_is_verb_error():
    @register_verb("test.slowverb")
    async def _slow(ctx, verb, args):
        await asyncio.sleep(30)

    source = "def run():\n    nym.test.slowverb()\n"
    budget = WorkflowBudget(wall_clock_seconds=WALL, verb_timeout_seconds=1.0)
    result = await _run(source, budget=budget)
    env = result.envelope
    assert env.status == "error"
    assert env.error is not None and env.error.kind == "verb_error"
    assert "timed out" in env.error.message


async def test_second_connection_is_rejected():
    """The single-child invariant: only one connection is ever served."""
    from nymeria.core.workflows.budget import BudgetUsage
    from nymeria.core.workflows.pump import VerbPump
    from nymeria.core.workflows.registry import VerbContext
    from nymeria.core.workflows.trace import StepTrace

    ctx = VerbContext(
        user_id="tester", thread_id="t", run_id="r",
        budget=WorkflowBudget(), usage=BudgetUsage(),
    )
    pump = VerbPump(
        ctx=ctx, trace=StepTrace(run_id="r", workflow_id="adhoc"), token="tok"
    )
    server = await asyncio.start_server(pump.serve, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        # First connection authenticates and gets a welcome.
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        w1.write(json.dumps({"t": "hello", "token": "tok"}).encode() + b"\n")
        await w1.drain()
        welcome = json.loads(await r1.readline())
        assert welcome["t"] == "welcome"
        # Second connection, correct token, is still refused (already accepted).
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(json.dumps({"t": "hello", "token": "tok"}).encode() + b"\n")
        await w2.drain()
        assert await r2.readline() == b""
        w1.close()
        w2.close()
    finally:
        pump.abort()
        server.close()
        await server.wait_closed()
    assert pump.auth_failures == 1


async def test_grandchild_does_not_pin_run_to_wall_clock():
    """A backgrounded grandchild holding fd 1 must not delay a finished run.

    Regression for the communicate()-EOF completion bug: completion is the
    child's exit, not pipe EOF, so this returns in ~1s not the wall clock.
    """
    _register_echo()
    source = (
        "import subprocess, sys\n"
        "def run():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "    return nym.test.echo(value='done')['echo']\n"
    )
    result = await _run(
        source, budget=WorkflowBudget(wall_clock_seconds=WALL)
    )
    assert result.envelope.status == "ok"
    assert result.envelope.output == "done"
    # Well under the 30s grandchild sleep and the WALL cap.
    assert result.envelope.budget["wall_seconds"] < 15
