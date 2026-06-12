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

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console
from rich.markup import escape

from ..cliproxy.catalog import CLIProxyProviderSpec, get_cliproxy_provider
from ..cliproxy.management_client import (
    CLIProxyAuthError,
    CLIProxyManagementClient,
    CLIProxyManagementError,
    CLIProxyUnreachable,
    CLIProxyUnsupported,
)
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


# --- headless (non-interactive) orchestration --------------------------------


def _start_paste_reader() -> "queue.Queue[str]":
    """Feed stripped stdin lines into a queue from a daemon thread.

    Module-level seam so tests can substitute a pre-filled queue. The thread
    is a daemon and is deliberately not run through the asyncio executor: a
    pending readline must never block process exit, and executor shutdown
    would wait on it.
    """
    lines: "queue.Queue[str]" = queue.Queue()

    def _read() -> None:
        try:
            for line in sys.stdin:
                lines.put(line.strip())
        except Exception:  # noqa: BLE001 (closed/odd stdin just ends the reader)
            pass

    threading.Thread(target=_read, daemon=True, name="cliproxy-login-stdin").start()
    return lines


async def _post_login_console(
    state: WizardState, client: CLIProxyManagementClient, spec: CLIProxyProviderSpec
) -> None:
    """Post-login hardening shared by the import and console-login paths."""
    if spec.id == "claude":
        try:
            await ensure_claude_tool_prefix_disabled(client)
        except CLIProxyManagementError as exc:
            # Best-effort hardening; never fail a completed login over it.
            logger.warning("tool_prefix_disabled fixup failed: %s", exc)
    state.cliproxy_logged_in = True


async def _import_auth_file(
    state: WizardState,
    client: CLIProxyManagementClient,
    spec: CLIProxyProviderSpec,
    console: Console,
    path: Path,
) -> bool:
    """Upload an auth-file JSON and confirm the proxy registered it as active."""
    if path.suffix.lower() != ".json":
        console.print(
            f"[red]--cliproxy-auth-file expects a .json auth file, got "
            f"{escape(path.name)}.[/red]"
        )
        return False
    try:
        content = path.read_bytes()
    except OSError as exc:
        console.print(f"[red]Cannot read {escape(str(path))}: {escape(str(exc))}[/red]")
        return False
    try:
        json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        console.print(
            f"[red]{escape(path.name)} is not valid JSON; auth files are the "
            "proxy's auths/*.json documents.[/red]"
        )
        return False
    try:
        await client.upload_auth_file(path.name, content)
        files = await client.list_auth_files()
    except CLIProxyManagementError as exc:
        console.print(f"[red]Auth-file upload failed: {escape(str(exc))}[/red]")
        return False
    entry = active_login_entry(files, spec)
    if entry is None:
        console.print(
            f"[red]The proxy accepted {escape(path.name)} but lists no active "
            f"{spec.label} login; the file may be disabled, expired, or for a "
            "different provider.[/red]"
        )
        return False
    account = login_account_label(entry)
    console.print(
        f"[green]Imported {escape(path.name)}:[/green] active {spec.label} "
        f"login{f' as {escape(account)}' if account else ''}."
    )
    await _post_login_console(state, client, spec)
    return True


async def _login_console(
    state: WizardState,
    client: CLIProxyManagementClient,
    spec: CLIProxyProviderSpec,
    console: Console,
) -> bool:
    """Complete the OAuth login from a plain terminal (no TUI).

    Prints the login URL; browser flows read an optionally pasted redirect URL
    from stdin (the headless-host path), device flows only poll. Login state
    is confirmed against the auth-file list because the proxy's status
    endpoint answers ok for unknown or expired sessions.
    """
    try:
        files = await client.list_auth_files()
    except CLIProxyManagementError as exc:
        console.print(f"[red]Cannot reach the proxy: {escape(str(exc))}[/red]")
        return False
    entry = active_login_entry(files, spec)
    if entry is not None:
        account = login_account_label(entry)
        console.print(
            f"[green]Already logged in to {spec.label}"
            f"{f' as {escape(account)}' if account else ''}.[/green]"
        )
        await _post_login_console(state, client, spec)
        return True

    try:
        started = await client.start_oauth(spec)
    except CLIProxyUnsupported:
        console.print(
            f"[red]This proxy build does not support {spec.label}; pick "
            "another subscription or upgrade the proxy.[/red]"
        )
        return False
    except CLIProxyManagementError as exc:
        console.print(f"[red]Could not start the login: {escape(str(exc))}[/red]")
        return False

    console.print("\nOpen this URL to approve the login:")
    console.print(started["url"], markup=False, soft_wrap=True)
    pasted_lines: "queue.Queue[str] | None" = None
    if spec.flow == "device":
        console.print(
            "Open the link above on any device and approve the login; setup "
            "polls until it completes."
        )
    else:
        if not _is_remote_session():
            try:
                webbrowser.open(started["url"])
            except Exception as exc:  # noqa: BLE001 (best-effort; URL is printed)
                logger.debug("webbrowser.open failed: %s", exc)
        console.print(
            "Approve the login in your browser. If it ends on an unreachable "
            "localhost page, paste that page's full URL here and press Enter; "
            "otherwise just wait."
        )
        pasted_lines = _start_paste_reader()

    deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pasted_lines is not None:
            pasted = ""
            try:
                while True:
                    pasted = pasted_lines.get_nowait() or pasted
            except queue.Empty:
                pass  # queue drained; the latest non-empty paste wins
            if pasted:
                try:
                    await client.oauth_callback(spec, redirect_url=pasted)
                except CLIProxyAuthError as exc:
                    # A management-auth failure is never retried (the proxy
                    # bans the IP after 5); inviting a re-paste would burn
                    # the remaining budget.
                    console.print(
                        f"[red]{escape(str(exc))}[/red] Check "
                        "--cliproxy-management-key."
                    )
                    return False
                except CLIProxyManagementError as exc:
                    # Non-fatal: a mangled paste can be retried.
                    console.print(
                        f"[red]The proxy rejected the callback: "
                        f"{escape(str(exc))}[/red] Paste the full redirect URL "
                        "again."
                    )
        try:
            status = await client.auth_status(started["state"])
        except CLIProxyManagementError as exc:
            console.print(
                f"[red]Lost contact with the proxy: {escape(str(exc))}[/red]"
            )
            return False
        if status == "ok":
            try:
                files = await client.list_auth_files()
            except CLIProxyManagementError as exc:
                console.print(
                    f"[red]Lost contact with the proxy: {escape(str(exc))}[/red]"
                )
                return False
            entry = active_login_entry(files, spec)
            if entry is None:
                # The proxy answers ok for unknown/expired sessions, so a bare
                # ok with no auth file means the login did not actually land.
                console.print(
                    f"[red]The proxy reported the login complete but lists no "
                    f"active {spec.label} auth file; re-run --cliproxy-login "
                    "(the proxy's status endpoint answers ok for unknown "
                    "sessions).[/red]"
                )
                return False
            account = login_account_label(entry)
            console.print(
                f"[green]Logged in to {spec.label}"
                f"{f' as {escape(account)}' if account else ''}.[/green]"
            )
            await _post_login_console(state, client, spec)
            return True
        if status == "error":
            console.print(
                "[red]The provider reported a login error; re-run "
                "--cliproxy-login to try again.[/red]"
            )
            return False
        await asyncio.sleep(LOGIN_POLL_INTERVAL_SECONDS)
    console.print(
        "[red]The login session expired (10 minutes); re-run --cliproxy-login "
        "to restart it.[/red]"
    )
    return False


async def _preflight(
    state: WizardState,
    client: CLIProxyManagementClient | None,
    spec: CLIProxyProviderSpec,
    console: Console,
    *,
    gatekeeper_available: bool,
) -> int:
    """Verify the proxy actually holds a login for the chosen provider.

    Returns 0 to proceed (verified, or unverifiable-but-survivable) and 2 on
    a fatal problem (no login, bad management secret, or an unreachable proxy
    the run still needs for the gatekeeper key).
    """
    if client is None:
        # Gatekeeper supplied without a management key: nothing to verify with.
        console.print(
            "[yellow]Cannot verify the subscription login without "
            "--cliproxy-management-key; continuing unverified.[/yellow]"
        )
        return 0
    try:
        files = await client.list_auth_files()
    except CLIProxyAuthError as exc:
        # Never retried: the proxy bans an IP after 5 bad management attempts.
        console.print(
            f"[red]{escape(str(exc))}[/red] Check --cliproxy-management-key."
        )
        return 2
    except CLIProxyUnreachable as exc:
        message = (
            f"Could not reach the proxy at {escape(client.base_url)} to verify "
            f"the login ({escape(str(exc))}). It may be a backend-facing URL "
            "this machine cannot reach; pass a host-reachable "
            "--cliproxy-management-url to verify."
        )
        if gatekeeper_available:
            console.print(f"[yellow]{message} Continuing unverified.[/yellow]")
            return 0
        console.print(
            f"[red]{message} Without it the gatekeeper key cannot be read or "
            "minted either.[/red]"
        )
        return 2
    except CLIProxyManagementError as exc:
        console.print(f"[red]Cannot reach the proxy: {escape(str(exc))}[/red]")
        return 2
    entry = active_login_entry(files, spec)
    if entry is None:
        console.print(
            f"[red]No active {spec.label} login on the proxy.[/red] Complete "
            "it with --cliproxy-login, import a saved auth file with "
            "--cliproxy-auth-file, or run `nymeria init` interactively. Pass "
            "--skip-llm-test to write the config anyway."
        )
        return 2
    account = login_account_label(entry)
    console.print(
        f"[green]Verified:[/green] {spec.label} login active"
        f"{f' as {escape(account)}' if account else ''}."
    )
    state.cliproxy_logged_in = True
    return 0


def prepare_headless_cliproxy(
    state: WizardState,
    *,
    console: Console,
    login: bool = False,
    auth_file: str | Path | None = None,
    strict: bool = True,
) -> int:
    """Headless CLIProxy preparation for `--non-interactive` runs.

    Validates the branch inputs against the post-hydrate state, optionally
    imports an auth file or runs a console OAuth login, verifies the chosen
    provider holds an active login on the proxy, and fills the gatekeeper key
    through the management API when it was not supplied. Returns 0 to proceed
    to finalize and 2 on a fatal error (message already printed); raises
    SystemExit for missing-flag errors, matching the runner's style.

    `strict=False` (a plain reconfigure of an already-routed install) turns a
    failed preflight into a warning instead of a failure: the run is editing
    something else and must not be blocked by a lapsed subscription login.
    """
    spec = get_cliproxy_provider(state.cliproxy_provider or "")
    if spec is None:
        raise SystemExit(
            "--cliproxy-provider is required with --non-interactive "
            "--auth-method cliproxy_oauth"
        )
    url, _key = management_credentials(state)
    if not url:
        raise SystemExit(
            "--cliproxy-management-url is required with "
            "--non-interactive --auth-method cliproxy_oauth"
        )
    from .finalize import cliproxy_key_env_override

    key_env = cliproxy_key_env_override(state)
    gatekeeper_available = bool(
        state.cliproxy_gatekeeper_key.strip()
        or (key_env and key_env in state.present_env_keys)
    )
    client = make_management_client(state)
    if client is None and (login or auth_file):
        raise SystemExit(
            "--cliproxy-login and --cliproxy-auth-file need "
            "--cliproxy-management-key (or an existing install that recorded "
            "it) to drive the proxy's management API"
        )
    if client is None and not gatekeeper_available:
        raise SystemExit(
            "--cliproxy-gatekeeper-key or --cliproxy-management-key is "
            "required with --non-interactive --auth-method cliproxy_oauth (a "
            "management key lets setup read or mint the cpx- gatekeeper key "
            "itself; --cliproxy-login or the interactive wizard completes the "
            "OAuth login)"
        )

    async def _run() -> int:
        assert spec is not None
        if auth_file is not None:
            assert client is not None
            if not await _import_auth_file(
                state, client, spec, console, Path(auth_file)
            ):
                return 2
        elif login:
            assert client is not None
            if not await _login_console(state, client, spec, console):
                return 2
        elif state.skip_llm_test:
            console.print("[yellow]Skipping the CLIProxy login preflight.[/yellow]")
        else:
            rc = await _preflight(
                state, client, spec, console,
                gatekeeper_available=gatekeeper_available,
            )
            if rc != 0:
                if strict:
                    return rc
                console.print(
                    "[yellow]Continuing anyway: this reconfigure leaves the "
                    "LLM route unchanged.[/yellow]"
                )
        if (
            client is not None
            and not state.cliproxy_gatekeeper_key.strip()
            and not (key_env and key_env in state.present_env_keys)
        ):
            try:
                await ensure_gatekeeper_key(state, client)
            except CLIProxyManagementError as exc:
                console.print(
                    f"[red]Could not read or mint a cpx- gatekeeper key "
                    f"through the management API: {escape(str(exc))}.[/red] "
                    "Pass --cliproxy-gatekeeper-key explicitly."
                )
                return 2
        return 0

    return asyncio.run(_run())


__all__ = [
    "LOGIN_POLL_INTERVAL_SECONDS",
    "LOGIN_TIMEOUT_SECONDS",
    "active_login_entry",
    "ensure_claude_tool_prefix_disabled",
    "ensure_gatekeeper_key",
    "login_account_label",
    "make_management_client",
    "management_credentials",
    "prepare_headless_cliproxy",
]
