"""Per-channel Twitch chat log: the bot pushes, the API stores, a tool reads.

Twitch has no chat-history endpoint, so a chatter's record has to be kept
by us from the bot's own EventSub stream. The bot (a thin client) posts
batches to ``POST /twitch/chat-log``; this store writes one JSONL line per
message under the OWNING user's data dir, one file per channel per UTC day
(``users/<user>/twitch_chatlog/<channel>/<YYYY-MM-DD>.jsonl``), so age-based
retention is a directory listing and a query for "what has X said today"
touches one or two files. ``twitch_get_chatter_log`` reads it in-process
(tools run in the API process).

Chat text is untrusted public input: the fence helpers that the bot uses
for its prompts live here so the tool's rendering gets the same treatment
(the closing marker is neutralized inside the body, including separator and
zero-width tricks, so a chatter cannot end the fence early and continue as
trusted narration).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .storage_paths import safe_path_segment

logger = logging.getLogger(__name__)

#: Default retention for day files; the settings field mirrors it.
DEFAULT_RETENTION_DAYS = 14

#: Hard caps a query honours whatever the caller asks for.
MAX_QUERY_LIMIT = 200
MAX_QUERY_HOURS = 24 * 365

#: Lines per push. The request schema enforces it and the bot chunks to it,
#: so a queue that outgrew one batch during an outage drains instead of
#: being rejected whole forever.
CHATLOG_BATCH_MAX = 500

_UNTRUSTED_OPEN = "<untrusted_chat_messages>"
_UNTRUSTED_CLOSE = "</untrusted_chat_messages>"
_SEP = r"[\s\u200b-\u200f\u2060\ufeff]*"
_CLOSE_TAG_RE = re.compile(
    _SEP.join([r"<", r"/", *list("untrusted_chat_messages")]), re.IGNORECASE
)


def fence_chat(text: str) -> str:
    """Wrap chat-derived text so its provenance is unmistakable."""
    body = _CLOSE_TAG_RE.sub("<\\\\/untrusted_chat_messages", text)
    return f"{_UNTRUSTED_OPEN}\n{body}\n{_UNTRUSTED_CLOSE}"


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_entry(raw: Any, *, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """One pushed message as a storable line, or None when unusable.

    A line needs a login and text; a missing timestamp is stamped now (the
    bot always sends one, a hand-rolled client may not).
    """
    if not isinstance(raw, dict):
        return None
    login = str(raw.get("user_login") or "").strip().lstrip("@").lower()
    text = raw.get("text")
    if not login or not isinstance(text, str):
        return None
    when = _parse_timestamp(raw.get("timestamp")) or now or datetime.now(timezone.utc)
    badges = raw.get("badges")
    return {
        "message_id": str(raw.get("message_id") or ""),
        "user_login": login,
        "display_name": str(raw.get("display_name") or login),
        "user_id": str(raw.get("user_id") or ""),
        "text": " ".join(text.splitlines()),
        "timestamp": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "badges": [str(b) for b in badges] if isinstance(badges, list) else [],
    }


class ChatLogStore:
    """Append-only per-day JSONL files for one user's channels."""

    def __init__(self, data_dir: Path, user_id: str, *, retention_days: int = DEFAULT_RETENTION_DAYS):
        self._root = Path(data_dir) / "users" / safe_path_segment(user_id) / "twitch_chatlog"
        self._retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        # The bot retries a failed batch whole, so a redelivered id is normal.
        # Lazy import: core modules reach into triggers/ only at call time
        # (the trigger_manager / turn_executor precedent), never at import.
        from ..triggers.bot_helpers import SeenEventCache

        self._seen = SeenEventCache(ttl_seconds=6 * 3600, max_items=20000)

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @retention_days.setter
    def retention_days(self, value: int) -> None:
        self._retention_days = max(1, int(value))

    def _channel_dir(self, channel: str) -> Path:
        return self._root / safe_path_segment(channel.strip().lstrip("#").lower(), default="channel")

    @staticmethod
    def _day_name(timestamp: str) -> str:
        return timestamp[:10]

    def append(self, channel: str, messages: Iterable[Any]) -> tuple[int, int]:
        """Store ``messages``; returns (stored, dropped). Rotates afterwards."""
        stored = dropped = 0
        by_day: dict[str, list[str]] = {}
        keys: list[str] = []
        batch_seen: set[str] = set()
        for raw in messages:
            entry = normalize_entry(raw)
            if entry is None:
                dropped += 1
                continue
            key = f"{channel}:{entry['message_id']}" if entry["message_id"] else ""
            if key and (key in batch_seen or self._seen.was_seen(key)):
                dropped += 1
                continue
            if key:
                batch_seen.add(key)
                keys.append(key)
            by_day.setdefault(self._day_name(entry["timestamp"]), []).append(
                json.dumps(entry, ensure_ascii=False, sort_keys=True)
            )
            stored += 1
        if by_day:
            channel_dir = self._channel_dir(channel)
            with self._lock:
                channel_dir.mkdir(parents=True, exist_ok=True)
                for day, lines in by_day.items():
                    with (channel_dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                        fh.write("\n".join(lines) + "\n")
            # Only a written id counts as seen: a failed write (disk full,
            # read-only volume) must let the bot's retry land, not be
            # reported as "already stored".
            for key in keys:
                self._seen.mark_seen(key)
            self.rotate(channel)
        return stored, dropped

    def rotate(self, channel: str, *, now: Optional[datetime] = None) -> int:
        """Delete day files older than the retention; returns how many."""
        channel_dir = self._channel_dir(channel)
        if not channel_dir.is_dir():
            return 0
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=self._retention_days)).date()
        removed = 0
        for path in channel_dir.glob("*.jsonl"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    logger.warning("Could not remove expired chat log %s", path, exc_info=True)
        return removed

    def query(
        self,
        channel: str,
        *,
        login: str,
        limit: int = 50,
        hours: int = 24,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """That chatter's lines inside the window, newest first, at most ``limit``."""
        wanted = login.strip().lstrip("@").lower()
        if not wanted:
            return []
        limit = max(1, min(MAX_QUERY_LIMIT, int(limit)))
        hours = max(1, min(MAX_QUERY_HOURS, int(hours)))
        current = now or datetime.now(timezone.utc)
        since = current - timedelta(hours=hours)
        channel_dir = self._channel_dir(channel)
        if not channel_dir.is_dir():
            return []
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for path in sorted(channel_dir.glob("*.jsonl"), reverse=True):
            if path.stem < since.date().isoformat():
                break
            day_hits: list[dict[str, Any]] = []
            with self._lock:
                try:
                    raw_lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
            for line in reversed(raw_lines):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("user_login") != wanted and str(entry.get("display_name", "")).lower() != wanted:
                    continue
                when = _parse_timestamp(entry.get("timestamp"))
                if when is None or when < since or when > current + timedelta(minutes=5):
                    continue
                mid = str(entry.get("message_id") or "")
                if mid:
                    if mid in seen_ids:
                        continue  # a push acknowledged late and retried across an API restart
                    seen_ids.add(mid)
                day_hits.append(entry)
                if len(results) + len(day_hits) >= limit:
                    break
            results.extend(day_hits)
            if len(results) >= limit:
                break
        return results[:limit]


def render_chatter_log(login: str, entries: list[dict[str, Any]], *, hours: int) -> str:
    """The tool's view: a fenced, oldest-to-newest transcript with a header."""
    if not entries:
        return f"Nothing logged from {login} in the last {hours}h (the log only covers what the bot saw while running)."
    lines = []
    for entry in reversed(entries):
        when = _parse_timestamp(entry.get("timestamp"))
        stamp = when.strftime("%Y-%m-%d %H:%M") if when else "?"
        tag = f" [msg:{entry['message_id']}]" if entry.get("message_id") else ""
        lines.append(f"[{stamp}] {entry.get('display_name') or login}{tag}: {entry.get('text', '')}")
    header = f"Latest {len(entries)} message(s) from {login} in the last {hours}h, oldest first (UTC):"
    return header + "\n" + fence_chat("\n".join(lines))


_STORES: dict[tuple[str, str], ChatLogStore] = {}
_STORES_LOCK = threading.Lock()


def get_chat_log_store(data_dir: Path, user_id: str, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> ChatLogStore:
    """Process-wide store per (data_dir, user): one seen-cache per user."""
    key = (str(Path(data_dir).resolve()), user_id)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = ChatLogStore(data_dir, user_id, retention_days=retention_days)
            _STORES[key] = store
        else:
            store.retention_days = retention_days  # a settings change applies on the next call
        return store
