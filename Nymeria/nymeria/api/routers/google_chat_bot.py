"""Google Chat webhook routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ...config import Settings
from ...core.accounts import AuthenticatedUser
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...triggers.bot_helpers import SeenEventCache
from ...triggers.google_chat_bot import (
    BotAPIError,
    GoogleChatRESTClient,
    NymeriaGoogleChatBot,
    credential_source_present,
    validate_googlechat_authorization,
)
from ._bot_inprocess import InProcessBotAPI

logger = logging.getLogger(__name__)

_GOOGLECHAT_SEEN_CACHE = SeenEventCache()


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
        audience = _google_chat_auth_audience(settings, request)
        try:
            await validate_googlechat_authorization(
                request.headers.get("authorization"),
                audience=audience,
                audience_type=settings.google_chat_auth_audience_type,
            )
        except BotAPIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
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
        api=InProcessBotAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            origin_client_id="googlechat",
            display_name="Google Chat",
            error_cls=BotAPIError,
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
