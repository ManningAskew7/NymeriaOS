"""Firebase Cloud Messaging (FCM) push notification support.

Sends data-only messages to registered devices (WearOS watch, etc.)
when autonomous tasks complete.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .storage_paths import StoreUnavailableError, write_text_atomic
from .store_repair import (
    UnavailableEpisodes,
    read_json,
    record_store_repair,
    repair_file,
)

logger = logging.getLogger(__name__)

# Lazy-initialized Firebase app
_firebase_app = None


def _init_firebase(credentials_path: str) -> bool:
    """Initialize Firebase Admin SDK. Returns True on success."""
    global _firebase_app
    if _firebase_app is not None:
        return True
    try:
        import firebase_admin
        from firebase_admin import credentials
        cred = credentials.Certificate(credentials_path)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("[FCM] Firebase Admin SDK initialized")
        return True
    except Exception as e:
        logger.error(f"[FCM] Firebase init failed: {e}")
        return False


def send_push(
    token: str,
    text: str,
    thread_id: str = "",
    task_id: str = "",
    summary: str = "",
) -> bool:
    """Send an FCM message with both notification (guaranteed display) and data payload (TTS if app alive).

    The notification ensures the user sees the message even if the app process is dead.
    The data payload allows the app to do TTS + audio playback if it's running.

    Returns True if sent successfully.
    """
    try:
        from firebase_admin import messaging

        # Truncate display text for notification body (keep it readable)
        display_text = summary if summary else text
        if len(display_text) > 200:
            display_text = display_text[:197] + "..."

        message = messaging.Message(
            # Notification payload: system displays this even if app is dead
            notification=messaging.Notification(
                title="Nymeria",
                body=display_text,
            ),
            # Data payload: app uses this for TTS if it's alive
            data={
                "text": text,
                "thread_id": thread_id,
                "task_id": task_id,
                "summary": summary,
            },
            token=token,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="nymeria_autonomous",
                    priority="high",
                ),
            ),
        )
        response = messaging.send(message)
        logger.info(f"[FCM] Sent push to {token[:20]}...: {response}")
        return True
    except Exception as e:
        logger.error(f"[FCM] Send failed for {token[:20]}...: {e}")
        return False


# ─── Token Storage ────────────────────────────────────────────────────────────

_TOKENS_FILE = "fcm_tokens.json"

# Serializes every load-mutate-save of the token file in this process (two
# concurrent registrations used to lose one) and the repair of a file that
# does not load. Re-entrant: the mutators hold it across ``_load``.
_tokens_lock = threading.RLock()

# One ERROR line per episode while the file is unreadable: every push reads it.
_unavailable = UnavailableEpisodes()


class PushTokensUnavailableError(StoreUnavailableError):
    """A token change was refused because ``fcm_tokens.json`` exists but could
    not be read or repaired (#401). Saving would replace every device's
    registration. The API answers it 503."""


def _get_tokens_path(data_dir: str) -> Path:
    return Path(data_dir) / _TOKENS_FILE


def _serialize(tokens: List[Dict]) -> str:
    return json.dumps(tokens, indent=2)


def _is_entry(entry: Any) -> bool:
    """A registration the push path can use: a non-empty string ``token`` and,
    when present, a ``thread_ids`` filter that is a list of strings (a string
    filter was substring-matched against thread ids)."""
    if not isinstance(entry, dict):
        return False
    token = entry.get("token")
    thread_ids = entry.get("thread_ids")
    filter_ok = thread_ids is None or (
        isinstance(thread_ids, list) and all(isinstance(t, str) for t in thread_ids)
    )
    return isinstance(token, str) and bool(token) and filter_ok


def _valid_tokens(read: Any) -> Optional[List[Dict]]:
    data = read.data if read.parsed else None
    if isinstance(data, list) and all(_is_entry(entry) for entry in data):
        return data
    return None


def _load(data_dir: str) -> Tuple[List[Dict], bool]:
    """The registered tokens, and whether the file may be written.

    A file that does not load (not JSON, not a list, an entry without a
    string ``token``) is never saved over (#401): the original bytes go to
    ``quarantine/`` in the data dir and the file is replaced with the entries
    that still load. A file that cannot be read, or whose original cannot be
    preserved, is served read-only: pushes still go to what could be read,
    and every change raises ``PushTokensUnavailableError``.
    """
    path = _get_tokens_path(data_dir)
    with _tokens_lock:
        read = read_json(path)
        if read.absent:
            _unavailable.end(data_dir)
            return [], True
        if read.unreadable:
            return [], _report_unavailable(data_dir, str(read.error))
        valid = _valid_tokens(read)
        if valid is not None:
            _unavailable.end(data_dir)
            return valid, True
        assert read.raw is not None
        data = read.data if read.parsed else None
        kept = [entry for entry in data if _is_entry(entry)] if isinstance(data, list) else []
        dropped = len(data) - len(kept) if isinstance(data, list) else None
        repaired = repair_file(
            path, read.raw, _serialize(kept), _valid_tokens, store="fcm_tokens", mode=0o600
        )
        if repaired.status == "superseded":
            _unavailable.end(data_dir)
            return repaired.fresh, True
        if repaired.status == "replaced" and repaired.quarantine is not None:
            _unavailable.end(data_dir)
            detail = (
                f"invalid device token file repaired: kept {len(kept)} "
                f"registration(s), dropped {dropped}"
                if dropped is not None
                else "corrupt device token file preserved; registrations start empty"
            )
            logger.warning("[FCM] %s (original at %s)", detail, repaired.quarantine)
            record_store_repair(
                "fcm_tokens", f"{detail} (original at quarantine/{repaired.quarantine.name})"
            )
            return kept, True
        return kept, _report_unavailable(
            data_dir, f"{read.error or 'invalid entries'}; could not be repaired"
        )


def _report_unavailable(data_dir: str, why: str) -> bool:
    """Log once per episode that the token file is read-only; returns False."""
    if _unavailable.start(data_dir):
        logger.error(
            "[FCM] Device token file is unavailable (%s); serving what could "
            "be read and refusing changes",
            why,
        )
    else:
        logger.debug("[FCM] Device token file still unavailable: %s", why)
    return False


def _writable_tokens(data_dir: str) -> List[Dict]:
    tokens, writable = _load(data_dir)
    if not writable:
        raise PushTokensUnavailableError(
            "The device token file could not be read or repaired; refusing to "
            "overwrite it"
        )
    return tokens


def load_tokens(data_dir: str) -> List[Dict]:
    """Load registered FCM tokens from JSON file."""
    return _load(data_dir)[0]


def save_tokens(data_dir: str, tokens: List[Dict]):
    """Save FCM tokens to JSON file, atomically.

    A bare write killed mid-way left a torn file that the next load could
    not read (#401). New files are created 0600: they map every device to its
    user (audit E8-02); an existing file keeps its mode.
    """
    path = _get_tokens_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _tokens_lock:
        write_text_atomic(path, _serialize(tokens), mode=0o600)


def register_token(
    data_dir: str,
    token: str,
    platform: str,
    user_id: str,
    thread_ids: Optional[List[str]] = None,
) -> bool:
    """Register a device token. Deduplicates by token value.

    thread_ids: if provided, the device only receives pushes for these threads.
    An empty list or None means the device receives all pushes.

    Returns True if new token was added, False if already exists.
    Raises ``PushTokensUnavailableError`` when the file cannot be loaded.
    """
    with _tokens_lock:
        tokens = _writable_tokens(data_dir)
        # Check for existing
        for t in tokens:
            if t.get("token") == token:
                t["platform"] = platform
                t["user_id"] = user_id
                if thread_ids is not None:
                    t["thread_ids"] = thread_ids
                save_tokens(data_dir, tokens)
                return False
        # Add new
        entry: Dict = {
            "token": token,
            "platform": platform,
            "user_id": user_id,
        }
        if thread_ids is not None:
            entry["thread_ids"] = thread_ids
        tokens.append(entry)
        save_tokens(data_dir, tokens)
    logger.info(f"[FCM] Registered new {platform} device (total: {len(tokens)})")
    return True


def unregister_token(data_dir: str, token: str) -> bool:
    """Remove a device token. Returns True if found and removed.
    Raises ``PushTokensUnavailableError`` when the file cannot be loaded."""
    with _tokens_lock:
        tokens = _writable_tokens(data_dir)
        original_count = len(tokens)
        tokens = [t for t in tokens if t.get("token") != token]
        if len(tokens) < original_count:
            save_tokens(data_dir, tokens)
            logger.info(f"[FCM] Unregistered device (remaining: {len(tokens)})")
            return True
    return False


def remove_thread_from_tokens(data_dir: str, thread_id: str) -> int:
    """
    Remove a deleted thread from device subscription filters.

    Device tokens themselves are account-level registrations and are not
    deleted here; only the thread-specific filter list is pruned.

    Returns:
        Number of token entries updated.

    Raises ``PushTokensUnavailableError`` when the file cannot be loaded.
    """
    with _tokens_lock:
        tokens = _writable_tokens(data_dir)
        updated = 0
        for entry in tokens:
            subscribed = entry.get("thread_ids")
            if isinstance(subscribed, list) and thread_id in subscribed:
                entry["thread_ids"] = [tid for tid in subscribed if tid != thread_id]
                updated += 1
        if updated:
            save_tokens(data_dir, tokens)
    if updated:
        logger.info("[FCM] Removed thread %s from %s device filter(s)", thread_id, updated)
    return updated


# ─── Broadcast ────────────────────────────────────────────────────────────────

def send_to_all_devices(
    data_dir: str,
    text: str,
    thread_id: str = "",
    task_id: str = "",
    summary: str = "",
    user_id: Optional[str] = None,
):
    """Send a push notification to matching registered devices.

    Filtering rules (all must pass):
    - user_id: if provided, only devices registered by that user
    - thread_ids: if the device registered with thread_ids, the push's
      thread_id must be in that list. Devices with no thread_ids get everything.
    """
    tokens = load_tokens(data_dir)
    if not tokens:
        return

    for entry in tokens:
        if user_id and entry.get("user_id") != user_id:
            continue
        subscribed = entry.get("thread_ids")
        if subscribed and thread_id and thread_id not in subscribed:
            logger.debug(f"[FCM] Skipping device (thread {thread_id} not in {subscribed})")
            continue
        token = entry.get("token")
        if token:
            send_push(
                token=token,
                text=text,
                thread_id=thread_id,
                task_id=task_id,
                summary=summary,
            )
