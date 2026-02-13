"""Webhook trigger source -- fires when an HTTP POST hits the fire endpoint.

This is the MVP source.  Unlike poll-based sources, webhook events are
*pushed* via ``POST /triggers/fire/{trigger_id}`` and queued internally.
The ``check()`` method simply drains that queue.
"""

import logging
import threading
from typing import Any, Dict, List

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)


class WebhookSource(BaseTriggerSource):
    """External HTTP push source.

    Events arrive via the REST endpoint and are queued per-trigger.  The
    ticker (or direct-fire path) calls ``check()`` to drain the queue.
    """

    name = "webhook"
    description = "Fires when an HTTP POST is received at /triggers/fire/{trigger_id}"
    config_schema: Dict[str, Any] = {
        "secret": {
            "type": "string",
            "description": "Optional shared secret for request validation",
            "required": False,
        },
    }

    def __init__(self):
        super().__init__()
        # trigger_id -> list of queued event dicts
        self._queues: Dict[str, List[dict]] = {}
        self._lock = threading.Lock()

    def queue_event(self, trigger_id: str, event: dict) -> None:
        """Push an event into a trigger's queue (called by the API endpoint)."""
        with self._lock:
            self._queues.setdefault(trigger_id, []).append(event)

    def check(self, config: dict, state: dict) -> List[dict]:
        """Drain queued events for the trigger identified in *state*.

        The TriggerManager sets ``state["trigger_id"]`` before calling.
        """
        trigger_id = state.get("trigger_id", "")
        with self._lock:
            events = self._queues.pop(trigger_id, [])
        return events

    def validate_secret(self, config: dict, provided_secret: str | None) -> bool:
        """Check the shared secret if one is configured."""
        expected = config.get("secret")
        if not expected:
            return True  # No secret configured -- allow all
        return provided_secret == expected


# Auto-register on import
register_source("webhook", WebhookSource)
