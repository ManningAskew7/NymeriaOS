"""Thin-client helpers for Nymeria's public MCP server.

The MCP server should not construct its own ``NymeriaAgent``.  This module
contains the small HTTP/SSE adapter it uses to talk to the already-running
Nymeria API, plus transcript formatting that mirrors the desktop copy buttons.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional, Sequence
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)
TranscriptVerbosity = Literal["verbose", "concise", "chat"]
_VALID_TRANSCRIPT_VERBOSITIES = {"verbose", "concise", "chat"}


class NymeriaAPIError(RuntimeError):
    """Raised when the backend API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": str(self),
            "status_code": self.status_code,
            "payload": self.payload,
        }


def _coerce_error_payload(response: httpx.Response, body: Optional[str] = None) -> tuple[str, Any]:
    payload: Any = None
    text = body
    if text is None:
        text = response.text
    try:
        payload = response.json()
    except Exception:
        if text:
            try:
                payload = json.loads(text)
            except Exception:
                payload = text
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail, payload
        return json.dumps(payload, ensure_ascii=False), payload
    if isinstance(payload, str) and payload:
        return payload, payload
    return f"Nymeria API returned HTTP {response.status_code}", payload


class NymeriaBackendClient:
    """Async HTTP client for the Nymeria REST/SSE API."""

    def __init__(self, base_url: str, service_token: str, client_id: str = "nymeria-mcp"):
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.client_id = client_id

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{path}"

    def _headers(self, act_as: Optional[str] = None, accept: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "Content-Type": "application/json",
            "X-Nymeria-Client-Id": self.client_id,
        }
        if act_as:
            headers["X-Nymeria-Act-As"] = act_as
        if accept:
            headers["Accept"] = accept
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        act_as: Optional[str] = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> Any:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                self._url(path),
                headers=self._headers(act_as=act_as),
                json=json_body,
                params={k: v for k, v in (params or {}).items() if v is not None},
            )

        if response.status_code >= 400:
            message, payload = _coerce_error_payload(response)
            raise NymeriaAPIError(response.status_code, message, payload)
        if response.status_code == 204 or not response.content:
            return {"status": "ok"}
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        try:
            return response.json()
        except Exception:
            return {"status": "ok", "body": response.text}

    async def get(self, path: str, *, params: Optional[Dict[str, Any]] = None, act_as: Optional[str] = None) -> Any:
        return await self.request("GET", path, params=params, act_as=act_as)

    async def post(
        self,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        act_as: Optional[str] = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
    ) -> Any:
        return await self.request("POST", path, json_body=json_body, params=params, act_as=act_as, timeout=timeout)

    async def patch(
        self,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        act_as: Optional[str] = None,
    ) -> Any:
        return await self.request("PATCH", path, json_body=json_body, params=params, act_as=act_as)

    async def put(
        self,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        act_as: Optional[str] = None,
    ) -> Any:
        return await self.request("PUT", path, json_body=json_body, params=params, act_as=act_as)

    async def delete(self, path: str, *, params: Optional[Dict[str, Any]] = None, act_as: Optional[str] = None) -> Any:
        return await self.request("DELETE", path, params=params, act_as=act_as)

    async def stream_chat(
        self,
        *,
        message: str,
        user_id: str,
        thread_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        force_unsupported_attachments: bool = False,
        is_self_invoke: bool = False,
        trigger_override: Optional[str] = None,
        source: Optional[str] = "mcp",
        source_id: Optional[str] = None,
        source_label: Optional[str] = "mcp-client",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        body: Dict[str, Any] = {
            "message": message,
            "thread_id": thread_id,
            "user_id": user_id,
            "stream": True,
        }
        if attachments:
            body["attachments"] = attachments
        if force_unsupported_attachments:
            body["force_unsupported_attachments"] = True
        if is_self_invoke:
            body["is_self_invoke"] = True
        if trigger_override:
            body["trigger_override"] = trigger_override
        if source:
            body["source"] = source
        if source_id:
            body["source_id"] = source_id
        if source_label:
            body["source_label"] = source_label

        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT) as client:
            async with client.stream(
                "POST",
                self._url("/chat"),
                headers=self._headers(act_as=user_id, accept="text/event-stream"),
                json=body,
            ) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    message_text, payload = _coerce_error_payload(response, raw)
                    raise NymeriaAPIError(response.status_code, message_text, payload)

                async for event in _iter_sse_events(response):
                    yield event

    async def stream_turn_replay(
        self,
        *,
        thread_id: str,
        user_id: str,
        turn_id: Optional[str] = None,
        from_seq: int = 0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Re-attach to a thread's in-flight or just-finished turn.

        Replays the turn's buffered events (byte-identical to the original
        ``POST /chat`` stream, each stamped with ``seq``) and then tails live.
        This is the SERVER's copy of a turn, which is what makes a resumed
        collect survive the MCP process forgetting a dispatch.

        Raises :class:`NymeriaAPIError` with 404 when there is nothing to
        attach to (no recent turn, or the buffer expired) and 410 when
        overflow evicted events after ``from_seq``. Both are meaningful to a
        caller and must not be flattened into a generic failure: 404 means go
        read history instead, 410 means the record has a hole in it.
        """
        params = {"from_seq": str(max(0, int(from_seq)))}
        if turn_id:
            params["turn_id"] = turn_id
        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT) as client:
            async with client.stream(
                "GET",
                self._url(f"/threads/{quote(str(thread_id), safe='')}/turn/stream"),
                headers=self._headers(act_as=user_id, accept="text/event-stream"),
                params=params,
            ) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    message_text, payload = _coerce_error_payload(response, raw)
                    raise NymeriaAPIError(response.status_code, message_text, payload)
                async for event in _iter_sse_events(response):
                    yield event


async def _iter_sse_events(response: httpx.Response) -> AsyncGenerator[Dict[str, Any], None]:
    """Yield decoded JSON objects from a text/event-stream response.

    Shared by the chat stream and the turn-replay stream so both tolerate the
    same wire noise: keepalive comment frames, the ``[DONE]`` sentinel, and
    blank separator lines. A malformed frame is skipped rather than fatal,
    because one bad payload should not discard a turn's worth of good ones.
    """
    async for line in response.aiter_lines():
        if not line or not line.startswith("data: "):
            continue
        raw = line[6:].strip()
        if not raw or raw == "[DONE]" or raw.startswith(":"):
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Ignoring malformed SSE payload from Nymeria API: %s", raw)
            continue
        if isinstance(parsed, dict):
            yield parsed


def _merge_or_append_text_step(steps: List[Dict[str, Any]], step_type: str, content: str) -> None:
    if not content:
        return
    if steps and steps[-1].get("type") == step_type and "content" in steps[-1]:
        steps[-1]["content"] = f"{steps[-1].get('content', '')}{content}"
        return
    steps.append({"type": step_type, "content": content})


def _tool_result_status(result: str) -> str:
    """"error" when a tool result carries the failure convention, else "success".

    Tools in this codebase return failures as text rather than raising, so the
    prefix is the only signal a transcript consumer has. Matched after a strip
    because some results arrive with leading whitespace.
    """
    return "error" if result.lstrip().startswith("[Error]") else "success"


def _find_tool_step(steps: List[Dict[str, Any]], tool_call_id: Optional[str], name: Optional[str]) -> Optional[Dict[str, Any]]:
    if tool_call_id:
        for step in reversed(steps):
            if step.get("type") == "tool_call" and step.get("id") == tool_call_id:
                return step
    if name:
        for step in reversed(steps):
            if step.get("type") == "tool_call" and step.get("name") == name and "result" not in step:
                return step
    return None


def normalize_transcript_verbosity(verbosity: str = "verbose") -> TranscriptVerbosity:
    """Normalize public MCP transcript verbosity names."""
    normalized = (verbosity or "verbose").strip().lower()
    if normalized == "full":
        normalized = "verbose"
    if normalized not in _VALID_TRANSCRIPT_VERBOSITIES:
        raise ValueError(
            "verbosity must be one of: verbose, concise, chat"
        )
    return normalized  # type: ignore[return-value]


def project_steps_for_verbosity(
    steps: List[Dict[str, Any]],
    verbosity: str = "verbose",
) -> List[Dict[str, Any]]:
    """Return assistant steps shaped for an MCP transcript verbosity."""
    mode = normalize_transcript_verbosity(verbosity)
    if mode == "verbose":
        return copy.deepcopy(steps)
    if mode == "chat":
        return [
            {"type": "response", "content": str(step.get("content") or "")}
            for step in steps
            if isinstance(step, dict)
            and step.get("type") == "response"
            and step.get("content")
        ]

    projected: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        if step_type in {"thinking", "response"}:
            content = step.get("content")
            if content:
                projected.append({"type": step_type, "content": str(content)})
        elif step_type == "tool_call":
            tool_step = {
                "type": "tool_call",
                "name": step.get("name") or "unknown",
            }
            if step.get("status"):
                tool_step["status"] = step["status"]
            projected.append(tool_step)
    return projected


def message_steps_to_markdown(
    steps: List[Dict[str, Any]],
    verbosity: str = "verbose",
) -> str:
    """Mirror desktop ``messageToMarkdown`` for assistant message steps."""
    mode = normalize_transcript_verbosity(verbosity)
    sections: List[str] = []
    for step in steps:
        step_type = step.get("type")
        if step_type == "thinking" and step.get("content"):
            if mode == "chat":
                continue
            quoted = "\n".join(f"> {line}" for line in str(step["content"]).split("\n"))
            sections.append(f"> *Thinking:*\n{quoted}")
        elif step_type == "tool_call":
            block = f"### Tool: {step.get('name') or 'unknown'}"
            if mode == "chat":
                continue
            if mode == "verbose":
                arguments = step.get("arguments")
                if isinstance(arguments, dict) and arguments:
                    block += f"\n\n**Arguments:**\n```json\n{json.dumps(arguments, indent=2, ensure_ascii=False)}\n```"
                if step.get("result"):
                    block += f"\n\n**Result:**\n```\n{step['result']}\n```"
                artifacts = step.get("artifacts")
                if isinstance(artifacts, list) and artifacts:
                    block += f"\n\n**Artifacts:**\n```json\n{json.dumps(artifacts, indent=2, ensure_ascii=False)}\n```"
            sections.append(block)
        elif step_type == "response" and step.get("content"):
            sections.append(str(step["content"]))
    return "\n\n---\n\n".join(sections) if sections else "(empty message)"


def message_steps_to_response_text(steps: List[Dict[str, Any]]) -> str:
    """Mirror desktop ``messageToResponseText`` for assistant message steps."""
    last_non_response_idx = -1
    for idx in range(len(steps) - 1, -1, -1):
        if steps[idx].get("type") != "response":
            last_non_response_idx = idx
            break

    trailing_parts = [
        str(step.get("content", ""))
        for step in steps[last_non_response_idx + 1 :]
        if step.get("type") == "response" and step.get("content")
    ]
    if trailing_parts:
        return "\n\n".join(trailing_parts)

    fallback_parts = [
        str(step.get("content", ""))
        for step in steps
        if step.get("type") == "response" and step.get("content")
    ]
    return "\n\n".join(fallback_parts) if fallback_parts else "(empty message)"


def _enc(value: str) -> str:
    return quote(str(value), safe="")


@dataclass
class ChatTranscript:
    """Accumulates Nymeria chat SSE events into desktop-compatible output."""

    thread_id: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    raw_events: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    context_stats: Optional[Dict[str, Any]] = None
    halted: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    title: Optional[str] = None
    title_source: Optional[str] = None
    done: bool = False

    def add_event(self, event: Dict[str, Any], *, keep_raw: bool = False) -> None:
        if keep_raw:
            self.raw_events.append(event)
        if event.get("thread_id"):
            self.thread_id = str(event["thread_id"])

        event_type = event.get("type")
        if event_type == "thinking":
            _merge_or_append_text_step(self.steps, "thinking", str(event.get("content") or event.get("message") or ""))
        elif event_type == "response":
            _merge_or_append_text_step(self.steps, "response", str(event.get("content") or ""))
        elif event_type == "tool_call":
            self.steps.append(
                {
                    "type": "tool_call",
                    "id": event.get("id"),
                    "name": event.get("name") or "unknown",
                    "arguments": event.get("args") or event.get("arguments") or {},
                    "status": "running",
                }
            )
        elif event_type == "tool_result":
            tool_call_id = event.get("id") or event.get("tool_call_id")
            name = event.get("name") or event.get("tool_name")
            step = _find_tool_step(self.steps, tool_call_id, name)
            if step is None:
                step = {
                    "type": "tool_call",
                    "id": tool_call_id,
                    "name": name or "unknown",
                    "arguments": {},
                }
                self.steps.append(step)
            step["result"] = str(event.get("result") or "")
            # The backend's tool_result chunk carries no status (see
            # agent_streaming's tool-end chunk), so defaulting to "success"
            # made this field a CONSTANT: a refused or failed call read as a
            # success to every MCP caller. Nymeria's tools report failure as a
            # result string rather than by raising, prefixed "[Error]", which
            # is the same convention agent_prune keys on. An explicit status
            # from a producer that has one still wins.
            step["status"] = event.get("status") or _tool_result_status(step["result"])
        elif event_type == "workspace_artifact":
            tool_call_id = event.get("tool_call_id") or event.get("toolCallId")
            name = event.get("tool_name") or event.get("toolName")
            artifact = {
                k: v
                for k, v in event.items()
                if k
                not in {
                    "type",
                    "thread_id",
                    "tool_call_id",
                    "toolCallId",
                    "tool_name",
                    "toolName",
                }
            }
            step = _find_tool_step(self.steps, tool_call_id, name)
            if step is not None and artifact:
                step.setdefault("artifacts", []).append(artifact)
        elif event_type == "iteration_limit":
            # Deliberately NOT merged into a response step. Doing that made the
            # runtime's halt notice indistinguishable from the model's own
            # words: the turn read as a completed answer that happened to end
            # by mentioning a limit. A halt is a property of the TURN, so it is
            # recorded as one and surfaced by _with_halt_note.
            self.halted = {
                "message": str(
                    event.get("content")
                    or event.get("message")
                    or "Agent reached the maximum number of steps."
                ),
                "reason": event.get("reason"),
                "max_iterations": event.get("max_iterations"),
                "resumable": bool(event.get("resumable")),
            }
        elif event_type == "error":
            self.errors.append(
                {
                    "message": event.get("content") or event.get("error") or event.get("message") or "Unknown error",
                    "code": event.get("code"),
                    "details": event.get("details"),
                }
            )
        elif event_type == "done":
            self.done = True
            self.context_stats = event.get("context_stats")
            self.model = event.get("model")
            self.title = event.get("title")
            self.title_source = event.get("title_source")

    def as_dict(
        self,
        *,
        include_events: bool = False,
        verbosity: str = "verbose",
    ) -> Dict[str, Any]:
        mode = normalize_transcript_verbosity(verbosity)
        return project_chat_payload_for_verbosity(
            self.unprojected(include_events=include_events, verbosity=mode), mode
        )

    def unprojected(
        self,
        *,
        include_events: bool = False,
        verbosity: str = "verbose",
    ) -> Dict[str, Any]:
        """The accumulated payload BEFORE any end-of-turn projection.

        A caller that has more to do to the payload (notably the
        persisted-steps overlay) must work on this, not on ``as_dict``'s
        output. The projection rewrites ``final_response`` to carry the failure
        line and the halt note, and the overlay matches the streamed answer
        against the persisted one to decide whether they are the same turn, so
        projecting first made a halted turn fail that comparison and silently
        lose its canonical step order. Taking this seam also means the shipped
        path projects ONCE instead of twice.
        """
        mode = normalize_transcript_verbosity(verbosity)
        full_steps = copy.deepcopy(self.steps)
        payload: Dict[str, Any] = {
            "thread_id": self.thread_id,
            "final_response": message_steps_to_response_text(self.steps),
            "full_markdown": message_steps_to_markdown(full_steps, "verbose"),
            "steps": full_steps,
            "errors": self.errors,
            "done": self.done,
            "context_stats": self.context_stats,
            "halted": self.halted,
            "model": self.model,
            "verbosity": mode,
        }
        if self.title:
            payload["title"] = self.title
            payload["title_source"] = self.title_source
        if include_events and mode == "verbose":
            payload["events"] = self.raw_events
        return payload


async def transcript_from_events(
    client: Optional[NymeriaBackendClient],
    events: Sequence[Dict[str, Any]],
    *,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    message: Optional[str] = None,
    include_events: bool = False,
    verbosity: str = "verbose",
) -> Dict[str, Any]:
    """Render collected SSE events into a transcript payload.

    THE renderer: accumulate -> ``as_dict`` -> persisted-steps overlay ->
    verbosity projection. Every MCP chat path feeds it, so a caller cannot tell
    whether a transcript arrived inline or was resumed from a background
    dispatch. That indistinguishability is structural rather than asserted, and
    it is what lets a resumed result be trusted like an inline one. Before it
    existed the background pair returned the raw SSE event list while only the
    blocking tool got a rendered transcript, so the tool that survived a long
    turn was the one that handed back something unreadable.

    The persisted-steps overlay is applied ONLY to a finished turn, and only
    when the caller supplies the identity and prompt needed to find it. It
    exists to prefer the checkpointer's canonical step ORDER over raw SSE
    arrival order for parallel tool calls; mid-turn there is no persisted
    assistant message to prefer, so on a partial it would at best be a wasted
    round trip and at worst match the PREVIOUS turn's message. Passing a
    ``client`` of ``None`` skips it outright for callers that have no backend
    handle.
    """
    mode = normalize_transcript_verbosity(verbosity)
    transcript = ChatTranscript(thread_id=thread_id)
    for event in events:
        transcript.add_event(event, keep_raw=include_events)
    payload = transcript.unprojected(include_events=include_events, verbosity="verbose")
    if client is not None and user_id is not None and message is not None and transcript.done:
        payload = await _apply_persisted_assistant_steps(
            client, payload, user_id=user_id, message=message
        )
    return project_chat_payload_for_verbosity(payload, mode)


def _with_failure_line(
    result: Dict[str, Any], steps: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Render a failed turn's error as the response text, in place.

    A turn with zero response steps and recorded errors surfaces
    ``[turn failed] <error>`` instead of the "(empty message)" placeholder,
    which buried the real signal (``errors[]``) for MCP callers (live
    2026-08-10). Applied at the END of ``project_chat_payload_for_verbosity``,
    the one stage every verbosity mode and call order passes through last:
    earlier stages (``as_dict``, the persisted-steps overlay, the per-mode
    recompute above) all rebuild ``final_response``/``full_markdown`` from
    steps and would silently undo an earlier synthesis.
    """
    errors = result.get("errors") or []
    if not errors:
        return result
    if any(
        step.get("type") == "response" and step.get("content") for step in steps
    ):
        return result
    first = errors[0] if isinstance(errors[0], dict) else {}
    failure = f"[turn failed] {first.get('message') or 'Unknown error'}"
    if "final_response" in result:
        result["final_response"] = failure
    if "full_markdown" in result:
        # Per-field idempotent: the projection runs twice on the shipped
        # path (as_dict's own tail plus the renderer's), and a
        # second application must repair a still-placeholder markdown
        # without appending a duplicate failure section.
        markdown = str(result["full_markdown"] or "")
        if markdown in ("", "(empty message)"):
            result["full_markdown"] = failure
        elif not markdown.endswith(failure):
            result["full_markdown"] = f"{markdown}\n\n---\n\n{failure}"
    return result


def _with_halt_note(
    result: Dict[str, Any], halted: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Make a halted turn impossible to mistake for a completed one.

    A turn stopped by a safety limit has produced SOME output, which is what
    makes it dangerous to report plainly: it reads like a finished answer. So
    the answer and the markdown both carry an explicit incompleteness note, and
    the structured ``halted`` block stays on the payload for callers that
    branch rather than read prose. Mirrors the callable-thread contract, which
    appends "[Note: This response may be incomplete ...]" for the same reason.

    Per-field idempotent, for the same reason :func:`_with_failure_line` is:
    the projection runs TWICE on the shipped path (``as_dict``'s own tail, then
    the renderer's), so a second application must not append a second note.
    """
    if not isinstance(halted, dict):
        return result
    result["halted"] = copy.deepcopy(halted)
    message = str(halted.get("message") or "").strip().rstrip(".")
    if not message:
        message = "the turn was stopped by a safety limit"
    follow_up = (
        " It is resumable: /resume re-drives it from where it stopped."
        if halted.get("resumable")
        else " The task may require manual follow-up."
    )
    note = f"[Note: This response may be incomplete. {message}.{follow_up}]"
    standalone = f"[Halted: {message}.{follow_up}]"

    for key in ("final_response", "full_markdown"):
        if key not in result:
            continue
        existing = str(result[key] or "").strip()
        if existing.endswith(note) or existing == standalone:
            continue
        if existing and existing != "(empty message)":
            result[key] = f"{existing}\n\n{note}"
        else:
            result[key] = standalone
    return result


def _with_reasoning_only_note(
    result: Dict[str, Any], steps: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Say when a turn produced reasoning but never an answer.

    The bare "(empty message)" placeholder is not dishonest, but it is
    uninformative: it looks like a glitch rather than a description of what
    happened. Note what this deliberately does NOT do, and why: the in-process
    sibling (``core/stream_bridge.py``'s
    ``response_text(fallback_to_thinking=True)``) hands the caller the model's
    raw THINKING as though it were the answer, unlabelled. That is a quieter
    failure than the placeholder it replaces, so it is named here rather than
    copied.

    Runs after the failure and halt passes, both of which describe the same
    empty output more specifically when they apply.
    """
    if str(result.get("final_response") or "").strip() != "(empty message)":
        return result
    if not any(
        step.get("type") == "thinking" and step.get("content") for step in steps
    ):
        return result
    note = (
        "[No answer: the turn produced reasoning but never a final message, so "
        "nothing was said to you. The reasoning is in the transcript's thinking "
        "steps; ask again if you need it turned into an answer.]"
    )
    result["final_response"] = note
    if str(result.get("full_markdown") or "").strip() in ("", "(empty message)"):
        result["full_markdown"] = note
    return result


def _turn_outcome(
    result: Dict[str, Any],
    steps: List[Dict[str, Any]],
    halted: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply the end-of-turn honesty passes, most specific description first."""
    return _with_reasoning_only_note(
        _with_halt_note(_with_failure_line(result, steps), halted), steps
    )


def project_chat_payload_for_verbosity(
    payload: Dict[str, Any],
    verbosity: str = "verbose",
) -> Dict[str, Any]:
    """Shape a single chat tool response for a caller's token budget."""
    mode = normalize_transcript_verbosity(verbosity)
    source_steps = (
        payload.get("steps") if isinstance(payload.get("steps"), list) else []
    )
    if mode == "verbose":
        projected = copy.deepcopy(payload)
        projected["verbosity"] = mode
        return _turn_outcome(projected, source_steps, payload.get("halted"))

    steps = source_steps
    final_response = message_steps_to_response_text(steps) if steps else str(payload.get("final_response") or "")

    if mode == "chat":
        result = {
            "thread_id": payload.get("thread_id"),
            "final_response": final_response,
            "errors": copy.deepcopy(payload.get("errors") or []),
            "done": payload.get("done"),
            "verbosity": mode,
        }
        return _turn_outcome(
            {k: v for k, v in result.items() if v is not None},
            steps,
            payload.get("halted"),
        )

    projected_steps = project_steps_for_verbosity(steps, mode)
    result = {
        "thread_id": payload.get("thread_id"),
        "final_response": final_response,
        "full_markdown": message_steps_to_markdown(projected_steps, mode),
        "steps": projected_steps,
        "errors": copy.deepcopy(payload.get("errors") or []),
        "done": payload.get("done"),
        "context_stats": copy.deepcopy(payload.get("context_stats")),
        "model": payload.get("model"),
        "verbosity": mode,
    }
    for key in ("title", "title_source", "history_message_id"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    return _turn_outcome(
        {k: v for k, v in result.items() if v is not None},
        steps,
        payload.get("halted"),
    )


def project_history_message_for_verbosity(
    message: Dict[str, Any],
    verbosity: str = "verbose",
    *,
    include_markdown: bool = True,
) -> Dict[str, Any]:
    """Shape one persisted history message for an MCP transcript verbosity."""
    mode = normalize_transcript_verbosity(verbosity)
    projected = copy.deepcopy(message)

    steps = projected.get("steps")
    if mode == "verbose":
        if projected.get("role") == "assistant" and isinstance(steps, list) and steps:
            if include_markdown:
                projected["full_markdown"] = message_steps_to_markdown(steps, mode)
                projected["final_response"] = message_steps_to_response_text(steps)
        return projected

    compact: Dict[str, Any] = {}
    for key in ("id", "role", "content", "timestamp", "autonomous_source"):
        if projected.get(key) is not None:
            compact[key] = projected[key]

    if mode == "chat":
        if projected.get("role") == "assistant" and isinstance(steps, list) and steps:
            final_response = message_steps_to_response_text(steps)
            compact["content"] = final_response
            compact["final_response"] = final_response
        return compact

    attachments = projected.get("attachments")
    if isinstance(attachments, list) and attachments:
        compact["attachments"] = [
            {
                key: attachment[key]
                for key in ("id", "type", "mimeType", "name", "size")
                if isinstance(attachment, dict) and attachment.get(key) is not None
            }
            for attachment in attachments
            if isinstance(attachment, dict)
        ]

    if projected.get("role") == "assistant" and isinstance(steps, list) and steps:
        concise_steps = project_steps_for_verbosity(steps, mode)
        compact["steps"] = concise_steps
        compact["intermediate_content"] = "\n".join(
            step["content"]
            for step in concise_steps
            if step.get("type") == "thinking" and step.get("content")
        ) or None
        compact["tool_calls"] = [
            step
            for step in concise_steps
            if step.get("type") == "tool_call"
        ]
        if include_markdown:
            compact["full_markdown"] = message_steps_to_markdown(concise_steps, mode)
        compact["final_response"] = message_steps_to_response_text(steps)
    return {k: v for k, v in compact.items() if v is not None}


async def _apply_persisted_assistant_steps(
    client: NymeriaBackendClient,
    payload: Dict[str, Any],
    *,
    user_id: str,
    message: str,
) -> Dict[str, Any]:
    """
    Prefer the backend's persisted assistant steps for copy-ready output.

    SSE events are intentionally kept in raw arrival order for diagnostics, but
    parallel tool calls can arrive in a different order from the canonical
    assistant message saved by the backend/checkpointer. The desktop copy path
    uses persisted message steps, so the MCP response should do the same.
    """
    thread_id = payload.get("thread_id")
    if not thread_id:
        return payload

    try:
        history = await client.get(
            f"/threads/{_enc(thread_id)}/history",
            params={"include_internal": "true"},
            act_as=user_id,
        )
    except Exception as exc:
        logger.debug("Could not load persisted chat steps for MCP response: %s", exc)
        return payload

    if not isinstance(history, dict):
        return payload
    messages = history.get("messages")
    if not isinstance(messages, list):
        return payload

    stream_final = str(payload.get("final_response") or "").strip()

    for idx in range(len(messages) - 1, -1, -1):
        history_message = messages[idx]
        if not isinstance(history_message, dict) or history_message.get("role") != "assistant":
            continue

        previous = messages[idx - 1] if idx > 0 else None
        if not isinstance(previous, dict) or previous.get("role") != "user":
            continue
        if str(previous.get("content") or "") != message:
            continue

        steps = history_message.get("steps")
        if not isinstance(steps, list) or not steps:
            continue
        persisted_response = message_steps_to_response_text(steps)
        persisted_final = persisted_response.strip()
        if stream_final and persisted_final and persisted_final != stream_final:
            continue

        payload["steps"] = steps
        payload["full_markdown"] = message_steps_to_markdown(steps)
        payload["final_response"] = persisted_response
        if history_message.get("id"):
            payload["history_message_id"] = history_message["id"]
        return payload

    return payload
