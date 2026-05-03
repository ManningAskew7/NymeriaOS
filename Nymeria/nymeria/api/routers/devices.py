"""Device registration routes for FCM push notifications."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.accounts import AuthenticatedUser


def create_devices_router(
    verify_api_key: Callable[..., Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the devices router with app dependencies injected."""
    router = APIRouter(tags=["Devices"])

    @router.post("/devices/register")
    async def register_device(
        request: Request,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Register a device for FCM push notifications.

        The device is bound to the authenticated caller; any client-supplied
        ``user_id`` in the body is ignored.
        """
        from ...core.fcm import register_token

        body = await request.json()
        token = body.get("token", "").strip()
        platform = body.get("platform", "unknown")
        thread_ids = body.get("thread_ids")

        if not token:
            raise HTTPException(status_code=400, detail="Token is required")

        if thread_ids is not None and not isinstance(thread_ids, list):
            raise HTTPException(status_code=400, detail="thread_ids must be a list")

        settings = get_settings_fn()
        data_dir = str(settings.data_dir)

        is_new = register_token(data_dir, token, platform, user.id, thread_ids=thread_ids)
        return {
            "status": "registered" if is_new else "updated",
            "platform": platform,
        }

    @router.delete("/devices/{token}")
    async def unregister_device(
        token: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Unregister a device from FCM push notifications."""
        from ...core.fcm import load_tokens, unregister_token

        settings = get_settings_fn()
        data_dir = str(settings.data_dir)

        if user.role != "admin":
            tokens = load_tokens(data_dir)
            owner_id = next(
                (t.get("user_id") for t in tokens if t.get("token") == token),
                None,
            )
            if owner_id is None:
                raise HTTPException(status_code=404, detail="Token not found")
            if owner_id != user.id:
                raise HTTPException(status_code=404, detail="Token not found")

        removed = unregister_token(data_dir, token)
        if not removed:
            raise HTTPException(status_code=404, detail="Token not found")
        return {"status": "unregistered"}

    return router
