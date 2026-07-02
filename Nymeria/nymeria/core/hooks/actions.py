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

import json
import logging
import os
import signal
import subprocess
from typing import Any, Callable, Dict, Optional

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
    return {
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
_RUN_COMMAND_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def _run_command_env(ctx: HookContext) -> Dict[str, str]:
    """Minimal environment for a run_command subprocess (no inherited secrets)."""
    env = {k: os.environ[k] for k in _RUN_COMMAND_ENV_PASSTHROUGH if k in os.environ}
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


def _execute_command(ctx: HookContext, command: str, timeout: float) -> _CommandResult:
    """Run ``command`` with the context on stdin; never raises.

    Own process group + SIGKILL of the whole group on timeout (mirrors
    ``claude_code_bridge``), minimal env. Working dir is the data dir (a stable,
    writable location; not the repo root). The read cap bounds what we RETAIN
    (`communicate` still buffers the child's full output first); a runaway
    emitter is bounded instead by the wall-clock ``timeout`` and the
    child-biased OOM score, so the API process is evicted last.
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
            start_new_session=True,  # own process group, so we can kill children
            preexec_fn=oom_score_preexec(),
        )
        stdout, stderr = proc.communicate(input=_run_command_payload(ctx), timeout=timeout)
        return _CommandResult(
            returncode=proc.returncode,
            stdout=(stdout or "")[:_RUN_COMMAND_READ_CAP],
            stderr=(stderr or "")[:_RUN_COMMAND_READ_CAP],
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        return _CommandResult(timed_out=True)
    except Exception:  # noqa: BLE001 - spawn failure (bad cwd, fork limit, etc.)
        logger.warning("hook run_command failed to execute", exc_info=True)
        _kill_process_group(proc)
        return _CommandResult(spawn_failed=True)


def _kill_process_group(proc) -> None:
    """Best-effort SIGKILL of a subprocess's whole group (POSIX); never raises."""
    if proc is None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - the process may already be gone
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
      -> None (script bug, non-blocking); timeout / spawn failure -> DENY. A
      guardrail that could not be evaluated must not silently pass, so this fails
      CLOSED (consistent with the dispatcher's PRE fault policy).
    - ``post_tool_use`` / ``done`` (observe): run for side effects, return None.

    Never raises: every failure mode maps to an explicit outcome or None.
    """
    from ...config import get_settings
    if not getattr(get_settings(), "hooks_run_command_enabled", False):
        logger.warning("hook run_command skipped: HOOKS_RUN_COMMAND_ENABLED is off")
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
        logger.warning(
            "hook run_command (pre_tool_use) exited %s (non-blocking); stderr: %s",
            result.returncode, (result.stderr or "")[:200],
        )
        return None

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
# ``tests/test_hook_spec.py`` pins the three in lockstep.
ActionFn = Callable[[HookContext, dict], Optional[HookOutcome]]
ACTIONS: Dict[str, ActionFn] = {
    "inject_context": inject_context,
    "block_if_matches": block_if_matches,
    "rewrite_arg": rewrite_arg,
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
