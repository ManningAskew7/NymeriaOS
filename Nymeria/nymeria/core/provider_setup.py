"""In-flight ``/provider setup`` state: per-user, in-process, TTL-bounded.

The chained provider-configure flow is a sequence of stateless slash
commands, and the pasted API key must NEVER ride a form payload
(``CommandResult.data`` ships to every frontend), so the in-flight state
between steps lives here instead: a small per-user store in the API process
(the single agent runtime, so in-process is authoritative; same idiom as
``ui_prompt_coordinator``/``hook_approvals``). Secrets stay in memory only,
are redacted from ``repr``, and expire with the record after
``PENDING_SETUP_TTL_SECONDS`` or an explicit cancel/apply.
"""

from __future__ import annotations

import threading
import time
import unicodedata
from dataclasses import dataclass, field

PENDING_SETUP_TTL_SECONDS = 600.0

# Bracketed-paste guard sequences some terminals leak into pasted values.
_PASTE_MARKERS = ("\x1b[200~", "\x1b[201~")


@dataclass
class PendingProviderSetup:
    """One user's in-flight provider configuration.

    ``key_choice`` tracks the key step outcome: ``""`` undecided, ``paste``
    (``api_key`` holds the new secret), ``keep`` (existing server key stays),
    ``clear`` (stored key removed on apply), ``none`` (provider needs no
    key). ``base_url``/``api_mode``/``model`` are ``None`` until their step
    decides them; ``base_url == ""`` means "clear to the provider default".
    """

    provider: str
    api_key: str = ""
    key_choice: str = ""
    base_url: str | None = None
    api_mode: str | None = None
    model: str | None = None
    # Step-rail rendering state (the chain form re-renders every reached
    # step as a tab): entry-mode flags for the typed sub-steps, the server
    # key env vars discovered at start, and the cached model list so
    # revisiting a tab never refetches with unchanged credentials.
    key_entry: bool = False
    base_url_custom: bool = False
    model_custom: bool = False
    server_key_envs: tuple[str, ...] = ()
    # Settings fields the update model can actually patch to "" for a key
    # CLEAR; empty means clearing is not supported for this provider from
    # the settings surface (the option is then not offered).
    server_key_fields: tuple[str, ...] = ()
    model_options: list[dict[str, object]] | None = None
    models_fingerprint: str = ""
    models_note: str = ""
    expires_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = time.monotonic() + PENDING_SETUP_TTL_SECONDS

    def __repr__(self) -> str:  # pragma: no cover - defensive logging shape
        key_state = "set" if self.api_key else "unset"
        return (
            f"PendingProviderSetup(provider={self.provider!r}, "
            f"api_key=<{key_state}>, key_choice={self.key_choice!r}, "
            f"base_url={self.base_url!r}, api_mode={self.api_mode!r}, "
            f"model={self.model!r})"
        )


_lock = threading.Lock()
_pending: dict[str, PendingProviderSetup] = {}


def _purge_expired_locked(now: float) -> None:
    for user_id in [u for u, p in _pending.items() if p.expires_at <= now]:
        _pending.pop(user_id, None)


def start_setup(user_id: str, provider: str) -> PendingProviderSetup:
    """Start (or restart) the user's pending setup for ``provider``."""
    record = PendingProviderSetup(provider=provider)
    with _lock:
        _purge_expired_locked(time.monotonic())
        _pending[str(user_id)] = record
    return record


def get_setup(user_id: str) -> PendingProviderSetup | None:
    with _lock:
        now = time.monotonic()
        _purge_expired_locked(now)
        record = _pending.get(str(user_id))
        if record is not None:
            # Sliding expiry: activity keeps an in-flight chain alive (the
            # first screen may send the user off to a signup page).
            record.expires_at = now + PENDING_SETUP_TTL_SECONDS
        return record


def update_setup(user_id: str, **fields: object) -> PendingProviderSetup | None:
    """Update the user's pending setup in place; None when absent/expired."""
    with _lock:
        now = time.monotonic()
        _purge_expired_locked(now)
        record = _pending.get(str(user_id))
        if record is None:
            return None
        for name, value in fields.items():
            if not hasattr(record, name):
                raise AttributeError(f"Unknown pending-setup field: {name}")
            setattr(record, name, value)
        record.expires_at = now + PENDING_SETUP_TTL_SECONDS
        return record


def clear_setup(user_id: str) -> bool:
    with _lock:
        return _pending.pop(str(user_id), None) is not None


@dataclass
class PendingCliproxyLogin:
    """One user's in-flight CLIProxy subscription OAuth chain.

    ``oauth_state`` is the proxy's pending-session token (the proxy holds
    the actual OAuth session server-side; nothing here is a secret, but the
    state token and auth URL are single-purpose and expire with the proxy's
    ~10-minute session, which the TTL mirrors). ``account`` is filled once
    the login is CONFIRMED against the auth-file list.
    """

    target: str
    oauth_state: str = ""
    auth_url: str = ""
    flow: str = "browser"
    # Monotonic stamp of the oauth start. The proxy answers ok for a
    # session it no longer knows (expired, ~10 min), so an ok on an OLD
    # session with no callback delivered through this chain is refused as
    # the stale-session trap; within the session's lifetime an unknown-
    # session ok cannot happen for our state, so ok is genuine (the local-
    # browser flow delivers the callback straight to the proxy's port,
    # nothing is ever pasted).
    oauth_started_at: float = 0.0
    # True once a callback was delivered (pasted) for THIS session; a
    # delivery to a dead session errors, so a post-paste ok stays
    # trustworthy even past the session-age guard.
    callback_delivered: bool = False
    account: str = ""
    model: str | None = None
    model_custom: bool = False
    model_options: list[dict[str, object]] | None = None
    models_note: str = ""
    expires_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = time.monotonic() + PENDING_SETUP_TTL_SECONDS


_pending_cliproxy: dict[str, PendingCliproxyLogin] = {}


def _purge_expired_cliproxy_locked(now: float) -> None:
    for user_id in [
        u for u, p in _pending_cliproxy.items() if p.expires_at <= now
    ]:
        _pending_cliproxy.pop(user_id, None)


def start_cliproxy_login(user_id: str, target: str) -> PendingCliproxyLogin:
    """Start (or restart) the user's pending CLIProxy chain for ``target``."""
    record = PendingCliproxyLogin(target=target)
    with _lock:
        _purge_expired_cliproxy_locked(time.monotonic())
        _pending_cliproxy[str(user_id)] = record
    return record


def get_cliproxy_login(user_id: str) -> PendingCliproxyLogin | None:
    with _lock:
        now = time.monotonic()
        _purge_expired_cliproxy_locked(now)
        record = _pending_cliproxy.get(str(user_id))
        if record is not None:
            record.expires_at = now + PENDING_SETUP_TTL_SECONDS
        return record


def update_cliproxy_login(
    user_id: str, **fields: object
) -> PendingCliproxyLogin | None:
    """Update the user's pending chain in place; None when absent/expired."""
    with _lock:
        now = time.monotonic()
        _purge_expired_cliproxy_locked(now)
        record = _pending_cliproxy.get(str(user_id))
        if record is None:
            return None
        for name, value in fields.items():
            if not hasattr(record, name):
                raise AttributeError(f"Unknown pending-login field: {name}")
            setattr(record, name, value)
        record.expires_at = now + PENDING_SETUP_TTL_SECONDS
        return record


def clear_cliproxy_login(user_id: str) -> bool:
    with _lock:
        return _pending_cliproxy.pop(str(user_id), None) is not None


def clean_pasted_secret(raw: str) -> tuple[str, list[str]]:
    """Normalize a pasted secret and report suspicious content.

    Strips bracketed-paste markers, CR/LF, and surrounding whitespace, then
    reports (without blocking) non-ASCII characters by position and
    codepoint: a smart-quote or lookalike glyph from a chat app or PDF is
    the classic "my key doesn't work" cause, and naming the exact character
    beats a generic auth failure later.
    """
    value = str(raw or "")
    for marker in _PASTE_MARKERS:
        value = value.replace(marker, "")
    value = value.replace("\r", "").replace("\n", "").strip()
    warnings: list[str] = []
    if any(ch.isspace() for ch in value):
        # Internal whitespace almost always means a wrapped paste or a
        # copied "Bearer <key>" prefix; the provider will reject it.
        warnings.append(
            "the value contains internal whitespace (a wrapped paste or a"
            " copied prefix?); most keys are one unbroken token"
        )
    suspicious = [
        (index, char) for index, char in enumerate(value) if ord(char) > 127
    ]
    for index, char in suspicious[:3]:
        try:
            name = unicodedata.name(char)
        except ValueError:
            name = "unnamed"
        warnings.append(
            f"non-ASCII character at position {index + 1}: "
            f"U+{ord(char):04X} ({name})"
        )
    if len(suspicious) > 3:
        warnings.append(f"...and {len(suspicious) - 3} more non-ASCII characters")
    return value, warnings


__all__ = [
    "PENDING_SETUP_TTL_SECONDS",
    "PendingCliproxyLogin",
    "PendingProviderSetup",
    "clean_pasted_secret",
    "clear_cliproxy_login",
    "clear_setup",
    "get_cliproxy_login",
    "get_setup",
    "start_cliproxy_login",
    "start_setup",
    "update_cliproxy_login",
    "update_setup",
]
