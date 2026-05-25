"""HTTP endpoints that resolve in-flight ``request_credential`` prompts."""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ...core import secrets as nymeria_secrets
from ...core.accounts import AuthenticatedUser
from ...core.auth_prompt_coordinator import (
    PendingPrompt,
    get_auth_prompt_coordinator,
)
from ...core.credential_tests import CredentialTestResult, test_credential_fields
from ...core.credential_vault import CredentialVaultRepo
from ...core.event_bus import publish_autonomous_event
from ...core.oauth_callback_handler import handle_auth_code_callback
from ..schemas.credentials import CredentialResponse, credential_to_response

logger = logging.getLogger(__name__)

_MAX_USER_MESSAGE_CHARS = 2000
_CANCEL_TEXTS = {
    "/cancel",
    "cancel",
    "cancel setup",
    "cancel credential",
    "cancel credentials",
    "nevermind",
    "never mind",
    "stop",
}


class CredentialPromptSubmitRequest(BaseModel):
    secret_fields: dict[str, str] = Field(default_factory=dict)
    account_label: Optional[str] = Field(default=None, max_length=240)
    user_message: Optional[str] = Field(default=None, max_length=_MAX_USER_MESSAGE_CHARS)


class CredentialPromptTestRequest(BaseModel):
    secret_fields: dict[str, str] = Field(default_factory=dict)
    account_label: Optional[str] = Field(default=None, max_length=240)


class CredentialPromptSubmitResponse(BaseModel):
    ok: bool
    status: str
    attempts: int
    error: Optional[str] = None
    message: Optional[str] = None
    code: Optional[str] = None
    tested: bool = False
    test_status: Optional[str] = None
    test_error: Optional[str] = None
    credential: Optional[CredentialResponse] = None


class CredentialPromptExitRequest(BaseModel):
    last_test_error: Optional[str] = Field(default=None, max_length=1024)
    attempts: int = 0
    user_message: Optional[str] = Field(default=None, max_length=_MAX_USER_MESSAGE_CHARS)


class CredentialPromptCancelRequest(BaseModel):
    user_message: Optional[str] = Field(default=None, max_length=_MAX_USER_MESSAGE_CHARS)


class CredentialPromptAck(BaseModel):
    ok: bool
    status: str


class CredentialPromptStatusResponse(BaseModel):
    ok: bool
    status: str
    prompt_id: str
    credential_id: Optional[str] = None
    message: str
    credential: Optional[CredentialResponse] = None


class CredentialPromptMetadataResponse(BaseModel):
    prompt_id: str
    credential_id: str
    provider: str
    display_name: str
    mode: str
    description: str
    fields: list[dict[str, Any]]
    account_label: str
    existing_accounts: list[dict[str, Any]]
    timeout_seconds: int
    expires_at: Optional[str] = None


def _repo(get_agent_fn: Callable[[], Any]) -> CredentialVaultRepo:
    return get_agent_fn().credential_vault


def _load_prompt_or_404(prompt_id: str, user: AuthenticatedUser) -> PendingPrompt:
    prompt = get_auth_prompt_coordinator().get(prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found or already resolved")
    if prompt.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=404, detail="Prompt not found or already resolved")
    return prompt


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing prompt token")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(status_code=401, detail="Invalid prompt token")
    return parts[1]


def _load_prompt_by_token_or_404(prompt_id: str, authorization: Optional[str]) -> PendingPrompt:
    token = _extract_bearer_token(authorization)
    prompt = get_auth_prompt_coordinator().verify_prompt_token(prompt_id, token)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found or token expired")
    return prompt


def _bounded_message(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:_MAX_USER_MESSAGE_CHARS]


def _is_oauth_prompt(prompt: PendingPrompt) -> bool:
    return str((prompt.metadata or {}).get("mode") or "").startswith("oauth")


def _reject_if_oauth(prompt: PendingPrompt) -> None:
    """Raise 409 if this is an OAuth prompt being touched by a form endpoint.

    OAuth prompts must be resolved by their callback (``/oauth/callback``)
    or device-code poller. The form ``submit``/``test`` paths take
    ``secret_fields`` from the caller, which would corrupt the in-flight
    OAuth credential record. Exit/cancel still pass through fine.
    """
    if _is_oauth_prompt(prompt):
        raise HTTPException(
            status_code=409,
            detail=(
                "This prompt is an OAuth flow — finish it in your browser "
                "(or on the device-login page), not via this endpoint."
            ),
        )


def is_credential_prompt_cancel_text(message: str) -> bool:
    return message.strip().lower() in _CANCEL_TEXTS


def _test_status(result: CredentialTestResult) -> str:
    if not result.ok:
        return "test_failed"
    return "verified" if result.verified else "not_verified"


def _response_from_test(
    *,
    result: CredentialTestResult,
    attempts: int,
    credential: Any | None = None,
) -> CredentialPromptSubmitResponse:
    return CredentialPromptSubmitResponse(
        ok=result.ok,
        status=_test_status(result),
        attempts=attempts,
        error=None if result.ok else result.message,
        message=result.message,
        code=result.code,
        tested=result.verified,
        test_status=_test_status(result),
        test_error=None if result.ok else result.message,
        credential=credential_to_response(credential) if credential is not None else None,
    )


def _public_prompt_metadata(prompt: PendingPrompt) -> dict[str, Any]:
    """Return a copy of ``prompt.metadata`` safe to send to credential testers.

    Internal-only keys (any key starting with an underscore) are stripped so
    secrets like ``_oauth_state`` (which holds ``client_secret`` and
    ``code_verifier`` for the in-flight OAuth flow) never reach loggers or
    third-party probes.
    """
    raw = prompt.metadata or {}
    return {k: v for k, v in raw.items() if not str(k).startswith("_")}


async def _run_prompt_test(
    *,
    prompt: PendingPrompt,
    body: CredentialPromptTestRequest,
    get_settings_fn: Callable[[], Any],
) -> CredentialTestResult:
    metadata = _public_prompt_metadata(prompt)
    metadata["account_label"] = body.account_label
    return await test_credential_fields(
        provider=prompt.provider,
        kind=str(metadata.get("mode") or "api_key"),
        metadata=metadata,
        secret_fields=body.secret_fields,
        settings=get_settings_fn(),
    )


def _publish_cancelled(prompt: PendingPrompt, prompt_id: str, reason: str) -> None:
    publish_autonomous_event(
        event_type="auth_prompt_cancelled",
        thread_id=prompt.thread_id,
        user_id=prompt.user_id,
        task_id="",
        data={"prompt_id": prompt_id, "reason": reason},
    )


def _publish_resolved(prompt: PendingPrompt, prompt_id: str, credential_id: str, status: str) -> None:
    publish_autonomous_event(
        event_type="auth_prompt_resolved",
        thread_id=prompt.thread_id,
        user_id=prompt.user_id,
        task_id="",
        data={"prompt_id": prompt_id, "credential_id": credential_id, "status": status},
    )


def _prompt_status_response(
    *,
    prompt_id: str,
    record: Any | None,
    pending_message: str = "Credential setup is still pending.",
) -> CredentialPromptStatusResponse:
    if record is None:
        return CredentialPromptStatusResponse(
            ok=False,
            status="missing",
            prompt_id=prompt_id,
            message="Credential record was not found.",
        )
    if record.status == "active":
        metadata = record.metadata or {}
        email = metadata.get("email") or record.account_label
        message = (
            f"Connected as {email}."
            if email
            else "Credential is connected."
        )
        return CredentialPromptStatusResponse(
            ok=True,
            status="active",
            prompt_id=prompt_id,
            credential_id=record.id,
            message=message,
            credential=credential_to_response(record),
        )
    if record.status == "disabled":
        message = "This setup prompt was superseded by a newer credential."
    elif record.status == "invalid":
        message = "Credential setup completed, but the credential is marked invalid."
    else:
        message = pending_message
    return CredentialPromptStatusResponse(
        ok=False,
        status=record.status,
        prompt_id=prompt_id,
        credential_id=record.id,
        message=message,
        credential=credential_to_response(record),
    )


def _find_credential_by_prompt_id(
    *,
    repo: CredentialVaultRepo,
    prompt_id: str,
    user: AuthenticatedUser,
) -> Any | None:
    records = repo.list_credentials(
        owner_user_id=None if user.role == "admin" else user.id,
        include_disabled=True,
    )
    for record in records:
        metadata = record.metadata or {}
        if metadata.get("prompt_id") != prompt_id:
            continue
        if record.owner_user_id != user.id and user.role != "admin":
            continue
        return record
    return None


async def _submit_prompt_impl(
    *,
    prompt_id: str,
    prompt: PendingPrompt,
    body: CredentialPromptSubmitRequest,
    actor_user_id: str,
    actor_is_admin: bool,
    repo: CredentialVaultRepo,
    get_settings_fn: Callable[[], Any],
) -> CredentialPromptSubmitResponse:
    _reject_if_oauth(prompt)
    coordinator = get_auth_prompt_coordinator()
    current = repo.get_credential(prompt.credential_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if current.owner_user_id != actor_user_id and not actor_is_admin:
        raise HTTPException(status_code=403, detail="Credential is not owned by this user")

    try:
        updated = repo.upsert_credential(
            credential_id=current.id,
            owner_type=current.owner_type,
            owner_user_id=current.owner_user_id,
            name=current.name,
            provider=current.provider,
            kind=current.kind,
            account_label=(
                body.account_label
                if body.account_label is not None
                else current.account_label
            ),
            metadata=current.metadata,
            scopes=current.scopes,
            allowed_targets=current.allowed_targets,
            expires_at=current.expires_at,
            status="active",
            secret_fields=body.secret_fields,
            actor_user_id=actor_user_id,
        )
    except (
        nymeria_secrets.SecretsKeyMissing,
        nymeria_secrets.SecretsKeyInvalid,
    ) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    test_result = await _run_prompt_test(
        prompt=prompt,
        body=CredentialPromptTestRequest(
            secret_fields=body.secret_fields,
            account_label=body.account_label,
        ),
        get_settings_fn=get_settings_fn,
    )
    repo.mark_tested(
        updated.id,
        status="active" if test_result.ok else "invalid",
        actor_user_id=actor_user_id,
        details={
            "ok": test_result.ok,
            "verified": test_result.verified,
            "code": test_result.code,
            "message": test_result.message,
            **(test_result.metadata or {}),
        },
    )

    final_record = repo.get_credential(updated.id)
    if final_record is None:
        raise HTTPException(status_code=404, detail=f"Credential {updated.id} not found after save")

    if not test_result.ok:
        attempts = coordinator.record_attempt(prompt_id, error=test_result.message)
        return _response_from_test(result=test_result, attempts=attempts, credential=final_record)

    test_status = _test_status(test_result)
    coordinator.resolve(
        prompt_id,
        {
            "ok": True,
            "status": "active",
            "tested": test_result.verified,
            "test_status": test_status,
            "test_error": None,
            "message": test_result.message,
            "account_label": final_record.account_label,
            "credential_id": final_record.id,
            "user_message": _bounded_message(body.user_message),
        },
    )
    _publish_resolved(prompt, prompt_id, final_record.id, "active")
    return _response_from_test(
        result=test_result,
        attempts=prompt.attempts,
        credential=final_record,
    ).model_copy(update={"status": "active"})


def _metadata_response(prompt: PendingPrompt) -> CredentialPromptMetadataResponse:
    data = prompt.metadata or {}
    return CredentialPromptMetadataResponse(
        prompt_id=prompt.prompt_id,
        credential_id=prompt.credential_id,
        provider=prompt.provider,
        display_name=str(data.get("display_name") or prompt.provider),
        mode=str(data.get("mode") or "api_key"),
        description=str(data.get("description") or ""),
        fields=list(data.get("fields") or []),
        account_label=str(data.get("account_label") or ""),
        existing_accounts=list(data.get("existing_accounts") or []),
        timeout_seconds=int(data.get("timeout_seconds") or 0),
        expires_at=prompt.token_expires_at_iso or data.get("expires_at"),
    )


def _oauth_result_page(*, title: str, message: str, ok: bool) -> str:
    """Tiny HTML page shown to the user's browser after an OAuth redirect."""
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    accent = "#16a34a" if ok else "#dc2626"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: Canvas; color: CanvasText; padding: 16px; }}
  .card {{ background: color-mix(in srgb, Canvas 96%, CanvasText); border-radius: 12px; padding: 32px 36px;
          max-width: 480px; text-align: center; box-shadow: 0 6px 24px rgba(0,0,0,0.15); }}
  h1 {{ color: {accent}; margin: 0 0 10px; font-size: 1.4rem; }}
  p {{ margin: 0; line-height: 1.5; }}
</style></head>
<body><div class="card"><h1>{safe_title}</h1><p>{safe_message}</p></div></body></html>"""


def _hosted_form_shell(prompt_id: str) -> str:
    safe_prompt_id = html.escape(prompt_id, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Credential</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: Canvas; color: CanvasText; }}
    main {{ width: min(680px, calc(100vw - 32px)); margin: 40px auto; }}
    h1 {{ font-size: 1.4rem; margin: 0 0 0.35rem; }}
    p {{ line-height: 1.45; }}
    form {{ display: grid; gap: 16px; margin-top: 24px; }}
    label {{ display: grid; gap: 6px; font-weight: 600; margin: 0 0 14px; }}
    input, textarea {{ font: inherit; padding: 10px 12px; border: 1px solid color-mix(in srgb, CanvasText 28%, transparent); border-radius: 6px; background: Canvas; color: CanvasText; }}
    textarea {{ min-height: 110px; resize: vertical; }}
    small {{ color: color-mix(in srgb, CanvasText 68%, transparent); font-weight: 400; }}
    .actions {{ display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; }}
    button {{ font: inherit; padding: 10px 14px; border-radius: 6px; border: 1px solid color-mix(in srgb, CanvasText 28%, transparent); background: Canvas; color: CanvasText; cursor: pointer; }}
    button.primary {{ background: #2563eb; border-color: #2563eb; color: white; }}
    .notice {{ padding: 12px; border-radius: 6px; background: color-mix(in srgb, #2563eb 12%, Canvas); }}
    .error {{ padding: 12px; border-radius: 6px; background: color-mix(in srgb, #dc2626 14%, Canvas); }}
    .hidden {{ display: none; }}
  </style>
</head>
<body>
  <main>
    <h1 id="title">Connect credential</h1>
    <p id="description"></p>
    <p class="notice">Secrets entered here are sent directly to your Nymeria server. Do not paste secrets into chat.</p>
    <div id="status" class="notice hidden"></div>
    <form id="form" class="hidden">
      <div id="fields"></div>
      <label id="label-wrap" class="hidden">Name this connection
        <input id="account-label" autocomplete="off" placeholder="e.g. work, personal">
      </label>
      <label>Note to Nymeria (optional)
        <textarea id="user-message" placeholder="Anything you want Nymeria to know after this setup closes"></textarea>
      </label>
      <div class="actions">
        <button id="cancel" type="button">Cancel</button>
        <button id="exit" type="button">Close without saving</button>
        <button id="test" type="button">Test</button>
        <button id="save" class="primary" type="submit">Save</button>
      </div>
    </form>
  </main>
  <script>
    const promptId = "{safe_prompt_id}";
    const token = decodeURIComponent((location.hash || "").slice(1));
    const statusEl = document.getElementById("status");
    const form = document.getElementById("form");
    const fieldsEl = document.getElementById("fields");
    const titleEl = document.getElementById("title");
    const descriptionEl = document.getElementById("description");
    const labelWrap = document.getElementById("label-wrap");
    const labelInput = document.getElementById("account-label");
    const userMessageInput = document.getElementById("user-message");
    let fieldNames = [];
    let lastTestError = null;
    let attempts = 0;
    function show(message, isError = false) {{
      statusEl.textContent = message;
      statusEl.className = isError ? "error" : "notice";
    }}
    async function api(path, options = {{}}) {{
      const response = await fetch(`/connect/credentials/${{encodeURIComponent(promptId)}}${{path}}`, {{
        ...options,
        headers: {{
          "Authorization": `Bearer ${{token}}`,
          "Content-Type": "application/json",
          ...(options.headers || {{}})
        }}
      }});
      if (!response.ok) {{
        const text = await response.text();
        throw new Error(text || `Request failed with HTTP ${{response.status}}`);
      }}
      return await response.json();
    }}
    function collect() {{
      const secret_fields = {{}};
      for (const name of fieldNames) {{
        secret_fields[name] = document.getElementById(`field-${{name}}`).value;
      }}
      return {{
        secret_fields,
        account_label: labelInput.value.trim() || null,
        user_message: userMessageInput.value.trim() || null
      }};
    }}
    function render(meta) {{
      titleEl.textContent = `Connect ${{meta.display_name || meta.provider}}`;
      descriptionEl.textContent = meta.description || "";
      labelInput.value = meta.account_label || "";
      if ((meta.existing_accounts || []).length || meta.account_label) {{
        labelWrap.classList.remove("hidden");
      }}
      fieldsEl.innerHTML = "";
      fieldNames = [];
      for (const field of meta.fields || []) {{
        const name = field.name;
        fieldNames.push(name);
        const label = document.createElement("label");
        label.textContent = field.label || name;
        const input = field.kind === "textarea" ? document.createElement("textarea") : document.createElement("input");
        input.id = `field-${{name}}`;
        input.required = true;
        input.autocomplete = "off";
        input.spellcheck = false;
        if (input.tagName === "INPUT") {{
          input.type = field.secret === false ? "text" : "password";
        }}
        input.placeholder = field.placeholder || "";
        label.appendChild(input);
        if (field.help) {{
          const help = document.createElement("small");
          help.textContent = field.help;
          label.appendChild(help);
        }}
        fieldsEl.appendChild(label);
      }}
      form.classList.remove("hidden");
      show(meta.expires_at ? `This setup link expires at ${{meta.expires_at}}.` : "Setup link ready.");
    }}
    async function testFields() {{
      const result = await api("/test", {{
        method: "POST",
        body: JSON.stringify(collect())
      }});
      attempts = result.attempts || attempts;
      lastTestError = result.ok ? null : (result.error || result.message || "Credential test failed");
      show(result.message || (result.ok ? "Credential test completed." : "Credential test failed."), !result.ok);
      return result;
    }}
    if (!token) {{
      show("This setup link is missing its prompt token. Open the original link from chat.", true);
    }} else {{
      api("/prompt").then(render).catch((err) => show(err.message, true));
    }}
    document.getElementById("test").addEventListener("click", () => {{
      testFields().catch((err) => show(err.message, true));
    }});
    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      try {{
        const result = await api("/submit", {{
          method: "POST",
          body: JSON.stringify(collect())
        }});
        if (!result.ok) {{
          attempts = result.attempts || attempts;
          lastTestError = result.error || result.message || "Credential test failed";
          show(lastTestError, true);
          return;
        }}
        form.classList.add("hidden");
        show(result.tested ? "Credential saved and verified." : "Credential saved. This provider was not verified.");
      }} catch (err) {{
        show(err.message, true);
      }}
    }});
    document.getElementById("exit").addEventListener("click", async () => {{
      try {{
        await api("/exit", {{
          method: "POST",
          body: JSON.stringify({{ last_test_error: lastTestError, attempts, user_message: userMessageInput.value.trim() || null }})
        }});
        form.classList.add("hidden");
        show("Closed. You can return to chat.");
      }} catch (err) {{ show(err.message, true); }}
    }});
    document.getElementById("cancel").addEventListener("click", async () => {{
      try {{
        await api("/cancel", {{
          method: "POST",
          body: JSON.stringify({{ user_message: userMessageInput.value.trim() || null }})
        }});
        form.classList.add("hidden");
        show("Cancelled. You can return to chat.");
      }} catch (err) {{ show(err.message, true); }}
    }});
  </script>
</body>
</html>"""


def create_credential_prompts_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Optional[Callable[[], Any]] = None,
) -> APIRouter:
    router = APIRouter(tags=["Credential Prompts"])
    if get_settings_fn is None:
        from ...config import get_settings

        get_settings_fn = get_settings

    @router.get(
        "/connect/credentials/{prompt_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def hosted_prompt_form(prompt_id: str) -> HTMLResponse:
        return HTMLResponse(_hosted_form_shell(prompt_id))

    @router.get(
        "/connect/credentials/oauth/callback",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def oauth_callback(
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ) -> HTMLResponse:
        # Static path: every prompt redirects here. The prompt id is packed
        # into ``state`` by oauth_start so users only register one redirect
        # URI per origin in their Google Cloud Console / Azure Portal.
        result = await handle_auth_code_callback(
            code=code,
            state=state,
            error=error,
        )
        return HTMLResponse(
            _oauth_result_page(title=result.title, message=result.message, ok=result.ok),
            status_code=result.status_code,
        )

    @router.get(
        "/connect/credentials/{prompt_id}/prompt",
        response_model=CredentialPromptMetadataResponse,
    )
    async def hosted_prompt_metadata(
        prompt_id: str,
        authorization: Optional[str] = Header(None),
    ) -> CredentialPromptMetadataResponse:
        prompt = _load_prompt_by_token_or_404(prompt_id, authorization)
        return _metadata_response(prompt)

    @router.post(
        "/connect/credentials/{prompt_id}/test",
        response_model=CredentialPromptSubmitResponse,
    )
    async def hosted_test_prompt(
        prompt_id: str,
        body: CredentialPromptTestRequest,
        authorization: Optional[str] = Header(None),
    ) -> CredentialPromptSubmitResponse:
        prompt = _load_prompt_by_token_or_404(prompt_id, authorization)
        _reject_if_oauth(prompt)
        result = await _run_prompt_test(
            prompt=prompt,
            body=body,
            get_settings_fn=get_settings_fn,
        )
        attempts = (
            get_auth_prompt_coordinator().record_attempt(prompt_id, error=result.message)
            if not result.ok
            else prompt.attempts
        )
        return _response_from_test(result=result, attempts=attempts)

    @router.post(
        "/connect/credentials/{prompt_id}/submit",
        response_model=CredentialPromptSubmitResponse,
    )
    async def hosted_submit_prompt(
        prompt_id: str,
        body: CredentialPromptSubmitRequest,
        authorization: Optional[str] = Header(None),
    ) -> CredentialPromptSubmitResponse:
        prompt = _load_prompt_by_token_or_404(prompt_id, authorization)
        return await _submit_prompt_impl(
            prompt_id=prompt_id,
            prompt=prompt,
            body=body,
            actor_user_id=prompt.user_id,
            actor_is_admin=False,
            repo=_repo(get_agent_fn),
            get_settings_fn=get_settings_fn,
        )

    @router.post(
        "/connect/credentials/{prompt_id}/exit",
        response_model=CredentialPromptAck,
    )
    async def hosted_exit_prompt(
        prompt_id: str,
        body: CredentialPromptExitRequest,
        authorization: Optional[str] = Header(None),
    ) -> CredentialPromptAck:
        prompt = _load_prompt_by_token_or_404(prompt_id, authorization)
        return _resolve_exit(prompt_id, prompt, body)

    @router.post(
        "/connect/credentials/{prompt_id}/cancel",
        response_model=CredentialPromptAck,
    )
    async def hosted_cancel_prompt(
        prompt_id: str,
        body: CredentialPromptCancelRequest | None = None,
        authorization: Optional[str] = Header(None),
    ) -> CredentialPromptAck:
        prompt = _load_prompt_by_token_or_404(prompt_id, authorization)
        return _resolve_cancel(prompt_id, prompt, body)

    @router.post(
        "/credential-prompts/{prompt_id}/test",
        response_model=CredentialPromptSubmitResponse,
    )
    async def test_prompt(
        prompt_id: str,
        body: CredentialPromptTestRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptSubmitResponse:
        prompt = _load_prompt_or_404(prompt_id, user)
        _reject_if_oauth(prompt)
        result = await _run_prompt_test(
            prompt=prompt,
            body=body,
            get_settings_fn=get_settings_fn,
        )
        attempts = (
            get_auth_prompt_coordinator().record_attempt(prompt_id, error=result.message)
            if not result.ok
            else prompt.attempts
        )
        return _response_from_test(result=result, attempts=attempts)

    @router.get(
        "/credential-prompts/{prompt_id}/status",
        response_model=CredentialPromptStatusResponse,
    )
    async def prompt_status(
        prompt_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptStatusResponse:
        repo = _repo(get_agent_fn)
        prompt = get_auth_prompt_coordinator().get(prompt_id)
        if prompt is not None:
            if prompt.user_id != user.id and user.role != "admin":
                raise HTTPException(status_code=404, detail="Prompt not found or already resolved")
            record = repo.get_credential(prompt.credential_id)
            pending_message = (
                "Still waiting for the OAuth sign-in to complete."
                if _is_oauth_prompt(prompt)
                else "Credential setup is still pending."
            )
            return _prompt_status_response(
                prompt_id=prompt_id,
                record=record,
                pending_message=pending_message,
            )

        record = _find_credential_by_prompt_id(repo=repo, prompt_id=prompt_id, user=user)
        if record is None:
            raise HTTPException(status_code=404, detail="Prompt not found or already resolved")
        return _prompt_status_response(prompt_id=prompt_id, record=record)

    @router.post(
        "/credential-prompts/{prompt_id}/submit",
        response_model=CredentialPromptSubmitResponse,
    )
    async def submit_prompt(
        prompt_id: str,
        body: CredentialPromptSubmitRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptSubmitResponse:
        prompt = _load_prompt_or_404(prompt_id, user)
        return await _submit_prompt_impl(
            prompt_id=prompt_id,
            prompt=prompt,
            body=body,
            actor_user_id=user.id,
            actor_is_admin=user.role == "admin",
            repo=_repo(get_agent_fn),
            get_settings_fn=get_settings_fn,
        )

    @router.post(
        "/credential-prompts/{prompt_id}/exit",
        response_model=CredentialPromptAck,
    )
    async def exit_prompt(
        prompt_id: str,
        body: CredentialPromptExitRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptAck:
        prompt = _load_prompt_or_404(prompt_id, user)
        return _resolve_exit(prompt_id, prompt, body)

    @router.post(
        "/credential-prompts/{prompt_id}/cancel",
        response_model=CredentialPromptAck,
    )
    async def cancel_prompt(
        prompt_id: str,
        body: CredentialPromptCancelRequest | None = None,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptAck:
        prompt = _load_prompt_or_404(prompt_id, user)
        return _resolve_cancel(prompt_id, prompt, body)

    return router


def _resolve_exit(
    prompt_id: str,
    prompt: PendingPrompt,
    body: CredentialPromptExitRequest,
) -> CredentialPromptAck:
    resolved = get_auth_prompt_coordinator().resolve(
        prompt_id,
        {
            "ok": False,
            "status": "user_exited",
            "last_test_error": body.last_test_error,
            "user_message": _bounded_message(body.user_message),
        },
    )
    _publish_cancelled(prompt, prompt_id, "user_exited")
    return CredentialPromptAck(ok=resolved, status="user_exited")


def _resolve_cancel(
    prompt_id: str,
    prompt: PendingPrompt,
    body: CredentialPromptCancelRequest | None,
) -> CredentialPromptAck:
    resolved = get_auth_prompt_coordinator().resolve(
        prompt_id,
        {
            "ok": False,
            "status": "cancelled",
            "user_message": _bounded_message(body.user_message if body else None),
        },
    )
    _publish_cancelled(prompt, prompt_id, "cancelled")
    return CredentialPromptAck(ok=resolved, status="cancelled")


def resolve_pending_prompt_from_chat(
    *,
    user_id: str,
    thread_id: str,
    message: str,
) -> dict[str, Any] | None:
    """Resolve an active credential prompt with a same-thread chat message."""
    coordinator = get_auth_prompt_coordinator()
    prompt = coordinator.get_for_user_thread(user_id=user_id, thread_id=thread_id)
    if prompt is None:
        return None

    if is_credential_prompt_cancel_text(message):
        resolved = coordinator.resolve(
            prompt.prompt_id,
            {
                "ok": False,
                "status": "cancelled",
                "user_message": _bounded_message(message),
            },
        )
        _publish_cancelled(prompt, prompt.prompt_id, "cancelled")
        return {"prompt_id": prompt.prompt_id, "status": "cancelled", "resolved": resolved}

    if _is_oauth_prompt(prompt):
        return {
            "prompt_id": prompt.prompt_id,
            "status": "oauth_pending",
            "resolved": False,
            "message": (
                "OAuth sign-in is still pending. Keep the credential prompt open "
                "until it reports that the account is connected."
            ),
        }

    resolved = coordinator.resolve(
        prompt.prompt_id,
        {
            "ok": False,
            "status": "user_message",
            "user_message": _bounded_message(message),
            "message": "User responded in chat instead of completing the credential prompt.",
        },
    )
    _publish_cancelled(prompt, prompt.prompt_id, "user_message")
    return {"prompt_id": prompt.prompt_id, "status": "user_message", "resolved": resolved}


__all__ = [
    "create_credential_prompts_router",
    "is_credential_prompt_cancel_text",
    "resolve_pending_prompt_from_chat",
]
