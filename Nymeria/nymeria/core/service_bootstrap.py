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
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .accounts import AccountsRepo

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
