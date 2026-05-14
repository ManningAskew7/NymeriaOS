"""Credential vault routes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config.llm_providers import is_known_llm_provider
from ...core import secrets as nymeria_secrets
from ...core.accounts import AuthenticatedUser
from ...core.credential_vault import CredentialNotFound, CredentialVaultRepo
from ...core.llm_credentials import LLM_PROVIDER_TARGET_TYPE
from ..schemas.credentials import (
    CredentialBindingRequest,
    CredentialBindingResponse,
    CredentialCreateRequest,
    CredentialListResponse,
    CredentialResponse,
    CredentialSetupSessionRequest,
    CredentialUpdateRequest,
    credential_to_response,
)

logger = logging.getLogger(__name__)


def _repo(get_agent_fn: Callable[[], Any]) -> CredentialVaultRepo:
    return get_agent_fn().credential_vault


def _can_read(user: AuthenticatedUser, record: Any) -> bool:
    if user.role == "admin":
        return True
    if record.owner_type == "system":
        return True
    return record.owner_user_id == user.id


def _can_manage(user: AuthenticatedUser, record: Any) -> bool:
    if user.role == "admin":
        return True
    return record.owner_type == "user" and record.owner_user_id == user.id


def _require_read(user: AuthenticatedUser, record: Any) -> None:
    if not _can_read(user, record):
        raise HTTPException(status_code=404, detail="Credential not found")


def _require_manage(user: AuthenticatedUser, record: Any) -> None:
    if not _can_manage(user, record):
        raise HTTPException(status_code=403, detail="Credential is not owned by this user")


def _secret_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


def _is_llm_credential(record: Any) -> bool:
    targets = record.allowed_targets or []
    return is_known_llm_provider(record.provider) or any(
        str(target).startswith(f"{LLM_PROVIDER_TARGET_TYPE}:")
        for target in targets
    )


def _invalidate_llm_graphs(get_agent_fn: Callable[[], Any], record: Any) -> None:
    if not _is_llm_credential(record):
        return
    agent = get_agent_fn()
    try:
        with agent._graph_cache_lock:
            agent._user_graphs.clear()
            agent._async_user_graphs.clear()
        agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
        agent._default_async_graph = agent._build_async_graph_with_prompt(
            agent._base_system_prompt
        )
    except Exception:
        logger.warning("Failed to rebuild LLM graphs after credential change", exc_info=True)


def create_credentials_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(tags=["Credentials"])

    @router.get("/credentials", response_model=CredentialListResponse)
    async def list_credentials(
        scope: str = Query(default="visible", pattern="^(visible|mine|system|all)$"),
        include_disabled: bool = False,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        if scope == "mine":
            records = repo.list_credentials(
                owner_user_id=user.id,
                include_disabled=include_disabled,
            )
        elif scope == "system":
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="System credentials are admin-only")
            records = repo.list_credentials(
                owner_type="system",
                include_system=True,
                include_disabled=include_disabled,
            )
        elif scope == "all":
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="Listing all credentials is admin-only")
            records = repo.list_credentials(
                include_system=True,
                include_disabled=include_disabled,
            )
        else:
            records = repo.list_credentials(
                owner_user_id=user.id,
                include_system=True,
                include_disabled=include_disabled,
            )
        visible = [record for record in records if _can_read(user, record)]
        return CredentialListResponse(
            credentials=[credential_to_response(record) for record in visible],
            total=len(visible),
        )

    @router.post("/credentials", response_model=CredentialResponse)
    async def create_credential(
        body: CredentialCreateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        owner_user_id: Optional[str]
        if body.owner_type == "system":
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="System credentials are admin-only")
            owner_user_id = None
        else:
            owner_user_id = body.owner_user_id or user.id
            if owner_user_id != user.id and user.role != "admin":
                raise HTTPException(status_code=403, detail="Cannot create credentials for another user")
        try:
            record = _repo(get_agent_fn).create_credential(
                owner_type=body.owner_type,
                owner_user_id=owner_user_id,
                name=body.name,
                provider=body.provider,
                kind=body.kind,
                account_label=body.account_label,
                metadata=body.metadata,
                scopes=body.scopes,
                allowed_targets=body.allowed_targets,
                expires_at=body.expires_at,
                status=body.status,
                secret_fields=body.secret_fields,
                created_by_user_id=user.id,
            )
        except (
            nymeria_secrets.SecretsKeyMissing,
            nymeria_secrets.SecretsKeyInvalid,
        ) as exc:
            raise _secret_error(exc) from exc
        _invalidate_llm_graphs(get_agent_fn, record)
        return credential_to_response(record)

    @router.post("/credential-setup-sessions", response_model=CredentialResponse)
    async def create_setup_session(
        body: CredentialSetupSessionRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        allowed_targets = []
        if body.target_type and body.target_id:
            allowed_targets.append(f"{body.target_type}:{body.target_id}")
        metadata = {
            **body.metadata,
            "setup_session": True,
            "required_fields": body.required_fields,
            "target_type": body.target_type,
            "target_id": body.target_id,
        }
        record = _repo(get_agent_fn).create_credential(
            owner_type="user",
            owner_user_id=user.id,
            name=body.name,
            provider=body.provider,
            kind=body.kind,
            metadata=metadata,
            allowed_targets=allowed_targets,
            status="pending_setup",
            created_by_user_id=user.id,
        )
        if body.target_type and body.target_id:
            _repo(get_agent_fn).bind_credential(
                record.id,
                target_type=body.target_type,
                target_id=body.target_id,
                binding_name="setup_request",
                actor_user_id=user.id,
            )
        _invalidate_llm_graphs(get_agent_fn, record)
        return credential_to_response(record)

    @router.get("/credentials/{credential_id}", response_model=CredentialResponse)
    async def get_credential(
        credential_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        record = _repo(get_agent_fn).get_credential(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_read(user, record)
        return credential_to_response(record)

    @router.patch("/credentials/{credential_id}", response_model=CredentialResponse)
    async def update_credential(
        credential_id: str,
        body: CredentialUpdateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        current = repo.get_credential(credential_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_manage(user, current)
        try:
            record = repo.upsert_credential(
                credential_id=credential_id,
                owner_type=current.owner_type,
                owner_user_id=current.owner_user_id,
                name=body.name or current.name,
                provider=current.provider,
                kind=current.kind,
                account_label=body.account_label if body.account_label is not None else current.account_label,
                metadata=body.metadata if body.metadata is not None else current.metadata,
                scopes=body.scopes if body.scopes is not None else current.scopes,
                allowed_targets=(
                    body.allowed_targets
                    if body.allowed_targets is not None
                    else current.allowed_targets
                ),
                expires_at=body.expires_at if body.expires_at is not None else current.expires_at,
                status=body.status or current.status,
                secret_fields=body.secret_fields or {},
                actor_user_id=user.id,
            )
        except (
            nymeria_secrets.SecretsKeyMissing,
            nymeria_secrets.SecretsKeyInvalid,
        ) as exc:
            raise _secret_error(exc) from exc
        _invalidate_llm_graphs(get_agent_fn, record)
        return credential_to_response(record)

    @router.delete("/credentials/{credential_id}")
    async def delete_credential(
        credential_id: str,
        hard: bool = False,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        record = repo.get_credential(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_manage(user, record)
        deleted = (
            repo.delete_credential(credential_id, actor_user_id=user.id)
            if hard
            else repo.disable_credential(credential_id, actor_user_id=user.id)
        )
        _invalidate_llm_graphs(get_agent_fn, record)
        return {"status": "ok", "deleted": deleted, "hard": hard}

    @router.post("/credentials/{credential_id}/test", response_model=CredentialResponse)
    async def test_credential(
        credential_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        record = repo.get_credential(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_manage(user, record)
        status = "active" if record.secret_fields else "pending_setup"
        repo.mark_tested(
            credential_id,
            status=status,
            actor_user_id=user.id,
            details={"status": status, "generic_test": True},
        )
        updated = repo.get_credential(credential_id)
        return credential_to_response(updated)

    @router.get(
        "/credentials/{credential_id}/bindings",
        response_model=list[CredentialBindingResponse],
    )
    async def list_credential_bindings(
        credential_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        record = repo.get_credential(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_read(user, record)
        return [CredentialBindingResponse(**row) for row in repo.list_bindings(credential_id)]

    @router.post(
        "/credentials/{credential_id}/bindings",
        response_model=CredentialBindingResponse,
    )
    async def bind_credential(
        credential_id: str,
        body: CredentialBindingRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        record = repo.get_credential(credential_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_manage(user, record)
        try:
            binding_id = repo.bind_credential(
                credential_id,
                target_type=body.target_type,
                target_id=body.target_id,
                binding_name=body.binding_name,
                actor_user_id=user.id,
            )
        except CredentialNotFound as exc:
            raise HTTPException(status_code=404, detail="Credential not found") from exc
        row = next((r for r in repo.list_bindings(credential_id) if r["id"] == binding_id), None)
        if row is None:
            raise HTTPException(status_code=500, detail="Binding was not saved")
        _invalidate_llm_graphs(get_agent_fn, record)
        return CredentialBindingResponse(**row)

    @router.delete("/credential-bindings/{binding_id}")
    async def delete_credential_binding(
        binding_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        repo = _repo(get_agent_fn)
        row = next((r for r in repo.list_bindings() if r["id"] == binding_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="Binding not found")
        record = repo.get_credential(row["credential_id"])
        if record is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        _require_manage(user, record)
        deleted = repo.delete_binding(binding_id, actor_user_id=user.id)
        _invalidate_llm_graphs(get_agent_fn, record)
        return {"status": "ok", "deleted": deleted}

    return router
