"""Slash command execution and discovery routes."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query

from ...core.accounts import AuthenticatedUser
from ...core.command_service import (
    CommandBackendClient,
    CommandContext,
    get_command_service,
)
from ..schemas.commands import (
    CommandActor,
    CommandExecuteRequest,
    CommandExecuteResponse,
    CommandInfoResponse,
    CommandSource,
    CommandSurface,
)


def create_commands_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[[], Any] | None = None,
    get_settings_fn: Callable[[], Any] | None = None,
) -> APIRouter:
    """Create command routes with app auth dependencies injected."""
    router = APIRouter(tags=["Commands"])

    @router.post("/commands/execute", response_model=CommandExecuteResponse)
    async def execute_command(
        request: CommandExecuteRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> CommandExecuteResponse:
        ctx = CommandContext(
            user_id=user.id,
            thread_id=request.thread_id,
            source=request.source,
            actor=request.actor,
            surface=request.surface,
            is_admin=user.role == "admin",
            via_act_as=user.via_act_as,
        )
        backend = CommandBackendClient.from_context(
            ctx,
            agent=get_agent_fn() if get_agent_fn is not None else None,
            user=user,
            settings_fn=get_settings_fn if get_settings_fn is not None else None,
        )
        result = await get_command_service().execute(
            ctx,
            request.command,
            api=backend,
        )
        return CommandExecuteResponse(
            success=result.success,
            markdown=result.markdown,
            command=result.command,
            level=result.level,
            data=result.data,
        )

    @router.get("/commands", response_model=list[CommandInfoResponse])
    async def list_commands(
        source: CommandSource | None = Query(default=None),
        actor: CommandActor | None = Query(default=None),
        surface: CommandSurface | None = Query(default=None),
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> list[CommandInfoResponse]:
        return [
            CommandInfoResponse(
                name=cmd.name,
                description=cmd.description,
                usage=cmd.usage,
                category=cmd.category,
                subcommands=cmd.subcommands,
                id=cmd.id,
                path=cmd.path,
                aliases=cmd.aliases,
                scope=cmd.scope,
                surfaces=cmd.surfaces,
                blocked_surfaces=cmd.blocked_surfaces,
                blocked_reason=cmd.blocked_reason,
                agent_allowed=cmd.agent_allowed,
                requires_thread=cmd.requires_thread,
                requires_admin=cmd.requires_admin,
                mutates_state=cmd.mutates_state,
                danger_level=cmd.danger_level,
                execution_kind=cmd.execution_kind,
                note=cmd.note,
                examples=cmd.examples,
            )
            for cmd in get_command_service().list_commands(
                source,
                actor=actor,
                surface=surface,
                is_admin=user.role == "admin",
                user_id=user.id,
                agent=get_agent_fn() if get_agent_fn is not None else None,
            )
        ]

    return router
