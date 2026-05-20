"""Agent-facing tool: ``request_credential`` — opens an in-chat modal (or
launches an OAuth dance) that lets the user supply a credential, runs a
connection test, and returns success or failure to the agent without the
agent ever seeing the secret.

Modes:
    api_key / pat / form — collected via the modal + hosted form pair. The
        user types the secret into a sandboxed UI; the backend tests it and
        writes it to the vault.
    oauth — covers both ``auth_code`` (browser redirect to a hosted callback)
        and ``device_code`` (RFC 8628 — user enters a short code on a second
        device). Auto-degrades to ``device_code`` when no ``NYMERIA_PUBLIC_URL``
        is configured and the provider supports it.

The flow is described in ``nymeria/core/auth_prompt_coordinator.py``. This
module wires the agent side: create a pending credential, emit an
``auth_prompt`` SSE event, await the coordinator's future, return a
structured JSON result.

Caveat: this tool blocks for up to ``timeout_seconds`` (default 180s). It
should be called as a standalone tool call rather than in a parallel batch —
LangGraph's ToolNode dispatches parallel tool_calls together, so a long await
here will block its siblings.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Optional

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .. import config as config_mod
from ..core.auth_prompt_coordinator import (
    get_auth_prompt_coordinator,
    hash_prompt_token,
    new_prompt_id,
    new_prompt_token,
    prompt_expires_at,
)
from ..core.credential_vault import get_credential_vault_repo
from ..core.event_bus import publish_autonomous_event
from ..core.oauth_device_flow import (
    cancel_poll_task,
    poll_device_token,
    register_poll_task,
)
from ..core.oauth_start import OAuthStartError, OAuthStartResult, start_oauth_flow
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SECONDS = 180
_MIN_TIMEOUT_SECONDS = 15
_MAX_TIMEOUT_SECONDS = 270  # Stay under SafeToolNode's 300s default
_MAX_DESCRIPTION_CHARS = 2000  # Bound the markdown payload sent over SSE


def _generic_fields(provider: str) -> list[dict[str, Any]]:
    """Fallback field schema when the caller doesn't supply ``fields`` and
    no descriptor is available (Phase 1). One secret value, named ``value``."""
    return [
        {
            "name": "value",
            "label": f"{provider} API key",
            "secret": True,
            "placeholder": "",
            "help": "",
        }
    ]


def _existing_accounts_for(repo: Any, user_id: str, provider: str) -> list[dict[str, Any]]:
    provider_norm = provider.strip().lower()
    out: list[dict[str, Any]] = []
    for record in repo.list_credentials(owner_user_id=user_id, include_system=False, include_disabled=False):
        if (record.provider or "").strip().lower() != provider_norm:
            continue
        out.append(
            {
                "credential_id": record.id,
                "name": record.name,
                "account_label": record.account_label,
                "status": record.status,
                "last_used_at": record.last_used_at,
                "last_tested_at": record.last_tested_at,
            }
        )
    return out


def _safe_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip any caller-supplied field values; only the schema is allowed
    over SSE. Secret values must come from the user via the modal, never
    from the agent."""
    out: list[dict[str, Any]] = []
    for raw in fields or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "label": str(raw.get("label") or name),
                "secret": bool(raw.get("secret", True)),
                "placeholder": str(raw.get("placeholder") or ""),
                "help": str(raw.get("help") or ""),
                "kind": str(raw.get("kind") or ("password" if raw.get("secret", True) else "text")),
            }
        )
    return out


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _connect_url(public_url: str | None, prompt_id: str, token: str) -> str | None:
    if not public_url:
        return None
    base = public_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/connect/credentials/{prompt_id}#{token}"


@tool
async def request_credential(
    provider: str,
    kind: str = "api_key",
    account_label: str = "",
    display_name: str = "",
    description: str = "",
    fields: Optional[list[dict[str, Any]]] = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    flow: str = "",
    use_localhost: bool = False,
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Open a secure in-chat prompt (modal / hosted form / OAuth dance) to
    collect credentials from the user.

    For ``kind="api_key"``/``"pat"``/``"form"`` the user types the secret into
    a sandboxed modal — you never see the value. The backend tests the
    connection and returns a structured result.

    For ``kind="oauth"`` the user is sent through the provider's standard
    OAuth flow (browser redirect for ``flow="auth_code"``, on-screen
    short-code for ``flow="device_code"``). Tokens land in the vault as
    ``kind="oauth_token"``. The agent never sees the access or refresh token.

    Args:
        provider: Service identifier. For OAuth, must be in the registry
            (``google_calendar``, ``google_gmail``, ``google_docs``,
            ``google_analytics``, ``google_business_profile``, ``outlook``).
            For API-key modes any lowercase identifier works.
        kind: ``"api_key"`` (default), ``"pat"``, ``"oauth"``, or ``"form"``.
        account_label: Optional label for multi-account scenarios
            (e.g. ``"work"``, ``"personal"``).
        display_name: Optional human-readable name shown in the modal title.
            Defaults to the descriptor's display name or the provider id.
        description: Optional instructions shown above the form (markdown
            supported: bold, lists, links). Use for non-obvious services to
            tell the user where to find the credential.
        fields: Optional schema of secret fields, each
            ``{name, label, secret, placeholder, help, kind}``. ``kind`` is
            ``"password"`` (default for secret=True), ``"text"``, or
            ``"textarea"`` (use for multiline values like service-account
            JSON). Omit to use a single ``value`` field. Ignored for
            ``kind="oauth"`` (the registry supplies scopes).
        timeout_seconds: How long to wait for the user (15-270, default 180).
            On timeout the credential remains in ``pending_setup`` so the
            user can finish later in Settings → Connections.
        flow: For ``kind="oauth"`` only. ``"auth_code"`` for browser-redirect,
            ``"device_code"`` for on-screen short code. Omit to use the
            provider default (auth_code when a redirect URL is available,
            device_code otherwise).
        use_localhost: For ``kind="oauth"`` only. When True, builds an
            auth_code redirect URL pointing at ``http://localhost:<api_port>``
            instead of ``NYMERIA_PUBLIC_URL``. Safe only when the user's
            browser is on the same machine as Nymeria. Set only after the
            user explicitly confirms.

    Returns:
        JSON string with: ``ok``, ``status``, ``credential_id``, ``attempts``,
        ``last_test_error``, ``account_label``, ``message``. OAuth-specific
        statuses include ``missing_public_url`` (agent should ask the user
        whether to set the env var or retry with ``use_localhost=True``),
        ``unknown_provider``, ``unsupported_flow``, ``client_config_missing``,
        ``device_code_request_failed``, ``denied``, ``expired``.

    Caveat: this tool blocks the calling tool batch until the user responds
    (or it times out). Prefer calling it alone, not in parallel with other
    tool calls.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    provider_norm = (provider or "").strip().lower()
    if not provider_norm:
        return _json({"ok": False, "status": "error", "message": "provider is required"})
    timeout = max(_MIN_TIMEOUT_SECONDS, min(_MAX_TIMEOUT_SECONDS, int(timeout_seconds or _DEFAULT_TIMEOUT_SECONDS)))
    kind_norm = (kind or "api_key").strip().lower()
    if kind_norm not in {"api_key", "pat", "oauth", "form"}:
        kind_norm = "api_key"

    repo = get_credential_vault_repo()
    coordinator = get_auth_prompt_coordinator()
    settings = config_mod.get_settings()
    prompt_id = new_prompt_id()
    prompt_token = new_prompt_token()
    label_display = display_name.strip() or provider_norm
    cred_name = label_display
    if account_label.strip():
        cred_name = f"{label_display} ({account_label.strip()})"

    oauth_result: Optional[OAuthStartResult] = None
    oauth_display_name = label_display
    if kind_norm == "oauth":
        start_outcome = await start_oauth_flow(
            provider_id=provider_norm,
            flow_override=flow or None,
            use_localhost=use_localhost,
            prompt_id=prompt_id,
            public_url=getattr(settings, "nymeria_public_url", None),
            api_port=int(getattr(settings, "api_port", 8000) or 8000),
        )
        if isinstance(start_outcome, OAuthStartError):
            # No vault placeholder, no SSE event — early return to agent.
            return _json(
                {
                    "ok": False,
                    "status": start_outcome.status,
                    "credential_id": None,
                    "message": start_outcome.message,
                }
            )
        oauth_result = start_outcome
        oauth_display_name = oauth_result.event_extras.get("display_name") or label_display

    pending_fields = (
        []
        if kind_norm == "oauth"
        else (_safe_fields(fields) if fields else _generic_fields(label_display))
    )

    safe_description = (description or "").strip()
    if len(safe_description) > _MAX_DESCRIPTION_CHARS:
        safe_description = safe_description[:_MAX_DESCRIPTION_CHARS]

    existing_accounts = _existing_accounts_for(repo, user_id, provider_norm)
    token_expires_at, expires_at_iso = prompt_expires_at(timeout)
    if kind_norm == "oauth":
        connect_url = None
    else:
        connect_url = _connect_url(
            getattr(settings, "nymeria_public_url", None),
            prompt_id,
            prompt_token,
        )

    record = repo.create_credential(
        owner_type="user",
        owner_user_id=user_id,
        name=cred_name if kind_norm != "oauth" else (oauth_display_name + (f" ({account_label.strip()})" if account_label.strip() else "")),
        provider=provider_norm,
        kind="oauth_token" if kind_norm == "oauth" else kind_norm,
        account_label=account_label.strip() or None,
        status="pending_setup",
        metadata={
            "setup_session": True,
            "prompt_id": prompt_id,
            "mode": kind_norm,
            "required_fields": [f["name"] for f in pending_fields] if pending_fields else [],
            "source": "request_credential",
            **(
                {"oauth_pending": True, "provider_id": oauth_result.event_extras.get("provider_id")}
                if oauth_result is not None
                else {}
            ),
        },
        created_by_user_id=user_id,
    )

    future = coordinator.register(
        prompt_id=prompt_id,
        credential_id=record.id,
        user_id=user_id,
        thread_id=thread_id,
        provider=provider_norm,
        token_hash=hash_prompt_token(prompt_token),
        token_expires_at=token_expires_at,
        token_expires_at_iso=expires_at_iso,
    )

    if oauth_result is not None:
        event_payload = {
            "prompt_id": prompt_id,
            "credential_id": record.id,
            "provider": provider_norm,
            "display_name": oauth_display_name,
            "description": safe_description,
            "account_label": account_label.strip(),
            "existing_accounts": existing_accounts,
            "timeout_seconds": timeout,
            "expires_at": expires_at_iso,
            **oauth_result.event_extras,
        }
    else:
        event_payload = {
            "prompt_id": prompt_id,
            "credential_id": record.id,
            "provider": provider_norm,
            "display_name": label_display,
            "mode": kind_norm,
            "description": safe_description,
            "fields": pending_fields,
            "account_label": account_label.strip(),
            "existing_accounts": existing_accounts,
            "timeout_seconds": timeout,
            "expires_at": expires_at_iso,
            "connect_url": connect_url,
            "connect_url_required": connect_url is None,
            "connect_url_error": (
                None
                if connect_url
                else "NYMERIA_PUBLIC_URL is required for credential setup links in chat apps."
            ),
        }
    prompt = coordinator.get(prompt_id)
    if prompt is not None:
        prompt.metadata = dict(event_payload)
        if oauth_result is not None:
            prompt.metadata["_oauth_state"] = oauth_result.prompt_state

    publish_autonomous_event(
        event_type="auth_prompt",
        thread_id=thread_id,
        user_id=user_id,
        task_id="",
        data=event_payload,
    )
    try:
        await adispatch_custom_event("auth_prompt", event_payload, config=config)
    except RuntimeError:
        logger.debug("request_credential custom event skipped outside a parent run")

    # Spawn device-code poller AFTER publishing the event (so the user sees
    # the code before the first poll attempt). The poller resolves the future
    # on success or terminal error. The Task handle lives in a module-level
    # registry (not on prompt.metadata) because an asyncio.Task is not JSON
    # serialisable — anything that dumps metadata would crash.
    poll_kwargs = oauth_result.device_poll if oauth_result is not None else None
    if poll_kwargs is not None:
        poll_task = asyncio.create_task(
            poll_device_token(
                prompt_id=poll_kwargs["prompt_id"],
                descriptor=poll_kwargs["descriptor"],
                client_id=poll_kwargs["client_id"],
                client_secret=poll_kwargs["client_secret"] or None,
                device_code=poll_kwargs["device_code"],
                interval=poll_kwargs["interval"],
                expires_in=poll_kwargs["expires_in"],
            ),
            name=f"oauth-device-poll-{prompt_id}",
        )
        register_poll_task(prompt_id, poll_task)

    logger.info(
        "request_credential opened prompt=%s provider=%s user=%s timeout=%ds mode=%s",
        prompt_id,
        provider_norm,
        user_id,
        timeout,
        event_payload.get("mode") or kind_norm,
    )

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        cancel_poll_task(prompt_id)
        # Narrow race: the poller could have finalized between the timer
        # firing and us getting here, leaving the vault active but the
        # future cancelled. Check the vault before reporting "pending".
        post_timeout = repo.get_credential(record.id)
        if post_timeout is not None and post_timeout.status == "active":
            coordinator.discard(prompt_id)
            return _json(
                {
                    "ok": True,
                    "status": "active",
                    "credential_id": record.id,
                    "attempts": 0,
                    "message": (
                        f"User completed {provider_norm} sign-in just as the prompt "
                        "timed out — credential is active and ready to use."
                    ),
                }
            )
        coordinator.discard(prompt_id)
        publish_autonomous_event(
            event_type="auth_prompt_cancelled",
            thread_id=thread_id,
            user_id=user_id,
            task_id="",
            data={"prompt_id": prompt_id, "reason": "tool_timeout"},
        )
        return _json(
            {
                "ok": False,
                "status": "pending",
                "credential_id": record.id,
                "attempts": 0,
                "message": (
                    f"User did not complete the prompt within {timeout}s. "
                    "The credential is still in pending_setup — they can finish "
                    "it later in Settings → Connections."
                ),
            }
        )

    # If a device-code poller is still running (e.g. user cancelled in chat),
    # stop it so it doesn't keep hitting the provider after we've returned.
    cancel_poll_task(prompt_id)

    # Result is enriched with attempts/last_test_error by the coordinator.
    status = result.get("status") or ("active" if result.get("ok") else "error")
    payload = {
        "ok": bool(result.get("ok")),
        "status": status,
        "credential_id": record.id,
        "attempts": result.get("attempts", 0),
        "last_test_error": result.get("last_test_error"),
        "account_label": result.get("account_label") or (account_label.strip() or None),
        "user_message": result.get("user_message"),
        "tested": bool(result.get("tested", False)),
        "test_status": result.get("test_status"),
        "test_error": result.get("test_error") or result.get("last_test_error"),
        "message": result.get("message") or _default_message(status, result.get("last_test_error")),
    }
    return _json(payload)


def _default_message(status: str, last_error: Optional[str]) -> str:
    if status == "active":
        return "User connected successfully and the connection test passed."
    if status == "user_exited":
        if last_error:
            return f"User closed the modal after a failed test. Last error: {last_error}"
        return "User closed the modal without connecting."
    if status == "cancelled":
        return "User cancelled the prompt."
    if status == "pending":
        return "Credential is pending — user can finish in Settings."
    if status == "test_failed":
        return f"Connection test failed: {last_error or 'unknown error'}"
    if status == "user_message":
        return "User responded in chat instead of completing the credential prompt."
    if status == "swept":
        return "Prompt expired before resolution."
    if status == "denied":
        return "User declined the OAuth authorization request."
    if status == "expired":
        return "The OAuth sign-in window expired before the user completed it."
    if status == "missing_public_url":
        return (
            "OAuth cannot start: NYMERIA_PUBLIC_URL is unset and this provider doesn't "
            "support device_code. Ask the user whether to configure NYMERIA_PUBLIC_URL or "
            "retry this tool with use_localhost=True."
        )
    if status == "unknown_provider":
        return "Provider is not registered for OAuth. Use kind='api_key' instead."
    if status == "unsupported_flow":
        return "Requested OAuth flow is not supported by this provider."
    if status == "client_config_missing":
        return "OAuth client config is missing on the server."
    if status == "device_code_request_failed":
        return "Could not start the device-code flow with the provider."
    return "Prompt resolved without a clear status."


REQUEST_CREDENTIAL_TOOLS = [request_credential]


__all__ = ["request_credential", "REQUEST_CREDENTIAL_TOOLS"]
