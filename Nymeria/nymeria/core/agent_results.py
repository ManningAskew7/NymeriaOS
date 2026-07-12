"""Stream-error classification and tool-result post-processing helpers.

Extracted from ``NymeriaAgent`` so the agent class stays focused on
orchestration. These functions are mostly stateless: they classify provider
exceptions into UI-friendly error payloads, parse and strip sub-agent error
markers, scrub legacy ``[attach:…]`` tags from tool output, resolve workspace
artifact paths, and synthesise the extra stream events the frontend renders
alongside a tool result (iteration-limit and workspace-artifact events).

Each function is wired into ``NymeriaAgent`` as a thin facade so external
callers (tests, the streaming pipeline, ``agent_history.format_conversation_history``)
keep their existing call shape.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..vendor.react_agent.nodes import TURN_SAFETY_REASON_MAX_ITERATIONS
from .bot_reactions import REPLY_SUPPRESSED_MARKER

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401

logger = logging.getLogger(__name__)

SUBAGENT_ERROR_MARKER_PREFIX = "[NymeriaSubAgentError]"
_ATTACH_TAG_PATTERN = re.compile(r"\[attach:(.+?)\]")

# Only the react tool itself (and tool_invoke, its deferred wrapper, which
# returns the target's result text verbatim) may signal reply suppression.
# Keying on the tool name means arbitrary tool output (a fetched web page, an
# MCP tool) cannot suppress bot replies by echoing the marker.
_REPLY_SUPPRESSING_TOOL_NAMES = frozenset({"react", "tool_invoke"})


def extract_http_status_code(error: Exception) -> Optional[int]:
    """Best-effort extraction of HTTP status code from provider exceptions."""
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None) if response else None
    if isinstance(response_status, int):
        return response_status

    match = re.search(r"error code:\s*(\d{3})", str(error), re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None

    return None


def classify_stream_exception(error: Exception) -> Dict[str, Any]:
    """Map raw exceptions into frontend-friendly structured error payloads."""
    raw_message = str(error)
    status_code = extract_http_status_code(error)
    lower = raw_message.lower()

    if (
        status_code == 402
        or "error code: 402" in lower
        or "requires more credits" in lower
    ):
        requested_tokens = None
        affordable_tokens = None
        token_match = re.search(
            r"requested up to\s+(\d+)\s+tokens.*?can only afford\s+(\d+)",
            raw_message,
            re.IGNORECASE,
        )
        if token_match:
            try:
                requested_tokens = int(token_match.group(1))
                affordable_tokens = int(token_match.group(2))
            except ValueError:
                requested_tokens = None
                affordable_tokens = None

        details: Dict[str, Any] = {
            "provider": "openrouter",
            "http_status": 402,
        }
        if requested_tokens is not None:
            details["requested_max_tokens"] = requested_tokens
        if affordable_tokens is not None:
            details["affordable_max_tokens"] = affordable_tokens

        message = (
            "OpenRouter rejected the request due to insufficient credit/token budget. "
            "Reduce max output tokens (for example set LLM_MAX_TOKENS lower) "
            "or increase your OpenRouter credit limit, then retry."
        )
        if requested_tokens is not None and affordable_tokens is not None:
            message = (
                f"{message} Requested up to {requested_tokens} tokens, "
                f"but only {affordable_tokens} were affordable."
            )

        return {
            "type": "error",
            "content": message,
            "code": "openrouter_insufficient_credits",
            "details": details,
        }

    # Anthropic's Claude-subscription OAuth path (used via CLIProxy) rejects
    # requests that carry a tool whose name it recognizes as a known third-party
    # integration, returning a 400 that talks about "extra usage" rather than
    # plan limits. It reads like a billing/credits problem but is almost always a
    # tool-name collision (e.g. a tool named exactly like an MCP/Exa tool, or a
    # name starting/ending with "mcp"). Surface an actionable hint instead of the
    # raw provider text. See Nymeria/docs/ (cliproxy) and the web-search plan doc.
    if (
        ("extra usage" in lower and ("third-party" in lower or "third party" in lower))
        or "draw from your extra usage" in lower
    ):
        message = (
            "Anthropic's subscription API rejected this request as a third-party app "
            "and tried to bill it to extra usage instead of your plan. This is almost "
            "always a tool-name collision, not a credit problem: a tool bound to this "
            "thread has a name Anthropic recognizes as a known integration (for example "
            "a tool named exactly like an MCP or Exa tool, or one whose name starts or "
            "ends with 'mcp'). Disable or rename the offending tool on this thread, "
            "then retry."
        )
        return {
            "type": "error",
            "content": message,
            "code": "anthropic_third_party_tool_name",
            "details": {
                "http_status": status_code or 400,
                "hint": "tool_name_collision",
            },
        }

    details = {"http_status": status_code} if status_code is not None else {}
    return {
        "type": "error",
        "content": f"An error occurred: {raw_message}",
        "code": "agent_runtime_error",
        "details": details,
    }


def parse_subagent_error_marker(result: str) -> Optional[Dict[str, Any]]:
    """Parse structured sub-agent error markers from tool output."""
    if not isinstance(result, str):
        return None
    if not result.startswith(SUBAGENT_ERROR_MARKER_PREFIX):
        return None

    first_line = result.splitlines()[0]
    payload_json = first_line[len(SUBAGENT_ERROR_MARKER_PREFIX):].strip()
    if not payload_json:
        return None

    try:
        payload = json.loads(payload_json)
        if isinstance(payload, dict):
            return payload
        return None
    except Exception:
        return None


def strip_subagent_error_marker(result: str) -> str:
    """Remove structured marker line from tool output for frontend display."""
    if not isinstance(result, str):
        return str(result)
    if not result.startswith(SUBAGENT_ERROR_MARKER_PREFIX):
        return result

    lines = result.splitlines()
    cleaned = "\n".join(lines[1:]).strip()
    if cleaned:
        return cleaned

    payload = parse_subagent_error_marker(result)
    if payload:
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return result


def get_workspace_dir() -> Path:
    """Return the root directory exposed by the workspace download API."""
    return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()


def strip_attach_tags(result: str) -> str:
    """Remove legacy attach markers from tool output shown to clients."""
    if not isinstance(result, str):
        return str(result)
    cleaned = _ATTACH_TAG_PATTERN.sub("", result)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def clean_tool_result_for_display(result: str) -> str:
    """Remove internal markers from a tool result before streaming it to UIs."""
    return strip_attach_tags(strip_subagent_error_marker(result))


def serialize_workspace_artifact(path: Path) -> Dict[str, Any]:
    """Build metadata for a downloadable workspace artifact."""
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return {
        "path": str(path),
        "name": path.name,
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size,
    }


def extract_workspace_artifacts(result: str) -> List[Dict[str, Any]]:
    """Extract valid workspace artifacts from legacy attach tags."""
    if not isinstance(result, str):
        return []

    workspace_dir = get_workspace_dir()
    artifacts: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for raw_path in _ATTACH_TAG_PATTERN.findall(result):
        candidates = [Path(raw_path)]
        if not Path(raw_path).is_absolute():
            candidates.append(workspace_dir / raw_path)

        resolved_path: Optional[Path] = None
        for candidate in candidates:
            try:
                candidate_resolved = candidate.resolve()
            except Exception:
                continue
            if not candidate_resolved.is_relative_to(workspace_dir):
                continue
            if not candidate_resolved.is_file():
                continue
            resolved_path = candidate_resolved
            break

        if resolved_path is None:
            logger.debug("Ignoring non-downloadable attach path: %s", raw_path)
            continue

        resolved_str = str(resolved_path)
        if resolved_str in seen:
            continue
        seen.add(resolved_str)
        artifacts.append(serialize_workspace_artifact(resolved_path))

    return artifacts


def tool_result_extra_events(
    tool_name: str,
    raw_result: str,
    tool_call_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build extra stream events for structured tool results."""
    events: List[Dict[str, Any]] = []

    payload = parse_subagent_error_marker(raw_result)
    if payload:
        code = payload.get("code")
        message = payload.get("message")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        if code == "subagent_iteration_limit":
            max_iterations = metadata.get("max_iterations", 0)
            tool_call_count = metadata.get("tool_call_count", 0)
            agent_name = metadata.get("agent_name") or tool_name
            reason = metadata.get("reason") or TURN_SAFETY_REASON_MAX_ITERATIONS
            repeated_tool_name = metadata.get("repeated_tool_name")
            repeated_count = metadata.get("repeated_count")

            event = {
                "type": "iteration_limit",
                "scope": "sub_agent",
                "agent_name": agent_name,
                "reason": reason if isinstance(reason, str) and reason else TURN_SAFETY_REASON_MAX_ITERATIONS,
                "max_iterations": max_iterations if isinstance(max_iterations, int) and max_iterations > 0 else 0,
                "tool_call_count": tool_call_count if isinstance(tool_call_count, int) and tool_call_count > 0 else None,
                "content": message if isinstance(message, str) and message.strip()
                else f"{agent_name} hit its iteration limit.",
            }
            if isinstance(repeated_tool_name, str) and repeated_tool_name:
                event["repeated_tool_name"] = repeated_tool_name
            if isinstance(repeated_count, int) and repeated_count > 0:
                event["repeated_count"] = repeated_count

            if event["max_iterations"] <= 0:
                event["max_iterations"] = 30
            if event["tool_call_count"] is None:
                event.pop("tool_call_count")

            events.append(event)

    for artifact in extract_workspace_artifacts(raw_result):
        events.append({
            "type": "workspace_artifact",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            **artifact,
        })

    # The react tool's deterministic suppression marker becomes a stream
    # event immediately after its tool_result, before any later response
    # text, so bot clients can drop the reply as it streams (backlog #45;
    # see core/bot_reactions.py).
    if (
        tool_name in _REPLY_SUPPRESSING_TOOL_NAMES
        and isinstance(raw_result, str)
        and REPLY_SUPPRESSED_MARKER in raw_result
    ):
        events.append({
            "type": "reply_suppressed",
            "tool_call_id": tool_call_id,
        })

    return events
