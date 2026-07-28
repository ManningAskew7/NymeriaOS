"""Async client for CLIProxy's /v0/management API.

Drives subscription OAuth logins, auth-file management, and the small set of
proxy config knobs Nymeria surfaces (the "essentials + key knobs" scope; the
proxy's own /management.html panel covers the rest).

Auth: every request sends the plaintext remote-management secret as a Bearer
token (the proxy stores a bcrypt hash at rest and compares). The proxy bans an
IP for ~30 minutes after 5 consecutive auth failures, so this client NEVER
retries a 401/403; a bad key fails fast with that hint instead.

OAuth model (uniform across providers): GET /{endpoint}-auth-url returns
{url, state} and the proxy waits server-side; browser flows are completed by
POSTing the user's pasted redirect URL (or code+state) to /oauth-callback,
device flows complete on their own once the user approves; either way the
caller polls GET /get-auth-status?state= until ok/error (sessions expire in
about 10 minutes).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Optional, Sequence
from urllib.parse import parse_qs, urlparse

import httpx

from .catalog import CLIPROXY_PROVIDERS, CLIProxyProviderSpec

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0

# Just under the proxy's ~10-minute OAuth session TTL: past this, a polled
# ok with no callback delivered through this process is treated as the
# stale-session trap (see confirm_login_landed).
SESSION_OK_GUARD_SECONDS = 540.0

# In-process OAuth session ledger: state -> (started_at_monotonic,
# callback_delivered). Stamped inside start_oauth/oauth_callback so EVERY
# surface that drives OAuth through this client (wizard, headless console,
# REST route bodies, the command facade) records its sessions with no
# caller cooperation. ADVISORY, not a security boundary: it exists to stop
# a confusing false "login complete" (the relogin trap), so an unknown
# state fails OPEN (no stamp = no guard; e.g. an API restart mid-login),
# and nobody should "harden" it into persisted or authenticated state.
_LEDGER_TTL_SECONDS = 1800.0
_LEDGER_MAX_ENTRIES = 64
_oauth_session_ledger: dict[str, tuple[float, bool]] = {}


def _ledger_prune(now: float) -> None:
    expired = [
        state
        for state, (started, _delivered) in _oauth_session_ledger.items()
        if now - started > _LEDGER_TTL_SECONDS
    ]
    for state in expired:
        _oauth_session_ledger.pop(state, None)
    if len(_oauth_session_ledger) > _LEDGER_MAX_ENTRIES:
        # Runaway bound (admin-only flows never approach this): crude
        # clear, matching the router probe-cache idiom; the guard simply
        # fails open for the dropped sessions.
        _oauth_session_ledger.clear()


def _ledger_stamp_start(state: str) -> None:
    if not state:
        return
    now = time.monotonic()
    _ledger_prune(now)
    _oauth_session_ledger[state] = (now, False)


def _ledger_mark_delivered(state: str) -> None:
    """Record a successfully delivered callback for this session.

    A callback the proxy ACCEPTED proves the session was alive, so a later
    ok is trusted; an unknown state is added fresh (delivered) for the
    same reason.
    """
    if not state:
        return
    now = time.monotonic()
    _ledger_prune(now)
    started, _delivered = _oauth_session_ledger.get(state, (now, False))
    _oauth_session_ledger[state] = (started, True)


def stale_session_refusal(state: str) -> str:
    """A refusal detail when an ok cannot be trusted for this session.

    "" when the ok may be trusted: unknown state (fail-open), callback
    delivered, or the session is young enough that the proxy still knows
    it (an unknown-session ok cannot happen inside the TTL).
    """
    entry = _oauth_session_ledger.get(state)
    if entry is None:
        return ""
    started, delivered = entry
    if delivered:
        return ""
    if time.monotonic() - started <= SESSION_OK_GUARD_SECONDS:
        return ""
    return (
        "The proxy answered ok, but this login session is old enough to"
        " have expired and no callback was delivered, so that is likely a"
        " stale-session answer blessing an older login. Restart the login"
        " to be sure."
    )


def mint_gatekeeper_key() -> str:
    """A fresh cpx- data-plane key in the proxy's api-keys list format."""
    return "cpx-nymeria-" + secrets.token_urlsafe(24)


def active_login_entry(
    files: Sequence[dict[str, Any]], spec: CLIProxyProviderSpec
) -> Optional[dict[str, Any]]:
    """The first enabled, available auth-file entry for this provider, or None.

    The auth-file list is the ground truth for "logged in": the proxy's
    `/get-auth-status` answers ok for unknown or expired sessions, so every
    completed login must be confirmed against this before it is trusted.
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

# Knobs surfaced through GET/PATCH /cliproxy/config. Path -> JSON kind.
CONFIG_KNOB_PATHS: dict[str, str] = {
    "api-keys": "list",
    "request-retry": "scalar",
    "max-retry-interval": "scalar",
    "routing/strategy": "scalar",
    "oauth-model-alias": "map",
    "oauth-excluded-models": "map",
    "quota-exceeded/switch-project": "scalar",
    "quota-exceeded/switch-preview-model": "scalar",
}


class CLIProxyManagementError(RuntimeError):
    """Base error for management API failures."""


class CLIProxyUnreachable(CLIProxyManagementError):
    """The proxy did not answer (connect error or timeout)."""


class CLIProxyAuthError(CLIProxyManagementError):
    """The management key was rejected (401/403). Never retried."""


class CLIProxyNotFound(CLIProxyManagementError):
    """A route or named resource does not exist (404)."""


class CLIProxyUnsupported(CLIProxyManagementError):
    """The pinned proxy binary does not serve this provider's OAuth flow."""


class CLIProxyConflict(CLIProxyManagementError):
    """The proxy rejected the call for state reasons (409)."""


class CLIProxyManagementClient:
    """Thin async wrapper over /v0/management with a typed error taxonomy."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self._secret = (secret or "").strip()
        self._timeout = timeout
        self._transport = transport
        self._probe_cache: Optional[dict[str, bool]] = None

    @property
    def management_html_url(self) -> str:
        """The proxy's bundled control panel (link-out for exotic knobs)."""
        return f"{self.base_url}/management.html"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
        files: Any = None,
        expect_json: bool = True,
    ) -> Any:
        url = f"{self.base_url}/v0/management{path}"
        headers = {"Authorization": f"Bearer {self._secret}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    files=files,
                )
        except httpx.TransportError as exc:
            raise CLIProxyUnreachable(
                f"CLIProxy management API unreachable at {self.base_url}: {exc}"
            ) from exc
        if response.status_code in (401, 403):
            raise CLIProxyAuthError(
                "CLIProxy rejected the management key (the proxy bans an IP "
                "for ~30 minutes after 5 consecutive failures, so this is "
                "not retried); check CLIPROXY_MANAGEMENT_KEY"
            )
        if response.status_code == 404:
            raise CLIProxyNotFound(f"{method} {path} not found on this proxy")
        if response.status_code == 409:
            raise CLIProxyConflict(_error_message(response))
        if response.status_code >= 400:
            raise CLIProxyManagementError(
                f"{method} {path} failed with HTTP "
                f"{response.status_code}: {_error_message(response)}"
            )
        if not expect_json:
            return response.content
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise CLIProxyManagementError(
                f"{method} {path} returned non-JSON output"
            ) from exc

    # --- OAuth -----------------------------------------------------------

    async def probe_providers(self, *, refresh: bool = False) -> dict[str, bool]:
        """Map catalog id -> whether this binary serves its OAuth endpoint.

        Each probe is a real auth-url request (it registers a short-lived
        pending session server-side, which is harmless and expires), so the
        result is cached for the client's lifetime; pass refresh=True after
        repointing or upgrading the proxy. A bad management key raises on the
        first endpoint instead of burning the failure budget on all of them.
        """
        if self._probe_cache is not None and not refresh:
            return dict(self._probe_cache)
        supported: dict[str, bool] = {}
        for spec in CLIPROXY_PROVIDERS:
            try:
                await self._request("GET", f"/{spec.oauth_endpoint}-auth-url")
            except CLIProxyNotFound:
                supported[spec.id] = False
            else:
                supported[spec.id] = True
        self._probe_cache = dict(supported)
        return supported

    async def start_oauth(
        self,
        spec: CLIProxyProviderSpec,
        *,
        project_id: Optional[str] = None,
    ) -> dict[str, str]:
        """Begin a login; returns {url, state} (url is the page to open).

        For device flows the url is the provider's verification page; the
        proxy completes the login server-side once the user approves there.
        """
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        try:
            payload = await self._request(
                "GET",
                f"/{spec.oauth_endpoint}-auth-url",
                params=params or None,
            )
        except CLIProxyNotFound as exc:
            raise CLIProxyUnsupported(
                f"the pinned CLIProxy binary does not support {spec.id} OAuth"
            ) from exc
        url = str((payload or {}).get("url") or "")
        state = str((payload or {}).get("state") or "")
        if not url or not state:
            raise CLIProxyManagementError(
                f"{spec.id} auth-url returned no url/state"
            )
        _ledger_stamp_start(state)
        return {"url": url, "state": state}

    async def oauth_callback(
        self,
        spec: CLIProxyProviderSpec,
        *,
        redirect_url: Optional[str] = None,
        code: Optional[str] = None,
        state: Optional[str] = None,
    ) -> None:
        """Deliver a browser-flow callback (the headless paste path)."""
        if spec.callback_provider is None:
            raise CLIProxyConflict(
                f"{spec.id} uses a device flow; approve it in the browser and "
                "poll the login status instead of delivering a callback"
            )
        body: dict[str, str] = {"provider": spec.callback_provider}
        session_state = (state or "").strip()
        if redirect_url:
            body["redirect_url"] = redirect_url.strip()
            if not session_state:
                try:
                    query = parse_qs(urlparse(redirect_url).query)
                except ValueError:
                    query = {}
                session_state = str((query.get("state") or [""])[0])
        elif code and state:
            body["code"] = code.strip()
            body["state"] = session_state
        else:
            raise ValueError("oauth_callback needs redirect_url or code+state")
        await self._request("POST", "/oauth-callback", json_body=body)
        _ledger_mark_delivered(session_state)

    async def auth_status(self, state: str) -> str:
        """Poll a pending login; returns 'wait', 'ok', or 'error'."""
        payload = await self._request(
            "GET", "/get-auth-status", params={"state": state}
        )
        return str((payload or {}).get("status") or "error")

    # --- Auth files --------------------------------------------------------

    async def list_auth_files(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/auth-files")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            files = payload.get("files")
            if isinstance(files, list):
                return files
        return []

    async def download_auth_file(self, name: str) -> bytes:
        content = await self._request(
            "GET", "/auth-files/download", params={"name": name}, expect_json=False
        )
        return bytes(content or b"")

    async def upload_auth_file(self, name: str, content: bytes) -> None:
        await self._request(
            "POST",
            "/auth-files",
            files={"file": (name, content, "application/json")},
        )

    async def delete_auth_file(self, name: str) -> None:
        await self._request("DELETE", "/auth-files", params={"name": name})

    async def set_auth_file_disabled(self, name: str, *, disabled: bool) -> None:
        await self._request(
            "PATCH",
            "/auth-files/status",
            json_body={"name": name, "disabled": disabled},
        )

    async def set_auth_file_fields(self, name: str, fields: dict[str, Any]) -> None:
        """Patch metadata fields (priority, prefix, proxy_url, ...) by name."""
        if not fields:
            return
        await self._request(
            "PATCH",
            "/auth-files/fields",
            json_body={"name": name, **fields},
        )

    async def ensure_tool_prefix_disabled(self, name: str) -> None:
        """Make sure a Claude auth file carries tool_prefix_disabled: true.

        Inert on v7 binaries (claudeToolPrefix is empty there) but kept true
        for rollback compatibility with v6.9.36, where the proxy otherwise
        prepends proxy_ to tool names and breaks tool-call parsing. Prefers
        the fields PATCH (current binaries persist arbitrary metadata); falls
        back to download -> inject -> re-upload for older ones.
        """
        try:
            await self.set_auth_file_fields(name, {"tool_prefix_disabled": True})
            return
        except CLIProxyManagementError as exc:
            if isinstance(exc, (CLIProxyAuthError, CLIProxyUnreachable)):
                raise
            logger.info(
                "fields PATCH could not set tool_prefix_disabled on %s (%s); "
                "falling back to download/re-upload",
                name,
                exc,
            )
        raw = await self.download_auth_file(name)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CLIProxyManagementError(
                f"auth file {name} is not valid JSON"
            ) from exc
        if data.get("tool_prefix_disabled") is True:
            return
        data["tool_prefix_disabled"] = True
        await self.upload_auth_file(
            name, json.dumps(data, indent=2).encode("utf-8")
        )

    # --- Config knobs -------------------------------------------------------

    async def get_config_knobs(
        self, paths: Sequence[str] = tuple(CONFIG_KNOB_PATHS)
    ) -> dict[str, Any]:
        """Read the surfaced knob subset; missing routes are skipped.

        Keys are the knob paths (e.g. 'request-retry'); a knob the pinned
        binary does not serve is simply absent from the result.
        """
        knobs: dict[str, Any] = {}
        for path in paths:
            try:
                knobs[path] = _unwrap_value(
                    await self._request("GET", f"/{path}"), path
                )
            except CLIProxyNotFound:
                continue
        return knobs

    async def set_config_knob(self, path: str, value: Any) -> None:
        """Write one knob via PUT, wrapping scalars/lists the way the API expects."""
        if path not in CONFIG_KNOB_PATHS:
            raise ValueError(f"unsupported config knob: {path}")
        kind = CONFIG_KNOB_PATHS[path]
        if kind == "list":
            body: Any = {"items": list(value or [])}
        elif kind == "map":
            body = dict(value or {})
        else:
            body = {"value": value}
        await self._request("PUT", f"/{path}", json_body=body)


async def configured_gatekeeper_keys(
    client: CLIProxyManagementClient,
) -> list[str]:
    """The proxy's configured api-keys, READ-ONLY (never mints/writes).

    Any gatekeeper key unlocks every data-plane route (they are not
    provider-scoped). Read paths (model listing, validation) use this;
    mutating resolution belongs to :func:`resolve_or_mint_gatekeeper`,
    which only apply-route may call: writing the api-keys knob flips a
    key-less proxy from an OPEN data plane to key-required, which must
    never happen as a side effect of a read.
    """
    knobs = await client.get_config_knobs(["api-keys"])
    return [
        key
        for key in (knobs.get("api-keys") or [])
        if isinstance(key, str) and key
    ]


async def resolve_or_mint_gatekeeper(client: CLIProxyManagementClient) -> str:
    """First configured api-key, minting one when the proxy has none.

    The mint APPENDS to whatever the knob currently holds (the PUT replaces
    the list, so the raw entries are written back alongside the new key;
    a non-string entry the string filter skips is preserved, never wiped).
    Mutating: only the apply-route path should call this (see
    :func:`configured_gatekeeper_keys`).
    """
    knobs = await client.get_config_knobs(["api-keys"])
    raw = list(knobs.get("api-keys") or [])
    keys = [key for key in raw if isinstance(key, str) and key]
    if keys:
        return keys[0]
    minted = mint_gatekeeper_key()
    await client.set_config_knob("api-keys", raw + [minted])
    return minted


async def confirm_login_landed(
    client: CLIProxyManagementClient,
    state: str,
    spec: CLIProxyProviderSpec,
) -> tuple[str, str]:
    """(status, detail) for a pending login, with confirm-on-ok.

    THE one implementation of the confirm-on-ok invariant (see
    docs/private/cliproxy.md): the proxy's /get-auth-status answers ok for
    unknown or expired sessions, so a bare ok proves nothing. An ok is
    trusted only once an active auth file for the provider exists;
    confirmed ok carries the account label as detail, unconfirmed ok is
    reported as an error explaining the trap. On a RE-login a prior auth
    file exists and would bless an expired session's false ok (the relogin
    trap), so the session ledger's age guard runs first: a paste-less ok
    past SESSION_OK_GUARD_SECONDS is refused (fail-open for sessions this
    process never stamped). Callers own any post-login side effects (e.g.
    the Claude tool_prefix_disabled fixup).
    """
    status = await client.auth_status(state)
    if status == "ok":
        refusal = stale_session_refusal(state)
        if refusal:
            return "error", refusal
        entry = active_login_entry(await client.list_auth_files(), spec)
        if entry is None:
            return (
                "error",
                f"The proxy reported the login complete but lists no active "
                f"{spec.label} auth file (its status endpoint answers ok for "
                "unknown or expired sessions); restart the login.",
            )
        return "ok", login_account_label(entry)
    if status not in ("wait", "error"):
        status = "error"
    return status, ""


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(payload, dict):
        for key in ("message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return str(payload)[:200]


def _unwrap_value(payload: Any, path: str) -> Any:
    """Unwrap the knob GET envelope.

    Live binaries answer `{<last path segment>: value}` (e.g.
    `{"strategy": "round-robin"}` for routing/strategy); older docs show
    `{"value": ...}` / `{"items": ...}`. Bare values pass through.
    """
    if isinstance(payload, dict):
        segment = path.rsplit("/", 1)[-1]
        if set(payload.keys()) == {segment}:
            return payload[segment]
        if set(payload.keys()) <= {"value"}:
            return payload.get("value")
        if set(payload.keys()) <= {"items"}:
            return payload.get("items")
    return payload


__all__ = [
    "CLIProxyAuthError",
    "CLIProxyConflict",
    "CLIProxyManagementClient",
    "CLIProxyManagementError",
    "CLIProxyNotFound",
    "CLIProxyUnreachable",
    "CONFIG_KNOB_PATHS",
    "DEFAULT_TIMEOUT_SECONDS",
    "SESSION_OK_GUARD_SECONDS",
    "active_login_entry",
    "configured_gatekeeper_keys",
    "confirm_login_landed",
    "login_account_label",
    "mint_gatekeeper_key",
    "resolve_or_mint_gatekeeper",
    "stale_session_refusal",
]
