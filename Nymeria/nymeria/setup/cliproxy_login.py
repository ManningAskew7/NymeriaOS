"""TUI-free CLIProxy login helpers shared by the wizard and headless setup.

The interactive CLIProxy branch (`steps/cliproxy.py`) and the non-interactive
path both drive the proxy's `/v0/management` API: list auth files, start and
complete OAuth, resolve the cpx- gatekeeper key. The pure parts live here so
the headless path never imports Textual; the step module re-imports them.

The management client never retries 401/403 (the proxy bans an IP after 5 bad
attempts), and the proxy's `/get-auth-status` answers "ok" for unknown or
expired sessions, so a completed login must always be confirmed against
`list_auth_files` before it is trusted.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from ..cliproxy.catalog import CLIProxyProviderSpec
from ..cliproxy.management_client import CLIProxyManagementClient
from .cliproxy_deploy import mint_gatekeeper_key
from .state import WizardState

logger = logging.getLogger(__name__)

LOGIN_POLL_INTERVAL_SECONDS = 2.0
LOGIN_TIMEOUT_SECONDS = 600.0


def _is_remote_session() -> bool:
    """SSH session heuristic (hermes-agent's signal set): never auto-open a browser."""
    return any(os.environ.get(var) for var in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION"))


def management_credentials(state: WizardState) -> tuple[str, str]:
    """Effective (url, key) for driving the proxy from this machine.

    A blank key during a reconfigure means "keep the one on disk": it is read
    back from the located env file at use time (for the client only, never
    into the UI), mirroring the present-only secret policy.
    """
    url = (state.cliproxy_management_url or "").strip().rstrip("/")
    key = (state.cliproxy_management_key or "").strip()
    if url and key:
        return url, key
    if state.reconfigure and "CLIPROXY_MANAGEMENT_KEY" in state.present_env_keys:
        try:
            from dotenv import dotenv_values

            from .hydrate import _locate_config

            located = _locate_config(state)
            if located is not None:
                values = dotenv_values(str(located[0]))
                key = key or (values.get("CLIPROXY_MANAGEMENT_KEY") or "").strip()
                url = url or (values.get("CLIPROXY_MANAGEMENT_URL") or "").strip().rstrip("/")
        except OSError:
            # Unreadable env file: behave as not-configured; the endpoint
            # step asks for the values instead.
            pass
    return url, key


def make_management_client(state: WizardState) -> CLIProxyManagementClient | None:
    url, key = management_credentials(state)
    if not url or not key:
        return None
    return CLIProxyManagementClient(url, key)


async def ensure_gatekeeper_key(
    state: WizardState, client: CLIProxyManagementClient
) -> None:
    """Fill `state.cliproxy_gatekeeper_key` from the proxy's api-keys list.

    Any gatekeeper key unlocks every data-plane route (they are not
    provider-scoped), so the first configured key is reused; when the proxy
    has none, one is minted and appended through the management API.
    """
    if state.cliproxy_gatekeeper_key.strip():
        return
    knobs = await client.get_config_knobs(["api-keys"])
    keys = [k for k in (knobs.get("api-keys") or []) if isinstance(k, str) and k]
    if keys:
        state.cliproxy_gatekeeper_key = keys[0]
        return
    minted = mint_gatekeeper_key()
    await client.set_config_knob("api-keys", [minted])
    state.cliproxy_gatekeeper_key = minted


def active_login_entry(
    files: Sequence[dict[str, Any]], spec: CLIProxyProviderSpec
) -> dict[str, Any] | None:
    """The first enabled, available auth-file entry for this provider, or None.

    "Active" mirrors the wizard login step's filter: the entry's provider
    matches the spec's auth-file provider and it is neither disabled nor
    marked unavailable by the proxy.
    """
    for entry in files:
        if (
            str(entry.get("provider") or "").lower() == spec.auth_file_provider
            and not entry.get("disabled")
            and not entry.get("unavailable")
        ):
            return entry
    return None


def login_account_label(entry: dict[str, Any]) -> str:
    """Best-effort account identity from an auth-file entry ("" when unknown)."""
    return str(entry.get("account") or entry.get("email") or "")


async def ensure_claude_tool_prefix_disabled(client: CLIProxyManagementClient) -> None:
    """Keep tool_prefix_disabled true on every Claude auth file.

    Inert on v7 proxies, required if one is rolled back to v6.9.36. Raises
    `CLIProxyManagementError` on failure; callers treat it as best-effort
    hardening and never fail a completed login over it.
    """
    for entry in await client.list_auth_files():
        if str(entry.get("provider") or "").lower() == "claude":
            name = str(entry.get("name") or "")
            if name:
                await client.ensure_tool_prefix_disabled(name)


__all__ = [
    "LOGIN_POLL_INTERVAL_SECONDS",
    "LOGIN_TIMEOUT_SECONDS",
    "active_login_entry",
    "ensure_claude_tool_prefix_disabled",
    "ensure_gatekeeper_key",
    "login_account_label",
    "make_management_client",
    "management_credentials",
]
