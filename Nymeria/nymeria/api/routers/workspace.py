"""Workspace artifact download routes."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
import mimetypes
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ...core.accounts import AuthenticatedUser


def create_workspace_router(verify_api_key: Callable[..., Any]) -> APIRouter:
    """Create the workspace router with the app's auth dependency injected."""
    router = APIRouter(tags=["Workspace"])

    @router.get("/workspace/download")
    async def download_workspace_file(
        path: str = Query(..., description="Absolute file path within the workspace"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Download a file from the workspace directory.

        Admins may read any workspace file (artifacts span autonomous runs across
        users). Non-admins are scoped to their own generated-image directory so a
        chat user can fetch the images their own turns produced, but cannot read
        another user's files.
        """
        from ...tools.image_generation import generated_image_dir

        workspace_dir = Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()
        resolved = Path(path).resolve()

        if not resolved.is_relative_to(workspace_dir):
            raise HTTPException(status_code=403, detail="Path outside workspace")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        if user.role != "admin":
            allowed_root = generated_image_dir(user.id).resolve()
            if not resolved.is_relative_to(allowed_root):
                raise HTTPException(status_code=403, detail="Not permitted")

        media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        return FileResponse(
            path=str(resolved),
            media_type=media_type,
            filename=resolved.name,
        )

    return router
