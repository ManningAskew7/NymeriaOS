"""Tests for the global interactive-turn admission gate (backlog #83)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nymeria.core.interactive_admission import (
    ADMISSION_RETRY_AFTER_SECONDS,
    CAPACITY_DETAIL,
    InteractiveCapacityError,
    InteractiveTurnGate,
    admit_interactive_turn,
    get_interactive_turn_gate,
    interactive_admission_config,
    reset_interactive_turn_gate_for_tests,
)


@pytest.fixture(autouse=True)
def _fresh_gate():
    reset_interactive_turn_gate_for_tests()
    yield
    reset_interactive_turn_gate_for_tests()


# ---------------------------------------------------------------------------
# InteractiveTurnGate
# ---------------------------------------------------------------------------


def test_try_acquire_unlimited_always_admits_and_counts():
    gate = InteractiveTurnGate()
    slots = [gate.try_acquire(0) for _ in range(5)]
    assert all(slot is not None for slot in slots)
    assert gate.active == 5
    for slot in slots:
        slot.release()
    assert gate.active == 0


def test_try_acquire_sheds_at_limit():
    gate = InteractiveTurnGate()
    first = gate.try_acquire(1)
    assert first is not None
    assert gate.try_acquire(1) is None
    first.release()
    assert gate.try_acquire(1) is not None


def test_release_is_idempotent():
    gate = InteractiveTurnGate()
    slot = gate.try_acquire(1)
    assert slot is not None
    slot.release()
    slot.release()
    assert gate.active == 0


def test_acquire_immediate_shed_when_wait_zero():
    gate = InteractiveTurnGate()
    holder = gate.try_acquire(1)
    assert holder is not None

    async def _scenario():
        with pytest.raises(InteractiveCapacityError) as excinfo:
            await gate.acquire(1, 0.0)
        return excinfo.value

    exc = asyncio.run(_scenario())
    assert exc.active == 1
    assert exc.limit == 1
    assert exc.detail == CAPACITY_DETAIL
    assert exc.retry_after == ADMISSION_RETRY_AFTER_SECONDS
    assert gate.active == 1  # the shed did not disturb the count


def test_waiter_receives_slot_when_holder_releases():
    gate = InteractiveTurnGate()

    async def _scenario():
        holder = gate.try_acquire(1)
        assert holder is not None

        async def _waiter():
            slot = await gate.acquire(1, 5.0)
            return slot

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0)  # let the waiter enqueue
        holder.release()
        slot = await asyncio.wait_for(task, 2.0)
        assert gate.active == 1  # handoff transferred, not decremented
        slot.release()
        assert gate.active == 0

    asyncio.run(_scenario())


def test_waiters_are_fifo():
    gate = InteractiveTurnGate()

    async def _scenario():
        holder = gate.try_acquire(1)
        order: list[str] = []

        async def _waiter(name: str):
            slot = await gate.acquire(1, 5.0)
            order.append(name)
            slot.release()

        first = asyncio.create_task(_waiter("first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(_waiter("second"))
        await asyncio.sleep(0)
        holder.release()
        await asyncio.wait_for(asyncio.gather(first, second), 2.0)
        assert order == ["first", "second"]
        assert gate.active == 0

    asyncio.run(_scenario())


def test_waiter_times_out_and_sheds():
    gate = InteractiveTurnGate()

    async def _scenario():
        holder = gate.try_acquire(1)
        assert holder is not None
        with pytest.raises(InteractiveCapacityError):
            await gate.acquire(1, 0.05)
        # The timed-out waiter left no residue: a later release must not
        # hand a slot to it.
        holder.release()
        assert gate.active == 0
        assert gate.try_acquire(1) is not None

    asyncio.run(_scenario())


def test_cancelled_waiter_does_not_leak_a_slot():
    gate = InteractiveTurnGate()

    async def _scenario():
        holder = gate.try_acquire(1)

        async def _waiter():
            await gate.acquire(1, 5.0)

        task = asyncio.create_task(_waiter())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        holder.release()
        assert gate.active == 0
        assert gate.try_acquire(1) is not None

    asyncio.run(_scenario())


def test_lowered_limit_sheds_new_admissions_until_drain():
    gate = InteractiveTurnGate()
    a = gate.try_acquire(3)
    b = gate.try_acquire(3)
    assert a is not None and b is not None
    # Operator lowers the ceiling below the in-flight count: new admissions
    # shed, existing turns finish naturally.
    assert gate.try_acquire(1) is None
    a.release()
    b.release()
    assert gate.try_acquire(1) is not None


# ---------------------------------------------------------------------------
# interactive_admission_config
# ---------------------------------------------------------------------------


def test_config_reads_settings_fields():
    settings = SimpleNamespace(
        max_concurrent_interactive=4,
        interactive_admission_wait_seconds=2.5,
    )
    assert interactive_admission_config(settings) == (4, 2.5)


def test_config_degrades_on_missing_or_bad_fields():
    assert interactive_admission_config(SimpleNamespace()) == (0, 0.0)
    bad = SimpleNamespace(
        max_concurrent_interactive="nope",
        interactive_admission_wait_seconds=object(),
    )
    assert interactive_admission_config(bad) == (0, 0.0)


# ---------------------------------------------------------------------------
# admit_interactive_turn (exemptions + gate wiring)
# ---------------------------------------------------------------------------


def _agent(busy: bool = False):
    return SimpleNamespace(
        _thread_locks=SimpleNamespace(is_thread_busy=lambda _tid: busy)
    )


def _settings(limit: int = 1, wait: float = 0.0):
    return SimpleNamespace(
        max_concurrent_interactive=limit,
        interactive_admission_wait_seconds=wait,
    )


def test_admit_self_invoke_is_exempt():
    async def _scenario():
        # Saturate the gate first; the relay must still pass (slot None).
        held = get_interactive_turn_gate().try_acquire(1)
        assert held is not None
        slot = await admit_interactive_turn(
            _agent(), _settings(limit=1), "t1", is_self_invoke=True
        )
        assert slot is None
        held.release()

    asyncio.run(_scenario())


def test_admit_busy_thread_is_exempt():
    async def _scenario():
        held = get_interactive_turn_gate().try_acquire(1)
        assert held is not None
        slot = await admit_interactive_turn(
            _agent(busy=True), _settings(limit=1), "t1"
        )
        assert slot is None
        held.release()

    asyncio.run(_scenario())


def test_admit_idle_thread_takes_and_releases_a_slot():
    async def _scenario():
        slot = await admit_interactive_turn(_agent(), _settings(limit=1), "t1")
        assert slot is not None
        assert get_interactive_turn_gate().active == 1
        slot.release()
        assert get_interactive_turn_gate().active == 0

    asyncio.run(_scenario())


def test_admit_sheds_at_capacity():
    async def _scenario():
        first = await admit_interactive_turn(_agent(), _settings(limit=1), "t1")
        assert first is not None
        with pytest.raises(InteractiveCapacityError):
            await admit_interactive_turn(_agent(), _settings(limit=1), "t2")
        first.release()

    asyncio.run(_scenario())


def test_admit_counts_even_when_disabled():
    async def _scenario():
        slot = await admit_interactive_turn(_agent(), _settings(limit=0), "t1")
        assert slot is not None
        assert get_interactive_turn_gate().active == 1
        slot.release()

    asyncio.run(_scenario())


def test_admit_broken_busy_probe_fails_open():
    async def _scenario():
        agent = SimpleNamespace(
            _thread_locks=SimpleNamespace(
                is_thread_busy=lambda _tid: (_ for _ in ()).throw(RuntimeError())
            )
        )
        slot = await admit_interactive_turn(agent, _settings(limit=1), "t1")
        assert slot is not None
        slot.release()

    asyncio.run(_scenario())
