"""Auth preset bridge for managed MCP server installs.

Nymeria stores OAuth tokens per user, while third-party MCP servers usually
expect provider-specific files or env vars. This module applies conservative
presets for known MCP servers without exposing token values in server config.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from ..config import get_settings
from ..tools import auth_cache_utils as auth_utils
from ..tools.definitions.mcp_schema import MCPServerDefinition

logger = logging.getLogger(__name__)

GMAIL_MCP_PACKAGE = "@gongrzhe/server-gmail-autoauth-mcp"
GMAIL_MCP_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
GOOGLE_GMAIL_CACHE = "google_gmail.json"
GOOGLE_CACHE_CANDIDATES = (
    GOOGLE_GMAIL_CACHE,
    "google_docs.json",
    "google_calendar.json",
)
# Filename -> provider mapping used to look credentials up in the credential
# vault via ``resolve_oauth_cache``. Filenames remain the user-visible cache
# names; providers match ``OAUTH_PROVIDERS`` keys in ``config/oauth_providers``.
_GOOGLE_FILENAME_TO_PROVIDER = {
    GOOGLE_GMAIL_CACHE: "google_gmail",
    "google_docs.json": "google_docs",
    "google_calendar.json": "google_calendar",
}


def _is_gmail_mcp_server(defn: MCPServerDefinition) -> bool:
    parts = [defn.server_command, *list(defn.server_args or [])]
    joined = " ".join(parts)
    return GMAIL_MCP_PACKAGE in joined


def gmail_mcp_credentials_path(user_id: str) -> Path:
    return (
        get_settings().data_dir
        / "auth_tokens"
        / auth_utils.safe_user_id(user_id)
        / "mcp"
        / "gmail"
        / "credentials.json"
    )


def _account_has_scopes(account: dict, scopes: list[str]) -> bool:
    return set(account.get("scopes", [])) >= set(scopes)


def _google_auth_library_credentials(account: dict) -> dict:
    credentials = {
        "access_token": account.get("access_token"),
        "refresh_token": account.get("refresh_token"),
        "scope": " ".join(account.get("scopes", [])),
        "token_type": "Bearer",
    }
    expires_at = account.get("expires_at")
    if expires_at:
        credentials["expiry_date"] = int(float(expires_at) * 1000)
    return {k: v for k, v in credentials.items() if v}


def _write_google_auth_library_credentials(account: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        logger.debug("Failed to chmod Gmail MCP credentials directory %s", path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_google_auth_library_credentials(account), fh, indent=2)
            fh.write("\n")
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            logger.debug("Failed to chmod Gmail MCP credentials file %s", path)


def _refresh_if_needed(account: dict) -> tuple[bool, str]:
    if not account.get("refresh_token"):
        return False, "no refresh token stored"
    if not _account_has_scopes(account, GMAIL_MCP_SCOPES):
        return False, "missing Gmail scopes"
    if time.time() < float(account.get("expires_at", 0)) - 60:
        return True, ""
    status, reason = auth_utils.refresh_google_account(account, GMAIL_MCP_SCOPES)
    if status == "refreshed":
        return True, ""
    return False, reason or status


def export_google_account_for_gmail_mcp(
    user_id: str,
    *,
    account_id: Optional[str] = None,
    cache_filename: Optional[str] = None,
    credentials_path: Optional[Path] = None,
    log_sink: Optional[list[str]] = None,
) -> Optional[Path]:
    """Export a saved Gmail-scoped Google account for google-auth-library.

    Returns the written credentials path, or ``None`` if no usable account was
    found. The exported file contains the OAuth token bundle only; the OAuth
    client secret stays in ``GOOGLE_OAUTH_CREDENTIALS`` / ``GMAIL_OAUTH_PATH``.
    """
    logs = log_sink if log_sink is not None else []
    target = credentials_path or gmail_mcp_credentials_path(user_id)
    filenames = (cache_filename,) if cache_filename else GOOGLE_CACHE_CANDIDATES

    for filename in filenames:
        if not filename:
            continue
        provider = _GOOGLE_FILENAME_TO_PROVIDER.get(filename)
        if provider:
            source = auth_utils.resolve_oauth_cache(user_id, provider, cache_filename=filename)
            cache = source.cache
        else:
            source = None
            cache = auth_utils.load_token_cache(user_id, filename)
        accounts = cache.get("accounts", {})
        if not accounts:
            continue

        changed = False
        candidates = (
            [(account_id, accounts.get(account_id))]
            if account_id
            else list(accounts.items())
        )
        for candidate_id, account in candidates:
            if not candidate_id or not account:
                continue
            before_access_token = account.get("access_token")
            before_expires_at = account.get("expires_at")
            usable, reason = _refresh_if_needed(account)
            if not usable:
                logs.append(
                    "Google account "
                    f"{account.get('email', candidate_id)} in {filename} cannot "
                    f"be exported for Gmail MCP: {reason}."
                )
                continue
            if (
                account.get("access_token") != before_access_token
                or account.get("expires_at") != before_expires_at
            ):
                changed = True
            accounts[candidate_id] = account
            if changed:
                cache["accounts"] = accounts
                if source is not None:
                    source.persist(cache)
                else:
                    auth_utils.save_token_cache(user_id, filename, cache)
            _write_google_auth_library_credentials(account, target)
            logs.append(
                "Exported Google Gmail auth connection "
                f"{account.get('email', candidate_id)} to {target}."
            )
            return target

    return None


def apply_mcp_auth_presets(
    defn: MCPServerDefinition,
    *,
    user_id: str,
    log_sink: Optional[list[str]] = None,
) -> MCPServerDefinition:
    """Apply known auth env/path presets to an MCP server definition."""
    logs = log_sink if log_sink is not None else []
    if not _is_gmail_mcp_server(defn):
        return defn

    env_vars = dict(defn.env_vars or {})

    oauth_path = env_vars.get("GMAIL_OAUTH_PATH") or auth_utils.get_google_credentials_path()
    if oauth_path:
        env_vars.setdefault("GMAIL_OAUTH_PATH", oauth_path)
        logs.append(f"Applied Gmail MCP OAuth client path: {oauth_path}")
    else:
        logs.append(
            "Gmail MCP preset could not find GOOGLE_OAUTH_CREDENTIALS; "
            "set it before authenticating Gmail."
        )

    configured_credentials = env_vars.get("GMAIL_CREDENTIALS_PATH")
    if configured_credentials:
        credentials_path = Path(configured_credentials).expanduser()
    else:
        credentials_path = gmail_mcp_credentials_path(user_id)
        env_vars["GMAIL_CREDENTIALS_PATH"] = str(credentials_path)
        logs.append(f"Applied Gmail MCP credential path: {credentials_path}")

    if configured_credentials and configured_credentials.startswith("${env:"):
        logs.append(
            "Gmail MCP credential path is provided by an environment reference; "
            "Nymeria will not write an export file for it."
        )
    elif not credentials_path.exists():
        exported = export_google_account_for_gmail_mcp(
            user_id,
            credentials_path=credentials_path,
            log_sink=logs,
        )
        if exported is None:
            logs.append(
                "No saved Google auth connection has Gmail scopes yet. Ask the "
                "agent to call `request_credential(provider=\"google_gmail\", "
                "kind=\"oauth\")`, then retry or rediscover this MCP server."
            )
    else:
        logs.append(f"Using existing Gmail MCP credential file: {credentials_path}")

    defn.env_vars = env_vars
    return defn


__all__ = [
    "GMAIL_MCP_PACKAGE",
    "GMAIL_MCP_SCOPES",
    "apply_mcp_auth_presets",
    "export_google_account_for_gmail_mcp",
    "gmail_mcp_credentials_path",
]
