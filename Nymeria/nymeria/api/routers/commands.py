"""Slash command execution and discovery routes."""

from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ...core.accounts import AuthenticatedUser
from ...core.command_service import (
    CommandContext,
    CommandHttpClient,
    get_command_service,
)
from ..schemas.commands import (
    CommandExecuteRequest,
    CommandExecuteResponse,
    CommandInfoResponse,
    CommandSource,
)


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header format. Use: Bearer <api_key>",
        )
    return parts[1]


def create_commands_router(verify_api_key: Callable[..., Any]) -> APIRouter:
    """Create command routes with app auth dependencies injected."""
    router = APIRouter(tags=["Commands"])

    @router.post("/commands/execute", response_model=CommandExecuteResponse)
    async def execute_command(
        request: CommandExecuteRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        authorization: Optional[str] = Header(None),
    ) -> CommandExecuteResponse:
        token = _extract_bearer_token(authorization)
        api = CommandHttpClient.from_bearer(token, use_act_as=user.via_act_as)
        try:
            result = await get_command_service().execute(
                CommandContext(
                    user_id=user.id,
                    thread_id=request.thread_id,
                    source=request.source,
                ),
                request.command,
                api=api,
            )
        finally:
            await api.close()
        return CommandExecuteResponse(
            success=result.success,
            markdown=result.markdown,
            command=result.command,
        )

    @router.get("/commands", response_model=list[CommandInfoResponse])
    async def list_commands(
        source: CommandSource = Query(default="user"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> list[CommandInfoResponse]:
        del user
        return [
            CommandInfoResponse(
                name=cmd.name,
                description=cmd.description,
                usage=cmd.usage,
                category=cmd.category,
                subcommands=cmd.subcommands,
            )
            for cmd in get_command_service().list_commands(source)
        ]

    return router
