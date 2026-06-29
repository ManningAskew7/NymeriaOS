"""Chat-app binding and user-owned Telegram bot routes."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, get_args

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ...config import Settings
from ...core import secrets as nymeria_secrets
from ...core.accounts import AuthenticatedUser, UserNotFound
from ...core.chat_bindings import (
    BindClaimError,
    BindCodeInvalid,
    BindingAlreadyExists,
    BotAlreadyRegistered,
    _consume_bind_code_quietly,
    claim_platform_link,
    claim_thread_bind,
)
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...core.thread_classification import is_native_platform_thread
from ..schemas.accounts import PlatformIdentityResponse
from ..schemas.chat_apps import (
    AdminBindingLookupResponse,
    AdminChatAppBindClaimRequest,
    AdminChatAppBindClaimResponse,
    AdminChatAppBindClaimViaBotRequest,
    AdminChatAppSwitchRequest,
    AdminChatAppSwitchResponse,
    AdminPlatformLinkClaimRequest,
    AdminPlatformLinkClaimResponse,
    AdminTelegramBotResponse,
    ChatAppBindCodeRequest,
    ChatAppBindCodeResponse,
    ChatAppBindingResponse,
    ChatAppProvider,
    MyTelegramBotResponse,
    PlatformLinkCodeRequest,
    RegisterTelegramBotRequest,
)
from .threads import _thread_list_platform

logger = logging.getLogger(__name__)
KNOWN_CHAT_APP_PROVIDERS = frozenset(get_args(ChatAppProvider))


def _bot_username_for(provider: str, settings: Settings) -> str | None:
    """Return the bot's public username if configured."""
    if provider == "telegram":
        raw = (settings.telegram_bot_username or "").lstrip("@").strip()
        return raw or None
    return None


def _build_chatapp_link_payload(
    *,
    code: str,
    expires_at: str,
    provider: str,
    kind: str,
    settings: Settings,
) -> dict[str, Any]:
    """Shape bind-code responses and include Telegram deep links when possible."""
    bot_username = _bot_username_for(provider, settings)
    deep_link: str | None = None
    if bot_username:
        if kind == "platform_link":
            deep_link = f"https://t.me/{bot_username}?start=link_{code}"
        elif kind == "thread_bind":
            deep_link = f"https://t.me/{bot_username}?start=bind_{code}"
    return {
        "code": code,
        "expires_at": expires_at,
        "bot_username": bot_username,
        "deep_link": deep_link,
    }


def _expires_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def _require_known_provider(provider: str) -> None:
    if provider not in KNOWN_CHAT_APP_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown provider")


def _admin_binding_response(binding: Any) -> AdminBindingLookupResponse:
    return AdminBindingLookupResponse(
        id=binding.id,
        thread_id=binding.thread_id,
        provider=binding.provider,
        platform_chat_id=binding.platform_chat_id,
        user_id=binding.user_id,
        created_at=binding.created_at,
        user_telegram_bot_id=binding.user_telegram_bot_id,
    )


def _thread_binding_response(binding: Any) -> ChatAppBindingResponse:
    return ChatAppBindingResponse(
        id=binding.id,
        thread_id=binding.thread_id,
        provider=binding.provider,
        platform_chat_id=binding.platform_chat_id,
        created_at=binding.created_at,
        user_telegram_bot_id=binding.user_telegram_bot_id,
    )


def _my_telegram_bot_response(bot: Any) -> MyTelegramBotResponse:
    return MyTelegramBotResponse(
        id=bot.id,
        bot_username=bot.bot_username,
        enabled=bot.enabled,
        created_at=bot.created_at,
        last_seen_at=bot.last_seen_at,
    )


def _publish_chatapp_platform_sync(
    *,
    get_agent_fn: Callable[[], Any],
    publish_sync_event_fn: Callable[..., Any],
    thread_id: str,
    user_id: str,
    origin_client_id: str = "",
) -> None:
    """Notify clients when a chat-app binding changes a thread's platform icon."""
    agent = get_agent_fn()
    meta = agent.thread_metadata_manager.get_thread(user_id, thread_id)
    publish_sync_event_fn(
        event_type="thread_updated",
        thread_id=thread_id,
        user_id=user_id,
        data={"platform": _thread_list_platform(agent, thread_id, meta)},
        origin_client_id=origin_client_id,
    )


def create_chat_apps_router(
    verify_api_key: Callable[..., Any],
    require_rate_limited_admin_bot_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create the chat-app binding and BYO Telegram bot router."""
    router = APIRouter()

    def publish_platform_sync(
        thread_id: str,
        user_id: str,
        origin_client_id: str = "",
    ) -> None:
        _publish_chatapp_platform_sync(
            get_agent_fn=get_agent_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            thread_id=thread_id,
            user_id=user_id,
            origin_client_id=origin_client_id,
        )

    @router.get(
        "/admin/chatapp/bindings",
        response_model=list[AdminBindingLookupResponse],
        tags=["Admin"],
    )
    async def admin_list_chatapp_bindings(
        provider: str | None = None,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """List every chat-app binding, optionally filtered by provider."""
        if provider is not None:
            _require_known_provider(provider)
        repo = get_agent_fn().chat_bindings_repo
        return [
            _admin_binding_response(binding)
            for binding in repo.list_thread_bindings_global(provider=provider)
        ]

    @router.get(
        "/admin/chatapp/bindings/lookup",
        response_model=AdminBindingLookupResponse,
        tags=["Admin"],
    )
    async def admin_lookup_chatapp_binding(
        provider: str,
        platform_chat_id: str | None = None,
        thread_id: str | None = None,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Look up a thread-to-chat binding by either chat ID or thread ID."""
        _require_known_provider(provider)
        if (platform_chat_id is None) == (thread_id is None):
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one of 'platform_chat_id' or 'thread_id'",
            )
        repo = get_agent_fn().chat_bindings_repo
        binding = (
            repo.lookup_thread_binding_by_chat(provider, platform_chat_id)
            if platform_chat_id is not None
            else repo.lookup_thread_binding_by_thread(provider, thread_id)
        )
        if binding is None:
            raise HTTPException(status_code=404, detail="No binding")
        return _admin_binding_response(binding)

    @router.post(
        "/admin/chatapp/bindings/claim",
        response_model=AdminChatAppBindClaimResponse,
        tags=["Admin"],
    )
    async def admin_claim_thread_bind_code(
        body: AdminChatAppBindClaimRequest,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Consume a thread-bind code and create the chat-app binding."""
        agent = get_agent_fn()
        try:
            binding = claim_thread_bind(
                agent.accounts_repo,
                agent.chat_bindings_repo,
                code=body.code,
                provider=body.provider,
                platform_chat_id=body.platform_chat_id,
                expected_provider_user_id=body.expected_provider_user_id,
            )
        except BindCodeInvalid as exc:
            raise HTTPException(status_code=400, detail=f"Invalid code: {exc}") from exc
        except BindingAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BindClaimError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.reason) from exc
        publish_platform_sync(binding.thread_id, binding.user_id)
        return AdminChatAppBindClaimResponse(
            binding_id=binding.id,
            thread_id=binding.thread_id,
            user_id=binding.user_id,
        )

    @router.delete("/admin/chatapp/bindings/by-chat", tags=["Admin"])
    async def admin_unbind_chatapp_by_chat(
        provider: str,
        platform_chat_id: str,
        user_id: str | None = None,
        user_telegram_bot_id: int | None = None,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Remove the binding for a given provider chat ID."""
        _require_known_provider(provider)
        repo = get_agent_fn().chat_bindings_repo
        binding = repo.lookup_thread_binding_by_chat(provider, platform_chat_id)
        if binding is None:
            return {"unbound": False}
        if user_id is not None and binding.user_id != user_id:
            raise HTTPException(status_code=403, detail="Binding belongs to a different user")
        if (
            user_telegram_bot_id is not None
            and binding.user_telegram_bot_id != user_telegram_bot_id
        ):
            raise HTTPException(status_code=403, detail="Binding belongs to a different Telegram bot")
        repo.delete_thread_binding(binding.id, user_id=binding.user_id)
        publish_platform_sync(binding.thread_id, binding.user_id)
        return {"unbound": True, "thread_id": binding.thread_id}

    @router.post(
        "/admin/chatapp/bindings/switch",
        response_model=AdminChatAppSwitchResponse,
        tags=["Admin"],
    )
    async def admin_switch_chatapp_binding(
        body: AdminChatAppSwitchRequest,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Move a chat-app chat to another existing user-owned thread."""
        agent = get_agent_fn()
        repo = agent.accounts_repo
        bindings = agent.chat_bindings_repo
        target_thread_id = body.thread_id.strip()
        if not target_thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")
        if is_native_platform_thread(target_thread_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Native platform threads cannot be switch targets; use "
                    "/unbind to return a Telegram chat to its default thread"
                ),
            )
        user = repo.get_user_by_id(body.user_id)
        if user is None or user.disabled:
            raise HTTPException(status_code=404, detail="User not found")
        if target_thread_id not in set(repo.list_threads_for_user(body.user_id)):
            raise HTTPException(status_code=404, detail="Target thread not found")

        if body.user_telegram_bot_id is not None:
            bot = bindings.get_user_telegram_bot(body.user_telegram_bot_id)
            if bot is None or not bot.enabled:
                raise HTTPException(status_code=404, detail="Telegram bot not found")
            if bot.owner_user_id != body.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Telegram bot belongs to a different Nymeria user",
                )

        try:
            binding, previous_thread_id = bindings.switch_thread_binding_for_chat(
                thread_id=target_thread_id,
                provider=body.provider,
                platform_chat_id=body.platform_chat_id,
                user_id=body.user_id,
                user_telegram_bot_id=body.user_telegram_bot_id,
            )
        except BindingAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UserNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if previous_thread_id and previous_thread_id != binding.thread_id:
            publish_platform_sync(previous_thread_id, binding.user_id)
        publish_platform_sync(binding.thread_id, binding.user_id)
        return AdminChatAppSwitchResponse(
            binding_id=binding.id,
            thread_id=binding.thread_id,
            user_id=binding.user_id,
            previous_thread_id=previous_thread_id,
        )

    @router.post(
        "/admin/chatapp/bindings/claim-via-bot",
        response_model=AdminChatAppBindClaimResponse,
        tags=["Admin"],
    )
    async def admin_claim_thread_bind_code_via_bot(
        body: AdminChatAppBindClaimViaBotRequest,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Claim a thread-bind code from inside a user-owned Telegram bot."""
        repo = get_agent_fn().chat_bindings_repo
        bot = repo.get_user_telegram_bot(body.via_user_telegram_bot_id)
        if bot is None or not bot.enabled:
            logger.warning(
                "claim-via-bot: bot id=%s not found / disabled",
                body.via_user_telegram_bot_id,
            )
            raise HTTPException(status_code=404, detail="Bot not found")

        try:
            claim = repo.inspect_bind_code(
                body.code, kind="thread_bind", provider=body.provider
            )
        except BindCodeInvalid as exc:
            redacted = (
                (body.code[:2] + "*" * max(0, len(body.code) - 4) + body.code[-2:])
                if body.code
                else "(empty)"
            )
            logger.warning(
                "claim-via-bot: BindCodeInvalid for code=%s len=%d kind=thread_bind "
                "provider=%s bot_id=%s reason=%s",
                redacted,
                len(body.code or ""),
                body.provider,
                body.via_user_telegram_bot_id,
                exc,
            )
            raise HTTPException(status_code=400, detail=f"Invalid code: {exc}") from exc
        if claim.thread_id is None:
            raise HTTPException(status_code=500, detail="Code has no thread_id")
        if bot.owner_user_id != claim.user_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Bind code was issued by a different Nymeria account "
                    "than this bot's owner."
                ),
            )

        try:
            binding = repo.create_thread_binding(
                thread_id=claim.thread_id,
                provider=body.provider,
                platform_chat_id=body.platform_chat_id,
                user_id=claim.user_id,
                user_telegram_bot_id=bot.id,
            )
        except BindingAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # This via-bot variant keeps its own bot-ownership guard, redacted-code
        # logging, and ``user_telegram_bot_id`` passthrough above, so it does not
        # fold into ``claim_thread_bind``; only the best-effort code consume is
        # shared.
        _consume_bind_code_quietly(
            repo, body.code, kind="thread_bind", provider=body.provider
        )
        publish_platform_sync(binding.thread_id, binding.user_id)
        return AdminChatAppBindClaimResponse(
            binding_id=binding.id,
            thread_id=binding.thread_id,
            user_id=binding.user_id,
        )

    @router.get(
        "/admin/telegram-bots",
        response_model=list[AdminTelegramBotResponse],
        tags=["Admin"],
    )
    async def admin_list_telegram_bots(
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """List every enabled user-owned bot with decrypted tokens."""
        if not nymeria_secrets.has_secrets_key():
            return []
        repo = get_agent_fn().chat_bindings_repo
        out: list[AdminTelegramBotResponse] = []
        for bot, ciphertext in repo.list_user_telegram_bots_with_ciphertext():
            try:
                token = nymeria_secrets.decrypt(ciphertext)
            except (
                nymeria_secrets.InvalidToken,
                nymeria_secrets.SecretsKeyMissing,
                nymeria_secrets.SecretsKeyInvalid,
            ) as exc:
                logger.error(
                    "Couldn't decrypt token for bot id=%s username=@%s: %s",
                    bot.id,
                    bot.bot_username,
                    exc,
                )
                continue
            out.append(
                AdminTelegramBotResponse(
                    id=bot.id,
                    owner_user_id=bot.owner_user_id,
                    bot_username=bot.bot_username,
                    bot_token=token,
                    enabled=bot.enabled,
                    created_at=bot.created_at,
                    last_seen_at=bot.last_seen_at,
                )
            )
        return out

    @router.post("/admin/telegram-bots/{bot_id}/seen", tags=["Admin"])
    async def admin_telegram_bot_seen(
        bot_id: int,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Heartbeat ping from the supervisor after a successful poll cycle."""
        repo = get_agent_fn().chat_bindings_repo
        if repo.get_user_telegram_bot(bot_id) is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        repo.update_user_telegram_bot_seen(bot_id)
        return {"updated": True}

    @router.post(
        "/admin/platform/link-codes/claim",
        response_model=AdminPlatformLinkClaimResponse,
        tags=["Admin"],
    )
    async def admin_claim_platform_link_code(
        body: AdminPlatformLinkClaimRequest,
        _admin: AuthenticatedUser = Depends(require_rate_limited_admin_bot_user),
    ):
        """Consume a platform-link code and link the platform user."""
        agent = get_agent_fn()
        try:
            platform = claim_platform_link(
                agent.accounts_repo,
                agent.chat_bindings_repo,
                code=body.code,
                provider=body.provider,
                platform_user_id=body.platform_user_id,
            )
        except BindCodeInvalid as exc:
            raise HTTPException(status_code=400, detail=f"Invalid code: {exc}") from exc
        except BindClaimError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.reason) from exc
        return AdminPlatformLinkClaimResponse(
            user_id=platform.user_id,
            provider=body.provider,
            provider_user_id=body.platform_user_id,
            created_at=platform.created_at,
        )

    @router.get(
        "/me/platforms",
        response_model=list[PlatformIdentityResponse],
        tags=["Auth"],
    )
    async def list_my_platforms(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List the current user's linked platform identities."""
        repo = get_agent_fn().accounts_repo
        return [
            PlatformIdentityResponse(
                provider=platform.provider,
                provider_user_id=platform.provider_user_id,
                created_at=platform.created_at,
            )
            for platform in repo.list_platforms_for_user(user.id)
        ]

    @router.post(
        "/me/platform-link-codes",
        response_model=ChatAppBindCodeResponse,
        tags=["Auth"],
    )
    async def issue_my_platform_link_code(
        body: PlatformLinkCodeRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Issue a short-lived code for self-service chat-app identity linking."""
        ttl_seconds = 600
        repo = get_agent_fn().chat_bindings_repo
        raw = repo.issue_bind_code(
            kind="platform_link",
            provider=body.provider,
            user_id=user.id,
            ttl_seconds=ttl_seconds,
        )
        return ChatAppBindCodeResponse(
            **_build_chatapp_link_payload(
                code=raw,
                expires_at=_expires_in(ttl_seconds),
                provider=body.provider,
                kind="platform_link",
                settings=settings,
            )
        )

    @router.post(
        "/threads/{thread_id}/chatapp/bind-code",
        response_model=ChatAppBindCodeResponse,
        tags=["Threads"],
    )
    async def issue_thread_chatapp_bind_code(
        thread_id: str,
        body: ChatAppBindCodeRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Issue a short-lived code to bind a chat to this thread."""
        require_thread_access_fn(user, thread_id)
        repo = get_agent_fn().chat_bindings_repo
        existing = repo.lookup_thread_binding_by_thread(body.provider, thread_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Thread already bound to {body.provider} chat "
                    f"{existing.platform_chat_id}"
                ),
            )
        ttl_seconds = 600
        raw = repo.issue_bind_code(
            kind="thread_bind",
            provider=body.provider,
            user_id=user.id,
            thread_id=thread_id,
            ttl_seconds=ttl_seconds,
        )
        return ChatAppBindCodeResponse(
            **_build_chatapp_link_payload(
                code=raw,
                expires_at=_expires_in(ttl_seconds),
                provider=body.provider,
                kind="thread_bind",
                settings=settings,
            )
        )

    @router.get(
        "/threads/{thread_id}/chatapp/bindings",
        response_model=list[ChatAppBindingResponse],
        tags=["Threads"],
    )
    async def list_thread_chatapp_bindings(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List chat-app bindings for this thread."""
        require_thread_access_fn(user, thread_id, claim=False)
        repo = get_agent_fn().chat_bindings_repo
        return [
            _thread_binding_response(binding)
            for binding in repo.list_thread_bindings(thread_id)
        ]

    @router.delete(
        "/threads/{thread_id}/chatapp/bindings/{binding_id}",
        tags=["Threads"],
    )
    async def delete_thread_chatapp_binding(
        http_request: Request,
        thread_id: str,
        binding_id: int,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Unbind a chat from this thread."""
        require_thread_access_fn(user, thread_id)
        repo = get_agent_fn().chat_bindings_repo
        thread_bindings = repo.list_thread_bindings(thread_id)
        binding = next(
            (b for b in thread_bindings if b.id == binding_id and b.user_id == user.id),
            None,
        )
        ok = repo.delete_thread_binding(binding_id, user_id=user.id)
        if not ok:
            raise HTTPException(status_code=404, detail="Binding not found")
        if binding is not None:
            client_id = http_request.headers.get("x-nymeria-client-id", "")
            publish_platform_sync(
                binding.thread_id,
                binding.user_id,
                origin_client_id=client_id,
            )
        return {"unbound": True}

    @router.get(
        "/me/telegram-bots",
        response_model=list[MyTelegramBotResponse],
        tags=["Auth"],
    )
    async def list_my_telegram_bots(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List the current user's registered Telegram bots."""
        repo = get_agent_fn().chat_bindings_repo
        return [
            _my_telegram_bot_response(bot)
            for bot in repo.list_user_telegram_bots(user.id)
        ]

    @router.get(
        "/me/telegram-bots/{bot_id}",
        response_model=MyTelegramBotResponse,
        tags=["Auth"],
    )
    async def get_my_telegram_bot(
        bot_id: int,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Fetch a single registered Telegram bot owned by the caller."""
        repo = get_agent_fn().chat_bindings_repo
        bot = repo.get_user_telegram_bot(bot_id, owner_user_id=user.id)
        if bot is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        return _my_telegram_bot_response(bot)

    @router.post(
        "/me/telegram-bots",
        response_model=MyTelegramBotResponse,
        tags=["Auth"],
    )
    async def register_my_telegram_bot(
        body: RegisterTelegramBotRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Validate and register a user-owned Telegram bot token."""
        if not nymeria_secrets.has_secrets_key():
            raise HTTPException(
                status_code=503,
                detail=(
                    "NYMERIA_SECRETS_KEY is not configured on the server. "
                    "Add it to .env.docker (generate with `python3 -c \"from "
                    "cryptography.fernet import Fernet; print(Fernet."
                    "generate_key().decode())\"`) and restart the api "
                    "container before registering BYO bots."
                ),
            )
        token = (body.bot_token or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="bot_token is required")

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getMe"
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Couldn't reach Telegram: {exc}",
                ) from exc
        if resp.status_code != 200:
            try:
                detail = resp.json().get("description") or resp.text[:200]
            except Exception:
                detail = resp.text[:200]
            raise HTTPException(
                status_code=400,
                detail=f"Telegram rejected the token: {detail}",
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Bad JSON from Telegram") from exc
        if not data.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=f"Token invalid: {data.get('description', 'unknown')}",
            )
        result = data.get("result") or {}
        bot_username = result.get("username")
        if not bot_username:
            raise HTTPException(
                status_code=400,
                detail="Telegram didn't return a username for this token",
            )

        try:
            ciphertext = nymeria_secrets.encrypt(token)
        except (
            nymeria_secrets.SecretsKeyMissing,
            nymeria_secrets.SecretsKeyInvalid,
        ) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        repo = get_agent_fn().chat_bindings_repo
        try:
            bot = repo.register_user_telegram_bot(
                owner_user_id=user.id,
                bot_username=bot_username,
                bot_token_ciphertext=ciphertext,
            )
        except BotAlreadyRegistered:
            existing = repo.get_user_telegram_bot_by_username(bot_username)
            if existing is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Bot @{bot_username} is already registered",
                )
            if existing.owner_user_id != user.id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Bot @{bot_username} is registered to a different "
                        "account. Use BotFather to revoke + reissue the token, "
                        "then paste the new token."
                    ),
                )
            bot = existing
        return _my_telegram_bot_response(bot)

    @router.delete("/me/telegram-bots/{bot_id}", tags=["Auth"])
    async def delete_my_telegram_bot(
        bot_id: int,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Remove one of the caller's registered Telegram bots."""
        repo = get_agent_fn().chat_bindings_repo
        affected_bindings = [
            binding
            for binding in repo.list_thread_bindings_for_user(user.id)
            if binding.user_telegram_bot_id == bot_id
        ]
        ok = repo.delete_user_telegram_bot(bot_id, owner_user_id=user.id)
        if not ok:
            raise HTTPException(status_code=404, detail="Bot not found")
        for binding in affected_bindings:
            publish_platform_sync(binding.thread_id, binding.user_id)
        return {"deleted": True}

    return router
