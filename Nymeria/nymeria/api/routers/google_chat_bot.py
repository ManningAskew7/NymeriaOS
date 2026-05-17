"""Google Chat webhook routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ...config import Settings
from ...core.accounts import AuthenticatedUser, UserNotFound
from ...core.chat_bindings import BindCodeInvalid, BindingAlreadyExists
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...triggers.google_chat_bot import (
    BotAPIError,
    GoogleChatRESTClient,
    NymeriaGoogleChatBot,
    _SeenEventCache,
    credential_source_present,
    validate_googlechat_authorization,
)
from .threads import _thread_list_platform

logger = logging.getLogger(__name__)

_GOOGLECHAT_SEEN_CACHE = _SeenEventCache()


class InProcessGoogleChatAPI:
    """Nymeria API adapter used by the API-hosted Google Chat webhook client."""

    def __init__(
        self,
        *,
        agent: Any,
        require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
        publish_sync_event_fn: Callable[..., Any],
    ) -> None:
        self.agent = agent
        self._require_thread_access = require_thread_access_fn
        self._publish_sync_event = publish_sync_event_fn

    async def resolve_platform_user(self, provider: str, provider_user_id: str) -> Optional[str]:
        return self.agent.accounts_repo.resolve_platform(provider, provider_user_id)

    async def list_chatapp_bindings(self, provider: str) -> list[dict[str, Any]]:
        return [
            {
                "id": binding.id,
                "thread_id": binding.thread_id,
                "provider": binding.provider,
                "platform_chat_id": binding.platform_chat_id,
                "user_id": binding.user_id,
                "created_at": binding.created_at,
                "user_telegram_bot_id": binding.user_telegram_bot_id,
            }
            for binding in self.agent.chat_bindings_repo.list_thread_bindings_global(
                provider=provider
            )
        ]

    async def claim_platform_link_code(
        self,
        *,
        code: str,
        provider: str,
        platform_user_id: str,
    ) -> dict[str, Any]:
        repo = self.agent.accounts_repo
        bindings = self.agent.chat_bindings_repo
        try:
            claim = bindings.inspect_bind_code(
                code, kind="platform_link", provider=provider
            )
        except BindCodeInvalid as exc:
            raise BotAPIError(f"Invalid code: {exc}", status_code=400) from exc

        existing = repo.resolve_platform(provider, platform_user_id)
        if existing is not None and existing != claim.user_id:
            raise BotAPIError(
                f"Platform identity already linked to user '{existing}'",
                status_code=409,
            )
        try:
            repo.link_platform(provider, platform_user_id, claim.user_id)
        except UserNotFound as exc:
            raise BotAPIError("User not found", status_code=404) from exc
        try:
            bindings.claim_bind_code(code, kind="platform_link", provider=provider)
        except BindCodeInvalid:
            pass  # Link already succeeded; a concurrently consumed code is harmless.
        for platform in repo.list_platforms_for_user(claim.user_id):
            if (
                platform.provider == provider
                and platform.provider_user_id == platform_user_id
            ):
                return {
                    "user_id": claim.user_id,
                    "provider": provider,
                    "provider_user_id": platform_user_id,
                    "created_at": platform.created_at,
                }
        raise BotAPIError("Linked but not found", status_code=500)

    async def claim_thread_bind_code(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        expected_provider_user_id: str,
    ) -> dict[str, Any]:
        repo = self.agent.accounts_repo
        bindings = self.agent.chat_bindings_repo
        try:
            claim = bindings.inspect_bind_code(
                code, kind="thread_bind", provider=provider
            )
        except BindCodeInvalid as exc:
            raise BotAPIError(f"Invalid code: {exc}", status_code=400) from exc
        if claim.thread_id is None:
            raise BotAPIError("Code has no thread_id", status_code=500)

        resolved_user = repo.resolve_platform(provider, expected_provider_user_id)
        if resolved_user != claim.user_id:
            raise BotAPIError(
                "Code was issued by a different Nymeria account",
                status_code=403,
            )

        try:
            binding = bindings.create_thread_binding(
                thread_id=claim.thread_id,
                provider=provider,
                platform_chat_id=platform_chat_id,
                user_id=claim.user_id,
            )
        except BindingAlreadyExists as exc:
            raise BotAPIError(str(exc), status_code=409) from exc

        try:
            bindings.claim_bind_code(code, kind="thread_bind", provider=provider)
        except BindCodeInvalid:
            pass  # Binding already succeeded; a concurrently consumed code is harmless.
        self._publish_platform_sync(binding.thread_id, binding.user_id)
        return {
            "binding_id": binding.id,
            "thread_id": binding.thread_id,
            "user_id": binding.user_id,
        }

    async def unbind_chatapp_by_chat(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        repo = self.agent.chat_bindings_repo
        binding = repo.lookup_thread_binding_by_chat(provider, platform_chat_id)
        if binding is None:
            return {"unbound": False}
        if user_id is not None and binding.user_id != user_id:
            raise BotAPIError(
                "Binding belongs to a different user",
                status_code=403,
            )
        repo.delete_thread_binding(binding.id, user_id=binding.user_id)
        self._publish_platform_sync(binding.thread_id, binding.user_id)
        return {"unbound": True, "thread_id": binding.thread_id}

    async def stop(self, thread_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
        authed = self._authenticated_user(user_id)
        self._require_thread_access(authed, thread_id)
        lock_info = self.agent._thread_locks.get_lock_info(thread_id)
        if lock_info:
            self.agent.abort_with_cascade(thread_id)
            return {"status": "stopping", "thread_id": thread_id}
        return {"status": "idle", "thread_id": thread_id}

    def chat_stream(
        self,
        message: str,
        thread_id: str,
        user_id: str,
    ):
        return self._chat_stream(message, thread_id, user_id)

    async def _chat_stream(self, message: str, thread_id: str, user_id: str):
        authed = self._authenticated_user(user_id)
        self._require_thread_access(authed, thread_id)
        self._publish_sync_event(
            event_type="message_added",
            thread_id=thread_id,
            user_id=user_id,
            data={"role": "user", "content": message},
            origin_client_id="googlechat",
        )
        async for chunk in self.agent.astream(
            message,
            thread_id=thread_id,
            user_id=user_id,
        ):
            yield chunk
        done: dict[str, Any] = {
            "type": "done",
            "thread_id": thread_id,
        }
        try:
            done["context_stats"] = self.agent.get_context_stats(thread_id)
            done["model"] = (
                self.agent._get_llm_config_for_thread(thread_id).model
                or self.agent.settings.llm_model
            )
        except Exception:  # noqa: BLE001
            logger.debug("Google Chat done metadata failed", exc_info=True)
        try:
            title = self.agent.thread_metadata_manager.auto_title(
                user_id, thread_id, message
            )
            if title:
                done["title"] = title
                done["title_source"] = "auto"
                self._publish_sync_event(
                    event_type="thread_updated",
                    thread_id=thread_id,
                    user_id=user_id,
                    data={"title": title, "title_source": "auto"},
                    origin_client_id="googlechat",
                )
        except Exception:  # noqa: BLE001
            logger.debug("Google Chat auto-title failed", exc_info=True)
        yield done

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        authed = self._authenticated_user(user_id)
        self._require_thread_access(authed, thread_id)
        response = self.agent.chat(message, thread_id=thread_id, user_id=user_id)
        return {
            "response": response,
            "thread_id": thread_id,
            "tool_call_count": getattr(self.agent, "_last_chat_tool_calls", 0),
        }

    def _authenticated_user(self, user_id: Optional[str]) -> AuthenticatedUser:
        if not user_id:
            raise BotAPIError("User not found", status_code=404)
        user = self.agent.accounts_repo.get_user_by_id(user_id)
        if user is None or user.disabled:
            raise BotAPIError("User not found", status_code=404)
        return AuthenticatedUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            via_act_as=True,
        )

    def _publish_platform_sync(self, thread_id: str, user_id: str) -> None:
        try:
            meta = self.agent.thread_metadata_manager.get_thread(user_id, thread_id)
            self._publish_sync_event(
                event_type="thread_updated",
                thread_id=thread_id,
                user_id=user_id,
                data={"platform": _thread_list_platform(self.agent, thread_id, meta)},
                origin_client_id="googlechat",
            )
        except Exception:  # noqa: BLE001
            logger.debug("Google Chat platform sync publish failed", exc_info=True)


def create_google_chat_bot_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Settings],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create Google Chat webhook routes."""
    router = APIRouter(tags=["Google Chat"])

    @router.post("/integrations/google-chat/webhook")
    async def receive_google_chat_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        settings = get_settings_fn()
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        if settings.google_chat_validate_auth:
            audience = _google_chat_auth_audience(settings, request)
            try:
                await validate_googlechat_authorization(
                    request.headers.get("authorization"),
                    audience=audience,
                    audience_type=settings.google_chat_auth_audience_type,
                )
            except BotAPIError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        if not credential_source_present(
            service_account_json=settings.google_chat_service_account_json,
            service_account_file=settings.google_chat_service_account_file,
            use_adc=settings.google_chat_use_adc,
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON or "
                    "GOOGLE_CHAT_SERVICE_ACCOUNT_FILE is required"
                ),
            )
        background_tasks.add_task(
            _process_google_chat_webhook,
            payload,
            settings,
            get_agent_fn,
            require_thread_access_fn,
            publish_sync_event_fn,
        )
        return {"status": "accepted"}

    return router


def _google_chat_auth_audience(settings: Settings, request: Request) -> str:
    if settings.google_chat_auth_audience:
        return settings.google_chat_auth_audience
    if settings.google_chat_auth_audience_type == "project-number":
        return settings.google_chat_project_number or ""
    return str(request.url)


async def _process_google_chat_webhook(
    payload: dict[str, Any],
    settings: Settings,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any],
) -> None:
    client = GoogleChatRESTClient(
        service_account_json=settings.google_chat_service_account_json,
        service_account_file=settings.google_chat_service_account_file,
        api_base_url=settings.google_chat_api_base_url,
    )
    bot = NymeriaGoogleChatBot(
        api=InProcessGoogleChatAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
        ),
        googlechat_client=client,
        bot_name=settings.google_chat_bot_name,
        respond_mode=settings.google_chat_respond_mode,
        show_tool_events=settings.google_chat_show_tool_events,
        seen_cache=_GOOGLECHAT_SEEN_CACHE,
    )
    try:
        await bot.handle_payload(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Google Chat webhook processing failed")
    finally:
        await bot.close()
