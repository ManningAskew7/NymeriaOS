"""Best-effort usage tracking for thread-bound tools and skills."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Container, Iterable, NamedTuple, Optional

from ..config import get_settings
from .storage_paths import write_text_atomic
from .store_repair import UnavailableEpisodes, read_json, record_store_repair, repair_file
from .time_utils import ensure_aware_utc, parse_usage_timestamp, utc_now

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# Paths already reported unreadable or unrepairable: one WARNING per episode,
# since every tool call records usage.
_unavailable = UnavailableEpisodes()


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
            data, writable = self._load_locked()
            if not writable:
                return  # never save over a file that could not be preserved
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
        return self._load_locked()[0]

    def _load_locked(self) -> tuple[dict[str, Any], bool]:
        """The index, and whether the file may be written.

        A file that does not load (not JSON, not an object, or any bucket or
        usage record of the wrong shape) is never saved over (#401): its
        bytes go to ``quarantine/`` in the data dir and the file is replaced
        with every record of the right shape. A wrong-shaped record used to
        make ``record`` raise, silently ending usage tracking for that thread.
        Unreadable or not preservable: read as what could be kept, not written.
        """
        key = str(self.path)
        read = read_json(self.path)
        if read.absent:
            _unavailable.end(key)
            return {}, True
        if read.unreadable:
            if _unavailable.start(key):
                logger.warning("Capability usage store is unreadable: %s", read.error)
            return {}, False
        data = read.data if read.parsed else None
        kept = _salvaged(data)
        if kept == data:
            _unavailable.end(key)
            return kept, True
        assert read.raw is not None
        repaired = repair_file(
            self.path,
            read.raw,
            json.dumps(kept, indent=2, sort_keys=True),
            _valid_index,
            store="capability_usage",
        )
        if repaired.status == "superseded":
            _unavailable.end(key)
            return repaired.fresh, True
        if repaired.status == "replaced" and repaired.quarantine is not None:
            _unavailable.end(key)
            detail = (
                "invalid capability usage index repaired"
                if isinstance(data, dict)
                else "corrupt capability usage index preserved; usage starts empty"
            )
            logger.warning("%s (original at %s)", detail, repaired.quarantine)
            record_store_repair(
                "capability_usage",
                f"{detail} (original at quarantine/{repaired.quarantine.name})",
            )
            return kept, True
        if _unavailable.start(key):
            logger.warning(
                "Capability usage store did not load and could not be repaired; "
                "not recording usage over it"
            )
        return kept, False

    def _write_locked(self, data: dict[str, Any]) -> None:
        try:
            write_text_atomic(
                self.path, json.dumps(data, indent=2, sort_keys=True)
            )
        except Exception:
            logger.warning("Failed to write capability usage store", exc_info=True)

    @staticmethod
    def _safe_key(value: str) -> str:
        return str(value or "default")


def _usage_ok(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    count = item.get("use_count", 0)
    last = item.get("last_used_at")
    return (
        type(count) is int and count >= 0 and (last is None or isinstance(last, str))
    )


def _salvaged(data: Any) -> dict[str, Any]:
    """Every user bucket, thread bucket, capability kind and usage record of
    ``data`` that has its shape; the rest is dropped."""
    if not isinstance(data, dict):
        return {}
    kept: dict[str, Any] = {}
    for user, bucket in data.items():
        if not isinstance(bucket, dict):
            continue
        threads: dict[str, Any] = {}
        for thread, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            threads[thread] = {
                kind: {name: item for name, item in names.items() if _usage_ok(item)}
                for kind, names in entry.items()
                if isinstance(names, dict)
            }
        kept[user] = threads
    return kept


def _valid_index(read: Any) -> Optional[dict[str, Any]]:
    data = read.data if read.parsed else None
    return data if isinstance(data, dict) and _salvaged(data) == data else None


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


# ============================================================================
# Shared thread-config prune policy
# ============================================================================
#
# Both the tool prune (`tools/tool_search._prune_tools`) and the skill prune
# (`tools/search_skills._prune_thread_skills`) apply the same stale-window math
# and stale-by-usage detection. These helpers centralize that policy so the two
# prune surfaces cannot silently drift (e.g. a change to the age-gate rule).
# The removal-set construction and result envelopes stay per-surface: they are
# genuinely domain-divergent (temporary tools, default-bound vs global-enabled,
# distinct result keys/notes and serializers).


class PruneWindow(NamedTuple):
    """Resolved stale-window parameters for a thread-config prune."""

    stale_days: int
    min_age_days: int
    stale_cutoff: datetime
    config_old_enough: bool


def compute_prune_window(
    *,
    stale_after_days: int,
    min_enabled_age_days: int,
    config_updated_at: datetime,
    now: datetime,
) -> PruneWindow:
    """Resolve the shared prune-window policy (tools + skills).

    Centralizes the stale cutoff and the minimum-config-age gate so the two
    prune surfaces cannot drift. ``now`` is supplied by the caller so the same
    instant can be reused for any sibling time checks (in the tool prune, the
    temporary-tool expiry comparison).
    """
    stale_days = max(1, int(stale_after_days or 30))
    min_age_days = max(0, int(min_enabled_age_days or 0))
    stale_cutoff = now - timedelta(days=stale_days)
    config_old_enough = ensure_aware_utc(config_updated_at) <= now - timedelta(days=min_age_days)
    return PruneWindow(stale_days, min_age_days, stale_cutoff, config_old_enough)


def collect_stale_names(
    candidates: Iterable[str],
    *,
    skip: Container[str],
    get_last_used_at: Callable[[str], Optional[str]],
    stale_cutoff: datetime,
    config_old_enough: bool,
) -> list[str]:
    """Return candidates whose recorded usage predates ``stale_cutoff``.

    Shared stale-detection policy for thread-config pruning. Returns ``[]``
    unless the config is old enough; a candidate in ``skip`` is ignored; a
    candidate with no recorded usage (or an unparseable timestamp) is left
    intact. Iteration order of ``candidates`` is preserved.
    """
    if not config_old_enough:
        return []
    stale: list[str] = []
    for name in candidates:
        if name in skip:
            continue
        last_used = parse_usage_timestamp(get_last_used_at(name))
        if last_used is not None and last_used <= stale_cutoff:
            stale.append(name)
    return stale


__all__ = [
    "CapabilityUsage",
    "CapabilityUsageStore",
    "PruneWindow",
    "collect_stale_names",
    "compute_prune_window",
    "get_capability_usage_store",
    "record_capability_usage",
]
