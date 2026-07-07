"""Compaction policy, execution, and checkpoint pruning.

Owns the full lifecycle: threshold checking, summary generation, message
clearing, checkpoint pruning, and pending-summary state.  NymeriaAgent
delegates all compaction work here via self._compaction.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import uuid as _uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from ..config.model_capabilities import estimate_image_tokens, get_context_limit
from .checkpoint_cleanup import prune_checkpoints_before
from .time_utils import utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


def hook_context_stats(agent: "NymeriaAgent", thread_id: str) -> Dict[str, Optional[int]]:
    """Best-effort context-usage numbers for lifecycle-hook fire points.

    Returns ``context_tokens`` (current occupancy; None when unknown or the
    tracker has no measurement yet), ``context_limit`` (the thread's effective
    model window), and ``compact_trigger_tokens`` (the resolved auto-compact
    trigger; None when context_management != auto_compact). Reads the same
    sources as ``get_context_stats``/``should_auto_compact_now`` but never
    rehydrates from the checkpoint and never raises: hooks are best-effort
    consumers and a stats failure must not touch a turn.
    """
    empty: Dict[str, Optional[int]] = {
        "context_tokens": None,
        "context_limit": None,
        "compact_trigger_tokens": None,
    }
    try:
        usage = agent._token_tracker.get_usage(thread_id)
        tokens = int(usage.context_tokens) if usage.context_tokens > 0 else None
        llm_config = agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        trigger = None
        if model_limit and agent.settings.context_management == "auto_compact":
            mode, pct, abs_tokens = agent._compaction._resolve_threshold_config(thread_id)
            trigger = agent._compact_trigger_tokens(
                model_limit, pct, mode=mode, tokens=abs_tokens
            )
        return {
            "context_tokens": tokens,
            "context_limit": int(model_limit) if model_limit else None,
            "compact_trigger_tokens": int(trigger) if trigger else None,
        }
    except Exception:  # noqa: BLE001 - a stats failure must never touch a turn
        logger.debug("hook context stats unavailable for %s", thread_id, exc_info=True)
        return empty


COMPACTION_TIMEOUT_SECONDS = 900
COMPACTING_MESSAGE = "Compacting thread context..."
CompactionStartCallback = Callable[[], Any]

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

COMPACT_PROMPT = """**System Request: Context Compaction**

The conversation is getting long and is being summarized. The older messages
will be replaced by your summary plus your reloaded persistent memory, so write
the summary so that you (or a fresh session) can continue seamlessly.

Before summarizing, persist anything important so it survives the trim:
- Durable facts (user identity, lasting preferences, project names, technical
  constraints) -> memory_add(scope="global", key=..., content=...)
- Working/session state for THIS thread (current task, progress, intermediate
  results, file paths) -> memory_add(scope="thread", content=...)

You do NOT need to call memory_read here; your memory is reloaded automatically
right after this turn.

**Structure your summary using EXACTLY these sections:**

## Active Goal
What is the user's current objective? Be specific: the exact request,
not a paraphrase. Include any constraints or preferences stated.

## Progress
Concrete outcomes so far:
- Completed steps with results
- Decisions made and their reasoning
- Tool operations and their outcomes
- Errors encountered and how they were resolved

## Pending Work
What remains, and what you were in the MIDDLE of when compaction triggered.
Be explicit so you can resume without re-deriving it:
- The exact next concrete action
- Outstanding questions or decisions
- Blockers or dependencies

## Key Context
Facts and state that must survive:
- User preferences and constraints established
- Project/technical details referenced
- Configuration or environment details
- Tracked variables or temporary state

## Files & Resources
Specific paths, URLs, or resources for continuing work, so you can re-read them
after compaction:
- Files being actively edited (exact paths)
- Config files referenced
- Artifacts created during this session
- External resources consulted

## RAG Search Queries
3-5 search queries the resuming agent should run against the conversation
memory store. Target key concepts, decisions, and findings from this thread.
Format as a bulleted list of quoted strings.

**Rules:**
- Be specific: exact file paths, variable names, error messages
- If you were mid-task, record precisely where you stopped and the next step
- Omit greetings, failed-then-corrected attempts, verbose tool outputs
- Aim for under 1500 words total"""

# Optional user steering for a manual ``/compact <focus instruction>``. Added
# ONLY when the user supplies a focus; auto-compaction never carries one, so the
# base prompt stays byte-identical on that path. Framed as "prioritize, not
# filter": the agent must still complete every required section and persist all
# durable facts, the focus only changes emphasis/ordering. The primer is
# inserted just before the sections so the model writes them with the focus in
# mind; the addendum is appended last for recency.
MAX_COMPACT_PRIORITY_CHARS = 1000

_COMPACT_SECTIONS_ANCHOR = "**Structure your summary using EXACTLY these sections:**"

COMPACT_PRIORITY_PRIMER = (
    'The user has flagged a specific focus for this summary (see "Additional '
    'focus" at the end). Keep it in mind as you write every section below: it '
    "changes the order and specificity of what you record, not which sections "
    "or facts you include."
)

COMPACT_PRIORITY_ADDENDUM = """

**Additional focus for this summary (user-requested)**

The user asked you to pay special attention to the following:

<<<
{priority}
>>>

This is an extra priority layered on top of every instruction above, never a
replacement for them. Apply it like this:
- Still produce every required section in full, and still persist all durable
  facts and working state to memory exactly as already instructed. Nothing
  unrelated to this focus may be dropped, merged, or shortened because of it.
- Within the sections where this focus is relevant, lead with it and be more
  precise and complete about it (exact names, decisions, values, paths) than you
  would be by default. Steer detail by ordering and specificity, not by spending
  the whole summary on it.
- If honoring this focus and covering everything else would exceed the length
  guidance above, exceed it: completeness of the required sections and the focus
  both win over the word target. The word target is a default, not a reason to
  omit anything."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_priority(text: Optional[str]) -> Optional[str]:
    """Clean a user-supplied ``/compact`` focus instruction.

    Strips control characters (keeping newlines/tabs), removes any ``<<<``/``>>>``
    fence markers so the user text cannot break the addendum's delimiters, trims
    whitespace, caps length to ``MAX_COMPACT_PRIORITY_CHARS``, and collapses an
    empty result to ``None`` (so no addendum is added). The priority is transient
    steering: it rides the internal, discarded ``compact_prompt`` turn and is not
    persisted anywhere.
    """
    if not text:
        return None
    cleaned = "".join(
        ch for ch in text if ch in ("\n", "\t") or ord(ch) >= 32
    )
    cleaned = cleaned.replace("<<<", "").replace(">>>", "").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_COMPACT_PRIORITY_CHARS:
        cleaned = cleaned[:MAX_COMPACT_PRIORITY_CHARS].rstrip()
    return cleaned

def estimate_tokens(text: str) -> int:
    """Rough estimate of token count (~4 chars per token)."""
    return len(text) // 4


def _estimate_content_tokens(
    content: Any, model: str = "", image_dims: Optional[List[Any]] = None
) -> int:
    """Token estimate for one message's content, image-aware.

    Image blocks are counted by their per-provider image-token cost, NOT by the
    length of their inline base64 data URL (a 5 MB image is ~1.7M base64 chars,
    which ``str(content)`` would mis-estimate as ~420k tokens). ``image_dims`` is
    the per-block ``(width, height)`` list (k-th tuple pairs with the k-th
    ``image_url`` block, mirroring the ingress ordering); when present the
    per-provider patch/tile formula is used, otherwise a flat per-model estimate.
    """
    if isinstance(content, str):
        return estimate_tokens(content)
    if not isinstance(content, list):
        return estimate_tokens(str(content))
    dims = image_dims or []
    total = 0
    img_seen = 0
    for block in content:
        if isinstance(block, dict):
            btype = block.get("type")
            if btype == "image_url":
                wh = dims[img_seen] if img_seen < len(dims) else None
                img_seen += 1
                if wh:
                    total += estimate_image_tokens(model, wh[0], wh[1])
                else:
                    total += estimate_image_tokens(model)
                continue
            if btype == "text":
                total += estimate_tokens(str(block.get("text", "")))
                continue
        total += estimate_tokens(str(block))
    return total


def _message_image_dims(msg: Any) -> List[Any]:
    """Per-image ``(width, height)`` for a message, in content order.

    Reads the ``width``/``height`` stamped onto image attachment metadata at
    ingress (``additional_kwargs["attachments"]``); entries without dims yield
    ``None`` so the estimator falls back to its flat per-model estimate.
    """
    ak = getattr(msg, "additional_kwargs", None)
    if not isinstance(ak, dict):
        return []
    atts = ak.get("attachments")
    if not isinstance(atts, list):
        return []
    out: List[Any] = []
    for a in atts:
        if not isinstance(a, dict) or a.get("type") != "image":
            continue
        w, h = a.get("width"), a.get("height")
        out.append((w, h) if w and h else None)
    return out


async def _notify_compaction_started(
    on_started: Optional[CompactionStartCallback],
) -> None:
    if on_started is None:
        return
    result = on_started()
    if inspect.isawaitable(result):
        await result


def _notify_compaction_started_sync(
    on_started: Optional[CompactionStartCallback],
) -> None:
    if on_started is None:
        return
    result = on_started()
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        logger.debug("Ignoring async compaction start callback from sync compaction path")


# ---------------------------------------------------------------------------
# CompactionManager
# ---------------------------------------------------------------------------

class CompactionManager:
    """Owns compaction policy and execution.

    Instantiated by NymeriaAgent and accessed as ``agent._compaction``.
    """

    def __init__(self, agent: "NymeriaAgent") -> None:
        self._agent = agent

    # ------------------------------------------------------------------
    # Formatting helpers (previously on ConversationCompactor)
    # ------------------------------------------------------------------

    @staticmethod
    def get_compact_prompt() -> str:
        return COMPACT_PROMPT

    @staticmethod
    def extract_summary(summary_message: AIMessage) -> str:
        content = summary_message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict):
                    block_type = block.get("type")
                    if block_type in (None, "text", "output_text"):
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            parts.append(text)
                elif isinstance(block, str) and block:
                    parts.append(block)
            return "\n".join(parts)
        return str(content)

    # ------------------------------------------------------------------
    # Threshold / policy
    # ------------------------------------------------------------------

    def should_auto_compact_now(
        self,
        thread_id: str,
        user_id: str,
        *,
        rehydrate_if_empty: bool = False,
    ) -> bool:
        """Return True when token tracking says this thread should auto-compact."""
        agent = self._agent
        if agent.settings.context_management != "auto_compact":
            return False

        usage = agent._token_tracker.get_usage(thread_id)
        if rehydrate_if_empty and usage.context_tokens == 0 and usage.total_tokens == 0:
            agent._rehydrate_token_usage(thread_id)

        llm_config = agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        mode, pct, tokens = self._resolve_threshold_config(thread_id)
        trigger_tokens = self.compact_trigger_tokens(
            model_limit, pct, mode=mode, tokens=tokens
        )

        usage = agent._token_tracker.get_usage(thread_id)
        return usage.context_tokens >= trigger_tokens

    def _resolve_threshold_config(self, thread_id: str) -> tuple[str, float, int]:
        """Resolve effective (mode, percentage, tokens) with per-thread override.

        Thread-level ``llm_config.compact_threshold_*`` fields win over global
        settings; ``None`` inherits from global.
        """
        agent = self._agent
        tc_llm = None
        if thread_id:
            tc_obj = agent.thread_config_manager.get_config(thread_id)
            if tc_obj:
                tc_llm = tc_obj.llm_config

        def pick(attr: str, fallback: Any) -> Any:
            value = getattr(tc_llm, attr, None) if tc_llm else None
            return value if value is not None else fallback

        mode = pick("compact_threshold_mode", agent.settings.compact_threshold_mode)
        pct = pick("compact_threshold", agent.settings.compact_threshold)
        tokens = pick("compact_threshold_tokens", agent.settings.compact_threshold_tokens)
        return mode, float(pct), int(tokens)

    @staticmethod
    def compact_trigger_tokens(
        model_limit: int,
        threshold: float = 0.8,
        *,
        mode: str = "tokens",
        tokens: int = 200_000,
    ) -> int:
        """Return the input-token count that should trigger auto-compaction.

        ``mode="percentage"`` returns ``int(model_limit * threshold)``.
        ``mode="tokens"`` returns ``tokens`` clamped to ``model_limit`` so an
        oversized absolute setting never disables compaction.
        """
        if mode == "tokens":
            return max(1, min(int(tokens), int(model_limit)))
        return max(1, int(model_limit * threshold))

    def should_subturn_compact(self, thread_id: str, messages: List[Any]) -> bool:
        """True if the running context crossed the auto-compact trigger mid-loop.

        Called from ``route_after_tools`` (after a tool batch, before the next
        LLM call), where ``TokenTracker`` is stale. Reads the most recent
        AIMessage's provider-reported input tokens from ``messages`` instead and
        compares against the per-thread trigger.
        """
        agent = self._agent
        if agent.settings.context_management != "auto_compact":
            return False
        from .token_usage import extract_last_from_messages

        input_tokens, _ = extract_last_from_messages(messages or [])
        if not input_tokens:
            return False
        llm_config = agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        mode, pct, tokens = self._resolve_threshold_config(thread_id)
        trigger = self.compact_trigger_tokens(model_limit, pct, mode=mode, tokens=tokens)
        return input_tokens >= trigger

    # ------------------------------------------------------------------
    # Async compaction
    # ------------------------------------------------------------------

    async def check_and_compact(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Optional[Dict[str, Any]]:
        """Check if compaction is needed and prepare it (auto-compact).

        Returns a compaction result containing the internal resume state.
        The caller is responsible for streaming the resume graph invocation.
        """
        if not self.should_auto_compact_now(thread_id, user_id):
            return None
        return await self._do_auto_compact(
            thread_id,
            user_id,
            on_started=on_started,
        )

    async def check_and_compact_for_next_turn(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight async compaction; stores summary for the user message."""
        if not self.should_auto_compact_now(
            thread_id, user_id, rehydrate_if_empty=True
        ):
            return None

        result = await self.compact_now(thread_id, user_id, on_started=on_started)
        if result.get("success"):
            result["auto_preflight"] = True
        return result

    async def compact_now(
        self,
        thread_id: str,
        user_id: str = "default",
        *,
        on_started: Optional[CompactionStartCallback] = None,
        priority: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Manually trigger compaction (/compact command).

        Generates summary and stores it to be attached to the user's next
        message. The UI should show "(context summary attached)" instead
        of the full summary.

        ``priority`` is an optional free-text focus instruction (from
        ``/compact <text>``) that steers what the summary emphasizes without
        dropping any required section; normalized here so every caller inherits
        the guard.
        """
        agent = self._agent
        priority = _normalize_priority(priority)
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = await agent._default_async_graph.aget_state(config)
        messages = state.values.get("messages", [])
        msg_count_before = len(messages)

        min_messages = agent.settings.compact_keep_messages
        if msg_count_before < min_messages:
            return {
                "success": False,
                "reason": f"Not enough messages ({msg_count_before}, need {min_messages})",
            }

        await _notify_compaction_started(on_started)
        logger.info(f"Thread {thread_id}: Manual compact starting ({msg_count_before} messages)")

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

        # Manual /compact: build the retained tail but do NOT auto-resume; the
        # user's next message continues naturally after it.
        result = await self._run_compact_turn_and_prune(
            thread_id, user_id, auto_resumed=False, priority=priority,
        )
        if result.get("success"):
            logger.info(f"Thread {thread_id}: Manual compact complete")
        return result

    def _summary_input(self, priority: Optional[str] = None) -> dict:
        """Build the graph input state for summary generation.

        When ``priority`` is set (a manual ``/compact`` with a focus
        instruction), a primer is inserted before the section list and a focus
        addendum is appended. Auto-compaction and overflow recovery pass no
        priority, so the base prompt is byte-identical on those paths.
        """
        from .agent import _create_human_message

        return {"messages": [_create_human_message(
            self._build_compact_prompt(priority),
            internal=True,
            internal_type="compact_prompt",
        )]}

    @classmethod
    def _build_compact_prompt(cls, priority: Optional[str]) -> str:
        """Base compaction prompt, optionally steered by a user focus instruction."""
        prompt = cls.get_compact_prompt()
        if not priority:
            return prompt
        prompt = prompt.replace(
            _COMPACT_SECTIONS_ANCHOR,
            f"{COMPACT_PRIORITY_PRIMER}\n\n{_COMPACT_SECTIONS_ANCHOR}",
            1,
        )
        return prompt + COMPACT_PRIORITY_ADDENDUM.format(priority=priority)

    @staticmethod
    def _extract_summary_from_result(
        messages: List[Any],
    ) -> Optional[str]:
        """Find the last AIMessage in a graph result and extract the summary."""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                return CompactionManager.extract_summary(msg)
        return None

    async def _generate_summary(
        self,
        thread_id: str,
        user_id: str,
        priority: Optional[str] = None,
    ) -> Optional[str]:
        """Generate a summary by injecting a compaction prompt and running the agent."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._get_async_graph_for_user(user_id, thread_id=thread_id)

        agent._compacting_threads.add(thread_id)
        try:
            result = await asyncio.wait_for(
                graph.ainvoke(self._summary_input(priority), config=config),
                timeout=COMPACTION_TIMEOUT_SECONDS,
            )
            return self._extract_summary_from_result(result.get("messages", []))
        except asyncio.TimeoutError:
            logger.warning(
                "Thread %s: Async summary generation timed out after %s seconds",
                thread_id,
                COMPACTION_TIMEOUT_SECONDS,
            )
            return None
        except Exception as e:
            logger.error(
                f"Thread {thread_id}: Async summary generation failed: {e}",
                exc_info=True,
            )
            return None
        finally:
            agent._compacting_threads.discard(thread_id)

    @staticmethod
    def _prune_old_checkpoints(
        thread_id: str,
        verify_state: Any,
        cp_tuple: Any,
    ) -> None:
        """Prune pre-compaction checkpoint history using an already-fetched tuple."""
        post_cp_id = verify_state.config.get("configurable", {}).get("checkpoint_id")
        if not post_cp_id:
            return
        floor_versions: Dict[str, Any] = {}
        if cp_tuple is not None and cp_tuple.checkpoint:
            floor_versions = cp_tuple.checkpoint.get("channel_versions", {}) or {}
        counts = prune_checkpoints_before(thread_id, post_cp_id, floor_versions)
        logger.info(
            f"Thread {thread_id}: Pruned pre-compact history: "
            f"{counts[0]} checkpoints, {counts[1]} writes, {counts[2]} blobs"
        )

    async def _do_auto_compact(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Dict[str, Any]:
        """Post-turn auto-compaction: build the retained tail.

        The astream driver re-drives the graph with {"messages": []} on success
        so the agent continues from the read-back tool results.
        """
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = await agent._default_async_graph.aget_state(config)
        messages = state.values.get("messages", [])
        msg_count_before = len(messages)

        min_messages = agent.settings.compact_keep_messages
        if msg_count_before < min_messages:
            return {
                "success": False,
                "reason": f"Not enough messages ({msg_count_before}, need {min_messages})",
            }

        await _notify_compaction_started(on_started)
        logger.info(f"Thread {thread_id}: Auto-compact starting ({msg_count_before} messages)")

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

        result = await self._run_compact_turn_and_prune(
            thread_id, user_id, auto_resumed=True,
        )
        if result.get("success"):
            logger.info(f"Thread {thread_id}: Auto-compact complete")
        return result

    # ------------------------------------------------------------------
    # Sync compaction (for stream() / chat() / triggers / ticker / CLI)
    # ------------------------------------------------------------------

    def check_and_compact_sync(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Optional[Dict[str, Any]]:
        """Pre-flight auto-compact for the sync stream()/chat() path.

        Mirrors check_and_compact() but uses sync graph calls. On success the
        thread is left with the retained resume-tail; the user's next message
        continues from it.
        """
        agent = self._agent
        if agent.settings.context_management != "auto_compact":
            return None

        usage = agent._token_tracker.get_usage(thread_id)
        if usage.context_tokens == 0 and usage.total_tokens == 0:
            agent._rehydrate_token_usage(thread_id)

        llm_config = agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        mode, pct, tokens = self._resolve_threshold_config(thread_id)
        trigger_tokens = self.compact_trigger_tokens(
            model_limit, pct, mode=mode, tokens=tokens
        )
        usage = agent._token_tracker.get_usage(thread_id)

        if usage.context_tokens < trigger_tokens:
            return None

        return self._do_compact_sync(thread_id, user_id, on_started=on_started)

    def _do_compact_sync(
        self,
        thread_id: str,
        user_id: str,
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Dict[str, Any]:
        """Sync auto-compact: flush -> summarize -> clear -> store pending summary."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])
        msg_count = len(messages)

        if msg_count < agent.settings.compact_keep_messages:
            return {"success": False, "reason": f"Not enough messages ({msg_count})"}

        _notify_compaction_started_sync(on_started)
        logger.info(f"Thread {thread_id}: Sync auto-compact starting ({msg_count} messages)")

        try:
            agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-compact RAG flush failed for {thread_id}: {e}")

        result = self._run_compact_turn_and_prune_sync(
            thread_id, user_id, auto_resumed=False,
        )
        if result.get("success"):
            logger.info(f"Thread {thread_id}: Sync auto-compact complete")
        return result

    def _generate_summary_sync(
        self,
        thread_id: str,
        user_id: str,
    ) -> Optional[str]:
        """Generate context summary via sync graph.invoke()."""
        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._get_graph_for_user(user_id, thread_id=thread_id)

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nymeria-compact-summary",
        )
        future: Optional[concurrent.futures.Future] = None
        agent._compacting_threads.add(thread_id)
        try:
            # No ``priority`` here on purpose: the sync path serves only
            # auto-compaction and overflow recovery, which never carry a user
            # focus instruction. Manual ``/compact <text>`` runs the async chain.
            future = executor.submit(
                graph.invoke,
                self._summary_input(),
                config=config,
            )
            result = future.result(timeout=COMPACTION_TIMEOUT_SECONDS)
            return self._extract_summary_from_result(result.get("messages", []))
        except concurrent.futures.TimeoutError:
            logger.warning(
                "Thread %s: Sync summary generation timed out after %s seconds",
                thread_id,
                COMPACTION_TIMEOUT_SECONDS,
            )
            if future is not None:
                future.cancel()
            return None
        except Exception as e:
            logger.error(
                f"Thread {thread_id}: Sync summary generation failed: {e}",
                exc_info=True,
            )
            return None
        finally:
            agent._compacting_threads.discard(thread_id)
            executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Retained-turn compaction (run the compact turn, then rebuild the
    # thread down to a resume opener + authentic memory read-back). The
    # agent's compaction-turn messages are discarded; its memory writes
    # (side effects on the files) and its summary text are what survive.
    # ------------------------------------------------------------------

    @staticmethod
    def _stamp_seed_marker(
        marker: Any,
        *,
        summary: str,
        messages_removed: int,
        auto_resumed: bool,
    ) -> None:
        """Attach compaction-notice metadata to the resume opener for the UI."""
        marker.additional_kwargs.update({
            "summary": summary,
            "messages_removed": messages_removed,
            "auto_resumed": auto_resumed,
            "timestamp": utc_now().isoformat(),
        })

    @staticmethod
    def _verify_retained(thread_id: str, verify_state: Any, expected_len: int) -> bool:
        """Confirm the retained tail is exactly what we wrote and ends cleanly."""
        remaining = verify_state.values.get("messages", [])
        if len(remaining) != expected_len:
            logger.error(
                f"Thread {thread_id}: Retained-tail verification failed: "
                f"{len(remaining)} messages remain (expected {expected_len})"
            )
            return False
        last = remaining[-1] if remaining else None
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            logger.error(
                f"Thread {thread_id}: Retained tail ends on an open tool-calls AIMessage"
            )
            return False
        return True

    async def _run_compact_turn_and_prune(
        self, thread_id: str, user_id: str, *, auto_resumed: bool,
        priority: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the compaction turn, then rebuild the thread to the retained tail.

        Retained tail (built by ``build_resume_compaction_tail``): a
        ``memory_seed_marker`` resume opener carrying the summary inline, an
        ``AIMessage`` with memory_read tool calls, and the two authentic
        (post-edit) ``ToolMessage`` results: no trailing assistant message, so a
        ``{"messages": []}`` re-drive resumes the agent. The summary is also
        stamped onto the opener's metadata for the frontend compaction notice.
        """
        from .agent_memory_seed import build_resume_compaction_tail

        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._default_async_graph

        pre_messages = self._state_messages(await graph.aget_state(config))
        pre_ids = {m.id for m in pre_messages}
        conversational_before = self._count_conversational_messages(pre_messages)

        summary = await self._generate_summary(thread_id, user_id, priority=priority)
        if not summary:
            await self._discard_turn_delta(graph, config, pre_ids)
            return {"success": False, "reason": "Failed to generate summary"}

        tail = build_resume_compaction_tail(
            user_id=user_id, thread_id=thread_id, summary=summary
        )
        conversational_after = self._count_conversational_messages(tail)
        conversational_removed = max(0, conversational_before - conversational_after)
        self._stamp_seed_marker(
            tail[0], summary=summary, messages_removed=conversational_removed,
            auto_resumed=auto_resumed,
        )

        try:
            post_messages = self._state_messages(await graph.aget_state(config))
            remove = [RemoveMessage(id=m.id) for m in post_messages]
            await graph.aupdate_state(config, {"messages": remove + tail})

            verify_state = await graph.aget_state(config)
            if not self._verify_retained(thread_id, verify_state, len(tail)):
                return {"success": False, "reason": "Retained-tail verification failed"}

            try:
                post_cp_id = self._state_checkpoint_id(verify_state)
                if post_cp_id:
                    cp_tuple = await graph.checkpointer.aget_tuple({
                        "configurable": {"thread_id": thread_id, "checkpoint_id": post_cp_id}
                    })
                    self._prune_old_checkpoints(thread_id, verify_state, cp_tuple)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pruning call failed: {e}")
        except Exception as e:
            logger.error(f"Thread {thread_id}: Retained-tail rebuild failed: {e}", exc_info=True)
            return {"success": False, "reason": str(e)}

        compact_model = self._model_for(thread_id)
        agent._token_tracker.reset_after_compact(
            thread_id,
            self._estimate_messages_tokens(tail, compact_model),
            remaining_message_count=len(tail),
            context_model=compact_model,
        )
        logger.info(
            f"Thread {thread_id}: Compaction complete, summarized "
            f"{conversational_removed} conversation messages "
            f"({len(pre_ids)} checkpoint objects removed), "
            f"retained {len(tail)} (resume opener + memory read-back)"
        )
        return {
            "success": True,
            "messages_before": conversational_before,
            "messages_after": conversational_after,
            "messages_removed": conversational_removed,
            "auto_resumed": auto_resumed,
            "summary": summary,
        }

    async def _discard_turn_delta(self, graph, config: dict, pre_ids: set) -> None:
        """Drop messages a failed compaction turn added (injected prompt + partial turn)."""
        try:
            post_messages = self._state_messages(await graph.aget_state(config))
            delta = [RemoveMessage(id=m.id) for m in post_messages if m.id not in pre_ids]
            if delta:
                await graph.aupdate_state(config, {"messages": delta})
        except Exception as e:
            logger.warning(f"Thread {config}: Failed to discard compaction-turn delta: {e}")

    def _run_compact_turn_and_prune_sync(
        self, thread_id: str, user_id: str, *, auto_resumed: bool
    ) -> Dict[str, Any]:
        """Sync sibling of :meth:`_run_compact_turn_and_prune`."""
        from .agent_memory_seed import build_resume_compaction_tail

        agent = self._agent
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}
        graph = agent._default_graph

        pre_messages = self._state_messages(graph.get_state(config))
        pre_ids = {m.id for m in pre_messages}
        conversational_before = self._count_conversational_messages(pre_messages)

        summary = self._generate_summary_sync(thread_id, user_id)
        if not summary:
            self._discard_turn_delta_sync(graph, config, pre_ids)
            return {"success": False, "reason": "Failed to generate summary"}

        tail = build_resume_compaction_tail(
            user_id=user_id, thread_id=thread_id, summary=summary
        )
        conversational_after = self._count_conversational_messages(tail)
        conversational_removed = max(0, conversational_before - conversational_after)
        self._stamp_seed_marker(
            tail[0], summary=summary, messages_removed=conversational_removed,
            auto_resumed=auto_resumed,
        )

        try:
            post_messages = self._state_messages(graph.get_state(config))
            remove = [RemoveMessage(id=m.id) for m in post_messages]
            graph.update_state(config, {"messages": remove + tail})

            verify_state = graph.get_state(config)
            if not self._verify_retained(thread_id, verify_state, len(tail)):
                return {"success": False, "reason": "Retained-tail verification failed"}

            try:
                post_cp_id = self._state_checkpoint_id(verify_state)
                if post_cp_id:
                    cp_tuple = graph.checkpointer.get_tuple({
                        "configurable": {"thread_id": thread_id, "checkpoint_id": post_cp_id}
                    })
                    self._prune_old_checkpoints(thread_id, verify_state, cp_tuple)
            except Exception as e:
                logger.warning(f"Thread {thread_id}: Pruning call failed (sync): {e}")
        except Exception as e:
            logger.error(f"Thread {thread_id}: Sync retained-tail rebuild failed: {e}", exc_info=True)
            return {"success": False, "reason": str(e)}

        compact_model = self._model_for(thread_id)
        agent._token_tracker.reset_after_compact(
            thread_id,
            self._estimate_messages_tokens(tail, compact_model),
            remaining_message_count=len(tail),
            context_model=compact_model,
        )
        logger.info(
            f"Thread {thread_id}: Sync compaction complete, summarized "
            f"{conversational_removed} conversation messages "
            f"({len(pre_ids)} checkpoint objects removed), retained {len(tail)}"
        )
        return {
            "success": True,
            "messages_before": conversational_before,
            "messages_after": conversational_after,
            "messages_removed": conversational_removed,
            "auto_resumed": auto_resumed,
            "summary": summary,
        }

    def _discard_turn_delta_sync(self, graph, config: dict, pre_ids: set) -> None:
        try:
            post_messages = self._state_messages(graph.get_state(config))
            delta = [RemoveMessage(id=m.id) for m in post_messages if m.id not in pre_ids]
            if delta:
                graph.update_state(config, {"messages": delta})
        except Exception as e:
            logger.warning(f"Thread {config}: Failed to discard compaction-turn delta (sync): {e}")

    # ------------------------------------------------------------------
    # Overflow recovery
    # ------------------------------------------------------------------

    async def rewind_and_compact(
        self,
        thread_id: str,
        user_id: str = "default",
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Dict[str, Any]:
        """Recover from provider context overflow by rewinding before compaction.

        The current oversized state is flushed to RAG first. We then fork the
        thread from an older checkpoint so summary generation can run against a
        smaller active history, and finally use the normal compact_now() path.
        """
        agent = self._agent
        graph = agent._default_async_graph
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        try:
            state = await graph.aget_state(config)
            messages = state.values.get("messages", [])
            msg_count_before = len(messages)
            if not messages:
                return {"success": False, "reason": "No messages to compact"}

            logger.warning(
                "Thread %s: Context overflow recovery starting (%s messages)",
                thread_id,
                msg_count_before,
            )
            self._flush_recovery_messages(user_id, thread_id, messages)

            rewound = await self._rewind_to_older_checkpoint(
                graph,
                config,
                thread_id,
                user_id,
                messages,
            )
            if not rewound:
                logger.warning(
                    "Thread %s: No suitable rewind checkpoint; trimming oldest messages",
                    thread_id,
                )
                trimmed = await self._direct_trim_for_recovery(
                    graph,
                    config,
                    thread_id,
                )
                if not trimmed:
                    return {
                        "success": False,
                        "reason": "No suitable checkpoint and direct trim failed",
                    }

            result = await self.compact_now(
                thread_id,
                user_id,
                on_started=on_started,
            )
            if result.get("success"):
                result["overflow_recovery"] = True
                result["rewound"] = bool(rewound)
                result["messages_before_overflow_recovery"] = msg_count_before
                return result

            if rewound:
                logger.warning(
                    "Thread %s: Compaction after rewind failed (%s); trying direct trim",
                    thread_id,
                    result.get("reason", result),
                )
                trimmed = await self._direct_trim_for_recovery(
                    graph,
                    config,
                    thread_id,
                )
                if trimmed:
                    result = await self.compact_now(
                        thread_id,
                        user_id,
                        on_started=on_started,
                    )
                    if result.get("success"):
                        result["overflow_recovery"] = True
                        result["rewound"] = True
                        result["direct_trim_after_rewind"] = True
                        result["messages_before_overflow_recovery"] = msg_count_before
                        return result

            return result
        except Exception as e:
            logger.error(
                "Thread %s: Context overflow recovery failed: %s",
                thread_id,
                e,
                exc_info=True,
            )
            return {"success": False, "reason": str(e)}

    def rewind_and_compact_sync(
        self,
        thread_id: str,
        user_id: str = "default",
        *,
        on_started: Optional[CompactionStartCallback] = None,
    ) -> Dict[str, Any]:
        """Sync version of rewind_and_compact() for chat()/CLI callers."""
        agent = self._agent
        graph = agent._default_graph
        config = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

        try:
            state = graph.get_state(config)
            messages = state.values.get("messages", [])
            msg_count_before = len(messages)
            if not messages:
                return {"success": False, "reason": "No messages to compact"}

            logger.warning(
                "Thread %s: Sync context overflow recovery starting (%s messages)",
                thread_id,
                msg_count_before,
            )
            self._flush_recovery_messages(user_id, thread_id, messages)

            rewound = self._rewind_to_older_checkpoint_sync(
                graph,
                config,
                thread_id,
                user_id,
                messages,
            )
            if not rewound:
                logger.warning(
                    "Thread %s: No suitable sync rewind checkpoint; trimming oldest messages",
                    thread_id,
                )
                trimmed = self._direct_trim_for_recovery_sync(
                    graph,
                    config,
                    thread_id,
                )
                if not trimmed:
                    return {
                        "success": False,
                        "reason": "No suitable checkpoint and direct trim failed",
                    }

            result = self._do_compact_sync(
                thread_id,
                user_id,
                on_started=on_started,
            )
            if result.get("success"):
                result["overflow_recovery"] = True
                result["rewound"] = bool(rewound)
                result["messages_before_overflow_recovery"] = msg_count_before
                return result

            if rewound:
                logger.warning(
                    "Thread %s: Sync compaction after rewind failed (%s); trying direct trim",
                    thread_id,
                    result.get("reason", result),
                )
                trimmed = self._direct_trim_for_recovery_sync(
                    graph,
                    config,
                    thread_id,
                )
                if trimmed:
                    result = self._do_compact_sync(
                        thread_id,
                        user_id,
                        on_started=on_started,
                    )
                    if result.get("success"):
                        result["overflow_recovery"] = True
                        result["rewound"] = True
                        result["direct_trim_after_rewind"] = True
                        result["messages_before_overflow_recovery"] = msg_count_before
                        return result

            return result
        except Exception as e:
            logger.error(
                "Thread %s: Sync context overflow recovery failed: %s",
                thread_id,
                e,
                exc_info=True,
            )
            return {"success": False, "reason": str(e)}

    def _flush_recovery_messages(
        self,
        user_id: str,
        thread_id: str,
        messages: List[Any],
    ) -> None:
        """Flush the current oversized state to RAG before any rewind/trim."""
        try:
            self._agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(
                "Thread %s: Overflow recovery RAG flush failed: %s",
                thread_id,
                e,
            )

    async def _rewind_to_older_checkpoint(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        rewind_state = await self._find_rewind_state(graph, config, current_messages)
        if rewind_state is None:
            return False
        return await self._fork_from_rewind_state(
            graph,
            rewind_state,
            thread_id,
            user_id,
            current_messages,
        )

    def _rewind_to_older_checkpoint_sync(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        rewind_state = self._find_rewind_state_sync(graph, config, current_messages)
        if rewind_state is None:
            return False
        return self._fork_from_rewind_state_sync(
            graph,
            rewind_state,
            thread_id,
            user_id,
            current_messages,
        )

    async def _find_rewind_state(
        self,
        graph: Any,
        config: Dict[str, Any],
        current_messages: List[Any],
    ) -> Optional[Any]:
        min_messages = self._agent.settings.compact_keep_messages
        current_turns = self._count_user_turns(current_messages)
        target_turns = max(0, current_turns - 4)
        fallback_state: Optional[Any] = None

        try:
            async for state in graph.aget_state_history(config, limit=50):
                messages = self._state_messages(state)
                checkpoint_id = self._state_checkpoint_id(state)
                if not checkpoint_id or not messages:
                    continue
                if len(messages) >= len(current_messages):
                    continue
                if len(messages) < min_messages:
                    continue
                if fallback_state is None:
                    fallback_state = state
                if self._count_user_turns(messages) <= target_turns:
                    return state
        except Exception as e:
            logger.warning("Failed to scan async checkpoint history: %s", e)

        return fallback_state

    def _find_rewind_state_sync(
        self,
        graph: Any,
        config: Dict[str, Any],
        current_messages: List[Any],
    ) -> Optional[Any]:
        min_messages = self._agent.settings.compact_keep_messages
        current_turns = self._count_user_turns(current_messages)
        target_turns = max(0, current_turns - 4)
        fallback_state: Optional[Any] = None

        try:
            for state in graph.get_state_history(config, limit=50):
                messages = self._state_messages(state)
                checkpoint_id = self._state_checkpoint_id(state)
                if not checkpoint_id or not messages:
                    continue
                if len(messages) >= len(current_messages):
                    continue
                if len(messages) < min_messages:
                    continue
                if fallback_state is None:
                    fallback_state = state
                if self._count_user_turns(messages) <= target_turns:
                    return state
        except Exception as e:
            logger.warning("Failed to scan sync checkpoint history: %s", e)

        return fallback_state

    async def _fork_from_rewind_state(
        self,
        graph: Any,
        rewind_state: Any,
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        checkpoint_id = self._state_checkpoint_id(rewind_state)
        if checkpoint_id is None:
            return False
        rewind_messages = self._state_messages(rewind_state)
        fork_config = {
            "configurable": {
                "thread_id": thread_id,
                "user_id": user_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        marker = self._create_rewind_marker(
            messages_before=len(current_messages),
            messages_after=len(rewind_messages),
        )
        await graph.aupdate_state(fork_config, {"messages": [marker]})
        logger.warning(
            "Thread %s: Rewound context from %s to %s messages at checkpoint %s",
            thread_id,
            len(current_messages),
            len(rewind_messages),
            checkpoint_id,
        )
        return True

    def _fork_from_rewind_state_sync(
        self,
        graph: Any,
        rewind_state: Any,
        thread_id: str,
        user_id: str,
        current_messages: List[Any],
    ) -> bool:
        checkpoint_id = self._state_checkpoint_id(rewind_state)
        if checkpoint_id is None:
            return False
        rewind_messages = self._state_messages(rewind_state)
        fork_config = {
            "configurable": {
                "thread_id": thread_id,
                "user_id": user_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        marker = self._create_rewind_marker(
            messages_before=len(current_messages),
            messages_after=len(rewind_messages),
        )
        graph.update_state(fork_config, {"messages": [marker]})
        logger.warning(
            "Thread %s: Sync rewound context from %s to %s messages at checkpoint %s",
            thread_id,
            len(current_messages),
            len(rewind_messages),
            checkpoint_id,
        )
        return True

    async def _direct_trim_for_recovery(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
    ) -> bool:
        state = await graph.aget_state(config)
        messages = state.values.get("messages", [])
        remove_count = self._recovery_prefix_remove_count(thread_id, messages)
        if remove_count <= 0:
            return False

        remove_commands = [
            RemoveMessage(id=msg.id)
            for msg in messages[:remove_count]
            if getattr(msg, "id", None)
        ]
        if not remove_commands:
            return False

        await graph.aupdate_state(config, {"messages": remove_commands})
        logger.warning(
            "Thread %s: Direct overflow trim removed %s oldest messages",
            thread_id,
            len(remove_commands),
        )
        return True

    def _direct_trim_for_recovery_sync(
        self,
        graph: Any,
        config: Dict[str, Any],
        thread_id: str,
    ) -> bool:
        state = graph.get_state(config)
        messages = state.values.get("messages", [])
        remove_count = self._recovery_prefix_remove_count(thread_id, messages)
        if remove_count <= 0:
            return False

        remove_commands = [
            RemoveMessage(id=msg.id)
            for msg in messages[:remove_count]
            if getattr(msg, "id", None)
        ]
        if not remove_commands:
            return False

        graph.update_state(config, {"messages": remove_commands})
        logger.warning(
            "Thread %s: Sync direct overflow trim removed %s oldest messages",
            thread_id,
            len(remove_commands),
        )
        return True

    def _recovery_prefix_remove_count(
        self,
        thread_id: str,
        messages: List[Any],
    ) -> int:
        min_messages = self._agent.settings.compact_keep_messages
        if len(messages) <= min_messages:
            return 0

        max_prefix = len(messages) - min_messages
        boundaries = [
            idx
            for idx in range(1, max_prefix + 1)
            if idx == max_prefix or isinstance(messages[idx], HumanMessage)
        ]
        if not boundaries:
            boundaries = list(range(1, max_prefix + 1))

        target_tokens = self._recovery_target_tokens(thread_id)
        model = self._model_for(thread_id)
        current_tokens = self._estimate_messages_tokens(messages, model)
        if current_tokens <= target_tokens:
            return boundaries[0]

        for idx in boundaries:
            if self._estimate_messages_tokens(messages[idx:], model) <= target_tokens:
                return idx

        return boundaries[-1]

    def _recovery_target_tokens(self, thread_id: str) -> int:
        llm_config = self._agent._get_llm_config_for_thread(thread_id)
        model_limit = get_context_limit(llm_config.model)
        ratio = min(float(self._agent.settings.compact_threshold), 0.5)
        return max(1, int(model_limit * ratio))

    def _model_for(self, thread_id: str) -> str:
        """Best-effort model id for image-token sizing; '' if unavailable."""
        try:
            return self._agent._get_llm_config_for_thread(thread_id).model or ""
        except Exception:  # noqa: BLE001 - sizing falls back to a flat estimate
            return ""

    @staticmethod
    def _estimate_messages_tokens(messages: List[Any], model: str = "") -> int:
        total = 0
        for msg in messages:
            total += _estimate_content_tokens(
                getattr(msg, "content", ""), model, _message_image_dims(msg)
            )
        return total

    @staticmethod
    def _count_user_turns(messages: List[Any]) -> int:
        turns = 0
        for msg in messages:
            if not isinstance(msg, HumanMessage):
                continue
            if getattr(msg, "additional_kwargs", {}).get("internal"):
                continue
            turns += 1
        return turns

    @staticmethod
    def _state_messages(state: Any) -> List[Any]:
        values = getattr(state, "values", {}) or {}
        messages = values.get("messages", [])
        return messages if isinstance(messages, list) else []

    @staticmethod
    def _ai_message_has_text(msg: Any) -> bool:
        """True if an AIMessage carries visible reply text.

        Tool-call-only and thinking-only assistant steps have no user-visible
        text, so they are intermediate steps rather than replies.
        """
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    if block.strip():
                        return True
                elif isinstance(block, dict):
                    if block.get("type") == "text" and str(block.get("text") or "").strip():
                        return True
            return False
        return bool(content)

    @staticmethod
    def _count_conversational_messages(messages: List[Any]) -> int:
        """Count user-visible conversation messages in a message list.

        Counts real user turns plus assistant replies that produced visible
        text. Excludes tool-call and tool-result traffic, thinking-only or
        tool-call-only assistant steps, and system-generated scaffolding
        (memory-seed openers, autonomous wake-ups, compaction prompts: anything
        flagged ``internal``). This is what the post-compaction notice reports,
        so the number reflects the conversation rather than the raw checkpoint
        object count (which counts every intermediate tool call and result).
        """
        count = 0
        for m in messages:
            kwargs = getattr(m, "additional_kwargs", None) or {}
            if kwargs.get("internal"):
                continue
            if isinstance(m, HumanMessage):
                count += 1
            elif isinstance(m, AIMessage) and CompactionManager._ai_message_has_text(m):
                count += 1
        return count

    @staticmethod
    def _state_checkpoint_id(state: Any) -> Optional[str]:
        config = getattr(state, "config", None)
        if not isinstance(config, dict):
            return None
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            return None
        checkpoint_id = configurable.get("checkpoint_id")
        return str(checkpoint_id) if checkpoint_id else None

    @staticmethod
    def _create_rewind_marker(
        *,
        messages_before: int,
        messages_after: int,
    ) -> HumanMessage:
        from .agent import _create_human_message

        marker = _create_human_message(
            "Context overflow recovery: the active thread state was rewound "
            "to an earlier checkpoint before compaction because the full "
            "context exceeded the model window. Newer messages were flushed "
            "to conversation memory before this rewind.",
            internal=True,
            internal_type="context_rewind",
        )
        marker.id = str(_uuid.uuid4())
        marker.additional_kwargs.update({
            "messages_before_rewind": messages_before,
            "messages_after_rewind": messages_after,
            "timestamp": utc_now().isoformat(),
        })
        return marker

    # ------------------------------------------------------------------
    # Thread deletion cleanup
    # ------------------------------------------------------------------

    def clear_thread_state(self, thread_id: str) -> int:
        """No-op since compaction no longer keeps in-memory pending state.

        The retained turn lives in the checkpoint, which thread deletion drops
        with the rest of the thread. Kept as a stable hook for
        ``thread_deletion`` (which expects an int count of cleared items).
        """
        return 0
