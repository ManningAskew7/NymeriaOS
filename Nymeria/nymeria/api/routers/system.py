"""System and health-check routes."""

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from ...core.accounts import AuthenticatedUser
from ..schemas.system import (
    DependencyReadiness,
    HealthResponse,
    ReadinessResponse,
    ReportRequest,
)

logger = logging.getLogger(__name__)

_READINESS_CACHE_SECONDS = 1.0


def _dependency_ok(detail: str | None = None) -> DependencyReadiness:
    return DependencyReadiness(status="ok", detail=detail)


def _dependency_skipped(detail: str | None = None) -> DependencyReadiness:
    return DependencyReadiness(status="skipped", detail=detail)


def _dependency_error(detail: str) -> DependencyReadiness:
    return DependencyReadiness(status="error", detail=detail)


def _check_database_ready(settings: Any) -> DependencyReadiness:
    backend = getattr(settings, "database_backend", "sqlite")
    if backend == "memory":
        return _dependency_skipped("memory backend")

    if backend == "sqlite":
        db_path = getattr(settings, "db_path", None)
        if db_path is None:
            return _dependency_error("sqlite db_path is unavailable")
        parent = getattr(db_path, "parent", None)
        if parent is not None and not parent.exists():
            return _dependency_error("sqlite data directory is missing")
        return _dependency_ok("sqlite")

    if backend == "postgres":
        postgres_uri = getattr(settings, "postgres_uri", None)
        if not postgres_uri:
            return _dependency_error("POSTGRES_URI is unset")
        try:
            import psycopg  # type: ignore[import-untyped]

            with psycopg.connect(postgres_uri, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - readiness should report all failures.
            return _dependency_error(type(exc).__name__)
        return _dependency_ok("postgres")

    return _dependency_error(f"unknown database backend: {backend}")


def _check_redis_ready(settings: Any) -> DependencyReadiness:
    if not getattr(settings, "redis_enabled", False):
        return _dependency_skipped("disabled")

    redis_url = getattr(settings, "redis_url", None)
    if not redis_url:
        return _dependency_error("REDIS_URL is unset")

    try:
        import redis

        client = redis.from_url(
            redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - readiness should report all failures.
        return _dependency_error(type(exc).__name__)
    return _dependency_ok()


def _build_readiness(settings: Any) -> ReadinessResponse:
    checks = {
        "database": _check_database_ready(settings),
        "redis": _check_redis_ready(settings),
    }
    status = (
        "error"
        if any(check.status == "error" for check in checks.values())
        else "ok"
    )
    return ReadinessResponse(status=status, checks=checks)


def restart_api_process(agent: Any, settings: Any) -> None:
    """Schedule an API server restart on the event loop.

    Used by both the REST endpoint and the central command service.
    """
    import subprocess

    async def _do_restart():
        await asyncio.sleep(0.5)

        ticker = getattr(agent, "_ticker", None)
        if ticker:
            ticker.stop()

        from dotenv import dotenv_values
        from ...config.settings import get_env_file_paths

        child_env = os.environ.copy()
        project_root = settings.project_root
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
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if sys.platform == "win32" else 0
            ),
            start_new_session=(sys.platform != "win32"),
        )
        os._exit(0)

    asyncio.create_task(_do_restart())


def create_system_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the system router with app dependencies injected."""
    router = APIRouter(tags=["System"])
    readiness_lock = asyncio.Lock()
    readiness_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}

    @router.get("/health", response_model=HealthResponse)
    async def health_check():
        """Health check endpoint."""
        return HealthResponse()

    @router.get("/ready", response_model=ReadinessResponse)
    async def ready_check(
        response: Response,
        settings: Any = Depends(get_settings_fn),
    ):
        """Readiness endpoint with lightweight dependency checks."""
        now = time.monotonic()
        cached = readiness_cache["value"]
        if cached is not None and now < readiness_cache["expires_at"]:
            if cached.status == "error":
                response.status_code = 503
            return cached

        async with readiness_lock:
            now = time.monotonic()
            cached = readiness_cache["value"]
            if cached is not None and now < readiness_cache["expires_at"]:
                if cached.status == "error":
                    response.status_code = 503
                return cached

            readiness = await asyncio.to_thread(_build_readiness, settings)
            readiness_cache["value"] = readiness
            readiness_cache["expires_at"] = time.monotonic() + _READINESS_CACHE_SECONDS

        if readiness.status == "error":
            response.status_code = 503
        return readiness

    def _scheduler_status_without_ticker(settings: Any) -> dict[str, Any]:
        from ...core.scheduler_state import SchedulerStateManager
        from ...core.todo_schedule_db import TodoScheduleDB

        schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
        state = SchedulerStateManager(settings.data_dir).load()
        pending_ids = [
            str(todo_id)
            for todo_id in state.get("pending_missed_todo_ids") or []
            if todo_id
        ]
        pending_entries = []
        for todo_id in pending_ids:
            entry = schedule_db.get_entry(todo_id)
            if entry is None:
                continue
            pending_entries.append(
                {
                    "todo_id": entry.todo_id,
                    "user_id": entry.user_id,
                    "thread_id": entry.thread_id,
                    "scheduled_for": datetime.fromtimestamp(
                        entry.scheduled_for,
                        timezone.utc,
                    ).isoformat(),
                    "task_preview": entry.task_preview,
                }
            )

        return {
            "status": "ok",
            "ticker_running": False,
            "missed_work_policy": getattr(
                settings,
                "scheduler_missed_work_policy",
                "run",
            ),
            "pending_missed_todo_count": len(pending_ids),
            "pending_missed_todo_ids": pending_ids,
            "pending_missed_todos": pending_entries,
            "trigger_catchup_paused": bool(state.get("trigger_catchup_paused")),
            "last_started_at": state.get("last_started_at"),
            "last_clean_shutdown_at": state.get("last_clean_shutdown_at"),
            "last_missed_detection_at": state.get("last_missed_detection_at"),
            "active_execution_count": schedule_db.count_active_executions(),
            "active_execution_stale_minutes": int(
                getattr(settings, "scheduler_active_execution_stale_minutes", 1440)
            ),
        }

    @router.get("/scheduler/status")
    async def scheduler_status(
        _user: AuthenticatedUser = Depends(require_admin_user),
        settings: Any = Depends(get_settings_fn),
    ):
        """Return scheduler lifecycle state for local/on-off runtimes."""
        ticker = getattr(get_agent_fn(), "_ticker", None)
        if ticker is not None and hasattr(ticker, "get_scheduler_status"):
            return ticker.get_scheduler_status()
        return _scheduler_status_without_ticker(settings)

    @router.post("/scheduler/missed-work/run")
    async def run_scheduler_missed_work(
        _user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Release startup-missed work that was held by ask-mode recovery."""
        ticker = getattr(get_agent_fn(), "_ticker", None)
        if ticker is None or not hasattr(ticker, "release_missed_work"):
            raise HTTPException(
                status_code=409,
                detail="Scheduler ticker is not available in this API process.",
            )
        return ticker.release_missed_work()

    @router.post("/restart")
    async def restart_server(
        _user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Restart the API server process. Admin-only; affects every user."""
        restart_api_process(get_agent_fn(), get_settings_fn())
        return {"message": "Server restarting..."}

    @router.post("/report")
    async def report_problem(
        request: ReportRequest,
        _user: AuthenticatedUser = Depends(verify_api_key),
        settings: Any = Depends(get_settings_fn),
    ):
        """Send an error report email to the configured destination with debug context."""
        from ...tools.outlook_email import outlook_send_email

        recipient = settings.nymeria_error_report_email
        if not recipient:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Error reporting is not configured. Set "
                    "NYMERIA_ERROR_REPORT_EMAIL to a destination address to "
                    "enable the report endpoint."
                ),
            )

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
                    "to": recipient,
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
