"""HTTP endpoints that resolve in-flight ``request_credential`` prompts.

When the agent calls the ``request_credential`` tool it registers a Future
with :mod:`nymeria.core.auth_prompt_coordinator` and awaits resolution. The
desktop modal (and, in Phase 4, the hosted ``/connect`` page) calls these
endpoints to:

- ``POST /api/credential-prompts/{prompt_id}/submit`` — write secret fields,
  run a connection test, and on success resolve the Future. On test failure
  the endpoint returns ``200 OK`` with ``{ok: false, error}`` so the modal
  can show the error inline and let the user retry without closing.
- ``POST /api/credential-prompts/{prompt_id}/exit`` — user clicked X / pressed
  Esc. Resolves the Future with ``status="user_exited"`` and the most recent
  test error so the agent can offer targeted help.
- ``POST /api/credential-prompts/{prompt_id}/cancel`` — explicit Cancel
  button. Resolves with ``status="cancelled"``.

All endpoints require bearer auth (the modal POSTs with the user's existing
session token) and verify the prompt's recorded ``user_id`` matches the
authenticated user.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core import secrets as nymeria_secrets
from ...core.accounts import AuthenticatedUser
from ...core.auth_prompt_coordinator import (
    PendingPrompt,
    get_auth_prompt_coordinator,
)
from ...core.credential_vault import CredentialVaultRepo
from ...core.event_bus import publish_autonomous_event
from ..schemas.credentials import CredentialResponse, credential_to_response

logger = logging.getLogger(__name__)


class CredentialPromptSubmitRequest(BaseModel):
    secret_fields: dict[str, str] = Field(default_factory=dict)
    account_label: Optional[str] = Field(default=None, max_length=240)


class CredentialPromptSubmitResponse(BaseModel):
    ok: bool
    status: str
    attempts: int
    error: Optional[str] = None
    credential: Optional[CredentialResponse] = None


class CredentialPromptExitRequest(BaseModel):
    last_test_error: Optional[str] = Field(default=None, max_length=1024)
    attempts: int = 0


class CredentialPromptAck(BaseModel):
    ok: bool
    status: str


def _repo(get_agent_fn: Callable[[], Any]) -> CredentialVaultRepo:
    return get_agent_fn().credential_vault


def _load_prompt_or_404(prompt_id: str, user: AuthenticatedUser) -> PendingPrompt:
    coordinator = get_auth_prompt_coordinator()
    prompt = coordinator.get(prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found or already resolved")
    if prompt.user_id != user.id and user.role != "admin":
        # Don't leak the existence of someone else's prompt.
        raise HTTPException(status_code=404, detail="Prompt not found or already resolved")
    return prompt


def _generic_test(record: Any) -> tuple[bool, Optional[str]]:
    """Phase-1 generic test: succeed iff secret fields were stored.

    Phase 3 replaces this with a per-provider HTTP probe driven by the
    descriptor registry (e.g. ``GET https://api.github.com/user``).
    """
    if not record.secret_fields:
        return False, "No secret fields were saved"
    return True, None


def create_credential_prompts_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(tags=["Credential Prompts"])

    @router.post(
        "/credential-prompts/{prompt_id}/submit",
        response_model=CredentialPromptSubmitResponse,
    )
    async def submit_prompt(
        prompt_id: str,
        body: CredentialPromptSubmitRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptSubmitResponse:
        coordinator = get_auth_prompt_coordinator()
        prompt = _load_prompt_or_404(prompt_id, user)
        repo = _repo(get_agent_fn)
        current = repo.get_credential(prompt.credential_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        if current.owner_user_id != user.id and user.role != "admin":
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
                actor_user_id=user.id,
            )
        except (
            nymeria_secrets.SecretsKeyMissing,
            nymeria_secrets.SecretsKeyInvalid,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        tested_ok, test_error = _generic_test(updated)
        repo.mark_tested(
            updated.id,
            status="active" if tested_ok else "invalid",
            actor_user_id=user.id,
            details={"generic_test": True, "ok": tested_ok, "error": test_error},
        )

        if not tested_ok:
            attempts = coordinator.record_attempt(prompt_id, error=test_error)
            # Do NOT resolve the future — let the modal show inline retry.
            return CredentialPromptSubmitResponse(
                ok=False,
                status="test_failed",
                attempts=attempts,
                error=test_error,
                credential=credential_to_response(repo.get_credential(updated.id)),
            )

        final_record = repo.get_credential(updated.id)
        coordinator.resolve(
            prompt_id,
            {
                "ok": True,
                "status": "active",
                "tested": True,
                "account_label": final_record.account_label,
                "credential_id": final_record.id,
            },
        )
        publish_autonomous_event(
            event_type="auth_prompt_resolved",
            thread_id=prompt.thread_id,
            user_id=prompt.user_id,
            task_id="",
            data={
                "prompt_id": prompt_id,
                "credential_id": final_record.id,
                "status": "active",
            },
        )
        return CredentialPromptSubmitResponse(
            ok=True,
            status="active",
            attempts=coordinator.get(prompt_id).attempts if coordinator.get(prompt_id) else 0,
            credential=credential_to_response(final_record),
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
        coordinator = get_auth_prompt_coordinator()
        resolved = coordinator.resolve(
            prompt_id,
            {
                "ok": False,
                "status": "user_exited",
                "last_test_error": body.last_test_error,
            },
        )
        publish_autonomous_event(
            event_type="auth_prompt_cancelled",
            thread_id=prompt.thread_id,
            user_id=prompt.user_id,
            task_id="",
            data={"prompt_id": prompt_id, "reason": "user_exited"},
        )
        return CredentialPromptAck(ok=resolved, status="user_exited")

    @router.post(
        "/credential-prompts/{prompt_id}/cancel",
        response_model=CredentialPromptAck,
    )
    async def cancel_prompt(
        prompt_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CredentialPromptAck:
        prompt = _load_prompt_or_404(prompt_id, user)
        coordinator = get_auth_prompt_coordinator()
        resolved = coordinator.resolve(
            prompt_id,
            {"ok": False, "status": "cancelled"},
        )
        publish_autonomous_event(
            event_type="auth_prompt_cancelled",
            thread_id=prompt.thread_id,
            user_id=prompt.user_id,
            task_id="",
            data={"prompt_id": prompt_id, "reason": "cancelled"},
        )
        return CredentialPromptAck(ok=resolved, status="cancelled")

    return router
