"""Per-thread image-window resolution (newest-N images kept in context).

The sibling of ``memory_limits`` for the image sliding window: resolve the
per-thread ``image_window_size`` override, else the model's
``max_images_per_request``, clamped to that model max as a hard ceiling. The
``thread_config_manager`` is injectable (mirroring ``memory_limits``) so the
resolver is unit-testable without the agent singleton.
"""

from __future__ import annotations

from typing import Any

from ..config.model_capabilities import get_attachment_limits

# Fallback window when the model's max-images cap is unknown.
DEFAULT_IMAGE_WINDOW = 16


def get_model_max_images(model: str) -> int:
    """Return the model's max images per request, or the conservative default."""
    cap = get_attachment_limits(model or "").get("max_images_per_request")
    if isinstance(cap, int) and cap >= 1:
        return cap
    return DEFAULT_IMAGE_WINDOW


def get_effective_image_window_size(
    thread_id: str,
    model: str,
    *,
    thread_config_manager: Any | None = None,
) -> int:
    """Resolve the max images kept in context for a thread.

    Per-thread ``image_window_size`` override if set, else the model's
    ``max_images_per_request``. The model max is the hard ceiling, so an override
    is clamped to ``[1, model_max]``.
    """
    model_max = get_model_max_images(model)
    manager = thread_config_manager
    if manager is None:
        try:
            from .agent import get_current_agent

            agent = get_current_agent()
            manager = getattr(agent, "thread_config_manager", None) if agent else None
        except Exception:  # noqa: BLE001
            manager = None

    if manager is None or not thread_id:
        return model_max

    try:
        tc = manager.get_config(thread_id)
    except Exception:  # noqa: BLE001 - never let config lookup break a turn
        return model_max
    override = getattr(tc, "image_window_size", None) if tc else None
    if override is None:
        return model_max
    try:
        ival = int(override)
    except (TypeError, ValueError):
        return model_max
    if ival < 1:
        return model_max
    return min(ival, model_max)
