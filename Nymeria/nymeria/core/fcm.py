"""Firebase Cloud Messaging (FCM) push notification support.

Sends data-only messages to registered devices (WearOS watch, etc.)
when autonomous tasks complete.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

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


def _get_tokens_path(data_dir: str) -> Path:
    return Path(data_dir) / _TOKENS_FILE


def load_tokens(data_dir: str) -> List[Dict]:
    """Load registered FCM tokens from JSON file."""
    path = _get_tokens_path(data_dir)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[FCM] Failed to load tokens: {e}")
        return []


def save_tokens(data_dir: str, tokens: List[Dict]):
    """Save FCM tokens to JSON file."""
    path = _get_tokens_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def register_token(data_dir: str, token: str, platform: str, user_id: str) -> bool:
    """Register a device token. Deduplicates by token value.

    Returns True if new token was added, False if already exists.
    """
    tokens = load_tokens(data_dir)
    # Check for existing
    for t in tokens:
        if t.get("token") == token:
            # Update metadata
            t["platform"] = platform
            t["user_id"] = user_id
            save_tokens(data_dir, tokens)
            return False
    # Add new
    tokens.append({
        "token": token,
        "platform": platform,
        "user_id": user_id,
    })
    save_tokens(data_dir, tokens)
    logger.info(f"[FCM] Registered new {platform} device (total: {len(tokens)})")
    return True


def unregister_token(data_dir: str, token: str) -> bool:
    """Remove a device token. Returns True if found and removed."""
    tokens = load_tokens(data_dir)
    original_count = len(tokens)
    tokens = [t for t in tokens if t.get("token") != token]
    if len(tokens) < original_count:
        save_tokens(data_dir, tokens)
        logger.info(f"[FCM] Unregistered device (remaining: {len(tokens)})")
        return True
    return False


# ─── Broadcast ────────────────────────────────────────────────────────────────

def send_to_all_devices(
    data_dir: str,
    text: str,
    thread_id: str = "",
    task_id: str = "",
    summary: str = "",
    user_id: Optional[str] = None,
):
    """Send a push notification to all registered devices.

    If user_id is provided, only sends to devices registered by that user.
    """
    tokens = load_tokens(data_dir)
    if not tokens:
        return

    for entry in tokens:
        if user_id and entry.get("user_id") != user_id:
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
