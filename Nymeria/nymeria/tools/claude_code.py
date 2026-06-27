"""The ``claude_code`` tool: a Nymeria <-> Claude Code bridge.

Nymeria calls this tool to drive Claude Code (the CLI coding agent) on the
machine where the repo and real auth live, then relays Claude Code's final
message and a run summary back to the user.

Two transports, chosen by environment:

- **Remote** (``NYMERIA_CLAUDE_CODE_URL`` set): the tool POSTs the run to a host
  runner service and polls it to completion. This is the production path, where
  Nymeria runs in the read-only Docker container and Claude Code runs on the
  host. The runner is the policy boundary (it re-validates the working-directory
  allowlist and resolves the run config from its own host environment).
- **Local** (URL unset): the tool runs Claude Code in-process as a subprocess.
  This is the slim / desktop path where the agent and ``claude`` are co-located.

Long runs are handled without tripping the runtime's per-tool-call timeout: the
run is driven by a background watcher and the tool only *waits* on it for a
bounded budget (``< settings.tool_timeout``). If it finishes in time the result
comes back inline; otherwise the tool returns a "working in the background"
message and the watcher delivers an autonomous completion turn when it finishes.
``detach=True`` skips the wait entirely so Nymeria can keep talking to the user.

The per-call ``mode`` selects Claude Code's ``--permission-mode``: ``plan`` (write
a plan and stop for Nymeria to confirm), ``dont_ask`` (safe default), ``bypass``
(oneshot), etc. Hard deny rules (rm, git push, ...) are enforced in every mode.
"""

import logging
import os
import secrets
import time
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from .claude_code_background import ClaudeCodeJob, start_job
from .claude_code_bridge import (
    ClaudeCodeError,
    ClaudeCodeResult,
    ClaudeCodeRunConfig,
    ClaudeCodeRequest,
    DEFAULT_DISALLOWED_TOOLS,
    RemoteRunnerClient,
    RemoteRunnerError,
    SessionStore,
    build_subprocess_env,
    map_mode,
    parse_roots,
    resolve_claude_executable,
    resolve_cwd_against_roots,
    run_local_blocking,
)
from .utils import get_thread_id_or_none, get_user_id

logger = logging.getLogger(__name__)

# Absolute ceiling for one Claude Code run before the watcher gives up, well
# above any block budget. The block budget (derived from settings.tool_timeout)
# only controls how long the *tool call* waits before detaching.
LOCAL_HARD_TIMEOUT = 3600.0
REMOTE_HARD_TIMEOUT = 3600.0
REMOTE_POLL_INTERVAL = 3.0
# Consecutive poll failures tolerated before giving up (the runner job keeps
# running through transient network blips).
REMOTE_POLL_MAX_ERRORS = 5
# Safety margin kept below settings.tool_timeout so the inline wait always
# resolves (and detaches) before the runtime would kill the tool thread.
BLOCK_MARGIN_SECONDS = 30


def _session_store() -> SessionStore:
    return SessionStore(get_settings().data_dir / "claude_code_sessions.json")


def _block_budget(settings, detach: bool) -> float:
    if detach:
        return 0.0
    configured = settings.nymeria_claude_code_block_seconds
    if configured is not None:
        budget = float(configured)
    else:
        budget = max(10.0, float(settings.tool_timeout) - BLOCK_MARGIN_SECONDS)
    # Never wait past the runtime's kill ceiling.
    return float(min(budget, max(0.0, float(settings.tool_timeout) - BLOCK_MARGIN_SECONDS)))


def _disallowed_tools(settings) -> tuple[str, ...]:
    raw = settings.nymeria_claude_code_disallowed_tools
    if not raw or not str(raw).strip():
        return DEFAULT_DISALLOWED_TOOLS
    parts = [p.strip() for p in str(raw).replace(os.pathsep, ",").split(",")]
    return tuple(p for p in parts if p)


def _build_local_config(settings) -> ClaudeCodeRunConfig:
    executable = resolve_claude_executable()
    if not executable:
        raise ClaudeCodeError(
            "Claude Code executable not found. Install it with "
            "'npm install -g @anthropic-ai/claude-code', or set "
            "NYMERIA_CLAUDE_CODE_URL to use a remote runner."
        )
    return ClaudeCodeRunConfig(
        executable=executable,
        model=settings.nymeria_claude_code_model,
        fallback_model=settings.nymeria_claude_code_fallback_model,
        max_turns=settings.nymeria_claude_code_max_turns,
        max_budget_usd=settings.nymeria_claude_code_max_budget_usd,
        disallowed_tools=_disallowed_tools(settings),
        bare=bool(settings.nymeria_claude_code_bare),
    )


def _detached_message(job_id: str, mode: str, remote: bool) -> str:
    where = "the host runner" if remote else "in the background"
    return (
        f"Claude Code is now running {where} (job {job_id}, mode={mode}). "
        "It is taking longer than the inline wait, so I'll continue and deliver "
        "the result as a follow-up message when it finishes. You can keep talking "
        "to me in the meantime."
    )


@tool
def claude_code(
    prompt: str,
    working_dir: Optional[str] = None,
    mode: Optional[str] = None,
    resume: bool = True,
    detach: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Drive Claude Code (the CLI coding agent) to do real work in a project.

    Use this for substantive coding, refactors, multi-file edits, writing to
    project docs, or any task best handed to a dedicated coding agent with file
    and shell access. Claude Code runs where the repo lives (the host), reads and
    edits files, runs commands, and reports back its final message plus a summary
    of what changed (files, commits, cost, duration, session id).

    Args:
        prompt: The task or question for Claude Code. Be specific; include the
            context Claude Code needs since it starts fresh unless resuming.
        working_dir: Project directory to run in. Must be within the operator's
            allowed roots. Omit to use the default project root.
        mode: Permission mode controlling how much Claude Code can do unattended:
            - "dont_ask" (default): safe; acts within an allowlist, never prompts.
            - "plan": Claude Code writes an implementation plan and STOPS without
              editing. Read the returned plan, then call again to execute.
            - "accept_edits": auto-accepts file edits.
            - "auto": Claude Code's autonomy classifier decides per action.
            - "bypass": full autonomy / oneshot (still subject to hard deny rules
              like rm and git push).
        resume: If True (default), continue this thread's previous Claude Code
            session in the same directory so context carries over. False starts
            a fresh session.
        detach: If True, return immediately with a job id instead of waiting, and
            deliver the result as a follow-up message when Claude Code finishes.
            Use this when you want to keep talking to the user while it works.

    Returns:
        Claude Code's final message plus a run summary, or a "working in the
        background" notice if the run is long or detached (the result then
        arrives as a follow-up message).
    """
    settings = get_settings()
    logger.info(
        "claude_code called: prompt=%r working_dir=%s mode=%s resume=%s detach=%s",
        prompt[:60],
        working_dir,
        mode,
        resume,
        detach,
    )

    try:
        cli_mode = map_mode(mode if mode is not None else settings.nymeria_claude_code_default_mode)
    except ClaudeCodeError as exc:
        return f"[Error]: {exc}"

    remote_url = (settings.nymeria_claude_code_url or "").strip()
    thread_id = get_thread_id_or_none(config)
    user_id = get_user_id(config)

    # Session key: a stable per-(thread, project) handle. Local mode keys by the
    # resolved cwd; remote mode keys by the requested dir string (the runner owns
    # the real path), defaulting to "<default>" when omitted.
    store = _session_store()
    if remote_url:
        project_key = (working_dir or "<default>").strip() or "<default>"
    else:
        try:
            resolved = resolve_cwd_against_roots(
                working_dir,
                parse_roots(settings.nymeria_claude_code_roots, settings.project_root),
                settings.project_root,
            )
        except ClaudeCodeError as exc:
            return f"[Error]: {exc}"
        project_key = str(resolved)

    resume_session_id = store.get(thread_id or "default", project_key) if resume else None

    def _persist(result: ClaudeCodeResult) -> None:
        if result.session_id:
            store.set(thread_id or "default", project_key, result.session_id)

    # Build the producer (runs Claude Code to completion in the watcher thread).
    if remote_url:
        producer = _make_remote_producer(
            settings,
            remote_url,
            prompt=prompt,
            working_dir=working_dir,
            cli_mode=cli_mode,
            resume_session_id=resume_session_id,
        )
        run_cwd = working_dir or "<default>"
    else:
        try:
            run_config = _build_local_config(settings)
        except ClaudeCodeError as exc:
            return f"[Error]: {exc}"
        request = ClaudeCodeRequest(
            prompt=prompt,
            cwd=project_key,
            permission_mode=cli_mode,
            resume_session_id=resume_session_id,
        )
        run_env = build_subprocess_env(run_config.bare)
        producer = lambda: run_local_blocking(  # noqa: E731 - small closure.
            request, run_config, timeout=LOCAL_HARD_TIMEOUT, env=run_env
        )
        run_cwd = project_key

    # No real thread context (direct CLI / tests): run synchronously, no detach.
    if thread_id is None:
        result = producer()
        _persist(result)
        return result.format_for_agent()

    job_id = secrets.token_hex(4)
    job = ClaudeCodeJob(
        id=job_id,
        thread_id=thread_id,
        user_id=user_id,
        prompt=prompt,
        cwd=run_cwd,
        mode=cli_mode,
        started_at=time.time(),
        detached_message=_detached_message(job_id, cli_mode, bool(remote_url)),
    )
    start_job(job, producer, on_complete=_persist)

    decision = job.wait_inline(_block_budget(settings, detach))
    if decision == "inline" and job.result is not None:
        return job.result.format_for_agent()
    return job.detached_message


def _make_remote_producer(
    settings,
    remote_url: str,
    *,
    prompt: str,
    working_dir: Optional[str],
    cli_mode: str,
    resume_session_id: Optional[str],
):
    """Return a producer that drives the host runner to completion via polling."""
    client = RemoteRunnerClient(remote_url, settings.nymeria_claude_code_token)
    payload = {
        "prompt": prompt,
        "working_dir": working_dir,
        "mode": cli_mode,
        "resume_session_id": resume_session_id,
        "fork_session": False,
    }

    def producer() -> ClaudeCodeResult:
        try:
            started = client.run(payload, timeout=60.0)
        except RemoteRunnerError as exc:
            return ClaudeCodeResult(ok=False, is_error=True, error=str(exc))
        if started.get("status") == "completed" and started.get("result"):
            return ClaudeCodeResult.from_payload(started["result"])
        job_id = started.get("job_id")
        if not job_id:
            return ClaudeCodeResult(
                ok=False, is_error=True, error="runner did not return a job id"
            )
        deadline = time.time() + REMOTE_HARD_TIMEOUT
        consecutive_errors = 0
        last_error = ""
        while time.time() < deadline:
            time.sleep(REMOTE_POLL_INTERVAL)
            try:
                status = client.poll(job_id)
            except RemoteRunnerError as exc:
                # Tolerate transient blips: the runner job keeps running, so only
                # give up after several consecutive poll failures.
                consecutive_errors += 1
                last_error = str(exc)
                if consecutive_errors >= REMOTE_POLL_MAX_ERRORS:
                    return ClaudeCodeResult(ok=False, is_error=True, error=last_error)
                continue
            consecutive_errors = 0
            if status.get("status") == "completed" and status.get("result"):
                return ClaudeCodeResult.from_payload(status["result"])
        return ClaudeCodeResult(
            ok=False,
            is_error=True,
            error=f"Claude Code run did not finish within {REMOTE_HARD_TIMEOUT:.0f}s",
            subtype="timeout",
        )

    return producer
