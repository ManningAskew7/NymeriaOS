"""Best-effort usage tracking for thread-bound tools and skills."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import get_settings
from .time_utils import utc_now

logger = logging.getLogger(__name__)

_lock = threading.RLock()


@dataclass(frozen=True)
class CapabilityUsage:
    last_used_at: Optional[str] = None
    use_count: int = 0


class CapabilityUsageStore:
    """JSON-backed, best-effort capability usage index.

    This is intentionally small and thread-scoped. It is not an audit log; it
    only supports conservative cleanup suggestions and pruning.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (get_settings().data_dir / "capability_usage.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        user_id: str,
        thread_id: str,
        tools: Iterable[str] = (),
        skills: Iterable[str] = (),
    ) -> None:
        if not thread_id:
            return
        now = utc_now().isoformat()
        with _lock:
            data = self._read_locked()
            entry = self._thread_entry(data, user_id, thread_id)
            for kind, names in (("tools", tools), ("skills", skills)):
                bucket = entry.setdefault(kind, {})
                for raw_name in names:
                    name = str(raw_name or "").strip()
                    if not name:
                        continue
                    item = bucket.setdefault(name, {"use_count": 0})
                    item["last_used_at"] = now
                    item["use_count"] = int(item.get("use_count") or 0) + 1
            self._write_locked(data)

    def get_tool(self, *, user_id: str, thread_id: str, name: str) -> CapabilityUsage:
        return self._get(kind="tools", user_id=user_id, thread_id=thread_id, name=name)

    def get_skill(self, *, user_id: str, thread_id: str, name: str) -> CapabilityUsage:
        return self._get(kind="skills", user_id=user_id, thread_id=thread_id, name=name)

    def _get(self, *, kind: str, user_id: str, thread_id: str, name: str) -> CapabilityUsage:
        with _lock:
            data = self._read_locked()
        entry = (
            data.get(self._safe_key(user_id), {})
            .get(self._safe_key(thread_id), {})
            .get(kind, {})
            .get(name, {})
        )
        if not isinstance(entry, dict):
            return CapabilityUsage()
        return CapabilityUsage(
            last_used_at=str(entry.get("last_used_at") or "") or None,
            use_count=int(entry.get("use_count") or 0),
        )

    def _thread_entry(self, data: dict[str, Any], user_id: str, thread_id: str) -> dict[str, Any]:
        user_bucket = data.setdefault(self._safe_key(user_id), {})
        return user_bucket.setdefault(self._safe_key(thread_id), {})

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to read capability usage store", exc_info=True)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_locked(self, data: dict[str, Any]) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            logger.warning("Failed to write capability usage store", exc_info=True)

    @staticmethod
    def _safe_key(value: str) -> str:
        return str(value or "default")


def get_capability_usage_store() -> CapabilityUsageStore:
    return CapabilityUsageStore()


def record_capability_usage(
    *,
    user_id: str,
    thread_id: str,
    tools: Iterable[str] = (),
    skills: Iterable[str] = (),
) -> None:
    try:
        get_capability_usage_store().record(
            user_id=user_id,
            thread_id=thread_id,
            tools=tools,
            skills=skills,
        )
    except Exception:
        logger.debug("Capability usage recording failed", exc_info=True)


__all__ = [
    "CapabilityUsage",
    "CapabilityUsageStore",
    "get_capability_usage_store",
    "record_capability_usage",
]
