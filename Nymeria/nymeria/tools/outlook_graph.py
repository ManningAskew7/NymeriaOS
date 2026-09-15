"""Shared Microsoft Graph layer for the Outlook tool family.

Everything the ``outlook_*`` tools have in common lives here so that it exists
ONCE: account selection and token refresh, the request wrapper (single and
``$batch``), the mailbox root (``/me`` or ``/users/{upn}`` for a shared
mailbox), the folder resolver (alias, display name, ``Parent/Child`` path, or
raw id), the scope gate that turns a Graph 403 into a reconnect instruction,
and the multi-target contract every mutation tool shares.

Tokens are minted by ``request_credential(provider="outlook", kind="oauth")``
and live in the vault; legacy ``microsoft.json`` files are still honoured via
:func:`auth_cache_utils.resolve_oauth_cache`.

Egress: both hosts this module reaches are module constants
(``MICROSOFT_TOKEN_URI`` and ``GRAPH_BASE``); a caller only ever supplies a
PATH segment, never a host. ``tests/test_service_integration_egress.py``
declares that carve-out for this file.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Iterable, Optional
from urllib.parse import quote

from ..config.oauth_providers import MICROSOFT_TOKEN_URI, OUTLOOK_SCOPES, get_oauth_provider
from ..core.http_policy import policy_http_client as _http_client
from . import auth_cache_utils as auth_utils
from .auth_cache_utils import OAuthAccountSelectionError
from .utils import ambient_thread_id

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Token refresh endpoint, from the OAuth registry. Microsoft is a PUBLIC client
# here (the descriptor has a ``client_id_fallback`` and no ``client_secret_env``),
# so what rides this address is the user's refresh token rather than an operator
# secret. Still a destination worth pinning, and a second literal would be a
# second place to drift.
TOKEN_URL = MICROSOFT_TOKEN_URI

# Legacy Microsoft token cache filename (device-code flow, pre-vault).
OUTLOOK_CACHE_FILENAME = "microsoft.json"

# Graph ``$batch`` accepts at most 20 sub-requests per call.
BATCH_LIMIT = 20

# Multi-target mutations refuse above this many messages in one call: a bulk
# pass over a whole folder is several calls, each with a legible result, not
# one call whose outcome the agent cannot read.
MAX_TARGETS = 50

# Well-known folder names Graph accepts directly in a URL segment, keyed by the
# spellings agents actually use. ONE table for the whole family (this used to
# exist as three divergent copies).
FOLDER_ALIASES: dict[str, str] = {
    "inbox": "inbox",
    "sent": "sentitems",
    "sentitems": "sentitems",
    "sent items": "sentitems",
    "drafts": "drafts",
    "deleted": "deleteditems",
    "deleteditems": "deleteditems",
    "deleted items": "deleteditems",
    "trash": "deleteditems",
    "bin": "deleteditems",
    "junk": "junkemail",
    "junkemail": "junkemail",
    "junk email": "junkemail",
    "spam": "junkemail",
    "archive": "archive",
    "outbox": "outbox",
    "clutter": "clutter",
    "conversationhistory": "conversationhistory",
    "scheduled": "scheduled",
}

# Scopes the newer organisation tools need beyond the mail read/write pair.
SCOPE_MAILBOX_SETTINGS = "MailboxSettings.ReadWrite"
SCOPE_SHARED_READ = "Mail.ReadWrite.Shared"
SCOPE_SHARED_SEND = "Mail.Send.Shared"

# Internet message headers worth surfacing to an agent. Everything else is
# transport noise (Received chains, DKIM blobs, X-MS-Exchange-* internals).
INTERESTING_HEADERS: tuple[str, ...] = (
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "List-Id",
    "Auto-Submitted",
    "Precedence",
    "Return-Path",
    "Reply-To",
    "Message-ID",
    "In-Reply-To",
    "References",
    "X-Priority",
)

_RECONNECT_HINT = (
    'request_credential(provider="outlook", kind="oauth")'
)


class OutlookTokenError(Exception):
    """A connected account exists but no usable access token could be produced.

    Distinct from :class:`OAuthAccountSelectionError` (which account) so the
    message can say WHICH account failed and WHY (expired with a dead refresh
    token, refresh endpoint rejected it) instead of "no authenticated account",
    which sends an agent looking for a missing credential when the credential
    is right there and merely dead.
    """


# ---------------------------------------------------------------------------
# Account selection and tokens
# ---------------------------------------------------------------------------


def _client_id(account: Optional[dict] = None) -> str:
    """The OAuth client id for token requests: the account's own, else the registry's."""
    if account and account.get("client_id"):
        return str(account["client_id"])
    descriptor = get_oauth_provider("outlook")
    if descriptor is not None:
        env_name = getattr(descriptor, "client_id_env", None)
        if env_name and os.environ.get(env_name):
            return os.environ[env_name]
        fallback = getattr(descriptor, "client_id_fallback", None)
        if fallback:
            return str(fallback)
    return os.environ.get("MICROSOFT_MCP_CLIENT_ID", "")


def _select_account(
    accounts: dict,
    account_id: Optional[str] = None,
    *,
    thread_id: Optional[str] = None,
    thread_bound: bool = False,
) -> tuple[str, dict]:
    """Resolve which Microsoft account to use from a non-empty account view.

    Priority: explicit account_id > OUTLOOK_DEFAULT_ACCOUNT_ID setting > the
    only visible account. ``accounts`` is the view ``resolve_oauth_cache``
    returned for the calling thread, so a thread bound to one mailbox sees
    exactly that mailbox here. Several visible accounts with none selected
    raise ``OAuthAccountSelectionError`` (the shared picker's rules); so does
    an explicit ``account_id`` outside the view.
    """
    default_id = None
    try:
        from ..config import get_settings

        default_id = get_settings().outlook_default_account_id
    except Exception:
        logger.debug("Failed to resolve default Outlook account from settings")
    return auth_utils.select_oauth_account(
        accounts,
        account_id,
        default_id=default_id,
        thread_id=thread_id,
        thread_bound=thread_bound,
        provider_label="Outlook",
    )


def _resolve_outlook_source(user_id: str, thread_id: Optional[str]):
    return auth_utils.resolve_oauth_cache(
        user_id,
        "outlook",
        cache_filename=OUTLOOK_CACHE_FILENAME,
        thread_id=thread_id,
    )


def get_account(
    user_id: str,
    account_id: Optional[str] = None,
    *,
    thread_id: Optional[str] = None,
) -> Optional[dict]:
    """Get account info from the vault plus legacy cache.

    A binding for the calling thread (``thread_id``, or the ambient tool-call
    thread) narrows the visible accounts first; then explicit account_id >
    OUTLOOK_DEFAULT_ACCOUNT_ID setting > the only visible account. All reads
    are scoped to the Nymeria ``user_id``; another user's Microsoft accounts
    are invisible. Vault-stored credentials win over legacy files on
    account_id collision. Raises ``OAuthAccountSelectionError`` when several
    accounts are visible and nothing selects between them.
    """
    thread_id = thread_id or ambient_thread_id()
    source = _resolve_outlook_source(user_id, thread_id)
    accounts = source.accounts
    if not accounts:
        return None
    return _select_account(
        accounts, account_id, thread_id=thread_id, thread_bound=source.thread_bound
    )[1]


def try_complete_pending_auth(user_id: str) -> bool:
    """Try to complete a pending legacy device-code auth if the user signed in.

    Called when no accounts exist, to check whether a pending flow can be
    completed (user finished the sign-in but we never polled). Legacy-only:
    the vault flow has its own poller.
    """
    cache = auth_utils.load_token_cache(user_id, OUTLOOK_CACHE_FILENAME)
    pending = cache.get("pending_auth")
    if not pending:
        return False

    device_code = pending.get("device_code")
    expires_at = pending.get("expires_at", 0)
    if not device_code or time.time() > expires_at:
        return False

    try:
        with _http_client(timeout=10) as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": _client_id(),
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                },
            )

        if response.status_code == 200:
            data = response.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)

            email, name = "unknown", "Unknown User"
            try:
                with _http_client(timeout=10) as client:
                    user_response = client.get(
                        f"{GRAPH_BASE}/me",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                if user_response.status_code == 200:
                    user_info = user_response.json()
                    email = user_info.get("mail") or user_info.get("userPrincipalName", "unknown")
                    name = user_info.get("displayName", "Unknown User")
            except Exception:
                pass

            account_id = email.lower().replace("@", "_at_").replace(".", "_")
            cache["accounts"] = cache.get("accounts", {})
            cache["accounts"][account_id] = {
                "email": email,
                "name": name,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": time.time() + expires_in,
            }
            cache.pop("pending_auth", None)
            auth_utils.save_token_cache(user_id, OUTLOOK_CACHE_FILENAME, cache)
            logger.info("Auto-completed pending auth for %s", email)
            return True
    except Exception as e:
        logger.debug("Pending auth not ready: %s", e)
    return False


def _refresh_scope_string(account: dict) -> str:
    """The scopes a refresh must request: exactly what the credential holds.

    Entra issues the refreshed access token for the REQUESTED subset, so a
    hardcoded list here silently drops every consented scope it omits about an
    hour after connect (Calendars, Teams, MailboxSettings all vanished that way
    before this helper). Legacy accounts carry no scope list; they get the
    registry set, which is what they were consented with.
    """
    stored = account.get("scopes")
    scopes = [s for s in stored if isinstance(s, str) and s.strip()] if isinstance(stored, list) else []
    if not scopes:
        scopes = list(OUTLOOK_SCOPES)
    # offline_access is what makes a refresh token come back at all; Entra does
    # not echo it in the granted-scope field, so add it when the stored list
    # lacks it rather than lose the rotation.
    if "offline_access" not in scopes:
        scopes.insert(0, "offline_access")
    return " ".join(scopes)


def acquire_access_token(
    user_id: str,
    account_id: Optional[str] = None,
    *,
    thread_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """Return ``(access_token, account)`` for this user, refreshing if needed.

    Raises ``OAuthAccountSelectionError`` when several accounts are visible and
    nothing selects between them, and :class:`OutlookTokenError` when the
    selected account cannot produce a token (no refresh token, refresh
    rejected, transport failure). Returns ``(None, None)`` only when no account
    is connected at all.
    """
    thread_id = thread_id or ambient_thread_id()
    source = _resolve_outlook_source(user_id, thread_id)
    cache = source.cache  # the FULL set: the refresh write-back and persist payload
    accounts = source.accounts  # the thread's view: what selection chooses from

    if not accounts:
        if try_complete_pending_auth(user_id):
            source = _resolve_outlook_source(user_id, thread_id)
            cache = source.cache
            accounts = source.accounts
        if not accounts:
            return None, None

    aid, account = _select_account(
        accounts, account_id, thread_id=thread_id, thread_bound=source.thread_bound
    )
    label = account.get("email") or aid

    expires_at = account.get("expires_at", 0)
    if time.time() < expires_at - 60:  # 60 second buffer
        token = account.get("access_token")
        if token:
            return token, account
        raise OutlookTokenError(
            f"Outlook account '{label}' ({aid}) has no access token stored. "
            f"Reconnect it with {_RECONNECT_HINT}."
        )

    refresh_token = account.get("refresh_token")
    if not refresh_token:
        raise OutlookTokenError(
            f"Outlook account '{label}' ({aid}) has an expired access token and no "
            f"refresh token, so it cannot be renewed. Reconnect it with {_RECONNECT_HINT}."
        )

    try:
        with _http_client(timeout=30) as client:
            response = client.post(
                TOKEN_URL,
                data={
                    "client_id": _client_id(account),
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": _refresh_scope_string(account),
                },
            )
    except Exception as e:
        logger.error("Outlook token refresh transport failure for %s: %s", aid, e)
        raise OutlookTokenError(
            f"Outlook account '{label}' ({aid}): token refresh request failed ({e}). "
            "Retry shortly; if it persists, reconnect the account."
        ) from e

    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("error") or ""
        except Exception:
            detail = (response.text or "")[:200]
        logger.error("Outlook token refresh rejected for %s: %s %s", aid, response.status_code, detail)
        raise OutlookTokenError(
            f"Outlook account '{label}' ({aid}): the access token expired and the refresh "
            f"was rejected ({response.status_code}: {detail or 'no detail'}). "
            f"Reconnect it with {_RECONNECT_HINT}."
        )

    data = response.json()
    account["access_token"] = data.get("access_token")
    account["refresh_token"] = data.get("refresh_token", refresh_token)
    account["expires_at"] = time.time() + data.get("expires_in", 3600)
    granted = data.get("scope")
    if isinstance(granted, str) and granted.strip():
        account["scopes"] = granted.split()
    cache["accounts"][aid] = account
    source.persist(cache)
    return account["access_token"], account


def get_access_token(
    user_id: str,
    account_id: Optional[str] = None,
    *,
    thread_id: Optional[str] = None,
) -> Optional[str]:
    """Get a valid access token, refreshing if needed; ``None`` when unavailable.

    The tolerant shape for autonomous callers (trigger sources, notification
    channels) that treat "no token" as "skip this cycle". Tool bodies go
    through :func:`graph_request`, which surfaces WHY via
    :class:`OutlookTokenError`. Selection errors still propagate: polling the
    wrong mailbox is never the safe default.
    """
    try:
        token, _ = acquire_access_token(user_id, account_id, thread_id=thread_id)
    except OutlookTokenError as e:
        logger.warning("%s", e)
        return None
    return token


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def mail_root(mailbox: Optional[str] = None) -> str:
    """``/me`` for the signed-in account, ``/users/{upn}`` for a shared mailbox."""
    if mailbox and mailbox.strip():
        return f"/users/{quote(mailbox.strip(), safe='@')}"
    return "/me"


def _apply_mailbox(endpoint: str, mailbox: Optional[str]) -> str:
    """Rewrite a ``/me`` endpoint onto a shared mailbox root when one is given."""
    if not mailbox or not mailbox.strip():
        return endpoint
    if endpoint == "/me" or endpoint.startswith("/me/"):
        return mail_root(mailbox) + endpoint[3:]
    return endpoint


def _no_account_error() -> str:
    return f"No authenticated Outlook account. Call {_RECONNECT_HINT} to connect."


def _graph_error_text(status: int, payload: Any, fallback: str) -> str:
    message = fallback
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message") or message
            code = err.get("code")
            if code and code not in message:
                message = f"{code}: {message}"
    return f"API Error ({status}): {message}"


def graph_request(
    user_id: str,
    method: str,
    endpoint: str,
    account_id: Optional[str] = None,
    json_data: Optional[dict] = None,
    params: Optional[dict] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
    headers: Optional[dict] = None,
    timeout: float = 30,
) -> tuple[bool, Any]:
    """Make one Graph API request on behalf of ``user_id``.

    ``endpoint`` is a path under ``GRAPH_BASE`` written against ``/me``; when
    ``mailbox`` is given the ``/me`` prefix is rewritten to ``/users/{upn}`` so
    every tool addresses a shared mailbox with one pass-through argument.
    Returns ``(True, json_or_empty_dict)`` or ``(False, error_text)``. Account
    selection and token errors come back as ``(False, text)`` with the account
    named, never as exceptions.
    """
    try:
        token, _ = acquire_access_token(user_id, account_id, thread_id=thread_id)
    except (OAuthAccountSelectionError, OutlookTokenError) as e:
        return False, str(e)
    if not token:
        return False, _no_account_error()

    url = f"{GRAPH_BASE}{_apply_mailbox(endpoint, mailbox)}"
    request_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)

    try:
        with _http_client(timeout=timeout) as client:
            response = client.request(
                method=method,
                url=url,
                headers=request_headers,
                json=json_data,
                params=params,
            )
        if response.status_code >= 400:
            try:
                payload = response.json() if response.text else {}
            except Exception:
                payload = {}
            return False, _graph_error_text(response.status_code, payload, response.text)
        if response.status_code in (202, 204) or not response.text:
            return True, {}
        return True, response.json()
    except Exception as e:
        return False, f"Request failed: {e}"


def graph_request_raw(
    user_id: str,
    method: str,
    endpoint: str,
    account_id: Optional[str] = None,
    *,
    content: Optional[bytes] = None,
    headers: Optional[dict] = None,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
    absolute_url: Optional[str] = None,
    timeout: float = 60,
) -> tuple[bool, Any]:
    """A Graph request with a raw byte body (attachment upload sessions).

    ``absolute_url`` is used verbatim when given: upload-session URLs are
    Graph-issued absolute addresses on the same host family. Returns
    ``(True, json_or_headers_dict)`` or ``(False, error_text)``.
    """
    try:
        token, _ = acquire_access_token(user_id, account_id, thread_id=thread_id)
    except (OAuthAccountSelectionError, OutlookTokenError) as e:
        return False, str(e)
    if not token:
        return False, _no_account_error()

    url = absolute_url or f"{GRAPH_BASE}{_apply_mailbox(endpoint, mailbox)}"
    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)
    try:
        with _http_client(timeout=timeout) as client:
            response = client.request(method=method, url=url, headers=request_headers, content=content)
        if response.status_code >= 400:
            try:
                payload = response.json() if response.text else {}
            except Exception:
                payload = {}
            return False, _graph_error_text(response.status_code, payload, response.text)
        if not response.text:
            return True, {"status": response.status_code, "headers": dict(response.headers)}
        try:
            return True, response.json()
        except Exception:
            return True, {"status": response.status_code, "headers": dict(response.headers)}
    except Exception as e:
        return False, f"Request failed: {e}"


def graph_upload_put(
    upload_url: str,
    content: bytes,
    *,
    content_range: str,
    timeout: float = 120,
) -> tuple[bool, Any]:
    """PUT one byte range of an attachment upload session.

    The session URL Graph hands back (``uploadUrl``) lives on
    ``outlook.office.com`` and is PRE-AUTHENTICATED: Microsoft's contract is
    that the PUT carries no ``Authorization`` header at all. The URL is
    vendor-issued from a Graph response body, never caller-supplied. Returns
    ``(True, {"status", "headers", "body"})`` or ``(False, error_text)``.
    """
    if not upload_url.startswith("https://outlook.office.com/") and not upload_url.startswith("https://outlook.office365.com/"):
        return False, "Upload session URL is not on the Outlook service host; refusing to send bytes to it."
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(content)),
        "Content-Range": content_range,
    }
    try:
        with _http_client(timeout=timeout) as client:
            response = client.request(method="PUT", url=upload_url, headers=headers, content=content)
        if response.status_code >= 400:
            try:
                payload = response.json() if response.text else {}
            except Exception:
                payload = {}
            return False, _graph_error_text(response.status_code, payload, response.text)
        body: Any = {}
        if response.text:
            try:
                body = response.json()
            except Exception:
                body = {}
        return True, {"status": response.status_code, "headers": dict(response.headers), "body": body}
    except Exception as e:
        return False, f"Upload request failed: {e}"


def graph_batch(
    user_id: str,
    requests: list[dict],
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> list[dict]:
    """Run ``requests`` through Graph ``$batch`` in chunks of :data:`BATCH_LIMIT`.

    Each request is ``{"id": str, "method": str, "url": "/me/...", "body"?: dict,
    "headers"?: dict}`` (``url`` written against ``/me``, rewritten for a shared
    mailbox like :func:`graph_request`). Returns one dict per request, in input
    order: ``{"id", "status": int, "ok": bool, "body": Any, "error": str | None}``.
    A chunk that fails as a whole (auth, transport) yields a failure entry per
    request in it, so callers always get exactly ``len(requests)`` results.
    """
    results_by_id: dict[str, dict] = {}
    if not requests:
        return []

    try:
        token, _ = acquire_access_token(user_id, account_id, thread_id=thread_id)
        whole_error = None if token else _no_account_error()
    except (OAuthAccountSelectionError, OutlookTokenError) as e:
        token, whole_error = None, str(e)

    for start in range(0, len(requests), BATCH_LIMIT):
        chunk = requests[start : start + BATCH_LIMIT]
        if whole_error:
            for req in chunk:
                results_by_id[str(req["id"])] = {
                    "id": str(req["id"]), "status": 0, "ok": False, "body": None, "error": whole_error,
                }
            continue
        payload = []
        for req in chunk:
            item: dict[str, Any] = {
                "id": str(req["id"]),
                "method": req["method"].upper(),
                "url": _apply_mailbox(req["url"], mailbox),
            }
            if req.get("body") is not None:
                item["body"] = req["body"]
                item["headers"] = {"Content-Type": "application/json", **(req.get("headers") or {})}
            elif req.get("headers"):
                item["headers"] = dict(req["headers"])
            payload.append(item)

        chunk_error: Optional[str] = None
        responses: list[dict] = []
        try:
            with _http_client(timeout=60) as client:
                response = client.request(
                    method="POST",
                    url=f"{GRAPH_BASE}/$batch",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"requests": payload},
                )
            if response.status_code >= 400:
                try:
                    body = response.json() if response.text else {}
                except Exception:
                    body = {}
                chunk_error = _graph_error_text(response.status_code, body, response.text)
            else:
                responses = list((response.json() or {}).get("responses") or [])
        except Exception as e:
            chunk_error = f"Request failed: {e}"

        if chunk_error:
            for req in chunk:
                results_by_id[str(req["id"])] = {
                    "id": str(req["id"]), "status": 0, "ok": False, "body": None, "error": chunk_error,
                }
            continue

        seen: set[str] = set()
        for resp in responses:
            rid = str(resp.get("id"))
            status = int(resp.get("status") or 0)
            body = resp.get("body")
            ok = 200 <= status < 300
            results_by_id[rid] = {
                "id": rid,
                "status": status,
                "ok": ok,
                "body": body,
                "error": None if ok else _graph_error_text(status, body, str(body)),
            }
            seen.add(rid)
        for req in chunk:
            rid = str(req["id"])
            if rid not in seen:
                results_by_id[rid] = {
                    "id": rid, "status": 0, "ok": False, "body": None,
                    "error": "No response for this item in the batch reply",
                }

    return [results_by_id[str(req["id"])] for req in requests]


# ---------------------------------------------------------------------------
# Scope gate
# ---------------------------------------------------------------------------


def require_scopes(
    user_id: str,
    needed: Iterable[str],
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
    purpose: str = "this operation",
) -> Optional[str]:
    """Return an error string when the selected account lacks a needed scope.

    Graph answers a missing scope with a 403 whose text does not say which scope
    or how to fix it. This checks the granted list stored on the credential
    first and names the reconnect. A ``mailbox`` other than the signed-in
    account additionally needs the ``.Shared`` scopes. Legacy accounts carry no
    scope list and pass (Graph itself is the gate for them). Selection and
    token problems are reported too, so a tool can call this once up front.
    """
    try:
        account = get_account(user_id, account_id, thread_id=thread_id)
    except OAuthAccountSelectionError as e:
        return str(e)
    if account is None:
        return _no_account_error()

    wanted = {s for s in needed if s}
    if mailbox and mailbox.strip():
        wanted.add(SCOPE_SHARED_READ)
    granted = account.get("scopes")
    if not isinstance(granted, list) or not granted:
        return None  # legacy row: unknown grants, let Graph decide
    granted_set = {str(s).split("/")[-1] for s in granted}  # tolerate URL-form scopes
    missing = sorted(s for s in wanted if s not in granted_set)
    if not missing:
        return None
    label = account.get("email") or account_id or "the selected account"
    return (
        f"Outlook account '{label}' was connected without the permission(s) needed for "
        f"{purpose}: {', '.join(missing)}. Reconnect it with {_RECONNECT_HINT} to grant them "
        "(the sign-in will ask for the new permissions; existing tools keep working meanwhile)."
    )


# ---------------------------------------------------------------------------
# Folder resolution
# ---------------------------------------------------------------------------

_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-=]{40,}$")


def looks_like_graph_id(value: str) -> bool:
    """Graph ids are long URL-safe base64 strings; folder names never are."""
    return bool(_FOLDER_ID_RE.match(value.strip()))


def _resolve_folder_alias(folder: str) -> tuple[bool, str]:
    """Resolve a well-known folder alias to its Graph well-known name.

    Kept for callers that only accept the well-known set (the poll source
    and older tests). New code uses :func:`resolve_folder`.
    """
    key = folder.lower().strip()
    if key in FOLDER_ALIASES:
        return True, FOLDER_ALIASES[key]
    options = ", ".join(sorted(set(FOLDER_ALIASES)))
    return False, f"Invalid folder. Expected one of: {options}."


def _list_child_folders(
    user_id: str,
    parent_segment: str,
    account_id: Optional[str],
    *,
    mailbox: Optional[str],
    thread_id: Optional[str],
) -> tuple[bool, Any]:
    endpoint = (
        f"/me/mailFolders/{parent_segment}/childFolders" if parent_segment else "/me/mailFolders"
    )
    return graph_request(
        user_id, "GET", endpoint, account_id=account_id,
        params={"$top": 250, "$select": "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount"},
        mailbox=mailbox, thread_id=thread_id,
    )


def resolve_folder(
    user_id: str,
    folder: str,
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> tuple[bool, str]:
    """Resolve ``folder`` to a URL segment Graph accepts: well-known name or id.

    Accepts a well-known alias (``inbox``, ``sent``, ``trash``...), a raw Graph
    folder id, a top-level display name (``Clients``), or a slash path from the
    root (``Clients/Acme``). Display names match case-insensitively; a name
    that matches more than one sibling is refused with both ids listed rather
    than guessed. Returns ``(True, segment)`` or ``(False, error_text)``.
    """
    raw = (folder or "").strip()
    if not raw:
        return False, "Folder name is required."
    key = raw.lower()
    if key in FOLDER_ALIASES:
        return True, FOLDER_ALIASES[key]
    if looks_like_graph_id(raw):
        return True, raw

    segments = [s.strip() for s in raw.split("/") if s.strip()]
    if not segments:
        return False, "Folder name is required."
    # A well-known alias may head a path ("inbox/Clients").
    parent_segment = ""
    if segments[0].lower() in FOLDER_ALIASES and len(segments) > 1:
        parent_segment = FOLDER_ALIASES[segments[0].lower()]
        segments = segments[1:]

    for depth, name in enumerate(segments):
        ok, result = _list_child_folders(
            user_id, parent_segment, account_id, mailbox=mailbox, thread_id=thread_id
        )
        if not ok:
            return False, str(result)
        matches = [
            f for f in (result.get("value") or [])
            if str(f.get("displayName", "")).strip().lower() == name.lower()
        ]
        where = "/".join(segments[:depth]) or "the mailbox root"
        if not matches:
            return False, (
                f"No folder named '{name}' under {where}. Use outlook_list_folders to see the "
                "tree, or outlook_manage_folder(action=\"create\") to create it."
            )
        if len(matches) > 1:
            listing = "; ".join(f"{m.get('displayName')} (id {m.get('id')})" for m in matches)
            return False, (
                f"Folder name '{name}' is ambiguous under {where}: {listing}. Pass the folder id."
            )
        parent_segment = str(matches[0]["id"])
    return True, parent_segment


def folder_display_name(
    user_id: str,
    segment: str,
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> str:
    """Best-effort display name for a folder segment (falls back to the segment)."""
    ok, result = graph_request(
        user_id, "GET", f"/me/mailFolders/{segment}", account_id=account_id,
        params={"$select": "displayName"}, mailbox=mailbox, thread_id=thread_id,
    )
    if ok and isinstance(result, dict) and result.get("displayName"):
        return str(result["displayName"])
    return segment


# ---------------------------------------------------------------------------
# Multi-target contract
# ---------------------------------------------------------------------------


def split_ids(value: str) -> list[str]:
    """Split a comma- or newline-separated id list, dropping blanks and duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,\n]", value or ""):
        p = part.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_targets(
    user_id: str,
    *,
    email_id: str = "",
    email_ids: str = "",
    conversation_id: str = "",
    account_id: Optional[str] = None,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
    include_deleted: bool = False,
    max_targets: int = MAX_TARGETS,
) -> tuple[bool, Any]:
    """Resolve the shared ``email_id | email_ids | conversation_id`` contract.

    Exactly one selector is used, in that precedence. ``conversation_id``
    expands to every message in the conversation across folders, skipping
    Deleted Items unless ``include_deleted``. Returns ``(True, [ids])`` or
    ``(False, error_text)``; more than ``max_targets`` ids is refused BEFORE
    any mutation so a bulk pass is several legible calls, not one opaque one.
    """
    ids: list[str]
    if email_ids.strip():
        ids = split_ids(email_ids)
    elif email_id.strip():
        ids = [email_id.strip()]
    elif conversation_id.strip():
        conv = conversation_id.strip().replace("'", "''")
        ok, result = graph_request(
            user_id, "GET", "/me/messages", account_id=account_id,
            params={
                "$filter": f"conversationId eq '{conv}'",
                "$select": "id,parentFolderId",
                "$top": max_targets + 1,
            },
            mailbox=mailbox, thread_id=thread_id,
        )
        if not ok:
            return False, str(result)
        messages = list(result.get("value") or [])
        if not include_deleted and messages:
            ok_d, deleted = graph_request(
                user_id, "GET", "/me/mailFolders/deleteditems", account_id=account_id,
                params={"$select": "id"}, mailbox=mailbox, thread_id=thread_id,
            )
            deleted_id = deleted.get("id") if ok_d and isinstance(deleted, dict) else None
            if deleted_id:
                messages = [m for m in messages if m.get("parentFolderId") != deleted_id]
        ids = [str(m["id"]) for m in messages if m.get("id")]
        if not ids:
            return False, f"No messages found in conversation '{conversation_id.strip()[:24]}...'."
    else:
        return False, "Provide an email_id, comma-separated email_ids, or a conversation_id."

    if len(ids) > max_targets:
        return False, (
            f"{len(ids)} messages selected; the limit is {max_targets} per call. Split the work "
            "into several calls (or narrow the selection) so each result stays readable."
        )
    return True, ids


def batch_patch_messages(
    user_id: str,
    ids: list[str],
    body: dict,
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> list[dict]:
    """PATCH the same body onto every message id via ``$batch``."""
    requests = [
        {"id": str(i), "method": "PATCH", "url": f"/me/messages/{mid}", "body": body}
        for i, mid in enumerate(ids)
    ]
    return graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox, thread_id=thread_id)


def batch_post_messages(
    user_id: str,
    ids: list[str],
    action: str,
    body: Optional[dict],
    account_id: Optional[str] = None,
    *,
    mailbox: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> list[dict]:
    """POST ``/messages/{id}/{action}`` with ``body`` for every id via ``$batch``."""
    requests = [
        {"id": str(i), "method": "POST", "url": f"/me/messages/{mid}/{action}", "body": body or {}}
        for i, mid in enumerate(ids)
    ]
    return graph_batch(user_id, requests, account_id=account_id, mailbox=mailbox, thread_id=thread_id)


def summarize_batch(results: list[dict], ids: list[str], verb: str) -> str:
    """Render per-message outcomes: one line when all succeed, a list otherwise."""
    failures = [(ids[int(r["id"])], r["error"]) for r in results if not r["ok"]]
    total = len(ids)
    if not failures:
        if total == 1:
            return f"[Success]: 1 message {verb}."
        return f"[Success]: {total} messages {verb}."
    lines = [f"[Partial]: {total - len(failures)}/{total} messages {verb}; {len(failures)} failed:"]
    for mid, err in failures:
        lines.append(f"  - {mid[:28]}...: {err}")
    if len(failures) == total:
        lines[0] = f"[Error]: 0/{total} messages {verb}:"
    return "\n".join(lines)


def odata_quote(value: str) -> str:
    """Escape a string literal for an OData ``$filter`` clause."""
    return value.replace("'", "''")
