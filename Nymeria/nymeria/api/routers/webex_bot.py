"""Webex Messaging webhook routes."""

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
from ...triggers.webex_bot import (
    BotAPIError,
    NymeriaWebexBot,
    WebexMessagingClient,
    verify_webex_signature,
)
from ...triggers.webhook_security import require_configured_secret
from ._bot_inprocess import InProcessBotAPI

logger = logging.getLogger(__name__)

_WEBEX_SEEN_CACHE = SeenEventCache()


def create_webex_bot_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Settings],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create Webex Messaging webhook routes."""
    router = APIRouter(tags=["Webex"])

    @router.post("/integrations/webex/webhook")
    async def receive_webex_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
    ):
        settings = get_settings_fn()
        raw_body = await request.body()
        webhook_secret = require_configured_secret(
            settings.webex_webhook_secret,
            "WEBEX_WEBHOOK_SECRET",
        )
        if not verify_webex_signature(
            raw_body,
            request.headers.get("x-spark-signature"),
            webhook_secret,
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
        if not settings.webex_access_token:
            raise HTTPException(
                status_code=503,
                detail="WEBEX_ACCESS_TOKEN is required",
            )
        background_tasks.add_task(
            _process_webex_webhook,
            payload,
            settings,
            get_agent_fn,
            require_thread_access_fn,
            publish_sync_event_fn,
        )
        return {"status": "accepted"}

    return router


async def _process_webex_webhook(
    payload: dict[str, Any],
    settings: Settings,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any],
) -> None:
    webex = WebexMessagingClient(
        access_token=settings.webex_access_token or "",
        base_url=settings.webex_base_url,
    )
    bot = NymeriaWebexBot(
        InProcessBotAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            origin_client_id="webex",
            display_name="Webex",
            error_cls=BotAPIError,
        ),
        webex,
        bot_person_id=settings.webex_bot_person_id,
        bot_email=settings.webex_bot_email,
        show_tool_events=settings.webex_show_tool_events,
        seen_cache=_WEBEX_SEEN_CACHE,
    )
    try:
        await bot.handle_webhook(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Webex webhook processing failed")
    finally:
        await webex.close()
