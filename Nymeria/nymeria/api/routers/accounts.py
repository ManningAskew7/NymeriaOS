"""Identity, token, and admin account routes."""

import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any, get_args

from fastapi import APIRouter, Depends, HTTPException

from ...config import Settings
from ...core.accounts import (
    AmbiguousTokenPrefix,
    AuthenticatedUser,
    LastAdminError,
    TokenNotFound,
    TokenLimitExceeded,
    UserAlreadyExists,
    UserHasResources,
    UserNotFound,
)
from ...core.todo_manager import TodoManager
from ..schemas.accounts import (
    AdminUserCreateRequest,
    AdminUserResponse,
    AdminUserUpdateRequest,
    IssuedTokenResponse,
    MeUpdateRequest,
    PlatformProvider,
    PlatformIdentityResponse,
    PlatformLinkRequest,
    RotatedTokensResponse,
    TokenInfoResponse,
    TokenIssueRequest,
    token_info,
)


logger = logging.getLogger(__name__)
KNOWN_PLATFORM_PROVIDERS = frozenset(get_args(PlatformProvider))


def _identity_response(user: AuthenticatedUser) -> dict[str, str]:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }


def _find_issued_token_record(repo: Any, user_id: str, raw_token: str) -> Any:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    records = repo.list_tokens_for_user(user_id)
    return next((r for r in records if r.token_hash == token_hash), None)


def _issued_token_response(repo: Any, user_id: str, raw_token: str) -> IssuedTokenResponse:
    rec = _find_issued_token_record(repo, user_id, raw_token)
    if rec is None:
        raise HTTPException(status_code=500, detail="Token issued but not found")
    return IssuedTokenResponse(raw_token=raw_token, metadata=token_info(rec))


def _active_token_summary(repo: Any, user_id: str) -> tuple[int, str | None]:
    tokens = repo.list_tokens_for_user(user_id)
    active = [t for t in tokens if t.revoked_at is None]
    last_use = max((t.last_used_at for t in active if t.last_used_at), default=None)
    return len(active), last_use


def _admin_user_response(
    repo: Any,
    user_record: Any,
    *,
    settings: Settings | None = None,
    include_counts: bool = False,
) -> AdminUserResponse:
    token_count, last_token_use = _active_token_summary(repo, user_record.id)
    thread_count = None
    todo_count = None
    platform_count = None

    if include_counts:
        threads = repo.list_threads_for_user(user_record.id)
        platforms = repo.list_platforms_for_user(user_record.id)
        thread_count = len(threads)
        platform_count = len(platforms)
        if settings is not None:
            try:
                todos = TodoManager(settings.data_dir).get_todos(user_record.id)
                todo_count = len(todos.items) if todos else 0
            except Exception:
                todo_count = 0

    return AdminUserResponse(
        id=user_record.id,
        email=user_record.email,
        display_name=user_record.display_name,
        role=user_record.role,
        disabled=user_record.disabled,
        created_at=user_record.created_at,
        updated_at=user_record.updated_at,
        token_count=token_count,
        last_token_use=last_token_use,
        thread_count=thread_count,
        todo_count=todo_count,
        platform_count=platform_count,
    )


def _platform_response(platform_record: Any) -> PlatformIdentityResponse:
    return PlatformIdentityResponse(
        provider=platform_record.provider,
        provider_user_id=platform_record.provider_user_id,
        created_at=platform_record.created_at,
    )


def create_accounts_router(
    verify_api_key: Callable[..., Any],
    resolve_authenticated_user: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the account/admin router with app dependencies injected."""
    router = APIRouter()

    @router.get("/me", tags=["Auth"])
    async def get_me(user: AuthenticatedUser = Depends(resolve_authenticated_user)):
        """
        Return the authenticated user's identity.

        Frontends call this on first connect to learn their own ``user_id`` so
        they can namespace ``localStorage`` keys (``nymeria-<user_id>-*``).
        Requires a per-user account token (``nym_...``); honors
        ``X-Nymeria-Act-As: <user_id>`` for admin callers.
        """
        return _identity_response(user)

    @router.get("/platform/resolve", tags=["Auth"])
    async def platform_resolve(
        provider: str,
        provider_user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """
        Resolve a platform identity to a Nymeria ``user_id``. Admin-only.
        """
        if provider not in KNOWN_PLATFORM_PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        user_id = get_agent_fn().accounts_repo.resolve_platform(provider, provider_user_id)
        if user_id is None:
            raise HTTPException(status_code=404, detail="Not linked")
        return {"user_id": user_id}

    @router.patch("/me", tags=["Auth"])
    async def patch_me(
        body: MeUpdateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Settings = Depends(get_settings_fn),
    ):
        """Update the current user's own profile fields."""
        if body.display_name is not None and not body.display_name.strip():
            raise HTTPException(status_code=400, detail="display_name cannot be empty")
        try:
            updated = get_agent_fn().accounts_repo.update_user(
                user.id,
                display_name=body.display_name,
            )
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        return {
            "id": updated.id,
            "email": updated.email,
            "display_name": updated.display_name,
            "role": updated.role,
        }

    @router.get("/me/tokens", response_model=list[TokenInfoResponse], tags=["Auth"])
    async def list_my_tokens(user: AuthenticatedUser = Depends(verify_api_key)):
        """List the current user's own API tokens."""
        records = get_agent_fn().accounts_repo.list_tokens_for_user(user.id)
        return [token_info(r) for r in records]

    @router.post("/me/tokens", response_model=IssuedTokenResponse, tags=["Auth"])
    async def issue_my_token(
        body: TokenIssueRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Issue a new token for the current user. Raw token returned ONCE."""
        repo = get_agent_fn().accounts_repo
        try:
            raw = repo.issue_token(user.id, label=body.label)
        except TokenLimitExceeded as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _issued_token_response(repo, user.id, raw)

    @router.delete("/me/tokens/{token_hash_prefix}", tags=["Auth"])
    async def revoke_my_token(
        token_hash_prefix: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Revoke one of the current user's tokens by hash prefix."""
        try:
            revoked = get_agent_fn().accounts_repo.revoke_token(
                user.id,
                token_hash_prefix,
            )
        except TokenNotFound as exc:
            raise HTTPException(status_code=404, detail="Token not found") from exc
        except AmbiguousTokenPrefix as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"revoked": revoked}

    @router.get(
        "/admin/users",
        response_model=list[AdminUserResponse],
        tags=["Admin"],
    )
    async def admin_list_users(
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """List every account."""
        repo = get_agent_fn().accounts_repo
        return [_admin_user_response(repo, u) for u in repo.list_users()]

    @router.post(
        "/admin/users",
        response_model=IssuedTokenResponse,
        tags=["Admin"],
    )
    async def admin_create_user(
        body: AdminUserCreateRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Create a new user and issue them a first token. Raw token shown ONCE."""
        repo = get_agent_fn().accounts_repo
        email = body.email.strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Valid email required")

        if body.id:
            user_id = body.id
        else:
            user_id = (
                re.sub(r"[^a-z0-9_-]+", "", email.split("@", 1)[0])[:32]
                or uuid.uuid4().hex[:12]
            )
        if repo.get_user_by_id(user_id) is not None:
            user_id = f"{user_id}-{uuid.uuid4().hex[:6]}"

        display_name = (body.display_name or email.split("@", 1)[0]).strip()
        try:
            repo.create_user(
                user_id=user_id,
                email=email,
                display_name=display_name,
                role=body.role,
            )
        except UserAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        raw = repo.issue_token(user_id, label=body.token_label or "initial")
        return _issued_token_response(repo, user_id, raw)

    @router.get(
        "/admin/users/{user_id}",
        response_model=AdminUserResponse,
        tags=["Admin"],
    )
    async def admin_get_user(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get a single user with full counts."""
        repo = get_agent_fn().accounts_repo
        user_record = repo.get_user_by_id(user_id)
        if user_record is None:
            raise HTTPException(status_code=404, detail="User not found")
        return _admin_user_response(
            repo,
            user_record,
            settings=settings,
            include_counts=True,
        )

    @router.patch(
        "/admin/users/{user_id}",
        response_model=AdminUserResponse,
        tags=["Admin"],
    )
    async def admin_update_user(
        user_id: str,
        body: AdminUserUpdateRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update display_name / role / disabled. Last-admin guard applies."""
        repo = get_agent_fn().accounts_repo
        try:
            if body.display_name is not None or body.role is not None:
                if body.display_name is not None and not body.display_name.strip():
                    raise HTTPException(
                        status_code=400,
                        detail="display_name cannot be empty",
                    )
                repo.update_user(
                    user_id,
                    display_name=body.display_name,
                    role=body.role,
                )
            if body.disabled is not None:
                repo.set_disabled(user_id, body.disabled)
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        except LastAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        user_record = repo.get_user_by_id(user_id)
        return _admin_user_response(repo, user_record)

    @router.delete("/admin/users/{user_id}", tags=["Admin"])
    async def admin_delete_user(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Delete a user and cascade their tokens / platform identities."""
        repo = get_agent_fn().accounts_repo
        try:
            todos = TodoManager(settings.data_dir).get_todos(user_id)
            if todos and todos.items:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"User still owns {len(todos.items)} todo(s); delete "
                        "those first"
                    ),
                )
        except HTTPException:
            raise
        except Exception:
            logger.debug("Todo file cleanup skipped, may not exist")

        try:
            repo.delete_user_cascade(user_id)
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        except UserHasResources as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LastAdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": True}

    @router.get(
        "/admin/users/{user_id}/tokens",
        response_model=list[TokenInfoResponse],
        tags=["Admin"],
    )
    async def admin_list_user_tokens(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent_fn().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        return [token_info(r) for r in repo.list_tokens_for_user(user_id)]

    @router.post(
        "/admin/users/{user_id}/tokens",
        response_model=IssuedTokenResponse,
        tags=["Admin"],
    )
    async def admin_issue_user_token(
        user_id: str,
        body: TokenIssueRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent_fn().accounts_repo
        try:
            raw = repo.issue_token(user_id, label=body.label)
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        except TokenLimitExceeded as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _issued_token_response(repo, user_id, raw)

    @router.post(
        "/admin/users/{user_id}/tokens/rotate",
        response_model=RotatedTokensResponse,
        tags=["Admin"],
    )
    async def admin_rotate_user_tokens(
        user_id: str,
        body: TokenIssueRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Revoke every active token for the user, then issue a fresh one."""
        repo = get_agent_fn().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        revoked_count = repo.revoke_all_tokens(user_id)
        raw = repo.issue_token(user_id, label=body.label or "rotated")
        rec = _find_issued_token_record(repo, user_id, raw)
        if rec is None:
            raise HTTPException(status_code=500, detail="Token issued but not found")
        return RotatedTokensResponse(
            raw_token=raw,
            metadata=token_info(rec),
            revoked_count=revoked_count,
        )

    @router.delete(
        "/admin/users/{user_id}/tokens/{token_hash_prefix}",
        tags=["Admin"],
    )
    async def admin_revoke_user_token(
        user_id: str,
        token_hash_prefix: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent_fn().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        try:
            revoked = repo.revoke_token(user_id, token_hash_prefix)
        except TokenNotFound as exc:
            raise HTTPException(status_code=404, detail="Token not found") from exc
        except AmbiguousTokenPrefix as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"revoked": revoked}

    @router.get(
        "/admin/users/{user_id}/platforms",
        response_model=list[PlatformIdentityResponse],
        tags=["Admin"],
    )
    async def admin_list_user_platforms(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent_fn().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        return [_platform_response(p) for p in repo.list_platforms_for_user(user_id)]

    @router.post(
        "/admin/users/{user_id}/platforms",
        response_model=PlatformIdentityResponse,
        tags=["Admin"],
    )
    async def admin_link_user_platform(
        user_id: str,
        body: PlatformLinkRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent_fn().accounts_repo
        existing_owner = repo.resolve_platform(body.provider, body.provider_user_id)
        if existing_owner is not None and existing_owner != user_id:
            raise HTTPException(
                status_code=409,
                detail=f"Platform identity already linked to user '{existing_owner}'",
            )
        try:
            repo.link_platform(body.provider, body.provider_user_id, user_id)
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc

        for platform in repo.list_platforms_for_user(user_id):
            if (
                platform.provider == body.provider
                and platform.provider_user_id == body.provider_user_id
            ):
                return _platform_response(platform)
        raise HTTPException(status_code=500, detail="Linked but not found")

    @router.delete(
        "/admin/users/{user_id}/platforms/{provider}/{provider_user_id}",
        tags=["Admin"],
    )
    async def admin_unlink_user_platform(
        user_id: str,
        provider: str,
        provider_user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        if provider not in KNOWN_PLATFORM_PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        repo = get_agent_fn().accounts_repo
        owner = repo.resolve_platform(provider, provider_user_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="Not linked")
        if owner != user_id:
            raise HTTPException(status_code=404, detail="Not linked to this user")
        repo.unlink_platform(provider, provider_user_id)
        return {"unlinked": True}

    return router
