"""Tests for the per-thread claude_code override resolvers (model + mode)."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.claude_code_overrides import (
    get_effective_claude_code_mode,
    get_effective_claude_code_model,
)


class _FakeManager:
    def __init__(self, **config):
        self._config = SimpleNamespace(**config)

    def get_config(self, _thread_id):
        return self._config


class _RaisingManager:
    def get_config(self, _thread_id):
        raise RuntimeError("config backend down")


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


def test_model_no_override_returns_default():
    mgr = _FakeManager(claude_code_model=None)
    assert (
        get_effective_claude_code_model("t1", "default-model", thread_config_manager=mgr)
        == "default-model"
    )


def test_model_override_wins():
    mgr = _FakeManager(claude_code_model="claude-opus-4-8")
    assert (
        get_effective_claude_code_model("t1", "default-model", thread_config_manager=mgr)
        == "claude-opus-4-8"
    )


def test_model_blank_override_falls_back_to_default():
    mgr = _FakeManager(claude_code_model="   ")
    assert (
        get_effective_claude_code_model("t1", "default-model", thread_config_manager=mgr)
        == "default-model"
    )


def test_model_no_manager_returns_default(monkeypatch):
    # With no injected manager and no current agent, resolution falls back.
    import nymeria.core.agent as agent_module

    monkeypatch.setattr(agent_module, "get_current_agent", lambda: None)
    assert (
        get_effective_claude_code_model("t1", "default-model", thread_config_manager=None)
        == "default-model"
    )


def test_model_no_thread_id_returns_default():
    mgr = _FakeManager(claude_code_model="claude-opus-4-8")
    assert (
        get_effective_claude_code_model(None, "default-model", thread_config_manager=mgr)
        == "default-model"
    )


def test_model_default_may_be_none():
    mgr = _FakeManager(claude_code_model=None)
    assert (
        get_effective_claude_code_model("t1", None, thread_config_manager=mgr) is None
    )


def test_model_config_lookup_error_falls_back_to_default():
    assert (
        get_effective_claude_code_model(
            "t1", "default-model", thread_config_manager=_RaisingManager()
        )
        == "default-model"
    )


# --------------------------------------------------------------------------- #
# mode
# --------------------------------------------------------------------------- #


def test_mode_no_override_returns_default():
    mgr = _FakeManager(claude_code_mode=None)
    assert (
        get_effective_claude_code_mode("t1", "dontAsk", thread_config_manager=mgr)
        == "dontAsk"
    )


def test_mode_override_wins():
    mgr = _FakeManager(claude_code_mode="plan")
    assert (
        get_effective_claude_code_mode("t1", "dontAsk", thread_config_manager=mgr)
        == "plan"
    )


def test_mode_blank_override_falls_back_to_default():
    mgr = _FakeManager(claude_code_mode="")
    assert (
        get_effective_claude_code_mode("t1", "dontAsk", thread_config_manager=mgr)
        == "dontAsk"
    )


def test_mode_no_thread_id_returns_default():
    mgr = _FakeManager(claude_code_mode="plan")
    assert (
        get_effective_claude_code_mode(None, "dontAsk", thread_config_manager=mgr)
        == "dontAsk"
    )
