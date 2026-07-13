"""Agent-facing tool: ``request_credential`` opens an in-chat modal (or
launches an OAuth dance) that lets the user supply a credential, runs a
connection test, and lands the secret in the vault. The agent never sees
the secret.

Modes:
    api_key / pat / form: collected via the modal + hosted form pair. The
        user types the secret into a sandboxed UI; the backend tests it and
        writes it to the vault.
    oauth: covers both ``auth_code`` (browser redirect to a hosted callback)
        and ``device_code`` (RFC 8628, user enters a short code on a second
        device). Auto-degrades to ``device_code`` when no ``NYMERIA_PUBLIC_URL``
        is configured and the provider supports it.

The flow is described in ``nymeria/core/auth_prompt_coordinator.py``.

Fire-and-forget contract: this tool returns IMMEDIATELY with
``status="dispatched"``. The agent does not block waiting for the user.

When the user submits the prompt the credential lands in the vault and the
coordinator's future resolves. The future's done-callback runs the bind
side-effects (e.g. add_allowed_target + bind_credential +
mcp_manager.shutdown_server when ``bind_target`` was set) and then exits.
**No automatic follow-up turn fires.** The user drives the next step:
either by retrying their original request ("ok, try that again") or by
copying any inline error from the modal back into the chat. This keeps
the user in control and avoids surprise turns firing in the background.

Because the tool no longer awaits, it can safely be called alongside
other tool calls in a parallel batch.
"""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import asyncio
import json
import logging
import re
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
_MAX_INSTRUCTIONS_CHARS = 4000  # Step-by-step text can be longer than description

# Format for bind_target: ``"type:id"``. The type whitelist matches existing
# allowed_targets conventions (``mcp_server:<id>`` used by mcp_runtime,
# ``native_tool:<name>`` used by native_credentials).
_BIND_TARGET_RE = re.compile(r"^(mcp_server|native_tool):[A-Za-z0-9_\-]{1,64}$")


def _generic_fields(provider: str) -> list[dict[str, Any]]:
    """Fallback field schema when the caller doesn't supply ``fields`` and
    no descriptor is available. One secret value, named ``value``."""
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
    instructions: str = "",
    fields: Optional[list[dict[str, Any]]] = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    flow: str = "",
    use_localhost: bool = False,
    bind_target: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Open a secure in-chat prompt (modal / hosted form / OAuth dance) to
    collect credentials from the user.

    For ``kind="api_key"``/``"pat"``/``"form"`` the user types the secret into
    a sandboxed modal, you never see the value. The backend tests the
    connection and returns a structured result.

    For ``kind="oauth"`` the user is sent through the provider's standard
    OAuth flow (browser redirect for ``flow="auth_code"``, on-screen
    short-code for ``flow="device_code"``). Tokens land in the vault as
    ``kind="oauth_token"``. The agent never sees the access or refresh token.

    Fire-and-forget contract: this tool returns IMMEDIATELY with
    ``status="dispatched"``. When the user finishes (or cancels), no fresh
    agent turn fires automatically. The agent should write a short
    user-facing acknowledgement after dispatching and ask the user to
    reply when they want the original task retried.

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
        description: Optional 1 to 2 sentence summary of what this connection
            is for, shown above the form (markdown supported: bold, lists,
            links). Keep it short.
        instructions: Optional step-by-step instructions for HOW the user
            obtains or signs in to this credential, shown above the form in
            a highlighted callout (markdown supported). Use this to tailor
            guidance to context: where in the provider's dashboard the API
            key lives, which account to pick during OAuth consent, any
            quirks the user mentioned earlier. Different from ``description``,
            which is the short "what is this" line.
        fields: Optional schema of secret fields, each
            ``{name, label, secret, placeholder, help, kind}``. ``kind`` is
            ``"password"`` (default for secret=True), ``"text"``, or
            ``"textarea"`` (use for multiline values like service-account
            JSON). Omit to use a single ``value`` field. Ignored for
            ``kind="oauth"`` (the registry supplies scopes).
        timeout_seconds: Background timeout for the prompt (15-270, default
            180). If the user has not finished within this window the
            coordinator publishes a cancellation event and a resolution
            turn fires with ``status="pending"``. The credential remains in
            ``pending_setup`` so the user can finish later in
            Settings, Connections.
        flow: For ``kind="oauth"`` only. ``"auth_code"`` for browser-redirect,
            ``"device_code"`` for on-screen short code. Omit to use the
            provider default (auth_code when a redirect URL is available,
            device_code otherwise).
        use_localhost: For ``kind="oauth"`` only. When True, builds an
            auth_code redirect URL pointing at ``http://localhost:<api_port>``
            instead of ``NYMERIA_PUBLIC_URL``. Safe only when the user's
            browser is on the same machine as Nymeria. Set only after the
            user explicitly confirms.
        bind_target: Optional ``"type:id"`` binding to apply automatically
            once the user finishes setup. ``"mcp_server:<id>"`` binds the
            credential to a specific MCP server and force-restarts its
            connection so the next tool call picks up the new env value.
            ``"native_tool:<name>"`` scopes the credential to one Nymeria
            native tool. Bind failures are non-fatal: the credential still
            saves; the resolution turn surfaces the failure to the agent.
            Validated against ``^(mcp_server|native_tool):[A-Za-z0-9_-]{1,64}$``.

    Returns:
        JSON string with ``ok=True``, ``status="dispatched"``, ``credential_id``,
        ``prompt_id``, ``provider``, ``timeout_seconds``, ``message``. On error
        states (unknown provider, missing PUBLIC_URL, etc.) returns the same
        statuses as before with ``ok=False``: ``missing_public_url``,
        ``unknown_provider``, ``unsupported_flow``, ``client_config_missing``,
        ``device_code_request_failed``.

    Because the tool no longer blocks, it is safe to call alongside other
    tool calls in the same batch.
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

    bind_target_norm = (bind_target or "").strip()
    if bind_target_norm and not _BIND_TARGET_RE.match(bind_target_norm):
        return _json(
            {
                "ok": False,
                "status": "invalid_bind_target",
                "message": (
                    f"bind_target={bind_target_norm!r} is not valid. Use "
                    "'mcp_server:<id>' or 'native_tool:<name>' where <id>/<name> "
                    "is 1-64 chars matching [A-Za-z0-9_-]."
                ),
            }
        )

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

    safe_instructions = (instructions or "").strip()
    if len(safe_instructions) > _MAX_INSTRUCTIONS_CHARS:
        safe_instructions = safe_instructions[:_MAX_INSTRUCTIONS_CHARS]

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
            "instructions": safe_instructions,
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
            "instructions": safe_instructions,
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
        if bind_target_norm:
            prompt.metadata["_bind_target"] = bind_target_norm

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

    # Attach a done-callback for local side effects only. The tool returns
    # immediately, and prompt resolution never injects a message or queued
    # continuation into the chat thread.
    future.add_done_callback(
        _make_resolution_callback(
            prompt_id=prompt_id,
            credential_id=record.id,
            provider=provider_norm,
            thread_id=thread_id,
            user_id=user_id,
            bind_target=bind_target_norm or None,
        )
    )

    dispatched_message = (
        f"Sent the user an in-chat prompt to connect {label_display}. "
        "Tell the user a prompt is on screen. Credential prompt resolution "
        "is fire-and-forget: no prompt reply is routed into chat, and no "
        "automatic turn fires when they save."
    )
    return _json(
        {
            "ok": True,
            "status": "dispatched",
            "credential_id": record.id,
            "prompt_id": prompt_id,
            "provider": provider_norm,
            "timeout_seconds": timeout,
            "message": dispatched_message,
        }
    )


def _make_resolution_callback(
    *,
    prompt_id: str,
    credential_id: str,
    provider: str,
    thread_id: str,
    user_id: str,
    bind_target: Optional[str] = None,
):
    """Build the future done-callback that runs post-save side effects.

    No follow-up agent turn fires here. The user drives the next step in
    chat (e.g. "ok, try again now"), so this callback only:

    * cancels any device-code poller still spinning
    * runs ``_apply_bind_target`` when status=active and bind_target was set,
      so the MCP env-var resolution path picks up the new credential
    * logs the resolution for audit

    The callback runs synchronously on the loop where the future was
    resolved (typically the agent's loop). It must not block for long;
    the bind side-effects do a small vault update + best-effort MCP
    connection shutdown, no network I/O.
    """

    def _on_resolved(fut: "asyncio.Future") -> None:
        try:
            # Always cancel any lingering device-code poller. If the
            # poller itself resolved the future, this is a no-op.
            cancel_poll_task(prompt_id)

            if fut.cancelled():
                logger.warning(
                    "credential_prompt resolution callback fired on cancelled "
                    "future prompt=%s provider=%s",
                    prompt_id,
                    provider,
                )
                return

            exc = fut.exception()
            if exc is not None:
                logger.error(
                    "credential_prompt future raised: prompt=%s provider=%s err=%s",
                    prompt_id,
                    provider,
                    exc,
                )
                return

            result = fut.result() or {}
            status = str(result.get("status") or ("active" if result.get("ok") else "error"))
            effective_credential_id = str(result.get("credential_id") or credential_id)

            bind_outcome: Optional[str] = None
            if bind_target and status == "active":
                bind_outcome = _apply_bind_target(
                    credential_id=effective_credential_id,
                    bind_target=bind_target,
                    actor_user_id=user_id,
                )

            logger.info(
                "credential_prompt resolved prompt=%s provider=%s status=%s "
                "bind_target=%s bind_outcome=%s thread=%s",
                prompt_id,
                provider,
                status,
                bind_target or "-",
                bind_outcome or "-",
                thread_id,
            )
        except Exception:
            # The callback runs in user-invisible context; never let an
            # exception here escape and kill the resolver's loop.
            logger.exception(
                "credential_prompt resolution callback failed for prompt=%s",
                prompt_id,
            )

    return _on_resolved


def _apply_bind_target(
    *,
    credential_id: str,
    bind_target: str,
    actor_user_id: str,
) -> str:
    """Apply ``bind_target`` to the just-saved credential.

    Three coordinated writes, mirroring the proven pattern in
    ``mcp_runtime._credential_ref_for_secret``:

    1. ``add_allowed_target``: updates ``allowed_targets_json`` on the
       credentials row so vault read-side (``_target_allowed``) lets the
       target read the secret.
    2. ``bind_credential``: writes the bookkeeping row in
       ``credential_bindings`` (used by the credential inspection/binding tools).
    3. ``mcp_manager.shutdown_server`` if the target is an MCP server:
       force the next tool call to respawn the connection so the new
       vault value resolves into its env. Env-var resolution runs once
       at spawn time; without this restart the old empty/stale env
       persists.

    Returns a human-readable summary string for the resolution message.
    Never raises: bind failures are non-fatal and reported to the agent.
    """
    try:
        target_type, _, target_id = bind_target.partition(":")
        target_type = target_type.strip()
        target_id = target_id.strip()
        if not target_type or not target_id:
            return f"bind_target={bind_target!r} invalid; skipped"

        # Lazy imports to avoid pulling vault/mcp_manager into the tool
        # module at import time (which would pull half the agent stack
        # into the tool registry).
        from ..core.credential_vault import get_credential_vault_repo

        repo = get_credential_vault_repo()
        try:
            repo.add_allowed_target(
                credential_id,
                target=bind_target,
                actor_user_id=actor_user_id,
            )
        except Exception as e:
            logger.exception(
                "bind_target allowed_targets update failed credential=%s target=%s",
                credential_id,
                bind_target,
            )
            return f"bind failed (allowed_targets): {e}"

        try:
            repo.bind_credential(
                credential_id,
                target_type=target_type,
                target_id=target_id,
                actor_user_id=actor_user_id,
            )
        except Exception as e:
            logger.exception(
                "bind_target credential_bindings row failed credential=%s target=%s",
                credential_id,
                bind_target,
            )
            return f"bind failed (credential_bindings): {e}"

        if target_type == "mcp_server":
            try:
                from ..core.mcp_manager import get_mcp_manager

                outcome = get_mcp_manager().shutdown_server(target_id)
                if outcome == "shutdown":
                    return f"bound to {bind_target}, connection restarted"
                if outcome == "skipped_in_use":
                    return (
                        f"bound to {bind_target}, but connection restart skipped "
                        "because a tool call is in flight (idle sweep will pick "
                        "it up)"
                    )
                return f"bound to {bind_target}, no live connection to restart"
            except Exception as e:
                logger.exception(
                    "bind_target shutdown_server failed credential=%s target=%s",
                    credential_id,
                    bind_target,
                )
                return f"bound to {bind_target}, but connection restart failed: {e}"

        return f"bound to {bind_target}"
    except Exception as e:
        logger.exception(
            "bind_target apply failed credential=%s target=%s",
            credential_id,
            bind_target,
        )
        return f"bind failed: {e}"


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
        return "Legacy chat-side prompt resolution is no longer supported."
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


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="request_credential", tools=tuple(REQUEST_CREDENTIAL_TOOLS)))
