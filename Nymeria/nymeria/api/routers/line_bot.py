"""LINE Messaging API webhook routes."""

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
from ...triggers.line_bot import (
    BotAPIError,
    LineRESTClient,
    NymeriaLineBot,
    credential_source_present,
    validate_line_signature,
)
from ._bot_inprocess import InProcessBotAPI

logger = logging.getLogger(__name__)

_LINE_SEEN_CACHE = SeenEventCache()


def create_line_bot_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Settings],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create LINE webhook routes."""
    router = APIRouter(tags=["LINE"])

    @router.post("/integrations/line/webhook")
    async def receive_line_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        settings = get_settings_fn()
        raw_body = await request.body()

        if not credential_source_present(settings.line_channel_access_token):
            raise HTTPException(
                status_code=503,
                detail="LINE_CHANNEL_ACCESS_TOKEN is required",
            )
        if not settings.line_channel_secret:
            raise HTTPException(status_code=503, detail="LINE_CHANNEL_SECRET is required")
        signature = request.headers.get("x-line-signature")
        if not signature:
            raise HTTPException(status_code=401, detail="Missing LINE signature")
        if not validate_line_signature(raw_body, signature, settings.line_channel_secret):
            raise HTTPException(status_code=401, detail="Invalid LINE signature")

        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        background_tasks.add_task(
            _process_line_webhook,
            payload,
            settings,
            get_agent_fn,
            require_thread_access_fn,
            publish_sync_event_fn,
        )
        return {"status": "accepted"}

    return router


async def _process_line_webhook(
    payload: dict[str, Any],
    settings: Settings,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any],
) -> None:
    token = str(settings.line_channel_access_token or "")
    client = LineRESTClient(
        channel_access_token=token,
        api_base_url=settings.line_api_base_url,
    )
    bot = NymeriaLineBot(
        api=InProcessBotAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            origin_client_id="line",
            display_name="LINE",
            error_cls=BotAPIError,
        ),
        line_client=client,
        bot_name=settings.line_bot_name,
        bot_user_id=settings.line_bot_user_id,
        respond_mode=settings.line_respond_mode,
        show_tool_events=settings.line_show_tool_events,
        seen_cache=_LINE_SEEN_CACHE,
    )
    try:
        await bot.handle_payload(payload)
    except Exception:  # noqa: BLE001
        logger.exception("LINE webhook processing failed")
    finally:
        await bot.close()
