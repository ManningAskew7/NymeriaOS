"""Webhook trigger source -- fires when an HTTP POST hits the fire endpoint.

This is the MVP source.  Unlike poll-based sources, webhook events are
*pushed* via ``POST /triggers/fire/{trigger_id}`` and queued internally.
The ``check()`` method simply drains that queue.

Events are persisted to disk so they survive API restarts.
"""

import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import register_source
from .base import BaseTriggerSource
from ...core.storage_paths import write_text_atomic
from ...core.time_utils import utc_now

logger = logging.getLogger(__name__)


class WebhookSource(BaseTriggerSource):
    """External HTTP push source.

    Events arrive via the REST endpoint and are queued per-trigger.  The
    ticker (or direct-fire path) calls ``check()`` to drain the queue.
    """

    name = "webhook"
    description = "Fires when an HTTP POST is received at the trigger's unique URL"
    category = "custom"
    icon = "bolt"
    setup_guide = (
        "Webhook triggers fire when an external service sends an HTTP POST to "
        "your trigger's unique URL.\n\n"
        "1. Create the trigger and copy the fire URL shown after creation.\n"
        "2. Paste the URL into your external service, workflow tool, or local script.\n"
        "3. Send a JSON body — all keys become available as `{template_variables}` in your action template.\n\n"
        "Set a **shared secret** and include it as the `secret` query parameter."
    )
    template_variables = ["fired_at", "source_ip"]
    example_config = {"secret": "my-shared-secret"}

    config_schema: Dict[str, Any] = {
        "secret": {
            "type": "string",
            "description": "Shared secret required for public webhook fire requests",
            "required": True,
            "placeholder": "my-shared-secret",
            "secret": True,
            "order": 1,
        },
    }

    def __init__(self):
        super().__init__()
        self._queues: Dict[str, List[dict]] = {}
        self._lock = threading.Lock()
        self._queue_path: Path | None = None

    def on_register(self) -> None:
        self._load_persisted()

    def _get_queue_path(self) -> Path:
        if self._queue_path is None:
            from nymeria.config import get_settings
            settings = get_settings()
            self._queue_path = settings.data_dir / "triggers" / "_webhook_queue.json"
        return self._queue_path

    def _persist(self) -> None:
        try:
            path = self._get_queue_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, json.dumps(self._queues, default=str))
        except Exception as e:
            logger.warning(f"webhook source: failed to persist queue: {e}")

    def _load_persisted(self) -> None:
        try:
            path = self._get_queue_path()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._queues = data
                    total = sum(len(v) for v in self._queues.values())
                    if total:
                        logger.info(f"webhook source: recovered {total} persisted event(s)")
        except Exception as e:
            logger.warning(f"webhook source: failed to load persisted queue: {e}")

    def queue_event(self, trigger_id: str, event: dict) -> None:
        """Push an event into a trigger's queue (called by the API endpoint)."""
        with self._lock:
            self._queues.setdefault(trigger_id, []).append(event)
            self._persist()

    def check(self, config: dict, state: dict, user_id: str = "") -> List[dict]:
        """Drain queued events for the trigger identified in *state*."""
        trigger_id = state.get("trigger_id", "")
        with self._lock:
            events = self._queues.pop(trigger_id, [])
            if events:
                self._persist()
        return events

    def validate_config(self, config: dict) -> Tuple[bool, str]:
        ok, msg = super().validate_config(config)
        if not ok:
            return ok, msg
        configured_secret = config.get("secret")
        if not isinstance(configured_secret, str) or not configured_secret.strip():
            return False, "Field 'secret' must be a non-empty string"
        return True, "ok"

    def validate_secret(self, config: dict, provided_secret: str | None) -> bool:
        """Check the required shared secret for public fire requests."""
        expected = config.get("secret")
        if not isinstance(expected, str) or not expected:
            return False
        if not isinstance(provided_secret, str):
            return False
        return secrets.compare_digest(provided_secret, expected)

    def get_sample_event(self, config: dict) -> dict:
        return {
            "fired_at": utc_now().isoformat(),
            "source_ip": "192.168.1.1",
            "message": "Hello from webhook!",
            "status": "ok",
        }


# Auto-register on import
register_source("webhook", WebhookSource)
