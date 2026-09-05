"""The ``/code`` command: the user prompts Claude Code directly, no model in
the loop.

Break-glass repair surface. When the Nymeria agent itself is broken (every
turn errors, the provider is down, a self-modification went wrong), the
owner can still type ``/code <prompt>`` from Telegram, the desktop, the CLI
or the MCP command surface, and Claude Code runs on the host against the
checkout to fix it. Dispatch rides the slash-command pipeline
(``POST /commands/execute``), which every client routes BEFORE the chat
path, so it works while chat turns are failing. The run reuses the
``claude_code`` tool's transport and session map
(``tools.claude_code.prepare_run``), so the host runner stays the policy
boundary and a ``/code`` follow-up continues the same Claude Code session
the tool would. Output delivery, the job registry, and the ``/stop`` seam
live in ``core/claude_code_delivery.py``.

The permission mode defaults to ``bypass`` (Claude Code's
``bypassPermissions``): an explicit owner decision, because the point is
unattended repair and the operator's host-side deny list
(``NYMERIA_CLAUDE_CODE_DISALLOWED_TOOLS``) still applies in every mode. A
per-thread ``claude_code_mode`` override still wins, as it does for the
tool. Admin-only, thread-bound, never offered to the agent (it has the
tool).

House style: backend handler families live in their own domain mixin
module (see ``command_executor_browser.py``). Runtime leaf: the tool and
agent modules are imported function-locally at execution time.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from typing import Any

from .command_forms import CommandOutput, command_error, command_info, command_success
from .command_params import BoundArgs

logger = logging.getLogger(__name__)

# Owner decision (see the module docstring): unattended repair by default.
DEFAULT_MODE = "bypass"
# How long the command reply waits for a quick run before acknowledging and
# detaching. Must stay well under every command client's HTTP read timeout
# (the bots' API client reads for 300s; the desktop fetch has none).
INLINE_WAIT_SECONDS = 20.0


def _ack_markdown(job_id: str, mode: str, resumed_session: str | None) -> str:
    session = f"resuming session {resumed_session}" if resumed_session else "fresh session"
    return (
        f"Claude Code dispatched (job {job_id}, mode {mode}, {session}). "
        "It is still running; the result lands in this chat when it finishes. "
        "`/code <reply>` continues the same session, `/code --new <prompt>` "
        "starts over, `/stop` cancels the run."
    )


def _live_tool_jobs(thread_id: str) -> list[Any]:
    """Running Claude Code jobs on the thread in the shared live registry
    (the tool's runs and any queued follow-up)."""
    from ..tools.claude_code_background import jobs_for_thread

    return [job for job in jobs_for_thread(thread_id) if job.running]


class ClaudeCodeCommandsMixin:
    """The ``/code`` command body mixed into ``_CommandExecutor``.

    The host provides ``user_id``, ``thread_id`` and ``is_admin``; the
    annotations let the static checker see them on the mixin in isolation.
    """

    user_id: str
    thread_id: str
    is_admin: bool | None

    async def _cmd_code(self, bound: BoundArgs) -> str | CommandOutput:
        # The dispatch gate refuses a KNOWN non-admin; this command runs
        # arbitrary code on the host, so an UNKNOWN verdict is refused too
        # (fail closed), unlike the cosmetic handler-level checks elsewhere
        # in the catalog.
        if self.is_admin is not True:
            return command_error("`/code` requires an admin user.")
        prompt = str(bound.get("prompt") or "").strip()
        if not prompt:
            return await asyncio.to_thread(self._code_overview, bound)
        return await asyncio.to_thread(self._code_dispatch, bound, prompt)

    def _code_overview(self, bound: BoundArgs) -> CommandOutput:
        from ..config import get_settings
        from ..tools.claude_code import stored_session_id
        from ..tools.claude_code_bridge import ClaudeCodeError
        from .claude_code_delivery import active_job, last_outcome

        thread_id = self.thread_id
        working_dir = str(bound.get("dir") or "").strip() or None
        parts: list[str] = []
        data: dict[str, Any] = {"thread_id": thread_id}
        running = active_job(thread_id)
        if running is not None:
            elapsed = max(0.0, time.time() - running.started_at)
            state = "delivering its result" if running.done.is_set() else "running"
            parts.append(
                f"Claude Code job {running.id} is {state} (mode {running.mode}, "
                f"{elapsed:.0f}s so far); its result lands here when it finishes."
            )
            data["active_job"] = {
                "id": running.id,
                "mode": running.mode,
                "elapsed": round(elapsed),
                "done": running.done.is_set(),
            }
        else:
            live = _live_tool_jobs(thread_id)
            if live:
                tool_job = live[-1]
                elapsed = max(0.0, time.time() - tool_job.started_at)
                parts.append(
                    f"Claude Code job {tool_job.id} (started by the agent's tool) is "
                    f"running (mode {tool_job.mode}, {elapsed:.0f}s so far); `/code` "
                    "is refused until it ends, `/stop` cancels it."
                )
                data["active_job"] = {
                    "id": tool_job.id,
                    "mode": tool_job.mode,
                    "elapsed": round(elapsed),
                    "done": tool_job.done.is_set(),
                    "via": "tool",
                }
            else:
                parts.append("No Claude Code run is in flight on this thread.")
        last = last_outcome(thread_id)
        if last is not None:
            parts.append(f"Last job {last[0]} {last[1]}.")
            data["last_job"] = {"id": last[0], "outcome": last[1]}
        try:
            session = stored_session_id(get_settings(), thread_id, working_dir)
        except ClaudeCodeError as exc:
            return command_error(str(exc))
        data["session_id"] = session
        if session:
            parts.append(
                f"`/code <prompt>` resumes session {session}; `/code --new <prompt>` "
                "starts fresh."
            )
        else:
            parts.append("No stored session: the next `/code <prompt>` starts fresh.")
        parts.append(f"Default mode: {DEFAULT_MODE} (override with `--mode`).")
        return command_info(" ".join(parts), data=data)

    def _code_dispatch(self, bound: BoundArgs, prompt: str) -> CommandOutput:
        from ..config import get_settings
        from ..tools.claude_code import prepare_run
        from ..tools.claude_code_background import ClaudeCodeJob, start_job
        from ..tools.claude_code_bridge import ClaudeCodeError, map_mode
        from .agent import get_current_agent
        from .claude_code_delivery import (
            active_job,
            claim,
            deliver_code_result,
            finish,
            outcome_of,
            record_inline,
            render_result_markdown,
        )
        from .claude_code_overrides import (
            get_effective_claude_code_mode,
            get_effective_claude_code_model,
        )

        thread_id = self.thread_id
        settings = get_settings()
        # Precedence mirrors the tool: an explicit --mode, then the thread's
        # claude_code_mode override, then this command's bypass default (the
        # global NYMERIA_CLAUDE_CODE_DEFAULT_MODE is the tool's default, not
        # this command's).
        explicit_mode = str(bound.get("mode") or "").strip() or None
        mode_token = explicit_mode or get_effective_claude_code_mode(thread_id, DEFAULT_MODE)
        try:
            cli_mode = map_mode(mode_token)
        except ClaudeCodeError as exc:
            return command_error(str(exc))
        fresh = bool(bound.get("new"))
        working_dir = str(bound.get("dir") or "").strip() or None

        running = active_job(thread_id)
        if running is not None:
            return self._still_running(running)
        # The agent's tool runs live in the shared registry, not this
        # command's: a /code that resumed the session one of them is on
        # would fork it, so any live run on the thread refuses the dispatch.
        live = _live_tool_jobs(thread_id)
        if live:
            return self._still_running(live[-1])

        cancel_event = threading.Event()
        model = get_effective_claude_code_model(thread_id, settings.nymeria_claude_code_model)
        job_id = secrets.token_hex(4)
        try:
            prepared = prepare_run(
                settings,
                thread_id=thread_id,
                prompt=prompt,
                working_dir=working_dir,
                cli_mode=cli_mode,
                resume=not fresh,
                abort_event=cancel_event,
                model=model,
                job_id=job_id,
                dispatched_by="the user (/code command, no model in the loop)",
            )
        except ClaudeCodeError as exc:
            return command_error(f"Claude Code could not start: {exc}")

        job = ClaudeCodeJob(
            id=job_id,
            thread_id=thread_id,
            user_id=self.user_id,
            prompt=prompt,
            cwd=prepared.run_cwd,
            mode=cli_mode,
            started_at=time.time(),
            detached_message=_ack_markdown(job_id, cli_mode, prepared.resume_session_id),
            observer=prepared.observer,
            resumed_session_id=prepared.resume_session_id,
            deliver=deliver_code_result,
            cancel_event=cancel_event,
            peek=prepared.peek,
        )
        # Atomic check-and-set: two concurrent /code dispatches (desktop and
        # Telegram, or a double send) must not both start against one
        # Claude Code session.
        existing = claim(job)
        if existing is not None:
            return self._still_running(existing)
        logger.info(
            "/code dispatched job %s on thread %s (mode=%s, resume=%s, cwd=%s)",
            job.id,
            thread_id,
            cli_mode,
            prepared.resume_session_id,
            prepared.run_cwd,
        )
        try:
            start_job(job, prepared.producer, on_complete=prepared.persist)
        except BaseException:
            finish(job, "failed")  # a job that never started holds no slot
            raise

        data = {
            "job_id": job.id,
            "mode": cli_mode,
            "resumed_session": prepared.resume_session_id,
            "cwd": prepared.run_cwd,
        }
        if job.wait_inline(INLINE_WAIT_SECONDS) != "inline":
            return command_info(job.detached_message, data={**data, "detached": True})

        # Quick report: the reply IS the delivery. The exchange is still
        # recorded in thread history so the thread carries it and a later
        # model turn can read what happened. The first report may be an
        # INTERIM end-turn of a run that is still going (the watcher then
        # delivers the rest as they land); only a final one settles the
        # registry.
        report = job.first_report
        assert report is not None  # wait_inline claims inline only once a report exists
        text = render_result_markdown(job, report)
        agent = get_current_agent()
        if agent is not None:
            record_inline(agent, job, text, report)
        data["session_id"] = job.session_id
        if not report.final:
            return command_info(text, data={**data, "interim": True})
        assert job.result is not None
        outcome = outcome_of(job)
        finish(job, outcome)
        if outcome != "completed":
            return command_error(text, data=data)
        return command_success(text, data=data)

    @staticmethod
    def _still_running(job: Any) -> CommandOutput:
        elapsed = max(0.0, time.time() - job.started_at)
        state = "is delivering its result" if job.done.is_set() else "is still running"
        session = f" (session {job.session_id})" if job.session_id else ""
        return command_error(
            f"Claude Code job {job.id}{session} {state} on this thread ({elapsed:.0f}s). "
            "Wait for it to land, or `/stop` to cancel it."
        )
