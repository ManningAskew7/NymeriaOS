"""Dream invocation: spawn a shadow thread and dispatch the dream cycle.

The public entry point is :func:`invoke_dream`. It is synchronous up to the
point the shadow thread is fully configured and ownership is claimed; the
actual dream-cycle turn runs in a daemon thread so HTTP callers return
immediately. Frontends observe progress via the standard autonomous SSE
event stream keyed by the shadow ``thread_id``.

A dream invocation:

1. Validates the parent thread exists and is owned by the caller's user.
2. Generates a shadow ``thread_id`` of the form
   ``dream-{parent_slug}-{timestamp}-{rand}``.
3. Writes a fresh ``ThreadConfig`` with ``system_prompt`` = the dream prompt,
   ``shadow_parent_id`` = parent, plus the dream tool whitelist.
4. Registers thread metadata (lifetime=temporary, idle_timeout=24h) so the
   ``spawn_thread`` idle sweeper cleans it up later.
5. Claims ownership for the parent's user.
6. Updates the parent's ``dreaming.last_dream_at`` and
   ``last_dream_thread_id`` bookkeeping fields.
7. Spawns a daemon thread that dispatches the initial dream prompt and
   streams the turn through the standard agent runtime.

The same entry point serves both the manual trigger (``POST
/threads/{id}/dream``) and the automatic scheduler
(:mod:`nymeria.core.dreaming.scheduler`, which decides *when* to dream). A
process-local in-flight guard keyed on the parent thread prevents the manual
and scheduled paths from double-dreaming the same thread at once.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, Optional, Tuple

from ..time_utils import utc_now

logger = logging.getLogger(__name__)


# Core tools the dream agent should not touch. The dream cycle is internal
# reflection over the parent's memory and config; it has no business running
# shell commands, browsing the web, notifying the user, or requesting
# credentials. Keeping these out also limits the blast radius if the model
# misinterprets the prompt.
DEFAULT_DREAM_DISABLED_CORE_TOOLS: tuple[str, ...] = (
    "bash_execute",
    "file_read",
    "file_write",
    "web_search_perplexity",
    "notify",
    "request_credential",
    "slash_command",
    "consult",
    "auth_inspect",
    "auth_cleanup",
    "auth_bindings",
)

# Core tools dreams are allowed to bind. This is a positive allowlist used by
# graph construction for shadow threads; disabled_core_tools remains in the
# setup summary as operator-facing policy context.
DEFAULT_DREAM_ENABLED_CORE_TOOLS: tuple[str, ...] = (
    "memory_add",
    "memory_edit",
    "memory_read",
    "nym_todo",
    "nym_todo_delete",
    "nym_todo_list",
)

# Optional tools the dream cycle gets access to in addition to the always-on
# core set. These let the dream propose skill suggestions, manage skills, and
# write to the parent's instructions field.
DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS: tuple[str, ...] = (
    "thread_instructions_set",
    "skill_write",
    "skill_edit",
    "skill_manage",
    "list_installed_skills",
    "search_skills",
    "install_skill",
    "tool_create",
)

DREAM_THREAD_ID_PREFIX = "dream"
DREAM_IDLE_TIMEOUT_HOURS = 24


class DreamInvocationError(Exception):
    """Raised when a dream cannot be started (validation / persistence failure)."""


# Process-local single-flight guard: parent_thread_id of every dream whose
# daemon is currently running. Claimed in ``invoke_dream`` once setup is about
# to succeed and released in ``_run_dream_cycle``'s ``finally`` (or by
# ``invoke_dream`` itself if setup fails before the daemon starts). The
# scheduler's interval gate already serializes successive automatic dreams;
# this also stops a manual trigger from racing a scheduled one. Process-local
# is sufficient: the agent runtime is single-process, and a crash that skips
# the release is reset by the restart that empties the set.
_dreams_in_flight: set[str] = set()
_dreams_in_flight_lock = threading.Lock()


def _claim_dream_slot(parent_thread_id: str) -> None:
    """Reserve the in-flight slot for a parent, or raise if one is active."""
    with _dreams_in_flight_lock:
        if parent_thread_id in _dreams_in_flight:
            raise DreamInvocationError(
                f"A dream is already in progress for thread {parent_thread_id}"
            )
        _dreams_in_flight.add(parent_thread_id)


def _release_dream_slot(parent_thread_id: str) -> None:
    """Release a parent's in-flight slot (idempotent)."""
    with _dreams_in_flight_lock:
        _dreams_in_flight.discard(parent_thread_id)


def _slug_from_thread_id(thread_id: str, max_len: int = 24) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in thread_id.lower())
    safe = "-".join(filter(None, safe.split("-")))
    safe = safe[:max_len].rstrip("-")
    return safe or "thread"


def _build_initial_prompt(
    parent_thread_id: str,
    current_instructions: Optional[str],
    *,
    template: Optional[str] = None,
) -> str:
    """Assemble the orient-phase payload the dream agent reads first.

    The system prompt explains the cycle and rules; this is the per-cycle
    context: which thread we're dreaming for, plus a verbatim copy of the
    parent's current instructions (since the dream agent cannot see them
    by reading its own ``ThreadConfig``).

    ``template`` is the kickoff template (per-thread or global default). When
    omitted, the global default is loaded. The two placeholders
    ``{parent_thread_id}`` and ``{parent_instructions}`` are substituted with
    ``str.replace`` rather than ``str.format`` because the instructions text is
    user-authored and may contain literal braces.
    """
    instructions_block = (
        current_instructions.strip()
        if current_instructions and current_instructions.strip()
        else "(none, the parent has no per-thread instructions yet)"
    )
    if template is None:
        from ...config import get_settings

        template = get_settings().load_dream_kickoff_prompt()
    return template.replace("{parent_thread_id}", parent_thread_id).replace(
        "{parent_instructions}", instructions_block
    )


def _resolve_dream_system_prompt(dream_cfg: Any, settings: Any) -> str:
    """Per-thread dream system prompt if set, else the global default."""
    override = getattr(dream_cfg, "system_prompt", None) if dream_cfg else None
    if override and override.strip():
        return override
    return settings.load_dream_prompt()


def _resolve_dream_kickoff_template(dream_cfg: Any, settings: Any) -> str:
    """Per-thread dream kickoff template if set, else the global default."""
    override = getattr(dream_cfg, "kickoff_prompt", None) if dream_cfg else None
    if override and override.strip():
        return override
    return settings.load_dream_kickoff_prompt()


def invoke_dream(
    agent: Any,
    parent_thread_id: str,
    user_id: str,
    *,
    model_override: Optional[str] = None,
    enabled_optional_tools: Optional[tuple[str, ...]] = None,
    disabled_core_tools: Optional[tuple[str, ...]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Create the shadow thread and kick off the dream cycle in the background.

    Returns ``(shadow_thread_id, summary)`` where ``summary`` is a small dict
    describing what was set up (handy for the HTTP response).

    Raises ``DreamInvocationError`` on validation or persistence failure
    BEFORE the daemon thread is spawned. Failures inside the dream turn itself
    are logged and surfaced via the autonomous event stream, not raised.
    """
    if not parent_thread_id or not parent_thread_id.strip():
        raise DreamInvocationError("parent_thread_id is required")
    parent_thread_id = parent_thread_id.strip()

    if not user_id or not user_id.strip():
        raise DreamInvocationError("user_id is required")
    user_id = user_id.strip()

    if agent is None:
        raise DreamInvocationError("agent is required")

    # Validate parent exists and load its config so we can copy LLM settings.
    parent_tc = agent.thread_config_manager.get_config(parent_thread_id)
    if parent_tc is None:
        from ..thread_config import ThreadConfig

        parent_tc = ThreadConfig(thread_id=parent_thread_id)

    if parent_tc.shadow_parent_id:
        raise DreamInvocationError(
            f"Refusing to dream from a shadow thread (parent={parent_tc.shadow_parent_id})"
        )

    # Claim the single-flight slot before persisting anything. From here on any
    # early failure releases it; a successful daemon start hands the release off
    # to _run_dream_cycle's finally.
    _claim_dream_slot(parent_thread_id)
    started = False
    try:
        # Build the shadow thread ID. Suffix-only to keep the listing tidy.
        timestamp = utc_now().strftime("%Y%m%dT%H%M%S")
        rand = uuid.uuid4().hex[:6]
        parent_slug = _slug_from_thread_id(parent_thread_id, max_len=24)
        shadow_thread_id = f"{DREAM_THREAD_ID_PREFIX}-{parent_slug}-{timestamp}-{rand}"

        # Build the shadow's LLM override. Default: inherit the parent's
        # effective llm_config; allow the caller (manual trigger or scheduler)
        # to override just the model.
        from ..thread_config import ThreadConfig, ThreadLLMConfig

        shadow_llm_config: Optional[ThreadLLMConfig] = None
        if parent_tc.llm_config is not None:
            shadow_llm_config = parent_tc.llm_config.model_copy(deep=True)
        if model_override:
            if shadow_llm_config is None:
                shadow_llm_config = ThreadLLMConfig(model=model_override)
            else:
                shadow_llm_config.model = model_override

        enabled_opt = list(
            enabled_optional_tools or DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS
        )
        disabled_core = list(disabled_core_tools or DEFAULT_DREAM_DISABLED_CORE_TOOLS)

        # Load the dream system prompt — replaces soul.md entirely for this thread.
        # Precedence: per-thread override (parent_tc.dreaming.system_prompt) wins,
        # else the global default (data-dir override or shipped dream_prompt.md).
        from ...config import get_settings

        settings = get_settings()
        dream_cfg = parent_tc.dreaming
        dream_system_prompt = _resolve_dream_system_prompt(dream_cfg, settings)

        try:
            shadow_tc = ThreadConfig(
                thread_id=shadow_thread_id,
                system_prompt=dream_system_prompt,
                shadow_parent_id=parent_thread_id,
                enabled_tools=sorted(set(enabled_opt)),
                disabled_tools=sorted(set(disabled_core)),
                llm_config=shadow_llm_config,
                # Inject the user's saved profile so the dream sees who it's
                # reflecting for without needing extra tool calls in phase 1.
                inject_profile_in_prompt=True,
            )
        except Exception as e:  # pydantic validation
            raise DreamInvocationError(f"Invalid shadow ThreadConfig: {e}") from e

        if not agent.thread_config_manager.save_config(shadow_tc):
            raise DreamInvocationError("Failed to save shadow ThreadConfig")

        # Register thread metadata so the idle-sweeper cleans it up after 24h of
        # inactivity. Reuses the spawn_thread temporary-lifetime markers so the
        # existing housekeeping path picks it up without changes.
        from ...tools.spawn_thread import (
            PLATFORM_META_IDLE_TIMEOUT,
            PLATFORM_META_LAST_ACTIVE,
            PLATFORM_META_LIFETIME,
        )

        platform_meta: Dict[str, str] = {
            "dream": "true",
            "shadow_parent": parent_thread_id,
            PLATFORM_META_LIFETIME: "temporary",
            PLATFORM_META_IDLE_TIMEOUT: str(DREAM_IDLE_TIMEOUT_HOURS),
            PLATFORM_META_LAST_ACTIVE: utc_now().isoformat(),
        }
        title = f"Dream: {parent_thread_id[:48]}"

        try:
            agent.thread_metadata_manager.upsert_thread(
                user_id,
                shadow_thread_id,
                title=title,
                title_source="dream",
                platform="dream",
                platform_meta=platform_meta,
            )
        except Exception as e:
            try:
                agent.thread_config_manager.delete_config(shadow_thread_id)
            except Exception:
                logger.warning("Rollback of shadow config failed", exc_info=True)
            raise DreamInvocationError(f"Failed to save shadow metadata: {e}") from e

        try:
            agent.accounts_repo.claim_thread(shadow_thread_id, user_id)
        except Exception as e:
            logger.warning(
                "invoke_dream: claim_thread failed for %s: %s", shadow_thread_id, e
            )

        agent.invalidate_thread_config_cache(shadow_thread_id)

        # Bookkeeping on the parent: record when we kicked off and which shadow
        # thread carries the transcript. These are the gating fields the
        # scheduler reads next time around. Re-load the config immediately before
        # the write so we patch only the dreaming fields onto the freshest copy
        # rather than clobbering anything a concurrent writer (a parent turn, a
        # UI config edit) changed since we loaded parent_tc at the top.
        from ..thread_config import DreamingConfig

        fresh_parent = (
            agent.thread_config_manager.get_config(parent_thread_id) or parent_tc
        )
        parent_dream = fresh_parent.dreaming or DreamingConfig()
        parent_dream.last_dream_at = utc_now()
        parent_dream.last_dream_thread_id = shadow_thread_id
        fresh_parent.dreaming = parent_dream
        if not agent.thread_config_manager.save_config(fresh_parent):
            logger.warning(
                "invoke_dream: failed to persist last_dream_at on parent %s",
                parent_thread_id,
            )
        else:
            agent.invalidate_thread_config_cache(parent_thread_id)

        kickoff_template = _resolve_dream_kickoff_template(dream_cfg, settings)
        initial_prompt = _build_initial_prompt(
            parent_thread_id, parent_tc.instructions, template=kickoff_template
        )

        try:
            from ..event_bus import publish_sync_event

            publish_sync_event(
                event_type="thread_created",
                thread_id=shadow_thread_id,
                user_id=user_id,
                data={
                    "title": title,
                    "title_source": "dream",
                    "platform": "dream",
                    "platform_meta": platform_meta,
                },
            )
        except Exception as e:
            logger.debug("invoke_dream: thread_created publish failed: %s", e)

        started_at = utc_now()

        # Fire-and-forget dispatch. We deliberately don't await — the endpoint
        # returns immediately and observers consume the autonomous event stream
        # to watch the dream progress.
        thread = threading.Thread(
            target=_run_dream_cycle,
            kwargs={
                "agent": agent,
                "shadow_thread_id": shadow_thread_id,
                "parent_thread_id": parent_thread_id,
                "user_id": user_id,
                "initial_prompt": initial_prompt,
                "title": title,
            },
            name=f"dream-{shadow_thread_id}",
            daemon=True,
        )
        thread.start()
        started = True

        summary = {
            "shadow_thread_id": shadow_thread_id,
            "parent_thread_id": parent_thread_id,
            "started_at": started_at.isoformat(),
            "model": (shadow_llm_config.model if shadow_llm_config else None)
            or model_override
            or "(inherits global)",
            "enabled_optional_tools": list(enabled_opt),
            "disabled_core_tools": list(disabled_core),
        }
        logger.info(
            "invoke_dream: spawned shadow thread %s for parent %s (user=%s)",
            shadow_thread_id,
            parent_thread_id,
            user_id,
        )
        return shadow_thread_id, summary
    finally:
        # If the daemon never started, no _run_dream_cycle finally will fire,
        # so release here. On success the daemon owns the release.
        if not started:
            _release_dream_slot(parent_thread_id)


def _seed_shadow_from_parent(
    agent: Any, parent_thread_id: str, shadow_thread_id: str
) -> None:
    """Copy the parent's conversation into the shadow, soft-pruning tool results.

    The dream reflects on what actually happened on the parent thread, so we
    fork the parent's full LangGraph checkpoint into the (empty) shadow via the
    same row-level copy ``/branch`` uses, then soft-prune so bulky web/file tool
    results are truncated to their gist (cheap, and rarely relevant to
    reflection). Best-effort: on any failure the dream still runs and falls back
    to reading memory in its orient phase, so a copy error never aborts a dream.

    We deliberately do NOT compact (no LLM summary): the dream wants the real
    dialogue. Size control is the soft-prune, not compaction.
    """
    try:
        from ...config import get_settings
        from ..agent_prune import build_pruned_replacements
        from ..thread_branch import clone_thread_checkpoints

        clone_thread_checkpoints(get_settings(), parent_thread_id, shadow_thread_id)

        config = {"configurable": {"thread_id": shadow_thread_id}}
        graph = agent._default_graph
        state = graph.get_state(config)
        messages = state.values.get("messages", []) if state else []
        replacements, stats = build_pruned_replacements(messages, mode="soft")
        if replacements:
            graph.update_state(config, {"messages": replacements})
        logger.info(
            "dream seed: cloned parent %s -> shadow %s (%d msg(s), soft-pruned "
            "%d tool result(s))",
            parent_thread_id,
            shadow_thread_id,
            len(messages),
            stats["pruned_count"],
        )
    except Exception:
        logger.warning(
            "dream seed: clone+prune failed for shadow %s; the dream will "
            "reflect from memory only",
            shadow_thread_id,
            exc_info=True,
        )


def _run_dream_cycle(
    *,
    agent: Any,
    shadow_thread_id: str,
    parent_thread_id: str,
    user_id: str,
    initial_prompt: str,
    title: str,
) -> None:
    """Background thread body: dispatch the dream prompt and stream the turn.

    Modelled on :func:`nymeria.tools.spawn_thread._invoke_spawned` so the
    autonomous SSE event stream looks the same as for spawned-thread tasks.
    Any error inside the turn is published as a ``task_completed`` event with
    ``error=True`` rather than raised — the caller (HTTP endpoint or
    scheduler) has already returned by now.
    """
    from langchain_core.runnables.config import var_child_runnable_config
    from langchain_core.tracers.context import (
        run_collector_var,
        tracing_v2_callback_var,
    )

    # Default the tokens to None so the finally can run even if the setup below
    # raises: a failed import or seed must still release the single-flight slot,
    # or the parent would be barred from dreaming until the process restarts.
    config_token = callback_token = collector_token = None
    task_id = f"dream-{uuid.uuid4().hex[:8]}"
    started_published = False

    try:
        from ..event_bus import publish_agent_stream_chunk, publish_autonomous_event
        from ..stream_bridge import stream_and_collect

        config_token = var_child_runnable_config.set(None)
        callback_token = tracing_v2_callback_var.set(None)
        collector_token = run_collector_var.set(None)

        # Seed the shadow with the parent's conversation (forked + soft-pruned)
        # before the dream turn runs, so the dream reflects on what actually
        # happened. Best-effort; never raises.
        _seed_shadow_from_parent(agent, parent_thread_id, shadow_thread_id)

        trigger_override = f'Dream("{parent_thread_id}")'

        def handle_chunk(chunk: Dict[str, Any], _collection) -> None:
            nonlocal started_published
            if not started_published and chunk.get("type") not in (
                "queued",
                "prompt_queued",
            ):
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=shadow_thread_id,
                    user_id=user_id,
                    task_id=task_id,
                    data={
                        "prompt": initial_prompt,
                        "callable_name": title,
                        "trigger": "dream",
                        "parent_thread_id": parent_thread_id,
                    },
                )
                started_published = True

            publish_agent_stream_chunk(
                chunk,
                thread_id=shadow_thread_id,
                user_id=user_id,
                task_id=task_id,
            )

        def stream_error_message(chunk: Dict[str, Any]) -> str:
            content = chunk.get("content")
            return str(content) if content else "dream stream error"

        result = stream_and_collect(
            agent,
            astream_kwargs={
                "message": initial_prompt,
                "thread_id": shadow_thread_id,
                "user_id": user_id,
                "_is_self_invoke": True,
                "_trigger_override": trigger_override,
            },
            on_chunk=handle_chunk,
            error_message_factory=stream_error_message,
        )

        response_text = result.response_text() or ""
        if result.iteration_limit_hit and response_text:
            response_text += "\n\n[Note: Dream stopped at iteration limit.]"
        elif result.iteration_limit_hit:
            response_text = "[Dream hit iteration limit without producing a summary.]"

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=shadow_thread_id,
            user_id=user_id,
            task_id=task_id,
            data={
                "content": response_text,
                "callable_name": title,
                "parent_thread_id": parent_thread_id,
            },
        )

        try:
            from ..event_bus import publish_sync_event

            publish_sync_event(
                event_type="dream_completed",
                thread_id=parent_thread_id,
                user_id=user_id,
                data={
                    "shadow_thread_id": shadow_thread_id,
                    "summary": response_text[:2000],
                },
            )
        except Exception as e:
            logger.debug("dream_completed publish failed: %s", e)

        logger.info(
            "Dream cycle finished for shadow=%s parent=%s (%d chars summary)",
            shadow_thread_id,
            parent_thread_id,
            len(response_text),
        )

    except Exception as e:
        logger.error(
            "Dream cycle failed for shadow=%s parent=%s: %s",
            shadow_thread_id,
            parent_thread_id,
            e,
            exc_info=True,
        )
        try:
            # Re-import locally: the top-of-try import may not have bound the
            # name if setup failed before it, and this except still wants to
            # publish the failure event.
            from ..event_bus import publish_autonomous_event

            publish_autonomous_event(
                event_type="task_completed",
                thread_id=shadow_thread_id,
                user_id=user_id,
                task_id=task_id,
                data={
                    "error": True,
                    "error_message": str(e)[:200],
                    "content": f"Dream failed: {str(e)[:200]}",
                    "callable_name": title,
                    "parent_thread_id": parent_thread_id,
                },
            )
        except Exception:
            logger.warning("Failed to publish dream error event", exc_info=True)

    finally:
        # Release the single-flight slot first, so a later reset failure (or a
        # setup error that left the tokens unset) can never strand it and bar
        # the parent from future dreams.
        _release_dream_slot(parent_thread_id)
        if collector_token is not None:
            run_collector_var.reset(collector_token)
        if callback_token is not None:
            tracing_v2_callback_var.reset(callback_token)
        if config_token is not None:
            var_child_runnable_config.reset(config_token)
