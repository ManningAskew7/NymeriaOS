"""Persisted scheduler lifecycle state for on/off slim runtimes."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "last_started_at": None,
        "last_clean_shutdown_at": None,
        "last_missed_detection_at": None,
        "pending_missed_todo_ids": [],
        "trigger_catchup_paused": False,
    }


class SchedulerStateManager:
    """Read and write the scheduler's small JSON lifecycle state file."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "scheduler_state.json"
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        state = _default_state()
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
        except Exception as exc:
            logger.warning("Failed to read scheduler state %s: %s", self.path, exc)
        state["pending_missed_todo_ids"] = [
            str(todo_id)
            for todo_id in state.get("pending_missed_todo_ids") or []
            if todo_id
        ]
        state["trigger_catchup_paused"] = bool(state.get("trigger_catchup_paused"))
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temp.replace(self.path)
        except Exception as exc:
            logger.warning("Failed to write scheduler state %s: %s", self.path, exc)

    def record_start(self) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            state["last_started_at"] = _now_iso()
            self._save_unlocked(state)
            return state

    def record_clean_shutdown(self) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            state["last_clean_shutdown_at"] = _now_iso()
            self._save_unlocked(state)
            return state

    def set_pending_missed(
        self,
        todo_ids: list[str],
        *,
        trigger_catchup_paused: bool,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            state["last_missed_detection_at"] = _now_iso()
            state["pending_missed_todo_ids"] = list(dict.fromkeys(todo_ids))
            state["trigger_catchup_paused"] = trigger_catchup_paused
            self._save_unlocked(state)
            return state

    def clear_pending_missed(self) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            state["pending_missed_todo_ids"] = []
            state["trigger_catchup_paused"] = False
            self._save_unlocked(state)
            return state
