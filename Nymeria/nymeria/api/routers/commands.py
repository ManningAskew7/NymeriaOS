"""Slash command execution and discovery routes."""

from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.accounts import AuthenticatedUser
from ...core.command_service import (
    CommandBackendClient,
    CommandContext,
    get_command_service,
    http_error_detail,
)
from ..schemas.commands import (
    CommandActor,
    CommandExecuteRequest,
    CommandExecuteResponse,
    CommandInfoResponse,
    CommandOptionResponse,
    CommandParamModel,
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
            supports_forms=request.supports_forms,
        )
        backend = CommandBackendClient.from_context(
            ctx,
            agent=get_agent_fn() if get_agent_fn is not None else None,
            user=user,
            settings_fn=get_settings_fn if get_settings_fn is not None else None,
        )
        # The client chose request.thread_id: check it before anything runs,
        # as every other thread route does at the route (#425).
        result = await get_command_service().execute(
            ctx,
            request.command,
            api=backend,
            gate_thread=True,
        )
        return CommandExecuteResponse(
            success=result.success,
            markdown=result.markdown,
            command=result.command,
            level=result.level,
            data=result.data,
        )

    @router.get(
        "/commands/options/{ref}",
        response_model=list[CommandOptionResponse],
    )
    async def list_command_options(
        ref: str,
        thread_id: str | None = Query(default=None),
        q: str | None = Query(
            default=None,
            description="Substring filter over id, label, and meta",
        ),
        limit: int = Query(default=0, ge=0, le=100),
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> list[CommandOptionResponse]:
        """Resolve a ``choices_ref`` value set live, scoped to the caller.

        Serves every consumer that wants the option list WITHOUT running a
        command: Discord autocomplete, palette clients, generated forms on
        the client side. ``q``/``limit`` narrow server-side so per-keystroke
        callers never pull whole catalogs. 404 for a ref no resolver claims.
        """
        from ...core.command_option_resolvers import filter_command_options

        ctx = CommandContext(
            user_id=user.id,
            thread_id=thread_id,
            is_admin=user.role == "admin",
            via_act_as=user.via_act_as,
        )
        backend = CommandBackendClient.from_context(
            ctx,
            agent=get_agent_fn() if get_agent_fn is not None else None,
            user=user,
            settings_fn=get_settings_fn if get_settings_fn is not None else None,
        )
        if thread_id:
            # A client-chosen thread: the options carry its state (which
            # skills are active), so check it like every thread route (#425).
            try:
                backend.require_thread_access(thread_id)
            except httpx.HTTPStatusError as exc:
                raise HTTPException(
                    status_code=exc.response.status_code, detail=http_error_detail(exc)
                ) from exc
        options = await get_command_service().resolve_options(
            ctx, ref, api=backend
        )
        if options is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown option set: {ref}"
            )
        options = filter_command_options(options, q=q or "", limit=limit)
        return [CommandOptionResponse(**option) for option in options]

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
                user_aliases=cmd.user_aliases,
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
                params=(
                    [CommandParamModel(**param) for param in cmd.params]
                    if cmd.params is not None
                    else None
                ),
            )
            for cmd in get_command_service().list_commands(
                source,
                actor=actor,
                surface=surface,
                is_admin=user.role == "admin",
                user_id=user.id,
                agent=get_agent_fn() if get_agent_fn is not None else None,
                include_user_aliases=True,
            )
        ]

    return router
