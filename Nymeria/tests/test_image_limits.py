"""Tests for the per-model image limits (window size and pixel ceiling)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nymeria.core import image_limits
from nymeria.core.image_limits import (
    DEFAULT_IMAGE_WINDOW,
    get_effective_image_window_size,
    get_model_max_image_dimension,
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


def test_shipped_pixel_ceiling_is_the_many_image_limit():
    # 2000px is the ceiling Anthropic enforces once a request carries many
    # images, and the window's default IS the model's max-images cap, so no
    # replayed image may assume the roomier small-request ceiling.
    assert get_model_max_image_dimension("claude-opus-4-8") == 2000
    assert get_model_max_image_dimension("gpt-5.5") == 2000
    assert get_model_max_image_dimension("some-unknown-local-model") == 2000


def test_pixel_ceiling_follows_the_model_limit(monkeypatch):
    monkeypatch.setattr(
        image_limits, "get_attachment_limits", lambda _model: {"max_image_dimension": 8000}
    )
    assert get_model_max_image_dimension("claude-opus-4-8") == 8000


@pytest.mark.parametrize(
    "limits",
    [{}, {"max_image_dimension": None}, {"max_image_dimension": 0}, {"max_image_dimension": "big"}],
)
def test_unusable_pixel_ceiling_falls_back_to_the_safe_default(monkeypatch, limits):
    monkeypatch.setattr(image_limits, "get_attachment_limits", lambda _model: limits)
    assert get_model_max_image_dimension("m") == image_limits.DEFAULT_MAX_IMAGE_DIMENSION == 2000


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
