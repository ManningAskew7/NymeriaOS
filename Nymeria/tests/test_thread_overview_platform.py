"""Unit tests for the shared platform resolver and processing helper.

`resolve_display_platform` and `is_thread_processing` live in
`api/thread_overview.py` and are consumed by both the overview read model and
the thread-list/bot routers (via `threads._thread_list_platform`). The
end-to-end precedence is already pinned by `test_thread_list_recovery.py` and
`test_api_thread_overview.py`; these tests lock the shared helpers directly and
guard the cross-module import seams.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from nymeria.api.thread_overview import (
    is_thread_processing,
    resolve_display_platform,
)

_UNSET = object()


class _Meta:
    def __init__(self, platform: str):
        self.platform = platform


class _Config:
    def __init__(self, callable_: bool):
        self.callable = callable_


class _ConfigManager:
    def __init__(self, callable_: bool = False):
        self._config = _Config(callable_)

    def get_config(self, thread_id: str) -> _Config:
        return self._config


class _BindingRepo:
    """Reports a thread as bound to any provider in ``bound``."""

    def __init__(self, bound: Optional[set[str]] = None, raises: bool = False):
        self._bound = bound or set()
        self._raises = raises

    def lookup_thread_binding_by_thread(self, provider: str, thread_id: str) -> bool:
        if self._raises:
            raise RuntimeError("binding lookup boom")
        return provider in self._bound


class _Agent:
    def __init__(self, *, repo: Any = _UNSET, config_manager: Any = _UNSET):
        if repo is not _UNSET:
            self.chat_bindings_repo = repo
        if config_manager is not _UNSET:
            self.thread_config_manager = config_manager


class TestResolveDisplayPlatform:

    def test_binding_overrides_everything(self):
        agent = _Agent(repo=_BindingRepo(bound={"telegram"}))
        assert (
            resolve_display_platform(agent, "desktop-thread", meta=_Meta("desktop"))
            == "telegram"
        )

    def test_binding_resolution_order_first_match_wins(self):
        # slack precedes whatsapp in CHATAPP_BINDING_PLATFORMS, but telegram
        # precedes both, so telegram wins when all three are bound.
        agent = _Agent(repo=_BindingRepo(bound={"whatsapp", "slack", "telegram"}))
        assert resolve_display_platform(agent, "desktop-thread") == "telegram"

    def test_native_by_thread_id(self):
        agent = _Agent(repo=_BindingRepo())
        assert resolve_display_platform(agent, "telegram_123") == "telegram"

    def test_native_by_meta_platform_when_id_is_not_native(self):
        agent = _Agent(repo=_BindingRepo())
        assert (
            resolve_display_platform(agent, "desktop-thread", meta=_Meta("slack"))
            == "slack"
        )

    def test_callable_config_resolves_callable(self):
        agent = _Agent(
            repo=_BindingRepo(), config_manager=_ConfigManager(callable_=True)
        )
        assert resolve_display_platform(agent, "agent-my-agent-abc") == "callable"

    def test_callable_id_without_callable_config_demotes_to_desktop(self):
        agent = _Agent(
            repo=_BindingRepo(), config_manager=_ConfigManager(callable_=False)
        )
        assert resolve_display_platform(agent, "agent-my-agent-abc") == "desktop"

    def test_desktop_fallthrough(self):
        agent = _Agent(repo=_BindingRepo())
        assert resolve_display_platform(agent, "some-uuid") == "desktop"

    def test_falsy_meta_platform_resolves_to_desktop(self):
        # The unified resolver uses `meta.platform if meta else classify(...)`,
        # so an empty meta.platform on a non-native id resolves to desktop via
        # the final `platform or "desktop"` rather than re-deriving from the id.
        # (meta.platform is a defaulted non-empty str in practice; this locks
        # the unification intent.)
        agent = _Agent(repo=_BindingRepo())
        assert resolve_display_platform(agent, "some-uuid", meta=_Meta("")) == "desktop"

    def test_missing_binding_repo_falls_through(self):
        # getattr(agent, "chat_bindings_repo", None) is None -> skip binding loop.
        agent = _Agent()
        assert resolve_display_platform(agent, "telegram_123") == "telegram"

    def test_binding_lookup_exception_continues_to_native(self, caplog):
        agent = _Agent(repo=_BindingRepo(raises=True))
        with caplog.at_level("WARNING"):
            assert resolve_display_platform(agent, "slack_C1") == "slack"
        assert any("chat-app binding" in rec.message for rec in caplog.records)

    def test_missing_config_manager_is_not_callable(self):
        # agent has a repo but no thread_config_manager -> tc is None.
        agent = _Agent(repo=_BindingRepo())
        assert resolve_display_platform(agent, "agent-x") == "desktop"


class _Locks:
    def __init__(self, info: Any = None, raises: bool = False):
        self._info = info
        self._raises = raises

    def get_lock_info(self, thread_id: str) -> Any:
        if self._raises:
            raise RuntimeError("lock boom")
        return self._info


class _LockAgent:
    def __init__(self, locks: Any = _UNSET):
        if locks is not _UNSET:
            self._thread_locks = locks


class TestIsThreadProcessing:

    def test_processing_when_lock_info_present(self):
        agent = _LockAgent(locks=_Locks(info={"x": 1}))
        assert is_thread_processing(agent, "t1") is True

    def test_not_processing_when_lock_info_absent(self):
        agent = _LockAgent(locks=_Locks(info=None))
        assert is_thread_processing(agent, "t1") is False

    def test_missing_thread_locks_is_not_processing(self):
        assert is_thread_processing(_LockAgent(), "t1") is False

    def test_lock_inspection_error_is_not_processing(self, caplog):
        agent = _LockAgent(locks=_Locks(raises=True))
        with caplog.at_level("WARNING"):
            assert is_thread_processing(agent, "t1") is False
        assert any("processing state" in rec.message for rec in caplog.records)


class TestDelegationSeams:

    def test_threads_router_reuses_overview_processing_helper(self):
        from nymeria.api.routers import threads as threads_router

        assert threads_router.is_thread_processing is is_thread_processing

    @pytest.mark.parametrize(
        "thread_id, meta",
        [
            ("telegram_123", None),
            ("desktop-thread", _Meta("slack")),
            ("some-uuid", None),
        ],
    )
    def test_thread_list_platform_delegates_to_resolver(self, thread_id, meta):
        from nymeria.api.routers import threads as threads_router

        agent = _Agent(repo=_BindingRepo())
        assert threads_router._thread_list_platform(
            agent, thread_id, meta
        ) == resolve_display_platform(agent, thread_id, meta=meta)
