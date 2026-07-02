"""``nym.thread``: a sub-agent turn (plan: "The SDK surface").

Two shapes behind one verb:

- ``id_or_title`` set: a turn in an EXISTING thread, resolved by thread id or
  callable name, via ``thread_agent_executor.invoke`` (``mode="ask"``,
  blocking) or ``.handoff`` (``mode="handoff"``, fire-and-forget receipt).
  The target must be the calling user's own thread AND marked callable (the
  same consent flag the callable-tool path enforces); the callable-lifecycle
  circular guard plus a self-thread guard reject the two deadlock shapes.
- ``id_or_title`` unset: a FRESH configured thread via the ``spawn_thread``
  tool, invoked with an explicit configurable (no live turn needed). Fresh
  spawns are always blocking (``mode="ask"``); the spawned thread is real and
  inspectable in the UI.

``schema=`` runs the turn normally, then extracts structured JSON from the
reply with the shared structured-output helper (post-hoc extraction,
plan-settled). ``kit=`` is phase 5 breadth and rejected with a clear message.

Blocking dispatches run on a DEDICATED bounded pool (not the loop's default
executor: a sub-turn blocks its OS thread for minutes, and the default pool
is shared with the rest of the API's ``to_thread`` I/O), so a burst of
workflow sub-turns queues here instead of starving the process. On
cancellation (workflow killed or turn aborted) the handler cascades the
abort into the sub-thread so the cascade invariant holds through workflows.

A blocking ask also REGISTERS the caller->target invocation edge (the
``tool_factory`` idiom): ``is_ancestor_invocation`` is a BFS over that graph,
so without the edge a workflow-created cycle (A's workflow asks B, B asks A)
would be invisible to B's guard and deadlock instead of being rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Tuple

from .registry import VerbContext, VerbError, register_verb
from .structured import ainvoke_structured
from .verbs_llm import DEFAULT_LLM_TIER, build_llm_for_thread

logger = logging.getLogger(__name__)

MODES = ("ask", "handoff")

# Sub-turn dispatches only (invoke/handoff/spawn). Quick store reads stay on
# asyncio.to_thread; capping the minutes-long calls here bounds how many
# default-executor-equivalent threads workflows can pin at once.
DISPATCH_POOL_WORKERS = 4
_dispatch_pool_instance: Optional[ThreadPoolExecutor] = None
_dispatch_pool_lock = threading.Lock()


def _dispatch_pool() -> ThreadPoolExecutor:
    global _dispatch_pool_instance
    if _dispatch_pool_instance is None:
        with _dispatch_pool_lock:
            if _dispatch_pool_instance is None:
                _dispatch_pool_instance = ThreadPoolExecutor(
                    max_workers=DISPATCH_POOL_WORKERS,
                    thread_name_prefix="wf-thread-dispatch",
                )
    return _dispatch_pool_instance


async def _run_dispatch(fn, /, *args) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_dispatch_pool(), functools.partial(fn, *args))


def _current_agent() -> Optional[Any]:
    """Seam: the active agent runtime."""
    from ...tools.utils import current_agent

    return current_agent()


def _check_ownership(agent: Any, user_id: str, thread_id: str) -> Optional[str]:
    """Seam: the callable-thread ownership gate (fail-closed, admin rules)."""
    from ...agents.tool_factory import _check_callable_ownership

    return _check_callable_ownership(
        agent, user_id, name="nym.thread", thread_id=thread_id
    )


def _invoke_thread(
    thread_id: str, task: str, caller_user_id: str, label: str, trigger: str
) -> str:
    """Seam: the blocking sub-agent turn."""
    from ..thread_agent_executor import invoke

    return invoke(thread_id, task, caller_user_id, label, trigger_override=trigger)


def _handoff_thread(
    thread_id: str,
    task: str,
    caller_user_id: str,
    label: str,
    caller_thread_id: str,
    trigger: str,
) -> str:
    """Seam: the fire-and-forget sub-agent turn."""
    from ..thread_agent_executor import handoff

    return handoff(
        thread_id,
        task,
        caller_user_id,
        label,
        caller_thread_id=caller_thread_id,
        trigger_override=trigger,
    )


def _spawn_fresh(spawn_args: dict, config: dict) -> str:
    """Seam: the spawn_thread tool with an explicit configurable.

    Sync on purpose: the caller runs it on the dedicated dispatch pool (the
    tool's own ``.ainvoke`` would put the minutes-long blocking body on the
    loop's DEFAULT executor instead).
    """
    from typing import cast

    from langchain_core.runnables import RunnableConfig

    from ...tools.spawn_thread import spawn_thread

    return cast(str, spawn_thread.invoke(spawn_args, cast(RunnableConfig, config)))


def _error_markered(text: str, *, include_plain: bool) -> Optional[str]:
    """The executor reports failures as marker strings; surface them as errors.

    ``include_plain`` extends the check to bare ``[Error]`` prefixes, which is
    correct ONLY for controlled receipts (handoff validation returns, spawn
    failures). A blocking ask's reply is FREE-FORM sub-agent text: a
    legitimate reply may open with "[Error]" (e.g. quoting a log line), so
    ask replies are checked against the structured marker alone.
    """
    from ..thread_agent_executor import ERROR_MARKER_PREFIX

    stripped = (text or "").strip()
    if stripped.startswith(ERROR_MARKER_PREFIX):
        return stripped
    if include_plain and stripped.startswith("[Error]"):
        return stripped
    return None


def resolve_target_thread(agent: Any, user_id: str, id_or_title: str) -> Tuple[str, str]:
    """Resolve ``id_or_title`` to ``(thread_id, label)``; raises VerbError.

    Exact thread id first, then the user's callable threads by callable name
    (case-insensitive). Ownership is checked with the shared fail-closed gate,
    and the target must be marked callable: that flag is the thread owner's
    consent to programmatic invocation, for workflows exactly as for
    callable-thread tools.
    """
    manager = agent.thread_config_manager
    wanted = str(id_or_title or "").strip()
    if not wanted:
        raise VerbError("id_or_title must be a thread id or a callable thread name")

    tc = manager.get_config(wanted)
    if tc is None:
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        for candidate in manager.list_callable_threads(owned_thread_ids=owned):
            name = str(candidate.callable_name or "")
            if name.casefold() == wanted.casefold():
                tc = candidate
                break
    if tc is None:
        raise VerbError(
            f"no thread matches {wanted!r} (pass a thread id or the callable "
            "name of one of your callable threads)"
        )

    denial = _check_ownership(agent, user_id, tc.thread_id)
    if denial:
        raise VerbError(denial)
    if not getattr(tc, "callable", False):
        raise VerbError(
            f"thread {tc.thread_id!r} is not callable; mark it callable to "
            "allow workflow invocation"
        )
    return tc.thread_id, str(tc.callable_name or tc.thread_id)


async def _run_existing(
    ctx: VerbContext, agent: Any, target: str, label: str, prompt: str, mode: str
) -> str:
    trigger = f"workflow:{ctx.workflow_id}"
    if mode == "handoff":
        return await _run_dispatch(
            _handoff_thread, target, prompt, ctx.user_id, label, ctx.thread_id, trigger
        )
    if target == ctx.thread_id:
        raise VerbError(
            "a blocking nym.thread call into the workflow's own thread would "
            'deadlock on the thread lock; use mode="handoff"'
        )
    if agent.is_ancestor_invocation(ctx.thread_id, target):
        raise VerbError(
            f"thread {label!r} is an ancestor of this workflow's thread and is "
            'waiting on it; a blocking call would deadlock (use mode="handoff")'
        )
    # Register the caller->target edge for the duration of the blocking wait
    # (the tool_factory idiom), so a target that calls BACK toward the
    # workflow's thread is rejected by ITS ancestor guard instead of
    # deadlocking. When the workflow runs outside a live turn the edge is a
    # conservative false positive for that window, which is the safe side.
    agent.register_callable_invocation(ctx.thread_id, target)
    try:
        return await _run_dispatch(
            _invoke_thread, target, prompt, ctx.user_id, label, trigger
        )
    except asyncio.CancelledError:
        # The workflow was killed or the turn aborted mid sub-turn: the abort
        # cascade MUST reach the sub-thread (plan invariant); the underlying
        # OS thread cannot be cancelled, but the cascade stops the turn it runs.
        with contextlib.suppress(Exception):
            agent.abort_with_cascade(target)
        raise
    finally:
        with contextlib.suppress(Exception):
            agent.unregister_callable_invocation(ctx.thread_id, target)


def _build_spawn_args(agent: Any, args: dict) -> dict:
    from ...config.model_tiers import is_thread_tier_alias, split_provider_model

    spawn_args: dict = {"prompt": str(args.get("prompt") or "")}
    if args.get("title") is not None:
        spawn_args["title"] = str(args["title"])
    if args.get("instructions") is not None:
        spawn_args["instructions"] = str(args["instructions"])
    tools = args.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise VerbError("tools must be a list of tool names")
        spawn_args["optional_tools"] = [str(t) for t in tools]
    model = str(args.get("model") or "").strip()
    if model:
        if is_thread_tier_alias(model):
            spawn_args["llm_model"] = model
        else:
            provider, model_name = split_provider_model(
                model, agent.settings.llm_provider
            )
            spawn_args["llm_provider"] = provider
            spawn_args["llm_model"] = model_name
    return spawn_args


async def _extract(ctx: VerbContext, agent: Any, text: str, schema: Any) -> Any:
    """Post-hoc extraction: the reply through the structured helper."""
    llm = await asyncio.to_thread(
        build_llm_for_thread, agent, ctx.thread_id, DEFAULT_LLM_TIER
    )
    prompt = (
        "Extract the requested data from this sub-agent reply. Use only what "
        "the reply itself states; ignore bracketed mechanical preamble lines "
        "(e.g. [Spawned]: thread ids, deletion instructions).\n\n"
        f"REPLY:\n{text}"
    )
    return await ainvoke_structured(llm, prompt, schema)


@register_verb(
    "thread",
    ai=True,
    side_effect=True,
    positional=("prompt",),
    description=(
        "Run a prompt in a sub-agent thread: an existing callable thread "
        "(id_or_title=) or a freshly spawned one; schema= extracts JSON."
    ),
)
async def _thread_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise VerbError("nym.thread requires a non-empty prompt")
    if args.get("kit") is not None:
        raise VerbError(
            "kit= is not supported yet; pass tools= with explicit tool names"
        )
    mode = str(args.get("mode") or "ask").strip().lower()
    if mode not in MODES:
        raise VerbError(f'mode must be one of {MODES}, got {mode!r}')
    schema = args.get("schema")
    if schema is not None and mode == "handoff":
        raise VerbError(
            'schema= needs the reply, which mode="handoff" does not wait for'
        )

    agent = _current_agent()
    if agent is None:
        raise VerbError("no agent runtime is available for nym.thread")

    id_or_title = args.get("id_or_title")
    if id_or_title is not None:
        # Resolution reads thread/account stores (disk + SQL); off-loop.
        target, label = await asyncio.to_thread(
            resolve_target_thread, agent, ctx.user_id, str(id_or_title)
        )
        text = await _run_existing(ctx, agent, target, label, prompt, mode)
        # A blocking ask returns FREE-FORM sub-agent text; only the structured
        # marker means failure there. Handoff receipts are controlled strings.
        marker = _error_markered(text, include_plain=(mode == "handoff"))
    else:
        if mode == "handoff":
            raise VerbError(
                'a fresh spawn is always blocking; mode="handoff" needs '
                "id_or_title= (an existing thread)"
            )
        spawn_args = _build_spawn_args(agent, args)
        config = {
            "configurable": {"user_id": ctx.user_id, "thread_id": ctx.thread_id}
        }
        text = await _run_dispatch(_spawn_fresh, spawn_args, config)
        marker = _error_markered(text, include_plain=True)

    if marker is not None:
        raise VerbError(f"sub-agent turn failed: {marker}")
    if schema is not None:
        return await _extract(ctx, agent, text, schema)
    return text
