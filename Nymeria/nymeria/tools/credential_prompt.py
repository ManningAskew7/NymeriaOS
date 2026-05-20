"""Agent-facing tool: ``request_credential`` — opens an in-chat modal that lets
the user enter a credential (API key / PAT / OAuth — OAuth lands in Phase 3),
runs a connection test, and returns success or failure to the agent without
the agent ever seeing the secret.

The flow is fully described in
``nymeria/core/auth_prompt_coordinator.py``. This module wires the agent side:
create a pending credential, emit an ``auth_prompt`` SSE event, await the
coordinator's future, return a structured JSON result.

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
    config: Annotated[RunnableConfig | None, InjectedToolArg] = None,
) -> str:
    """Open a secure in-chat modal to collect credentials from the user.

    The user enters their secret (API key, token, etc.) directly into the
    desktop modal — you never see the value. The backend tests the connection
    and returns a structured result.

    Args:
        provider: Service identifier (e.g. ``"github"``, ``"openai"``,
            ``"anthropic"``). Lowercase, alphanumeric/underscore.
        kind: ``"api_key"`` (default), ``"pat"``, or ``"oauth"`` (OAuth is
            available from Phase 3 onward).
        account_label: Optional label for multi-account scenarios
            (e.g. ``"work"``, ``"personal"``). The user can rename it in
            the modal. Use this when the user already has another account
            for the same provider.
        display_name: Optional human-readable name shown in the modal title.
            Defaults to ``provider``.
        description: Optional instructions shown above the form (markdown
            supported: bold, lists, links). USE THIS for non-obvious
            services to tell the user where to find the credential.
            Example: ``"Get your API key at [Sendgrid → Settings → API
            Keys](https://app.sendgrid.com/settings/api_keys). Choose
            'Full Access' or 'Restricted'."`` Since the modal is modal
            (blocks chat), good instructions here prevent the user from
            having to dismiss and ask for help.
        fields: Optional schema of secret fields, each
            ``{name, label, secret, placeholder, help, kind}``. ``kind`` is
            ``"password"`` (default for secret=True), ``"text"``, or
            ``"textarea"`` (use for multiline values like service-account
            JSON). Omit to use a single ``value`` field. Phase 3 will
            auto-resolve this from a provider descriptor registry.
        timeout_seconds: How long to wait for the user (15-270, default 180).
            On timeout the credential remains in ``pending_setup`` so the
            user can finish later in Settings → Connections.

    Returns:
        JSON string with: ``ok``, ``status`` (``active`` | ``user_exited`` |
        ``cancelled`` | ``pending`` | ``test_failed`` | ``swept``),
        ``credential_id``, ``attempts``, ``last_test_error``,
        ``account_label``, ``message``.

    Caveat: this tool blocks the calling tool batch until the user responds
    (or it times out). Prefer calling it alone, not in parallel with other
    tool calls — siblings in the same batch will wait for this one to
    return.
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
    if kind_norm == "oauth":
        # Surface a clear "not yet" rather than silently degrading to PAT.
        return _json(
            {
                "ok": False,
                "status": "unsupported",
                "message": (
                    "OAuth mode is not yet available (Phase 3). "
                    "Ask the user for a personal access token instead, "
                    "or call this tool with kind='pat'."
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

    pending_fields = _safe_fields(fields) if fields else _generic_fields(label_display)

    safe_description = (description or "").strip()
    if len(safe_description) > _MAX_DESCRIPTION_CHARS:
        safe_description = safe_description[:_MAX_DESCRIPTION_CHARS]

    existing_accounts = _existing_accounts_for(repo, user_id, provider_norm)
    token_expires_at, expires_at_iso = prompt_expires_at(timeout)
    connect_url = _connect_url(
        getattr(settings, "nymeria_public_url", None),
        prompt_id,
        prompt_token,
    )

    record = repo.create_credential(
        owner_type="user",
        owner_user_id=user_id,
        name=cred_name,
        provider=provider_norm,
        kind=kind_norm,
        account_label=account_label.strip() or None,
        status="pending_setup",
        metadata={
            "setup_session": True,
            "prompt_id": prompt_id,
            "mode": kind_norm,
            "required_fields": [f["name"] for f in pending_fields],
            "source": "request_credential",
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

    logger.info(
        "request_credential opened prompt=%s provider=%s user=%s timeout=%ds",
        prompt_id,
        provider_norm,
        user_id,
        timeout,
    )

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
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
    return "Prompt resolved without a clear status."


REQUEST_CREDENTIAL_TOOLS = [request_credential]


__all__ = ["request_credential", "REQUEST_CREDENTIAL_TOOLS"]
