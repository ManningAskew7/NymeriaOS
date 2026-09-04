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
bounded budget (``< settings.tool_timeout``). If the run's first report is
ready in time it comes back inline; otherwise the tool returns a "working in
the background" message and the watcher delivers an autonomous completion turn
per report (every end-turn of the run, then the final result; see
``claude_code_background``). ``detach=True`` skips the wait entirely so Nymeria
can keep talking to the user.

Sessions are addressable. ``resume`` takes a session id (or a bridge job id)
as well as True/False; a prompt for a session that is still running is QUEUED
on that job and started as a resumed run when it ends, with a receipt returned
at once (the callable-thread follow-up shape). ``peek`` returns the live
transcript tail of a running or recent session without resuming it. Every
prompt goes out with a bridge-context block naming the originating thread
(``claude_code_bridge.frame_prompt``) so Claude Code can message it mid-task.

The per-call ``mode`` selects Claude Code's ``--permission-mode``: ``plan`` (write
a plan and stop for Nymeria to confirm), ``dont_ask`` (safe default), ``bypass``
(oneshot), etc. Hard deny rules (rm, git push, ...) are enforced in every mode.
"""

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from ..core.claude_code_overrides import (
    get_effective_claude_code_mode,
    get_effective_claude_code_model,
)
from .claude_code_background import (
    ClaudeCodeJob,
    find_job,
    format_peek,
    format_report_for_agent,
    live_job_for_session,
    start_job,
)
from .claude_code_bridge import (
    ClaudeCodeError,
    ClaudeCodeResult,
    ClaudeCodeRunConfig,
    ClaudeCodeRequest,
    DEFAULT_DISALLOWED_TOOLS,
    EndTurn,
    RemoteRunnerClient,
    RemoteRunnerError,
    RunObserver,
    SessionStore,
    build_subprocess_env,
    frame_prompt,
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
# What a report or peek says about a runner service still on pre-end-turn
# code (``RunObserver.legacy_runner``).
LEGACY_RUNNER_NOTE = (
    "the runner service predates per-end-turn reporting and live peek: it "
    "reports only the terminal message when the process exits, so earlier "
    "end-turns of a multi-turn run are lost. It loads code from the checkout; "
    "restart it at a quiet moment (the restart kills its in-flight runs)"
)
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


def _detached_message(job_id: str, mode: str, remote: bool, session_id: Optional[str]) -> str:
    where = "on the host runner" if remote else "in the background"
    session = f", resuming session {session_id}" if session_id else ""
    return (
        f"[Claude Code job {job_id} | running {where}, mode={mode}{session}] "
        "It is taking longer than the inline wait, so continue without it: every "
        "end-turn message it produces is delivered to this thread as a follow-up "
        "(tagged with this job id and its session id), and the final report "
        f"carries the run summary. claude_code(peek=\"{job_id}\") shows what it is "
        f"doing right now; claude_code(prompt, resume=\"{job_id}\") queues a "
        "follow-up for the same session. You can keep talking to the user in "
        "the meantime."
    )


def _queued_message(job: ClaudeCodeJob, position: int) -> str:
    session = job.session_id or "pending"
    return (
        f"[Queued] Claude Code session {session} is still running (job {job.id}, "
        f"{max(0.0, time.time() - job.started_at):.0f}s so far), and a running "
        "session cannot take a new prompt mid-run. Your prompt is queued "
        f"(position {position}) and will start as a resumed run of that session "
        "when the current run ends; its output arrives on this thread as a "
        "follow-up tagged with the same session id and a new job id. "
        f"claude_code(peek=\"{job.id}\") shows what the session is doing now; "
        "resume=False starts an independent session instead."
    )


def _coerce_resume(resume: Union[bool, str, None]) -> Union[bool, str]:
    """``resume`` as the model sent it: a bool, or a session / job reference."""
    if resume is None:
        return True
    if isinstance(resume, bool):
        return resume
    text = str(resume).strip()
    lowered = text.lower()
    if lowered in ("", "true", "yes", "on", "last", "latest"):
        return True
    if lowered in ("false", "no", "off", "none", "new", "fresh"):
        return False
    return text


@tool
def claude_code(
    prompt: Optional[str] = None,
    working_dir: Optional[str] = None,
    mode: Optional[str] = None,
    resume: Union[bool, str] = True,
    detach: bool = False,
    peek: Optional[str] = None,
    tail: int = 12,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Drive Claude Code (the CLI coding agent) to do real work in a project.

    Use this for substantive coding, refactors, multi-file edits, writing to
    project docs, or any task best handed to a dedicated coding agent with file
    and shell access. Claude Code runs where the repo lives (the host), reads and
    edits files, runs commands, and reports back. Every report is tagged
    "[Claude Code job <id> | session <id> | ...]": a run can end several turns
    (it is re-invoked when background subagents report), and EACH end-turn is
    delivered to this thread as its own message, marked INTERIM while the run
    continues and FINAL (with the run summary: files, commits, cost, duration)
    when the process exits. Claude Code is told which Nymeria thread dispatched
    it and can message that thread mid-task on its own.

    Args:
        prompt: The task or question for Claude Code (required unless peek is
            set). Be specific; include the context Claude Code needs since it
            starts fresh unless resuming.
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
        resume: Which session to continue. True (default): this thread's most
            recent Claude Code session in the same directory. A session id or
            a job id (from any report): that SPECIFIC session, so a follow-up
            reaches the run you mean even when another is in flight. False:
            a fresh session. A prompt for a session that is still running is
            queued and started as a resumed run when it ends; you get a
            [Queued] receipt now and the answer as a later follow-up.
        detach: If True, return immediately with a job id instead of waiting;
            every report then arrives as a follow-up message. Use this when
            you want to keep talking to the user while it works.
        peek: A job id, a session id, or "latest": instead of running anything,
            return that session's live transcript tail (the last few tool
            calls and messages, its end-turns so far, elapsed time) to see
            what a running job is doing without resuming it. Read-only.
        tail: With peek, how many transcript entries to show (default 12).

    Returns:
        Claude Code's message plus a run summary (tagged with job and session
        ids), a "working in the background" notice if the run is long or
        detached (reports then arrive as follow-up messages), a [Queued]
        receipt, or the peek snapshot.
    """
    settings = get_settings()
    logger.info(
        "claude_code called: prompt=%r working_dir=%s mode=%s resume=%s detach=%s peek=%s",
        (prompt or "")[:60],
        working_dir,
        mode,
        resume,
        detach,
        peek,
    )

    remote_url = (settings.nymeria_claude_code_url or "").strip()
    thread_id = get_thread_id_or_none(config)
    user_id = get_user_id(config)

    if peek is not None and str(peek).strip():
        return _peek(str(peek), tail, thread_id=thread_id, user_id=user_id)
    prompt = (prompt or "").strip()
    if not prompt:
        return "[Error]: prompt is required (or pass peek=<job id | session id | latest>)."

    # Per-thread overrides (an explicit per-call `mode` still beats the thread
    # default; the model override falls through to the runner's allowlist in
    # remote mode). Both inherit the global default when unset.
    effective_default_mode = get_effective_claude_code_mode(
        thread_id, settings.nymeria_claude_code_default_mode
    )
    try:
        cli_mode = map_mode(mode if mode is not None else effective_default_mode)
    except ClaudeCodeError as exc:
        return f"[Error]: {exc}"
    effective_model = get_effective_claude_code_model(
        thread_id, settings.nymeria_claude_code_model
    )

    # Session addressing: a job id resolves to its session; a session that is
    # still running takes the prompt as a queued follow-up (a running ``-p``
    # process cannot be given a new prompt, and resuming it concurrently would
    # fork the conversation, which is how tonight's wrong-session runs began).
    resume_target = _coerce_resume(resume)
    if thread_id is not None:
        try:
            live, resume_target = _resolve_live_target(
                settings, resume_target, thread_id=thread_id, user_id=user_id, working_dir=working_dir
            )
        except ClaudeCodeError as exc:
            return f"[Error]: {exc}"
        if live is not None:
            position = live.queue_followup(prompt)
            logger.info(
                "claude_code queued follow-up %d on job %s (session %s)",
                position,
                live.id,
                live.session_id,
            )
            return _queued_message(live, position)

    # Capture the thread's abort event so POST /threads/{id}/stop cascades into
    # the in-flight Claude Code run (local subprocess group-kill, or remote
    # /cancel). None when there is no thread context (direct CLI / tests).
    abort_event = None
    if thread_id is not None:
        try:
            from ..core.agent import get_current_agent

            _agent = get_current_agent()
            if _agent is not None:
                abort_event = _agent._thread_locks.get_abort_event(thread_id)
        except Exception:  # noqa: BLE001 - cancellation is best-effort.
            abort_event = None

    job_id = secrets.token_hex(4)
    try:
        prepared = prepare_run(
            settings,
            thread_id=thread_id,
            prompt=prompt,
            working_dir=working_dir,
            cli_mode=cli_mode,
            resume=resume_target,
            abort_event=abort_event,
            model=effective_model,
            job_id=job_id,
        )
    except ClaudeCodeError as exc:
        return f"[Error]: {exc}"
    producer = prepared.producer
    _persist = prepared.persist
    run_cwd = prepared.run_cwd

    # No real thread context (direct CLI / tests): run synchronously, no detach.
    if thread_id is None:
        result = producer()
        _persist(result)
        return result.format_for_agent()

    job = ClaudeCodeJob(
        id=job_id,
        thread_id=thread_id,
        user_id=user_id,
        prompt=prompt,
        cwd=run_cwd,
        mode=cli_mode,
        started_at=time.time(),
        detached_message=_detached_message(
            job_id, cli_mode, bool(remote_url), prepared.resume_session_id
        ),
        observer=prepared.observer,
        resumed_session_id=prepared.resume_session_id,
        peek=prepared.peek,
    )
    start_job(job, producer, on_complete=_persist)

    decision = job.wait_inline(_block_budget(settings, detach))
    if decision == "inline" and job.first_report is not None:
        return format_report_for_agent(job, job.first_report)
    return job.detached_message


def _resolve_live_target(
    settings,
    resume_target: Union[bool, str],
    *,
    thread_id: str,
    user_id: str,
    working_dir: Optional[str],
) -> tuple[Optional[ClaudeCodeJob], Union[bool, str]]:
    """Map ``resume`` to (the live job to queue on, the resume value to run).

    A job id becomes its session id (or the job itself while it has none yet
    but is still running). Bare True checks whether the thread's stored
    session is the one in flight. Raises ``ClaudeCodeError`` for an unknown
    reference.
    """
    if resume_target is False:
        return None, False
    if resume_target is True:
        stored = stored_session_id(settings, thread_id, working_dir)
        if stored:
            live = live_job_for_session(stored, user_id=user_id)
            if live is not None:
                return live, True
        return None, True
    ref = str(resume_target)
    job = find_job(ref, thread_id=thread_id, user_id=user_id)
    if job is None:
        # Not a job we know: treat it as a session id the host may still have.
        return None, ref
    if job.running:
        return job, ref
    session = job.session_id
    if not session:
        raise ClaudeCodeError(
            f"Claude Code job {job.id} finished without a session id; nothing to resume."
        )
    return None, session


def _peek(ref: str, tail: int, *, thread_id: Optional[str], user_id: str) -> str:
    job = find_job(ref, thread_id=thread_id, user_id=user_id)
    if job is None:
        return (
            f"[Error]: no Claude Code job or session '{ref}' is known to this process "
            "(jobs are addressable by the job id or session id from their reports, "
            "or 'latest' for this thread's most recent run)."
        )
    try:
        count = max(1, min(int(tail), 200))
    except (TypeError, ValueError):
        count = 12
    if job.peek is None:
        snapshot = job.observer.snapshot(count)
    else:
        try:
            snapshot = job.peek(count)
        except RemoteRunnerError as exc:
            # The runner cannot answer (old service, or its record is gone):
            # fall back to what this side observed from the polls. Both ids
            # are named: the runner tracks the run under its OWN job id, and
            # an error quoting only that one reads as a mismatch.
            snapshot = {**job.observer.snapshot(0), "tail": [], "tail_total": 0}
            remote_id = job.observer.remote_job_id or "not assigned"
            return (
                f"{format_peek(job, snapshot, count)}\n"
                f"(No transcript tail: {exc}. Bridge job {job.id} is runner job "
                f"{remote_id}.)"
            )
    return format_peek(job, snapshot, count)


def start_followup_run(previous: ClaudeCodeJob, session_id: str, prompts: list[str]) -> ClaudeCodeJob:
    """Start the queued follow-up(s) for a finished run as a resumed job.

    Called by the watcher once the run on ``session_id`` ends. The new job
    inherits the thread, user, mode, cwd, and deliverer of the run it
    follows, resumes that session, and reports through the same paths (every
    report tagged with the session id and the new job id).
    """
    settings = get_settings()
    if len(prompts) == 1:
        prompt = prompts[0]
    else:
        prompt = "\n\n".join(
            f"[Queued follow-up {i} of {len(prompts)}]\n{text}" for i, text in enumerate(prompts, 1)
        )
    remote_url = (settings.nymeria_claude_code_url or "").strip()
    cancel_event = previous.cancel_event
    if cancel_event is None:
        cancel_event = threading.Event()
    job_id = secrets.token_hex(4)
    prepared = prepare_run(
        settings,
        thread_id=previous.thread_id,
        prompt=prompt,
        working_dir=None if previous.cwd == "<default>" else previous.cwd,
        cli_mode=previous.mode,
        resume=session_id,
        abort_event=cancel_event,
        model=get_effective_claude_code_model(previous.thread_id, settings.nymeria_claude_code_model),
        job_id=job_id,
    )
    job = ClaudeCodeJob(
        id=job_id,
        thread_id=previous.thread_id,
        user_id=previous.user_id,
        prompt=prompt,
        cwd=prepared.run_cwd,
        mode=previous.mode,
        started_at=time.time(),
        detached_message=_detached_message(job_id, previous.mode, bool(remote_url), session_id),
        observer=prepared.observer,
        resumed_session_id=session_id,
        deliver=previous.deliver,
        cancel_event=cancel_event,
        peek=prepared.peek,
    )
    logger.info(
        "claude_code follow-up job %s resumes session %s after job %s (%d prompt(s))",
        job.id,
        session_id,
        previous.id,
        len(prompts),
    )
    start_job(job, prepared.producer, on_complete=prepared.persist)
    # Nobody waits inline on a queued follow-up: every report is delivered.
    job.wait_inline(0)
    return job


def session_project_key(settings, working_dir: Optional[str]) -> str:
    """The project half of the session-store key for ``working_dir``.

    Remote mode keys by the requested dir string (the runner owns the real
    path; "<default>" when omitted); local mode keys by the cwd resolved
    against the allowlist, which raises ``ClaudeCodeError`` outside it.
    """
    if (settings.nymeria_claude_code_url or "").strip():
        return (working_dir or "<default>").strip() or "<default>"
    resolved = resolve_cwd_against_roots(
        working_dir,
        parse_roots(settings.nymeria_claude_code_roots, settings.project_root),
        settings.project_root,
    )
    return str(resolved)


def stored_session_id(settings, thread_id: str, working_dir: Optional[str]) -> Optional[str]:
    """The Claude Code session a resume on ``thread_id`` would continue."""
    return _session_store().get(thread_id, session_project_key(settings, working_dir))


@dataclass
class PreparedRun:
    """A Claude Code run built but not started: the producer, the session
    persist hook, the cwd label the job reports, the observer the producer
    feeds, and the transport's live peek. Shared by the agent tool and the
    user's ``/code`` command so both drive one transport and one session
    map."""

    producer: Callable[[], ClaudeCodeResult]
    persist: Callable[[ClaudeCodeResult], None]
    run_cwd: str
    resume_session_id: Optional[str]
    observer: RunObserver = field(default_factory=RunObserver)
    peek: Optional[Callable[[int], dict]] = None
    # The prompt as sent to Claude Code (bridge context block + task).
    framed_prompt: str = ""


def prepare_run(
    settings,
    *,
    thread_id: Optional[str],
    prompt: str,
    working_dir: Optional[str],
    cli_mode: str,
    resume: Union[bool, str],
    abort_event=None,
    model: Optional[str] = None,
    job_id: str = "",
    dispatched_by: str = "the Nymeria agent",
) -> PreparedRun:
    """Resolve transport, session, and cwd for one run; raise ``ClaudeCodeError``.

    Session key: a stable per-(thread, project) handle. Local mode keys by
    the resolved cwd; remote mode keys by the requested dir string (the
    runner owns the real path), defaulting to "<default>" when omitted.
    ``resume`` is True (the stored session for that key), False (fresh), or
    an explicit session id. The session id is persisted as soon as the run
    announces it (``RunObserver.on_session``), not only at the end, so the
    thread's "last session" is the one in flight; ``persist`` at completion
    covers a transport that only reports it then. The prompt is framed with
    the originating thread's bridge context (``frame_prompt``).
    """
    remote_url = (settings.nymeria_claude_code_url or "").strip()
    store = _session_store()
    project_key = session_project_key(settings, working_dir)
    session_thread = thread_id or "default"
    if isinstance(resume, str):
        resume_session_id: Optional[str] = resume.strip() or None
    else:
        resume_session_id = store.get(session_thread, project_key) if resume else None
    job_id = job_id or secrets.token_hex(4)
    framed = frame_prompt(
        prompt,
        thread_id=thread_id,
        job_id=job_id,
        session_id=resume_session_id,
        dispatched_by=dispatched_by,
    )
    observer = RunObserver()

    def _persist(result: ClaudeCodeResult) -> None:
        if result.session_id:
            store.set(session_thread, project_key, result.session_id)

    observer.on_session = lambda sid: store.set(session_thread, project_key, sid)

    # Build the producer (runs Claude Code to completion in the watcher thread).
    if remote_url:
        client = RemoteRunnerClient(remote_url, settings.nymeria_claude_code_token)
        producer = _make_remote_producer(
            settings,
            remote_url,
            prompt=framed,
            working_dir=working_dir,
            cli_mode=cli_mode,
            resume_session_id=resume_session_id,
            abort_event=abort_event,
            model=model,
            observer=observer,
            client=client,
        )
        run_cwd = working_dir or "<default>"

        def _remote_peek(tail: int) -> dict:
            remote_id = observer.remote_job_id
            if not remote_id:
                raise RemoteRunnerError("the run has not been accepted by the runner yet")
            if observer.legacy_runner:
                raise RemoteRunnerError(LEGACY_RUNNER_NOTE)
            return client.peek(remote_id, tail)

        peek: Optional[Callable[[int], dict]] = _remote_peek
    else:
        run_config = _build_local_config(settings)
        run_config.model = model  # per-thread override (None = default)
        request = ClaudeCodeRequest(
            prompt=framed,
            cwd=project_key,
            permission_mode=cli_mode,
            resume_session_id=resume_session_id,
        )
        run_env = build_subprocess_env(run_config.bare)
        # Pass cancel_check only when a thread exists, so the no-thread sync path
        # (direct CLI / tests) keeps the original run_local_blocking call shape.
        if abort_event is not None:
            _cancel_check = abort_event.is_set
            producer = lambda: run_local_blocking(  # noqa: E731 - small closure.
                request,
                run_config,
                timeout=LOCAL_HARD_TIMEOUT,
                env=run_env,
                cancel_check=_cancel_check,
                observer=observer,
            )
        else:
            producer = lambda: run_local_blocking(  # noqa: E731 - small closure.
                request, run_config, timeout=LOCAL_HARD_TIMEOUT, env=run_env, observer=observer
            )
        run_cwd = project_key
        peek = observer.snapshot

    return PreparedRun(
        producer=producer,
        persist=_persist,
        run_cwd=run_cwd,
        resume_session_id=resume_session_id,
        observer=observer,
        peek=peek,
        framed_prompt=framed,
    )


def _make_remote_producer(
    settings,
    remote_url: str,
    *,
    prompt: str,
    working_dir: Optional[str],
    cli_mode: str,
    resume_session_id: Optional[str],
    abort_event=None,
    model: Optional[str] = None,
    observer: Optional[RunObserver] = None,
    client: Optional[RemoteRunnerClient] = None,
):
    """Return a producer that drives the host runner to completion via polling.

    Each poll feeds ``observer`` with the session id and any end-turns the
    runner has seen so far (a runner predating those fields reports neither
    until completion, and the result then carries what it has).
    """
    if client is None:
        client = RemoteRunnerClient(remote_url, settings.nymeria_claude_code_token)
    if observer is None:
        observer = RunObserver()
    payload = {
        "prompt": prompt,
        "working_dir": working_dir,
        "mode": cli_mode,
        "resume_session_id": resume_session_id,
        "fork_session": False,
        "model": model,
    }

    def _cancel_and_report(job_id: str) -> ClaudeCodeResult:
        try:
            client.cancel(job_id)
        except RemoteRunnerError:
            pass  # best-effort; the runner's own hard timeout still bounds it.
        return ClaudeCodeResult(
            ok=False,
            is_error=True,
            error="Claude Code run cancelled (thread aborted)",
            subtype="cancelled",
        )

    def _absorb(status: dict[str, Any]) -> None:
        if "end_turns" not in status:
            observer.legacy_runner = True
        observer.set_session_id(status.get("session_id"))
        turns = status.get("end_turns") or []
        for payload_turn in turns[observer.turn_count:]:
            if isinstance(payload_turn, dict):
                observer.record_end_turn(EndTurn.from_payload(payload_turn))

    def _finish(status: dict[str, Any]) -> ClaudeCodeResult:
        result = ClaudeCodeResult.from_payload(status["result"])
        _absorb(status)
        # A result that carries turns the polls never showed (old runner, or
        # the exit landed between polls) is folded in so nothing is skipped.
        for turn in result.end_turns[observer.turn_count:]:
            observer.record_end_turn(turn)
        observer.set_session_id(result.session_id)
        if not result.end_turns and result.result_text.strip():
            # A runner with no end-turn reporting: the terminal text is the
            # only end-turn this side will ever see (earlier ones are lost).
            observer.legacy_runner = True
            observer.record_end_turn(EndTurn(index=1, text=result.result_text, subtype=result.subtype, is_error=result.is_error))
        result.end_turns = observer.turns_after(0)
        return result

    def producer() -> ClaudeCodeResult:
        try:
            started = client.run(payload, timeout=60.0)
        except RemoteRunnerError as exc:
            return ClaudeCodeResult(ok=False, is_error=True, error=str(exc))
        job_id = started.get("job_id")
        if job_id:
            observer.remote_job_id = str(job_id)
        if started.get("status") == "completed" and started.get("result"):
            return _finish(started)
        if not job_id:
            return ClaudeCodeResult(
                ok=False, is_error=True, error="runner did not return a job id"
            )
        if abort_event is not None and abort_event.is_set():
            return _cancel_and_report(job_id)
        deadline = time.time() + REMOTE_HARD_TIMEOUT
        consecutive_errors = 0
        last_error = ""
        while time.time() < deadline:
            if abort_event is not None and abort_event.is_set():
                return _cancel_and_report(job_id)
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
                return _finish(status)
            _absorb(status)
        return ClaudeCodeResult(
            ok=False,
            is_error=True,
            error=f"Claude Code run did not finish within {REMOTE_HARD_TIMEOUT:.0f}s",
            subtype="timeout",
        )

    return producer
