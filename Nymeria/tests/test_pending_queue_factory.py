"""Verify the create_pending_queue factory and protocol surface."""

from __future__ import annotations

from nymeria.core.pending_prompt_queue import (
    InMemoryPendingPromptQueue,
    PendingPromptQueueBackend,
    create_pending_queue,
)


def test_factory_returns_in_memory_for_v1():
    backend = create_pending_queue(None)
    assert isinstance(backend, InMemoryPendingPromptQueue)


def test_in_memory_backend_implements_protocol_surface():
    backend = create_pending_queue(None)
    # The protocol declares these methods; they must all exist on the
    # concrete implementation. This pins the surface so a Redis v2
    # backend doesn't drift.
    expected = (
        "enqueue",
        "drain",
        "peek",
        "size",
        "clear",
        "mark_halt_observed",
        "consume_halt_observation",
        "begin_release",
        "end_release",
        "is_releasing",
    )
    for name in expected:
        assert hasattr(backend, name), f"backend missing {name}"
        assert callable(getattr(backend, name))


def test_in_memory_backend_is_a_runtime_substitutable_for_protocol():
    """The protocol is structural; instance-check requires
    @runtime_checkable. We don't decorate the protocol that way (it's
    a Protocol, not isinstance-checkable). Smoke-test that the
    declared protocol class itself is importable and that the methods
    line up structurally with the implementation."""
    impl = create_pending_queue(None)
    proto_methods = {
        name for name in dir(PendingPromptQueueBackend) if not name.startswith("_")
    }
    impl_methods = {name for name in dir(impl) if not name.startswith("_")}
    missing = proto_methods - impl_methods
    assert not missing, f"impl missing protocol methods: {missing}"


def test_factory_accepts_optional_settings_argument():
    """v2 will inspect settings; v1 ignores. Make sure passing settings
    doesn't break."""

    class _Stub:
        pass

    backend = create_pending_queue(_Stub())
    assert isinstance(backend, InMemoryPendingPromptQueue)
