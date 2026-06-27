"""Host-side Claude Code runner service for the Nymeria bridge.

Nymeria runs in a read-only Docker container and cannot reach the host repo or
the host's Claude Code auth. This service closes that gap: it runs on the host
(where the repo, ``claude`` binary, and real auth live) and executes Claude Code
runs on behalf of the ``claude_code`` tool over HTTP.

This service is the **policy boundary**. Even though the tool already maps the
permission mode, the runner independently:

- requires a bearer token (``NYMERIA_CLAUDE_CODE_TOKEN``);
- re-resolves the working-directory allowlist (``NYMERIA_CLAUDE_CODE_ROOTS``);
- re-maps the permission mode and applies the hard deny rules; and
- builds the run config (model, budgets, ``--bare``, auth env) from its own host
  environment.

So a compromised container cannot make the runner step outside the
working-directory allowlist. (The hard deny rules are command-prefix guardrails
against accidental destructive commands, not a sandbox against an adversarial
prompt; the cwd allowlist plus an unprivileged runner user on an isolated
checkout are the real boundary.)

Security: bind only to a private interface (loopback or the Docker bridge), never
a public one (UFW on the reference host opens only 22/8000/8001). Run as a normal
user, never root. Start it with ``python3 run.py claude-code-runner``.

Endpoints:
- ``POST /run``   -> ``{job_id, status}`` (kicks off the run; poll for the result)
- ``GET  /job/{id}`` -> ``{status, result?}``
- ``GET  /health`` -> ``{status, claude}`` (no auth)
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel

from ..config import get_settings
from ..tools.claude_code_bridge import (
    ClaudeCodeError,
    ClaudeCodeRequest,
    ClaudeCodeResult,
    ClaudeCodeRunConfig,
    DEFAULT_DISALLOWED_TOOLS,
    build_subprocess_env,
    map_mode,
    parse_roots,
    resolve_claude_executable,
    resolve_cwd_against_roots,
    run_local_blocking,
)

logger = logging.getLogger(__name__)

# Absolute ceiling for one run on the runner before it is abandoned.
RUNNER_HARD_TIMEOUT = 3600.0
MAX_COMPLETED_JOBS = 256


class RunRequest(BaseModel):
    """Body of ``POST /run``. The runner re-validates everything host-side."""

    prompt: str
    working_dir: Optional[str] = None
    mode: Optional[str] = None
    resume_session_id: Optional[str] = None
    fork_session: bool = False


@dataclass
class _RunnerJob:
    id: str
    status: str = "running"  # "running" | "completed"
    result: Optional[ClaudeCodeResult] = None
    created_at: float = field(default_factory=time.time)


class _JobRegistry:
    """In-memory job table with bounded retention of completed jobs."""

    def __init__(self, max_completed: int = MAX_COMPLETED_JOBS) -> None:
        self._jobs: "OrderedDict[str, _RunnerJob]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_completed = max_completed

    def create(self) -> _RunnerJob:
        job = _RunnerJob(id=secrets.token_hex(8))
        with self._lock:
            self._jobs[job.id] = job
            self._jobs.move_to_end(job.id)
        return job

    def get(self, job_id: str) -> Optional[_RunnerJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def complete(self, job_id: str, result: ClaudeCodeResult) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.result = result
            job.status = "completed"
            self._jobs.move_to_end(job_id)
            completed = [j for j, r in self._jobs.items() if r.status == "completed"]
            overflow = len(completed) - self._max_completed
            for stale in completed[: max(0, overflow)]:
                self._jobs.pop(stale, None)


def _resolve_runner_config(settings) -> ClaudeCodeRunConfig:
    """Build the run config from the runner's own host environment."""
    executable = resolve_claude_executable()
    if not executable:
        raise ClaudeCodeError(
            "Claude Code executable not found on the runner host "
            "(install with 'npm install -g @anthropic-ai/claude-code')."
        )
    raw_disallowed = settings.nymeria_claude_code_disallowed_tools
    if raw_disallowed and str(raw_disallowed).strip():
        import os

        parts = [p.strip() for p in str(raw_disallowed).replace(os.pathsep, ",").split(",")]
        disallowed = tuple(p for p in parts if p)
    else:
        disallowed = DEFAULT_DISALLOWED_TOOLS
    return ClaudeCodeRunConfig(
        executable=executable,
        model=settings.nymeria_claude_code_model,
        fallback_model=settings.nymeria_claude_code_fallback_model,
        max_turns=settings.nymeria_claude_code_max_turns,
        max_budget_usd=settings.nymeria_claude_code_max_budget_usd,
        disallowed_tools=disallowed,
        bare=bool(settings.nymeria_claude_code_bare),
    )


def _execute(job: _RunnerJob, request: ClaudeCodeRequest, config: ClaudeCodeRunConfig,
             registry: _JobRegistry) -> None:
    """Run Claude Code to completion in a worker thread and record the result."""
    try:
        result = run_local_blocking(
            request,
            config,
            timeout=RUNNER_HARD_TIMEOUT,
            env=build_subprocess_env(config.bare),
        )
    except Exception as exc:  # noqa: BLE001 - never leave a job stuck "running".
        logger.exception("Runner job %s failed", job.id)
        result = ClaudeCodeResult(ok=False, is_error=True, error=f"runner error: {exc}"[:2000])
    registry.complete(job.id, result)


def create_app(*, allow_insecure: bool = False):
    """Build the FastAPI runner app.

    ``allow_insecure=True`` permits running without a bearer token (loopback dev
    only); otherwise a missing ``NYMERIA_CLAUDE_CODE_TOKEN`` is a startup error.
    """
    from fastapi import Depends, FastAPI, Header, HTTPException

    settings = get_settings()
    token = (settings.nymeria_claude_code_token or "").strip() or None
    if token is None and not allow_insecure:
        raise RuntimeError(
            "NYMERIA_CLAUDE_CODE_TOKEN is required to start the Claude Code runner. "
            "Set it (and the same value for the tool side), or pass --insecure for "
            "loopback-only development."
        )

    registry = _JobRegistry()
    app = FastAPI(title="Nymeria Claude Code Runner", docs_url=None, redoc_url=None)

    def _auth(authorization: Optional[str] = Header(default=None)) -> None:
        if token is None:
            return  # insecure dev mode
        expected = f"Bearer {token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "claude": bool(resolve_claude_executable())}

    @app.post("/run")
    def run(body: RunRequest, _: None = Depends(_auth)) -> dict:
        live = get_settings()
        try:
            cli_mode = map_mode(body.mode if body.mode else live.nymeria_claude_code_default_mode)
            cwd = resolve_cwd_against_roots(
                body.working_dir,
                parse_roots(live.nymeria_claude_code_roots, live.project_root),
                live.project_root,
            )
            config = _resolve_runner_config(live)
        except ClaudeCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        request = ClaudeCodeRequest(
            prompt=body.prompt,
            cwd=str(cwd),
            permission_mode=cli_mode,
            resume_session_id=body.resume_session_id,
            fork_session=body.fork_session,
        )
        job = registry.create()
        thread = threading.Thread(
            target=_execute,
            args=(job, request, config, registry),
            name=f"ClaudeCodeRunner-{job.id}",
            daemon=True,
        )
        thread.start()
        logger.info("Runner started job %s (mode=%s, cwd=%s)", job.id, cli_mode, cwd)
        return {"job_id": job.id, "status": "running"}

    @app.get("/job/{job_id}")
    def job_status(job_id: str, _: None = Depends(_auth)) -> dict:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status == "completed" and job.result is not None:
            return {"status": "completed", "result": job.result.to_payload()}
        return {"status": job.status}

    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8200,
    *,
    allow_insecure: bool = False,
) -> None:
    """Run the runner with uvicorn. Bind to a PRIVATE interface only."""
    import uvicorn

    app = create_app(allow_insecure=allow_insecure)
    logger.info("Starting Claude Code runner on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
