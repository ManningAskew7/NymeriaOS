"""Slim-mode service-token bootstrap.

The single-process ``python run.py slim`` launcher needs an admin-role
Nymeria account token to authenticate same-process MCP, watchdog,
trigger-fire, and command-service calls. Production deployments expect
the operator to mint ``NYMERIA_SERVICE_TOKEN`` ahead of time, but slim
mode is meant to "just work" with no Docker, no Postgres, and no manual
account provisioning.

This module persists a slim-only raw token in ``data/SLIM_SERVICE_TOKEN.txt``
(mode ``0600``) and verifies it against the accounts repo on every boot.
The accounts DB stores token *hashes* only, so the raw value cannot be
recovered from ``accounts.db`` once issued — the file on disk is the
single source of truth.

The token is an internal service credential, not a human first-run
bootstrap token: it is intentionally distinct from
``data/BOOTSTRAP_TOKEN.txt`` (minted by ``AccountsRepo.ensure_bootstrap_admin``
for the desktop Setup Wizard).

This module also owns the other half of the service-token lifecycle
(backlog #107): the expiry-warning sweep at the bottom of the file, run
hourly by the API process in both shapes, which notifies admins before a
service-shaped token dies instead of letting it 401 silently.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .accounts import AccountsRepo, TokenRecord

logger = logging.getLogger(__name__)


SLIM_SERVICE_USER_ID = "bot-service"
SLIM_SERVICE_EMAIL = "bot-service@localhost"
SLIM_SERVICE_DISPLAY_NAME = "Bot Service"
SLIM_SERVICE_TOKEN_LABEL = "slim-service"
SLIM_SERVICE_TOKEN_FILENAME = "SLIM_SERVICE_TOKEN.txt"


def _slim_token_path(data_dir: Path) -> Path:
    return Path(data_dir) / SLIM_SERVICE_TOKEN_FILENAME


def _read_token_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None
    return raw or None


def _write_token_file(path: Path, raw_token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw_token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Some filesystems (e.g. Windows / network mounts) reject chmod.
        # The token is still written; permissions are best-effort.
        pass


def read_service_token_file(data_dir: Optional[Path]) -> Optional[str]:
    """Return the raw service token persisted on the data volume, or None.

    None-safe: a missing ``data_dir`` (some lightweight settings stubs omit it)
    or an absent file yields ``None`` instead of raising. This does NOT verify
    the token against any accounts repo; thin clients that only read the file
    the api minted (worker, mcp, watchdog, bots) call this, while the api uses
    ``ensure_service_token`` to mint/verify.
    """
    if data_dir is None:
        return None
    return _read_token_file(_slim_token_path(Path(data_dir)))


def resolve_service_token(
    configured: Optional[str], data_dir: Optional[Path]
) -> Optional[str]:
    """Resolve the internal service token without minting.

    Configured value (typically ``settings.nymeria_service_token`` from the
    ``NYMERIA_SERVICE_TOKEN`` env var) wins; otherwise fall back to the file the
    api minted into the shared data volume. Mirrors the precedence
    ``ensure_service_token`` uses, but read-only: the worker / mcp / watchdog /
    bots run in separate processes (or containers sharing ``nymeria_data``) and
    pick up the api-minted token from disk.
    """
    cleaned = (configured or "").strip()
    if cleaned:
        return cleaned
    return read_service_token_file(data_dir)


def service_token_refresher(settings) -> Callable[[], Optional[str]]:
    """Build a zero-arg callable that re-resolves the internal service token.

    The closure calls :func:`resolve_service_token` on every invocation, so it
    always reflects the CURRENT on-disk token: when the api re-mints onto the
    shared data volume (e.g. at the 90-day TTL boundary), a long-running thin
    client can pick up the rotation without a restart. ``NymeriaAPIClient``
    calls this after a 401 to refresh and retry once.

    Env-wins semantics make this a safe no-op when the operator pins
    ``NYMERIA_SERVICE_TOKEN``: the closure keeps returning the same env value,
    the client sees an unchanged token, and no retry is attempted.
    """

    def _refresh() -> Optional[str]:
        return resolve_service_token(
            settings.nymeria_service_token,
            getattr(settings, "data_dir", None),
        )

    return _refresh


def _verify_admin_token(repo: "AccountsRepo", raw: str) -> bool:
    """Return True if ``raw`` resolves to an enabled admin via the repo."""
    if not raw:
        return False
    user = repo.verify_token(raw)
    if user is None:
        return False
    if user.role != "admin":
        return False
    # verify_token already drops disabled/expired tokens; the role check
    # above is the only extra requirement.
    return True


def _ensure_bot_service_admin(repo: "AccountsRepo") -> None:
    """Make sure the bot-service user exists, is admin, and is enabled."""
    from .accounts import UserAlreadyExists

    existing = repo.get_user_by_id(SLIM_SERVICE_USER_ID)
    if existing is None:
        # Email collisions can happen if the operator pre-created the user
        # with a different id; surface that as a clear error rather than
        # silently overwriting.
        try:
            repo.create_user(
                user_id=SLIM_SERVICE_USER_ID,
                email=SLIM_SERVICE_EMAIL,
                display_name=SLIM_SERVICE_DISPLAY_NAME,
                role="admin",
            )
        except UserAlreadyExists as exc:
            raise RuntimeError(
                f"Cannot provision slim service user {SLIM_SERVICE_USER_ID!r}: "
                f"another user already owns the email {SLIM_SERVICE_EMAIL!r}. "
                f"Resolve the conflict manually before launching slim mode."
            ) from exc
        logger.info(
            "Slim bootstrap: created service user %s (admin)",
            SLIM_SERVICE_USER_ID,
        )
        return

    if existing.role != "admin":
        repo.update_user(SLIM_SERVICE_USER_ID, role="admin")
        logger.info("Slim bootstrap: promoted %s to admin", SLIM_SERVICE_USER_ID)

    if existing.disabled:
        repo.set_disabled(SLIM_SERVICE_USER_ID, False)
        logger.info("Slim bootstrap: re-enabled %s", SLIM_SERVICE_USER_ID)


def _issue_slim_token(repo: "AccountsRepo") -> str:
    """Issue a fresh slim-service token, revoking older slim tokens as needed.

    Other (non-slim) tokens on the bot-service user are left intact — slim
    only manages its own ``slim-service``-labeled tokens. If the active
    token count is at the per-user cap and no slim tokens exist to recycle,
    we revoke all bot-service tokens. The bot-service user is purely
    internal, so collateral revocation does not affect human users.
    """
    from .accounts import TokenLimitExceeded

    try:
        return repo.issue_token(SLIM_SERVICE_USER_ID, label=SLIM_SERVICE_TOKEN_LABEL)
    except TokenLimitExceeded:
        pass  # Expected: cap hit; fall through to recycle slim-service tokens.

    # Try to recycle just the slim-service tokens first.
    existing_slim = [
        t for t in repo.list_tokens_for_user(SLIM_SERVICE_USER_ID)
        if t.label == SLIM_SERVICE_TOKEN_LABEL and t.revoked_at is None
    ]
    for token in existing_slim:
        try:
            repo.revoke_token(SLIM_SERVICE_USER_ID, token.token_hash[:16])
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.warning("Failed to revoke stale slim-service token: %s", exc)

    try:
        return repo.issue_token(SLIM_SERVICE_USER_ID, label=SLIM_SERVICE_TOKEN_LABEL)
    except TokenLimitExceeded:
        logger.warning(
            "Slim bootstrap: revoking all %s tokens to reset cap",
            SLIM_SERVICE_USER_ID,
        )
        repo.revoke_all_tokens(SLIM_SERVICE_USER_ID)
        return repo.issue_token(SLIM_SERVICE_USER_ID, label=SLIM_SERVICE_TOKEN_LABEL)


def ensure_slim_service_token(
    repo: "AccountsRepo",
    data_dir: Path,
    *,
    configured_token: Optional[str] = None,
) -> str:
    """Return a valid raw admin token for same-process internal calls.

    Resolution order:

    1. ``configured_token`` (typically ``settings.nymeria_service_token``):
       if it is a non-empty string that resolves to an enabled admin token,
       it wins. This preserves explicit operator configuration.
    2. ``data/SLIM_SERVICE_TOKEN.txt`` if it exists and verifies.
    3. Otherwise: ensure ``bot-service`` exists as an enabled admin, issue
       a new ``slim-service`` token, persist it to disk at mode ``0600``,
       and return it.
    """
    configured = (configured_token or "").strip()
    if configured and _verify_admin_token(repo, configured):
        return configured

    token_path = _slim_token_path(data_dir)
    stored = _read_token_file(token_path)
    if stored and _verify_admin_token(repo, stored):
        return stored

    _ensure_bot_service_admin(repo)
    raw = _issue_slim_token(repo)
    _write_token_file(token_path, raw)
    logger.info(
        "Slim bootstrap: issued new %s token; raw value written to %s (mode 0600)",
        SLIM_SERVICE_TOKEN_LABEL,
        token_path,
    )
    return raw


# Canonical name. The same bootstrap now serves the full Docker stack as well as
# slim: the api process mints into the shared ``nymeria_data`` volume and the
# worker / mcp / watchdog read the file (via ``resolve_service_token``). The
# ``ensure_slim_service_token`` name is kept for back-compat with existing
# imports and tests.
ensure_service_token = ensure_slim_service_token


# ---------------------------------------------------------------------------
# Service-token expiry warning sweep (backlog #107)
#
# Token expiry is otherwise lazy: nothing looks at ``expires_at`` until a
# caller presents the token and 401s, which is exactly how the reference
# host's NYMERIA_SERVICE_TOKEN died silently for two days in 2026-07. This
# sweep is the before-the-fact half: warn every human admin while there is
# still time to mint a replacement. Docker's operator-minted env token cannot
# auto-rotate (the raw value is baked into container env), so warning is the
# floor for both shapes.
# ---------------------------------------------------------------------------

SERVICE_TOKEN_WARN_STATE_FILENAME = "service_token_warnings.json"


# Labels that mark a token as a service credential when it lives on a user
# other than bot-service. Deliberately exact-match: a substring test would
# false-positive on free-text human labels ("customer-service") and leak
# another user's token label into every admin's notifications.
SERVICE_TOKEN_LABELS = frozenset({SLIM_SERVICE_TOKEN_LABEL, "nymeria_service_token", "service"})


def is_service_token(record: "TokenRecord") -> bool:
    """Service-shaped: the internal bot-service user, or an exact service label.

    The token table has no "kind" column; identity is convention.
    ``bot-service`` covers both shapes' internal identity (the slim/API
    self-mint and the documented Docker operator mint) regardless of label;
    elsewhere only the exact conventional labels match (case-insensitive).
    A custom-labelled operator token is not auto-detected: relabel it (or
    mint onto ``bot-service``) to opt in. ``bootstrap`` never matches.
    """
    if record.user_id == SLIM_SERVICE_USER_ID:
        return True
    return (record.label or "").lower() in SERVICE_TOKEN_LABELS


def _warn_phases(warn_days: int) -> list[int]:
    """Descending warning thresholds in days; 0 means "has expired"."""
    return sorted({t for t in (warn_days, 3, 1, 0) if t <= warn_days}, reverse=True)


def _load_warn_state(path: Path) -> dict:
    """Read the dedupe state; a corrupt file deliberately reads as empty.

    This is a pure dedupe cache (worst case: one duplicate notification per
    threshold), so it is overwritten in place rather than quarantined like
    the real stores.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        state = json.loads(raw)
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _warn_state_key(record: "TokenRecord") -> str:
    """Non-secret dedupe key. Deliberately NOT the token hash.

    The state file lives in the agent-readable data dir while ``accounts.db``
    is on the file-tool secrets denylist, so hashes must not leak into it;
    and keying on fields the summary already exposes means tampering can at
    worst duplicate or repeat a notification, never mine the token store.
    A rotation changes ``expires_at``, so a replacement token re-arms.
    """
    return f"{record.user_id}|{record.label or ''}|{record.expires_at or ''}"


def sweep_expiring_service_tokens(
    repo: "AccountsRepo",
    data_dir: Path,
    *,
    warn_days: int,
    now: Optional[datetime] = None,
) -> int:
    """Warn enabled human admins about service tokens nearing (or past) expiry.

    Every in-window token is logged at WARNING on every pass (the log is the
    channel that survives a broken notification store); notifications are
    bounded and restart-safe: at most one per token per threshold phase
    (``warn_days`` out, 3 days, 1 day, expired), with the last-notified phase
    persisted to ``data_dir/service_token_warnings.json`` (atomic write,
    ``{key: phase}``). State entries for tokens no longer expiring (rotated
    or revoked) are pruned, so a replacement token re-arms naturally.
    Returns the number of tokens a notification was issued for.
    """
    if warn_days <= 0:
        return 0
    from .accounts import _parse_timestamp
    from .notifications import active_admin_user_ids, notify_user_best_effort
    from .storage_paths import write_text_atomic

    current = now or datetime.now(timezone.utc)
    cutoff = current + timedelta(days=warn_days)
    candidates = [
        record
        for record in repo.list_unrevoked_tokens_expiring_before(cutoff)
        if is_service_token(record)
    ]

    state_path = Path(data_dir) / SERVICE_TOKEN_WARN_STATE_FILENAME
    state = _load_warn_state(state_path)
    phases = _warn_phases(warn_days)
    new_state: dict = {}
    warned = 0

    admin_ids = [
        admin_id
        for admin_id in active_admin_user_ids(repo)
        if admin_id != SLIM_SERVICE_USER_ID
    ]
    if candidates and not admin_ids:
        logger.warning(
            "Service-token expiry warning has no enabled human admin to notify"
        )

    for record in candidates:
        expires = _parse_timestamp(record.expires_at)
        if expires is None or expires <= current:
            phase = 0
            days_left = 0.0
        else:
            days_left = (expires - current).total_seconds() / 86400.0
            phase = min((t for t in phases if days_left <= t), default=phases[0])

        summary = _expiry_warning_summary(record, phase=phase, days_left=days_left)
        # Unconditional per-pass log for in-window tokens; only the
        # notifications are phase-deduped.
        logger.warning("%s", summary)

        key = _warn_state_key(record)
        prior_phase = state.get(key)
        already_notified = (
            isinstance(prior_phase, int)
            and not isinstance(prior_phase, bool)
            and phase >= prior_phase
        )

        if already_notified:
            new_state[key] = prior_phase
        else:
            for admin_id in admin_ids:
                notify_user_best_effort(
                    admin_id,
                    summary,
                    push=True,
                    log_label="service token expiry warning",
                )
            warned += 1
            new_state[key] = phase

    if new_state != state:
        try:
            write_text_atomic(state_path, json.dumps(new_state, indent=2))
        except OSError as exc:
            logger.warning("Could not persist service-token warning state: %s", exc)

    return warned


def _expiry_warning_summary(record: "TokenRecord", *, phase: int, days_left: float) -> str:
    label = record.label or "unlabeled"
    date = (record.expires_at or "")[:10] or "unknown date"
    if phase <= 0:
        return (
            f"Service token '{label}' (user {record.user_id}) has EXPIRED "
            f"({date}). Internal calls (worker relays, bots, MCP) will 401 "
            "until it is replaced."
        )
    # floor, never ceil: understating the time left is the safe direction.
    days = max(1, math.floor(days_left))
    unit = "day" if days == 1 else "days"
    return (
        f"Service token '{label}' (user {record.user_id}) expires in {days} "
        f"{unit} ({date}). Mint a replacement and update the deployment "
        "before internal calls start failing."
    )
