"""System and health-check routes."""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from html import escape as html_escape
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ..schemas.system import HealthResponse, ReportRequest

logger = logging.getLogger(__name__)


def create_system_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the system router with app dependencies injected."""
    router = APIRouter(tags=["System"])

    @router.get("/health", response_model=HealthResponse)
    async def health_check():
        """Health check endpoint."""
        return HealthResponse()

    @router.post("/restart")
    async def restart_server(
        _user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Restart the API server process. Admin-only; affects every user."""
        import subprocess

        async def _do_restart():
            await asyncio.sleep(0.5)

            agent = get_agent_fn()
            ticker = getattr(agent, "_ticker", None)
            if ticker:
                ticker.stop()

            from dotenv import dotenv_values
            from ...config.settings import get_env_file_paths

            child_env = os.environ.copy()
            project_root = get_settings_fn().project_root
            for env_path in get_env_file_paths(project_root):
                if not env_path.exists():
                    continue
                for key, value in dotenv_values(env_path).items():
                    if key and value is not None:
                        child_env[key] = value

            subprocess.Popen(
                [sys.executable] + sys.argv,
                env=child_env,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32" else 0
                ),
                start_new_session=(sys.platform != "win32"),
            )
            os._exit(0)

        asyncio.create_task(_do_restart())
        return {"message": "Server restarting..."}

    @router.post("/report")
    async def report_problem(
        request: ReportRequest,
        _user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Send an error report email to support with debug context."""
        from ...tools.outlook_email import outlook_send_email

        sections = ["<h2>Nymeria Error Report</h2>"]
        sections.append(
            f"<p><strong>Timestamp:</strong> {html_escape(request.timestamp)}</p>"
        )

        if request.thread_id:
            sections.append(
                f"<p><strong>Thread ID:</strong> {html_escape(request.thread_id)}</p>"
            )
        if request.message_id:
            sections.append(
                f"<p><strong>Message ID:</strong> {html_escape(request.message_id)}</p>"
            )

        if request.description:
            sections.append(
                f"<h3>Description</h3><p>{html_escape(request.description)}</p>"
            )

        if request.client_info:
            items = "".join(
                f"<li><strong>{html_escape(str(k))}:</strong> {html_escape(str(v))}</li>"
                for k, v in request.client_info.items()
            )
            sections.append(f"<h3>Client Info</h3><ul>{items}</ul>")

        if request.messages:
            rows = ""
            for message in request.messages[-10:]:
                role = html_escape(message.get("role", "?"))
                content = html_escape((message.get("content", "") or "")[:500])
                ts = html_escape(message.get("timestamp", ""))
                rows += (
                    '<tr><td style="white-space:nowrap">'
                    f"{ts}</td><td><strong>{role}</strong></td>"
                    '<td><pre style="margin:0;white-space:pre-wrap;max-width:400px">'
                    f"{content}</pre></td></tr>"
                )
            sections.append(
                "<h3>Recent Messages</h3>"
                '<table border="1" cellpadding="4" cellspacing="0" '
                'style="border-collapse:collapse;font-size:13px">'
                "<tr><th>Time</th><th>Role</th><th>Content</th></tr>"
                f"{rows}</table>"
            )

        body = "\n".join(sections)
        date_str = request.timestamp[:10] if request.timestamp else "unknown"
        subject = f"Nymeria Error Report - {date_str}"

        try:
            result = outlook_send_email.invoke(
                {
                    "to": "reports@example.com",
                    "subject": subject,
                    "body": body,
                    "is_html": True,
                }
            )
            if "[Error]" in str(result):
                raise HTTPException(status_code=502, detail=str(result))
            return {"status": "sent", "detail": str(result)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to send error report: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
