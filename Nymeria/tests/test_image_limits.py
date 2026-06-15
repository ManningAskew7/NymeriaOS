"""Tests for the per-thread image-window resolver."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.image_limits import (
    DEFAULT_IMAGE_WINDOW,
    get_effective_image_window_size,
    get_model_max_images,
)


class _FakeManager:
    def __init__(self, window):
        self._window = window

    def get_config(self, _thread_id):
        return SimpleNamespace(image_window_size=self._window)


def test_model_max_images_known_and_unknown():
    assert get_model_max_images("claude-opus-4-8") == 100
    assert get_model_max_images("gpt-5.5") == 1500
    assert get_model_max_images("some-unknown-local-model") == DEFAULT_IMAGE_WINDOW


def test_no_override_returns_model_max():
    mgr = _FakeManager(None)
    assert get_effective_image_window_size("t1", "claude-opus-4-8", thread_config_manager=mgr) == 100


def test_override_below_max_wins():
    mgr = _FakeManager(5)
    assert get_effective_image_window_size("t1", "claude-opus-4-8", thread_config_manager=mgr) == 5


def test_override_above_max_is_clamped_to_model_max():
    mgr = _FakeManager(999)
    assert get_effective_image_window_size("t1", "claude-opus-4-8", thread_config_manager=mgr) == 100


def test_invalid_override_falls_back_to_model_max():
    assert get_effective_image_window_size(
        "t1", "gpt-5.5", thread_config_manager=_FakeManager("nope")
    ) == 1500
    assert get_effective_image_window_size(
        "t1", "gpt-5.5", thread_config_manager=_FakeManager(0)
    ) == 1500
