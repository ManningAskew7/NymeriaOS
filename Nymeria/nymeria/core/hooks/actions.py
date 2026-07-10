"""Canned hook actions: first-party logic that produces a typed outcome.

Each action is a pure ``(ctx, params)`` function over the frozen contract (no
store, registry, or agent handle). Actions in this module:

- ``inject_context`` (mutate): render a string and inject it, per event:
  ``PROMPT_SUBMIT`` -> ``PromptOutcome``, ``POST_TOOL_USE`` -> ``PostToolOutcome``
  (additional_context), ``DONE`` -> ``DoneOutcome`` (in-loop re-drive).
- ``block_if_matches`` (mutate, pre_tool_use): deny the tool call when all
  conditions match its args -> ``PreToolOutcome(decision="deny")``.
- ``rewrite_arg`` (mutate, pre_tool_use): rewrite matched args ->
  ``PreToolOutcome(decision="modify", updated_args=...)``.
- ``notify`` / ``create_todo`` / ``webhook`` (observe, post_tool_use / done):
  fire-and-forget side effects (an in-app + push notification, a user TODO, a
  POST to a webhook). They return ``None`` and never affect the turn; each wraps
  its side effect so a failure is logged, not raised.

Template substitution uses ``core/text_format.py`` so static text passes through
unchanged and unknown ``{placeholders}`` are left intact rather than raising.
Condition matching uses ``core/conditions.py`` (shared with the trigger stack).

Fault policy note: a raising PRE hook is treated as a ``deny`` (fail-closed) by
the dispatcher, so the guardrail actions are written never-raise -- a malformed
condition/params makes the hook a no-op (allow), not a block-everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from ..conditions import HookCondition, evaluate_conditions
from ..hook_spec import action_planes
from ..text_format import safe_format
from .base import (
    DoneOutcome,
    HookContext,
    HookEvent,
    HookOutcome,
    PostToolOutcome,
    PreToolOutcome,
    PromptOutcome,
)

logger = logging.getLogger(__name__)


def _coerce_conditions(raw) -> Optional[list]:
    """Build ``HookCondition`` objects from a params list; None on bad shape.

    Returns None (not a partial/garbage list) for anything that is not a list of
    dicts / HookConditions, so the caller no-ops (allow) instead of letting a bad
    element reach ``evaluate_conditions`` and raise -- which, under PRE
    fail-closed, would deny every matching tool call. In practice the CRUD/load
    validators reject non-dict conditions upstream; this is the backstop.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return None
    out = []
    try:
        for c in raw:
            if isinstance(c, HookCondition):
                out.append(c)
            elif isinstance(c, dict):
                out.append(HookCondition(**c))
            else:
                return None  # a non-dict element is a bad shape -> no-op
    except Exception:  # noqa: BLE001 - a malformed condition must not raise here
        return None
    return out


def context_usage_fields(ctx: HookContext) -> Dict[str, object]:
    """Raw numeric context-usage fields for fire-gate data and templating.

    Only fields with a known value are present, so a numeric fire condition on
    an unknown signal is a non-match rather than a compare against zero. The
    percent fields are derived here (one place) so the gate and the template
    map cannot drift.
    """
    out: Dict[str, object] = {}
    tokens = ctx.context_tokens
    if tokens is not None:
        out["context_tokens"] = tokens
    if ctx.context_limit:
        out["context_limit"] = ctx.context_limit
        if tokens is not None:
            out["context_pct_of_limit"] = round(tokens / ctx.context_limit * 100, 1)
    if ctx.compact_trigger_tokens:
        out["compact_trigger_tokens"] = ctx.compact_trigger_tokens
        if tokens is not None:
            out["context_pct_of_trigger"] = round(
                tokens / ctx.compact_trigger_tokens * 100, 1
            )
    return out


# Context-usage template keys, always present in the substitution map (empty
# string when the signal is unknown) so ``{context_tokens}``-style placeholders
# render empty instead of surviving literally in the injected text.
_CONTEXT_VAR_KEYS = (
    "context_tokens",
    "context_limit",
    "compact_trigger_tokens",
    "context_pct_of_trigger",
    "context_pct_of_limit",
)


def _template_vars(ctx: HookContext) -> Dict[str, str]:
    """Build the string substitution map from a HookContext.

    Every value is coerced to a string (``None`` -> ""), so a referenced-but-
    unset field renders empty rather than leaking a live object or the literal
    placeholder.
    """
    try:
        tool_args = json.dumps(ctx.tool_args, default=str) if ctx.tool_args else ""
    except Exception:  # noqa: BLE001 - templating must never raise
        tool_args = ""
    vars_ = {
        "event": ctx.event.value if isinstance(ctx.event, HookEvent) else str(ctx.event),
        "thread_id": ctx.thread_id or "",
        "user_id": ctx.user_id or "",
        "is_autonomous": str(ctx.is_autonomous),
        "holder_kind": ctx.holder_kind or "",
        "trigger_label": ctx.trigger_label or "",
        "prompt": ctx.prompt or "",
        "tool_name": ctx.tool_name or "",
        "tool_result": ctx.tool_result_text or "",
        "tool_status": ctx.tool_status or "",
        "tool_args": tool_args,
        "final_text": ctx.final_text or "",
    }
    vars_.update(dict.fromkeys(_CONTEXT_VAR_KEYS, ""))
    vars_.update({k: str(v) for k, v in context_usage_fields(ctx).items()})
    return vars_


def inject_context(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Render ``params['text']`` and return the injecting outcome for the event."""
    template = (params or {}).get("text")
    if not template:
        return None
    text = safe_format(str(template), _template_vars(ctx)).strip()
    if not text:
        return None  # nothing to inject / no empty DONE reason
    event = ctx.event
    if event is HookEvent.PROMPT_SUBMIT:
        return PromptOutcome(inject_context=text)
    if event is HookEvent.POST_TOOL_USE:
        return PostToolOutcome(additional_context=text)
    if event is HookEvent.DONE:
        # One-shot: inject the text as a single follow-up re-drive, never loop.
        # On a continuation turn this hook itself spawned, done_continuation_active
        # is True (mirrors Claude Code's stop_hook_active) -- return None so we do
        # not re-continue and ride the hard cap up to MAX_DONE_CONTINUATIONS.
        prov = ctx.provenance
        if prov is not None and prov.done_continuation_active:
            return None
        return DoneOutcome(continue_=True, reason=text)
    return None  # PRE_TOOL_USE and anything else: not an injection target


def block_if_matches(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Deny a tool call when all conditions match its args (else allow).

    Empty conditions = always deny (a blanket guardrail on the matched tools).
    Never raises: malformed conditions make the hook a no-op rather than a
    fail-closed block of every call.
    """
    params = params or {}
    conds = _coerce_conditions(params.get("conditions"))
    if conds is None:
        return None
    if not evaluate_conditions(ctx.tool_args or {}, conds):
        return None  # conditions not met -> allow
    reason = safe_format(
        str(params.get("reason") or "blocked by a lifecycle hook"), _template_vars(ctx)
    )
    return PreToolOutcome(decision="deny", reason=reason)


def rewrite_arg(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Rewrite one or more tool-call args when conditions match (else no-op).

    ``updates`` is ``arg-name -> {placeholder}-templated value``; only the named
    args are changed (the dispatcher shallow-merges them over the call's args).
    Empty conditions = always rewrite. Never raises.
    """
    params = params or {}
    conds = _coerce_conditions(params.get("conditions"))
    if conds is None:
        return None
    if not evaluate_conditions(ctx.tool_args or {}, conds):
        return None
    spec = params.get("updates")
    if not isinstance(spec, dict) or not spec:
        return None
    vars_ = _template_vars(ctx)
    updates = {str(k): safe_format(str(v), vars_) for k, v in spec.items()}
    if not updates:
        return None
    return PreToolOutcome(decision="modify", updated_args=updates)


def _approval_denial_tail() -> str:
    """The hardened no-consent tail appended to every approval denial.

    Explicit so the model cannot route around the gate (retry, rephrase, or
    reach the same effect another way); mirrors the strongest wording found
    in the field for human-in-the-loop denials.
    """
    return (
        " Do not retry the same call and do not attempt the action another "
        "way without the user's explicit approval."
    )


async def require_approval(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Hold the tool call until the user approves/denies; timeout = deny.

    Mutate plane, ``pre_tool_use`` only, and deliberately ``async``: on the
    async dispatch path a coroutine hook is awaited on the event loop
    (occupying NO mutate-pool worker), and on the sync bridge it runs under
    ``asyncio.run`` on the calling thread, so a minutes-long hold can never
    starve other guardrails. The dispatcher budget comes from the author's
    ``timeout_seconds`` via the bridge (+0.5s slack), so the action's own
    wait always resolves first with a clean outcome.

    Flow: conditions gate (same idiom as ``block_if_matches``) -> mint the
    durable pending record + rendezvous future (``core/hook_approvals.py``)
    -> announce (SSE event with ``tool_call_id`` for the tool-call-card UI,
    in-app notification, FCM push) -> await the future for the window.
    Approved -> allow (note: "approved by <resolver>"); denied/timeout/abort
    -> deny with an explicit no-consent reason. Fail-closed end to end: if
    the request cannot even be minted, the call is denied, consistent with
    the dispatcher's PRE fault policy. Cleanup runs in ``finally`` (also on
    turn cancellation): the waiter owns the record's lifetime.
    """
    params = params or {}
    conds = _coerce_conditions(params.get("conditions"))
    if conds is None:
        return None  # malformed conditions -> no-op (authoring bug, not a veto)
    if not evaluate_conditions(ctx.tool_args or {}, conds):
        return None  # conditions not met -> allow without asking
    from ..hook_approvals import (
        announce_request,
        clamp_window,
        create_pending_approval,
        delete_record,
        get_hook_approval_coordinator,
        publish_resolved_event,
    )
    window = clamp_window(params.get("timeout_seconds"))
    prompt_template = str(params.get("prompt") or "").strip() or "Approve tool call {tool_name}?"
    prompt = safe_format(prompt_template, _template_vars(ctx)).strip()
    try:
        record, future = create_pending_approval(
            user_id=ctx.user_id or "default",
            thread_id=ctx.thread_id or "",
            hook_id=str(params.get("__definition_id") or ""),
            hook_name=str(params.get("__definition_name") or ""),
            tool_name=ctx.tool_name or "",
            tool_call_id=ctx.tool_call_id or "",
            tool_args=ctx.tool_args,
            prompt=prompt,
            window_seconds=window,
            is_autonomous=bool(ctx.is_autonomous),
            holder_kind=ctx.holder_kind,
            trigger_label=ctx.trigger_label,
        )
    except Exception as e:  # noqa: BLE001 - a gate that cannot ask must not pass
        logger.warning("hook require_approval could not mint a request", exc_info=True)
        return PreToolOutcome(
            decision="deny",
            reason=f"approval request could not be created ({e}); denying (fail closed)",
        )
    record_id = str(record.get("record_id") or "")
    outcome_label, resolved_by, note_text = "aborted", "", ""
    announce_request(record)
    try:
        try:
            result = await asyncio.wait_for(future, timeout=window)
        except asyncio.TimeoutError:
            outcome_label = "timeout"
            return PreToolOutcome(
                decision="deny",
                reason=(
                    f"Denied: the user did not approve this tool call within "
                    f"{int(window)} seconds."
                    + _approval_denial_tail()
                    + " Silence is not consent."
                ),
            )
        status = str((result or {}).get("status") or "") if isinstance(result, dict) else ""
        if status == "aborted":
            outcome_label = "aborted"
            return PreToolOutcome(
                decision="deny", reason="turn cancelled while awaiting approval"
            )
        approved = bool((result or {}).get("approved"))
        resolved_by = str((result or {}).get("resolved_by") or "user")
        note_text = str((result or {}).get("note") or "")
        if approved:
            outcome_label = "approved"
            return PreToolOutcome(decision="allow", note=f"approved by {resolved_by}")
        outcome_label = "denied"
        reason = f"Denied by {resolved_by}"
        if note_text:
            reason += f": {note_text}"
        return PreToolOutcome(decision="deny", reason=reason + "." + _approval_denial_tail())
    finally:
        # The waiter owns cleanup, on every exit shape including cancellation:
        # drop the rendezvous entry (no-op if a resolver already popped it),
        # delete the durable record, and tell every surface the hold ended.
        try:
            get_hook_approval_coordinator().discard(record_id)
            delete_record(record_id)
            publish_resolved_event(
                record, outcome=outcome_label, resolved_by=resolved_by, note=note_text
            )
        except Exception:  # noqa: BLE001 - cleanup must never mask the outcome
            logger.warning("hook approval cleanup failed for %s", record_id, exc_info=True)


# Webhook wall-clock budget. Kept under the dispatcher's per-hook timeout
# (DEFAULT_HOOK_TIMEOUT = 5s) so a slow endpoint fails inside the action (logged)
# rather than being cut by the dispatcher mid-request.
_WEBHOOK_TIMEOUT = 4.5


def notify(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Deliver an in-app + push notification (observe plane). Returns None.

    Bypasses the autonomous-notification suppression gate (which only delivers on
    `all_autonomous` threads): a user-authored hook `notify` should always
    deliver. Wraps the side effect so a failure is logged, never raised.
    """
    text = safe_format(str((params or {}).get("text") or ""), _template_vars(ctx)).strip()
    if not text:
        return None
    try:
        from ...config import get_settings
        from ..notifications import create_notification
        settings = get_settings()
        create_notification(
            user_id=ctx.user_id or "",
            summary=text[:200],
            thread_id=ctx.thread_id or "",
            task_id=None,
        )
        if getattr(settings, "fcm_enabled", False):
            from ..fcm import send_to_all_devices
            send_to_all_devices(
                data_dir=str(settings.data_dir),
                text=text,
                thread_id=ctx.thread_id or "",
                user_id=ctx.user_id or "",
            )
    except Exception:  # noqa: BLE001 - observe never raises into a turn
        logger.warning("hook notify failed", exc_info=True)
    return None


def create_todo(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Create a user TODO from the rendered text (observe plane). Returns None."""
    text = safe_format(str((params or {}).get("text") or ""), _template_vars(ctx)).strip()
    if not text:
        return None
    try:
        from ...tools.todo import _get_todo_manager
        manager = _get_todo_manager()
        with manager.atomic_update(ctx.user_id or "default") as todo_list:
            item = todo_list.add_item(
                task=text, created_by="hook", thread_id=ctx.thread_id or ""
            )
        if item is None:
            logger.warning("hook create_todo: TODO not created (at limit?)")
    except Exception:  # noqa: BLE001 - observe never raises into a turn
        logger.warning("hook create_todo failed", exc_info=True)
    return None


def webhook(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """POST a JSON payload to a user-supplied URL (observe plane). Returns None.

    Uses the SSRF/egress-safe ``http_policy`` helper (never raw httpx): private,
    loopback, and metadata endpoints are refused before any socket connect, and
    DNS is pinned across the check. A blocked URL or a failed POST is logged, not
    raised.
    """
    params = params or {}
    vars_ = _template_vars(ctx)
    url = safe_format(str(params.get("url") or ""), vars_).strip()
    if not url:
        return None
    text = safe_format(str(params.get("text") or ""), vars_)
    # Import outside the try so the exception name is bound for the except clause.
    from ..http_policy import HTTPPolicyViolation, httpx_request_with_policy
    try:
        response, _redirects, _decision = httpx_request_with_policy(
            "POST",
            url,
            json={"text": text, "thread_id": ctx.thread_id or "", "user_id": ctx.user_id or ""},
            headers={"Content-Type": "application/json"},
            timeout=_WEBHOOK_TIMEOUT,
        )
        response.raise_for_status()
    except HTTPPolicyViolation as e:  # noqa: BLE001
        logger.warning("hook webhook blocked by egress policy: %s", e)
    except Exception:  # noqa: BLE001 - observe never raises into a turn
        logger.warning("hook webhook POST failed", exc_info=True)
    return None


# run_command: caps and the minimal env. A shell command hook is the SDK
# subprocess pathfinder, so it is deliberately conservative: stdin-only context
# (never argv), a minimal environment (the API process env carries provider keys
# and DB credentials that must not leak into an author's command), output reads
# truncated, and mutate-plane events (in-band on the turn) clamped harder than
# off-turn observe events.
_RUN_COMMAND_READ_CAP = 50_000      # bytes read from stdout/stderr before truncation
_RUN_COMMAND_INJECT_CAP = 10_000    # chars injected (matches InjectContextLogic.text)
_RUN_COMMAND_MUTATE_TIMEOUT_CAP = 60.0  # in-band events cannot block the turn for long


def _run_command_env(ctx: HookContext) -> Dict[str, str]:
    """Minimal environment for a run_command subprocess (no inherited secrets)."""
    from ...subprocess_env import scrubbed_subprocess_env

    env = scrubbed_subprocess_env()
    env["NYMERIA_HOOK_EVENT"] = ctx.event.value if isinstance(ctx.event, HookEvent) else str(ctx.event)
    env["NYMERIA_HOOK_THREAD_ID"] = ctx.thread_id or ""
    env["NYMERIA_HOOK_USER_ID"] = ctx.user_id or ""
    if ctx.tool_name:
        env["NYMERIA_HOOK_TOOL_NAME"] = ctx.tool_name
    return env


def _run_command_payload(ctx: HookContext) -> str:
    """The JSON hook context handed to the command on stdin."""
    payload: Dict[str, Any] = dict(_template_vars(ctx))
    # Structured (object) forms alongside the string-templated ones.
    payload["tool_args"] = ctx.tool_args or {}
    try:
        payload["scratch"] = dict(ctx.scratch or {})
    except Exception:  # noqa: BLE001 - scratch is a mapping snapshot; be defensive
        payload["scratch"] = {}
    try:
        return json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001 - a payload must always serialize
        return "{}"


class _CommandResult:
    __slots__ = ("returncode", "stdout", "stderr", "timed_out", "spawn_failed")

    def __init__(self, *, returncode=None, stdout="", stderr="", timed_out=False, spawn_failed=False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.spawn_failed = spawn_failed


_RUN_COMMAND_CHUNK = 8192


def _capped_reader(stream, cap: int, parts: list) -> None:
    """Drain ``stream`` to EOF, retaining at most ``cap`` characters.

    Parent memory stays O(cap + chunk) no matter how much the child emits:
    past the cap the loop keeps READING (so the child never blocks on a full
    pipe, the two-pipe deadlock ``communicate()`` existed to avoid) but drops
    the chunks. Never raises; a closed/broken pipe just ends the drain.
    """
    retained = 0
    try:
        while True:
            chunk = stream.read(_RUN_COMMAND_CHUNK)
            if not chunk:
                return
            if retained < cap:
                keep = chunk[: cap - retained]
                parts.append(keep)
                retained += len(keep)
    except Exception:  # noqa: BLE001 - draining must never raise
        return


def _feed_stdin(proc, payload: str) -> None:
    """Write the JSON context to the child's stdin and close it; never raises.

    A child that exits without reading (or never reads) makes the write fail
    with a broken pipe; that is the child's business, not an error here.
    """
    try:
        proc.stdin.write(payload)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass


def _execute_command(ctx: HookContext, command: str, timeout: float) -> _CommandResult:
    """Run ``command`` with the context on stdin; never raises.

    Own process group + SIGKILL of the whole group on timeout (mirrors
    ``claude_code_bridge``), minimal env. Working dir is the data dir (a
    stable, writable location; not the repo root).

    I/O is a bounded incremental pump (backlog #74A), not ``communicate()``:
    one stdin-writer thread and one capped reader-drainer per output pipe, so
    the parent retains at most ``_RUN_COMMAND_READ_CAP`` chars per stream and
    peak memory no longer scales with the child's output volume. Semantics
    match the old ``communicate(timeout=...)`` behavior: the wall clock also
    bounds pipe EOF, so a backgrounded grandchild that keeps the pipes open
    past the deadline is a timeout (group-killed), exactly as before.
    """
    from ...config import get_settings
    from ...oom import oom_score_preexec
    try:
        cwd = str(get_settings().data_dir)
    except Exception:  # noqa: BLE001
        cwd = None
    proc = None
    try:
        proc = subprocess.Popen(  # noqa: S602 - shell command is the feature; admin+flag gated
            command,
            shell=True,
            cwd=cwd,
            env=_run_command_env(ctx),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",  # invalid bytes must not abort the drain (fail-open risk)
            start_new_session=True,  # own process group, so we can kill children
            preexec_fn=oom_score_preexec(),
        )
    except Exception:  # noqa: BLE001 - spawn failure (bad cwd, fork limit, etc.)
        logger.warning("hook run_command failed to execute", exc_info=True)
        _kill_process_group(proc)
        return _CommandResult(spawn_failed=True)
    stdout_parts: list = []
    stderr_parts: list = []
    pumps = [
        threading.Thread(
            target=_feed_stdin, args=(proc, _run_command_payload(ctx)), daemon=True
        ),
        threading.Thread(
            target=_capped_reader,
            args=(proc.stdout, _RUN_COMMAND_READ_CAP, stdout_parts),
            daemon=True,
        ),
        threading.Thread(
            target=_capped_reader,
            args=(proc.stderr, _RUN_COMMAND_READ_CAP, stderr_parts),
            daemon=True,
        ),
    ]
    deadline = time.monotonic() + timeout
    try:
        for t in pumps:
            t.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            return _CommandResult(timed_out=True)
        # The process exited; the pipes must EOF within what is left of the
        # budget. Still-open pipes past the deadline mean a surviving
        # grandchild: the same condition communicate() surfaced as a timeout.
        readers = pumps[1:]
        for t in readers:
            t.join(timeout=max(0.1, deadline - time.monotonic()))
        if any(t.is_alive() for t in readers):
            _kill_process_group(proc)
            return _CommandResult(timed_out=True)
        return _CommandResult(
            returncode=proc.returncode,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
        )
    except Exception:  # noqa: BLE001 - pump wiring failure; fail like a spawn error
        logger.warning("hook run_command I/O pump failed", exc_info=True)
        _kill_process_group(proc)
        return _CommandResult(spawn_failed=True)


def _kill_process_group(proc) -> None:
    """Best-effort SIGKILL of a subprocess's whole group (POSIX); never raises.

    The child is spawned with ``start_new_session=True``, so its pid IS the
    process-group id. Signal that directly: ``os.getpgid(proc.pid)`` would
    raise once the leader has been reaped (the grandchild-holds-pipes branch
    calls this AFTER ``proc.wait()`` returned), silently leaving the group's
    survivors unkilled.
    """
    if proc is None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - the whole group may already be gone
        pass


def run_command(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Execute a shell command; map its result to the event's outcome.

    Gated at execution time on ``HOOKS_RUN_COMMAND_ENABLED`` (belt-and-braces
    with the admin+flag authoring gates, and the backstop for records that
    predate a flag flip or arrive by store-file edit). The command receives the
    hook context as JSON on stdin. Contract per event:

    - ``prompt_submit`` (mutate): exit 0 with stdout -> inject it; anything else
      (empty output, nonzero exit, timeout, spawn failure) -> None. Injection is
      an enhancement, so this fails OPEN.
    - ``pre_tool_use`` (mutate, guardrail): exit 0 + empty stdout -> allow; exit
      0 + JSON ``{"decision","reason","updated_args"}`` -> that outcome; exit 2
      -> deny (reason = stderr tail, Claude Code convention); other nonzero exit
      -> allow with a diagnostic note (script bug, non-blocking, but visible in
      the execution log and as an activity line); timeout / spawn failure ->
      DENY. A guardrail that could not be evaluated must not silently pass, so
      this fails CLOSED (consistent with the dispatcher's PRE fault policy).
    - ``post_tool_use`` / ``done`` (observe): run for side effects, return None.

    Never raises: every failure mode maps to an explicit outcome or None.
    """
    from ...config import get_settings
    if not getattr(get_settings(), "hooks_run_command_enabled", False):
        logger.warning("hook run_command skipped: HOOKS_RUN_COMMAND_ENABLED is off")
        return None
    # Owner-admin re-check at fire time. The authoring gate's admin half is
    # otherwise bypassable by a direct write to data/hooks/<user_id>.json; the
    # owner is ctx.user_id (the turn's auth identity, not a file field), so this
    # is unforgeable for a hook in its owner's own store. Action-local beside
    # the flag re-check: run_command is the only GATED_ACTION, and fire-time
    # resolution means an admin demotion neuters the hook on the next fire with
    # no reload. Neuter = return None (a no-op; on pre_tool_use that ALLOWS,
    # matching the flag-skip -- failing into deny would let a non-admin hook
    # block tool calls). ``or "default"`` matches the authoring gate and the
    # solo-install bootstrap admin.
    from ...tools.utils import is_admin
    owner = ctx.user_id or "default"
    if not is_admin(owner):
        logger.warning("hook run_command skipped: owner %r is not an admin", owner)
        return None
    params = params or {}
    command = str(params.get("command") or "").strip()
    if not command:
        return None
    event = ctx.event
    timeout = float(params.get("timeout_seconds") or 10.0)
    # In-band (mutate) events must not block the turn for long; observe events
    # run off-turn and keep the author's full budget.
    if event in (HookEvent.PROMPT_SUBMIT, HookEvent.PRE_TOOL_USE):
        timeout = min(timeout, _RUN_COMMAND_MUTATE_TIMEOUT_CAP)

    result = _execute_command(ctx, command, timeout)

    if event is HookEvent.PROMPT_SUBMIT:
        if result.returncode == 0 and result.stdout.strip():
            return PromptOutcome(inject_context=result.stdout.strip()[:_RUN_COMMAND_INJECT_CAP])
        return None

    if event is HookEvent.PRE_TOOL_USE:
        if result.timed_out or result.spawn_failed:
            why = "timed out" if result.timed_out else "could not run"
            return PreToolOutcome(
                decision="deny", reason=f"guardrail command {why}"
            )
        if result.returncode == 2:
            # Claude Code convention: exit 2 is a hard block, stderr is the reason.
            reason = (result.stderr or "").strip()[:500] or "blocked by a lifecycle hook"
            return PreToolOutcome(decision="deny", reason=reason)
        if result.returncode == 0:
            out = result.stdout.strip()
            if not out:
                return None  # allow
            return _pre_outcome_from_json(out)
        # Other nonzero exit: a script bug, non-blocking (mirrors Claude Code).
        # Returned as an allow WITH a note (not None) so the failure is
        # visible in /hook log and as a live activity line instead of an
        # indistinguishable bare no_op (backlog #74C).
        stderr_tail = (result.stderr or "").strip()[:200]
        logger.warning(
            "hook run_command (pre_tool_use) exited %s (non-blocking); stderr: %s",
            result.returncode, stderr_tail,
        )
        note = f"guardrail exited {result.returncode} (non-blocking, script bug?)"
        if stderr_tail:
            note += f"; stderr: {stderr_tail}"
        return PreToolOutcome(decision="allow", note=note)

    # post_tool_use / done: observe plane. Run for side effects; return nothing.
    if result.timed_out or result.spawn_failed:
        logger.warning("hook run_command (%s observe) did not complete cleanly", event)
    return None


def _pre_outcome_from_json(out: str) -> Optional[HookOutcome]:
    """Parse a pre_tool_use command's stdout JSON decision (lenient, never raises)."""
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001 - non-JSON stdout on exit 0 -> allow
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "allow").lower()
    if decision == "deny":
        reason = str(data.get("reason") or "blocked by a lifecycle hook")[:500]
        return PreToolOutcome(decision="deny", reason=reason)
    if decision == "modify":
        updates = data.get("updated_args")
        if isinstance(updates, dict) and updates:
            return PreToolOutcome(decision="modify", updated_args={str(k): v for k, v in updates.items()})
        return None
    return None  # allow / unknown decision -> no-op


# The action table. The bridge looks actions up by name. Adding an action:
# the function + an entry here, an ``ActionSpec`` in ``core/hook_spec.py`` (the
# taxonomy single source), and a logic variant in ``core/hook_manager.py``;
# ``tests/test_hook_spec.py`` pins the three in lockstep. An action may be a
# coroutine function (``require_approval``); the bridge preserves its
# coroutine-ness so the dispatcher awaits it on the loop instead of a pool.
ActionFn = Callable[
    [HookContext, dict],
    "Optional[HookOutcome] | Awaitable[Optional[HookOutcome]]",
]
ACTIONS: Dict[str, ActionFn] = {
    "inject_context": inject_context,
    "block_if_matches": block_if_matches,
    "rewrite_arg": rewrite_arg,
    "require_approval": require_approval,
    "notify": notify,
    "create_todo": create_todo,
    "webhook": webhook,
    "run_command": run_command,
}

# Each action's dispatch plane, derived from ``core/hook_spec.py``. Mutate-plane
# actions return an in-band outcome the fire point applies; observe-plane
# actions run fire-and-forget (the fire point ignores their return). The bridge
# reads this to register a hook on the right plane.
ACTION_PLANES: Dict[str, str] = action_planes()
