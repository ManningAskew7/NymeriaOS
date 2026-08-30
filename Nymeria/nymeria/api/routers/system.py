"""System and health-check routes."""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from ...core.accounts import AuthenticatedUser
from ...core.interactive_admission import get_interactive_turn_gate
from ..schemas.system import (
    BusyThread,
    DependencyReadiness,
    HealthResponse,
    ReadinessResponse,
    ReportRequest,
    TurnActivityResponse,
)

logger = logging.getLogger(__name__)

_READINESS_CACHE_SECONDS = 1.0

# The readiness probe's blocking dependency checks (a real Postgres connect,
# a real Redis ping) run on this dedicated single thread rather than the
# asyncio default executor: /ready must never report the process unready
# because unrelated blocking work saturated the shared pool, since an
# orchestrator restarting the single agent runtime turns transient load into
# an outage. One thread suffices: probes are cached and serialized behind
# ``readiness_lock``.
_readiness_executor: Optional[ThreadPoolExecutor] = None
_readiness_executor_lock = threading.Lock()


def _get_readiness_executor() -> ThreadPoolExecutor:
    global _readiness_executor
    with _readiness_executor_lock:
        if _readiness_executor is None:
            _readiness_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="readiness",
            )
        return _readiness_executor

# /health/stream emission shape. The inter-event interval must stay well
# above the SSE_MIN_SPREAD_SECONDS threshold in setup/external_access.py, or
# the setup wizard's streaming probe would flag every transparent relay as
# buffering (a test pins the relationship).
HEALTH_STREAM_EVENT_COUNT = 3
HEALTH_STREAM_INTERVAL_SECONDS = 0.6


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


def _restart_child_env(settings: Any) -> dict[str, str]:
    """The environment the restarted image starts from: ours, dotenv merged over.

    Re-merging the dotenv files is what makes a restart APPLY a config-file
    edit: the running process holds the values it booted with, and they outrank
    the dotenv source, so without this pass a restart would faithfully
    reproduce the stale config. This is the mechanism the settings applier
    points at when it declines to apply a hand-edit itself
    (`api/routers/settings.py::_sync_updated_env_vars`).

    Every file read is individually guarded, and the whole pass is
    belt-and-braces: the restarted image runs `run.py::_load_environment`,
    which re-merges the same three files in the same order with
    `override=True`. So a file this cannot read must never abort the restart.
    `run.py` swallows `UnicodeDecodeError` at its own call site for exactly the
    condition that motivates this (a non-UTF-8 byte in a password, a Notepad
    UTF-16 BOM), and aborting here would be worse than skipping: it would
    strand a process that has already begun shutting down.
    """
    from dotenv import dotenv_values

    from ...config.settings import get_env_file_paths

    child_env = os.environ.copy()
    try:
        env_paths = get_env_file_paths(settings.project_root)
    except Exception:  # noqa: BLE001 - a bad project_root must not block a restart
        logger.warning("Could not resolve env files for restart", exc_info=True)
        return child_env
    for env_path in env_paths:
        try:
            if not env_path.exists():
                continue
            values = dotenv_values(env_path)
        except (OSError, UnicodeDecodeError, ValueError):
            logger.warning(
                "Skipping unreadable env file during restart: %s", env_path,
                exc_info=True,
            )
            continue
        for key, value in values.items():
            if key and value is not None:
                child_env[key] = value
    return child_env


def _spawn_and_exit(exec_argv: list[str], child_env: dict[str, str]) -> None:
    """Restart by spawning a detached copy and exiting. Windows + last resort.

    This is what the whole function used to do, and it is broken under any
    supervisor (see `restart_api_process`). It survives here for Windows, where
    `os.execv` goes through the CRT and creates a process with a NEW pid
    anyway, so the in-place guarantee does not hold and the desktop shell's job
    object depends on the current shape.

    It is also the fallback if `execve` raises. That branch is belt-and-braces
    rather than a real second chance: both end in the same syscall with the
    same command and environment, so almost anything that fails the exec
    (ENOENT, EACCES, E2BIG) fails the spawn too. It costs nothing, since the
    helper has to exist for Windows regardless.

    `exec_argv` comes from `service_install.resolve_exec_argv`, NOT from
    `[sys.executable] + sys.argv`. That matters most here, because Windows has
    only this path and Windows is where the frozen build lives
    (`nymeria-backend.spec` builds a single .exe): under PyInstaller
    `sys.executable == sys.argv[0]`, so the naive form spawns
    `[exe, exe, "api"]`, run.py's subparser reads the exe path as the
    subcommand, and the replacement dies on SystemExit(2) without ever
    serving. The resolver's frozen branch exists precisely for that.
    """
    import subprocess

    # env-gate: full-copy - re-exec of the API process itself during a
    # self-restart. The child IS this service and must come up with identical
    # configuration. A scrubbed environment here is a broken backend, not a
    # hardened one.
    subprocess.Popen(
        exec_argv,
        env=child_env,
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if sys.platform == "win32" else 0
        ),
        start_new_session=(sys.platform != "win32"),
    )
    os._exit(0)


def restart_api_process(agent: Any, settings: Any) -> None:
    """Schedule an API server restart on the event loop.

    Used by both the REST endpoint and the central command service.

    Restarts IN PLACE via ``os.execve``: same pid, same cgroup, same PID
    namespace, same parent. That is load-bearing rather than tidy, because
    every way this service is supervised keys on the process EXITING, and the
    previous shape (spawn a detached copy, then ``os._exit(0)``) always exited
    (#300):

    - Under systemd the default ``KillMode=control-group`` SIGTERMs the
      replacement, which ``start_new_session=True`` does not escape (a session
      is not a cgroup), and ``Restart=on-failure`` then declines to restart a
      clean exit 0. Measured result: the unit ends ``inactive (dead)`` and the
      backend is gone until someone with shell access starts it, which on a
      slim install is the deployment the user reaches THROUGH that backend.
    - In a container the exiting process is the PID-namespace init (or tini's
      only child under ``init: true``, which amounts to the same thing), so the
      kernel tears the namespace down and takes the replacement with it. The
      service returns only because the compose files set a restart policy that
      re-runs the command; with ``restart: no`` it stays dead.

    Replacing the image sidesteps all of it without detecting anything, and
    fixes units already installed in the field without regenerating them. The
    listening socket does not block the rebind: Python marks descriptors
    non-inheritable (PEP 446), so it closes on exec.

    Two consequences worth knowing before changing this:

    - The command is rebuilt by `service_install.resolve_exec_argv`, NOT by
      `[sys.executable] + sys.argv`, which is wrong for a `python -m` launch
      (argv[0] is the module file, which cannot be re-run as a script) and for
      a frozen build. Getting that wrong would not look like a crash: the exec
      would succeed and the new image would die at startup, silently falling
      back to being rescued by the supervisor policy this is meant to
      sidestep.
    - Child processes now OUTLIVE the restart (MCP stdio servers, background
      bash jobs, Claude Code bridge runs). They never used to, but nothing in
      this code killed them either: the OS did, via the container's namespace
      teardown or systemd's cgroup kill, i.e. via the very bug this fixes.
      They are now reparented to nothing (same pid) and unowned by the new
      image, so they run on and are never reaped. Backlog #303 owns the
      decision about what SHOULD happen to them.
    """

    async def _do_restart():
        await asyncio.sleep(0.5)

        # Everything that can fail happens BEFORE the ticker stops. Stopping it
        # writes a clean-shutdown record and takes down scheduled TODOs,
        # trigger polling and the sweeps, so a preparation error that aborted
        # after that point would leave a live API whose scheduler is dead and
        # whose shutdown record lies, while the caller was told it was
        # restarting.
        from ...service_install import resolve_exec_argv

        child_env = _restart_child_env(settings)
        exec_argv = resolve_exec_argv(sys.argv[1:])

        ticker = getattr(agent, "_ticker", None)
        if ticker:
            ticker.stop()

        if sys.platform == "win32":
            _spawn_and_exit(exec_argv, child_env)
            return

        # Outside the try: a flush failure (a restarted journald closing the
        # pipe, say) is not an exec failure and must not be reported as one.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 - best-effort before the image goes
                pass

        # Nothing after this line runs: the image is replaced in place.
        try:
            # env-gate: full-copy - the replacement IS this service and must
            # come up with identical configuration. Not a child: this is the
            # same pid continuing as a new image, so a scrubbed environment
            # here is a broken backend, not a hardened one.
            os.execve(exec_argv[0], exec_argv, child_env)
        except Exception:
            logger.exception(
                "In-place restart failed; falling back to spawn-and-exit, which "
                "a supervisor may not bring back (#300)"
            )
            _spawn_and_exit(exec_argv, child_env)

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

    @router.get("/health/stream")
    async def health_stream():
        """Unauthenticated SSE probe: a few spaced events, then the stream ends.

        Exists so tunnels and reverse proxies can be verified end to end for
        streaming, not just request relay: a relay that buffers SSE delivers
        these events in one burst at close, which the setup wizard's URL check
        detects (some relays, like Cloudflare quick tunnels, pass /health but
        cannot carry the chat stream). Carries no state and costs three tiny
        events, so it is safe without auth, like /health itself.
        """
        from fastapi.responses import StreamingResponse

        async def _events():
            for seq in range(HEALTH_STREAM_EVENT_COUNT):
                if seq:
                    await asyncio.sleep(HEALTH_STREAM_INTERVAL_SECONDS)
                payload = {"seq": seq, "ts": datetime.now(timezone.utc).isoformat()}
                yield f"data: {json.dumps(payload)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

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

            readiness = await asyncio.get_running_loop().run_in_executor(
                _get_readiness_executor(), _build_readiness, settings
            )
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

    @router.get("/status/turns", response_model=TurnActivityResponse)
    async def status_turns(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Live turn activity; the deploy-sync idle gate reads this.

        ``active_turns`` counts currently held thread locks, which every
        turn shape (interactive, autonomous, dream) takes in this process;
        ``interactive_active`` covers the admission-to-lock gap;
        ``background_jobs`` covers detached bash jobs, which hold no lock
        by design. All-zero means a restart severs no tracked work (the
        Claude Code bridge's detached runs have no registry and are the
        one documented exception). Any authenticated caller gets the
        counts; ``busy_threads`` detail (thread ids, holder labels) is
        cross-user metadata and is included for admins only.
        """
        # Same defensive shape as thread_overview: a partially initialized
        # agent answers 503 (the sync script then fails safe and defers)
        # rather than a raw 500.
        locks = getattr(get_agent_fn(), "_thread_locks", None)
        if locks is None:
            raise HTTPException(
                status_code=503, detail="Agent runtime is not ready."
            )
        from ...tools.bash_background import get_registry as get_bash_registry

        busy = locks.active_locks()
        background = sum(
            1
            for record in get_bash_registry().records()
            if getattr(record, "status", "") == "running"
        )
        detail = busy if getattr(user, "role", "") == "admin" else []
        return TurnActivityResponse(
            active_turns=len(busy),
            interactive_active=get_interactive_turn_gate().active,
            background_jobs=background,
            busy_threads=[BusyThread(**entry) for entry in detail],
        )

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
