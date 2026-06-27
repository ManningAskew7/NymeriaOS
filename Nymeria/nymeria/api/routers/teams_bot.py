"""Microsoft Teams Bot Framework webhook routes."""

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
from ...triggers.teams_bot import (
    BotAPIError,
    NymeriaTeamsBot,
    TeamsBotFrameworkClient,
    validate_bot_framework_authorization,
)
from ._bot_inprocess import InProcessBotAPI

logger = logging.getLogger(__name__)

_TEAMS_SEEN_CACHE = SeenEventCache()


def create_teams_bot_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Settings],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create Microsoft Teams Bot Framework webhook routes."""
    router = APIRouter(tags=["Teams"])

    @router.post("/integrations/teams/webhook")
    async def receive_teams_webhook(
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

        if not settings.teams_bot_app_id or not settings.teams_bot_app_password:
            raise HTTPException(
                status_code=503,
                detail="TEAMS_BOT_APP_ID and TEAMS_BOT_APP_PASSWORD are required",
            )
        try:
            await validate_bot_framework_authorization(
                request.headers.get("authorization"),
                app_id=settings.teams_bot_app_id or "",
                service_url=str(payload.get("serviceUrl") or ""),
                openid_config_url=settings.teams_bot_openid_config_url,
            )
        except BotAPIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        background_tasks.add_task(
            _process_teams_webhook,
            payload,
            settings,
            get_agent_fn,
            require_thread_access_fn,
            publish_sync_event_fn,
        )
        return {"status": "accepted"}

    return router


async def _process_teams_webhook(
    payload: dict[str, Any],
    settings: Settings,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    publish_sync_event_fn: Callable[..., Any],
) -> None:
    client = TeamsBotFrameworkClient(
        app_id=settings.teams_bot_app_id or "",
        app_password=settings.teams_bot_app_password or "",
        token_url=settings.teams_bot_token_url,
    )
    bot = NymeriaTeamsBot(
        api=InProcessBotAPI(
            agent=get_agent_fn(),
            require_thread_access_fn=require_thread_access_fn,
            publish_sync_event_fn=publish_sync_event_fn,
            origin_client_id="teams",
            display_name="Teams",
            error_cls=BotAPIError,
        ),
        teams_client=client,
        respond_mode=settings.teams_bot_respond_mode,
        show_tool_events=settings.teams_bot_show_tool_events,
        seen_cache=_TEAMS_SEEN_CACHE,
    )
    try:
        await bot.handle_payload(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Teams webhook processing failed")
    finally:
        await bot.close()
