"""The shared structured-output primitive (plan: "Structured output").

Backs both ``nym.llm(schema=)`` and the post-hoc extraction behind
``nym.thread(schema=)``. Schemas arrive as plain JSON Schema dicts (the child
proxy converts pydantic classes before the wire).

Strategy, at most TWO model invocations total:

1. Native structured output (``with_structured_output``) when the provider
   supports it: strictest, the model is forced onto the schema (the
   improvement the plan takes over openclaw's prompt-only approach). A
   construction failure costs nothing and hands both attempts to the prompt
   path; a native INVOCATION (invalid result or raise, e.g. a gateway
   without tool support) was a real round-trip and consumes attempt one.
2. Prompt-embedded JSON: the schema is inlined in the prompt, the reply is
   parsed (code fences tolerated) and validated. Used as attempt one when
   native is unavailable, and as the single validation retry (with the
   validation error appended) when attempt one produced an invalid value.

Validation is deliberately LIGHT: top-level type plus required keys plus
one-level property primitive types. Full JSON-Schema validation would need a
new dependency (``jsonschema`` is not one); the native path is
provider-enforced and the light check catches the common drift (missing key,
string-instead-of-object) that matters for chaining steps.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

from .registry import VerbError

logger = logging.getLogger(__name__)

# JSON-Schema primitive name -> accepted Python types. bool must be checked
# before int (bool subclasses int).
_TYPE_MAP = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "null": (type(None),),
}


def validate_json_value(value: Any, schema: Any) -> Optional[str]:
    """Light validation; returns an error string or None when acceptable.

    Checks the top-level ``type``, ``required`` keys, and one level of
    property primitive types. Anything deeper is out of scope (see module
    docstring).
    """
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    if isinstance(declared, str) and declared in _TYPE_MAP:
        expected = _TYPE_MAP[declared]
        if declared in ("integer", "number") and isinstance(value, bool):
            return f"expected {declared}, got boolean"
        if not isinstance(value, expected):
            return f"expected top-level {declared}, got {type(value).__name__}"
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [k for k in required if k not in value]
            if missing:
                return f"missing required key(s): {', '.join(map(str, missing))}"
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, spec in properties.items():
                if key not in value or not isinstance(spec, dict):
                    continue
                prop_type = spec.get("type")
                if not isinstance(prop_type, str) or prop_type not in _TYPE_MAP:
                    continue
                got = value[key]
                if prop_type in ("integer", "number") and isinstance(got, bool):
                    return f"key {key!r}: expected {prop_type}, got boolean"
                if not isinstance(got, _TYPE_MAP[prop_type]):
                    return (
                        f"key {key!r}: expected {prop_type}, "
                        f"got {type(got).__name__}"
                    )
    return None


def normalize_message_content(result: Any) -> str:
    """Message ``content`` to plain text (the ``llm_extract`` idiom)."""
    text = getattr(result, "content", result)
    if isinstance(text, list):
        # Anthropic-style content blocks: keep text parts, tolerate non-dicts.
        parts = []
        for part in text:
            value = part.get("text") if isinstance(part, dict) else part
            if value:
                parts.append(str(value))
        text = " ".join(parts)
    return str(text or "").strip()


def parse_json_reply(text: str) -> Tuple[Any, Optional[str]]:
    """Parse a model reply as JSON, tolerating markdown code fences.

    Returns ``(value, None)`` or ``(None, error)``.
    """
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        if first_newline != -1:
            candidate = candidate[first_newline + 1 :]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[: -len("```")]
        candidate = candidate.strip()
    if not candidate:
        return None, "the model returned no content"
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as exc:
        # Second chance: grab the outermost JSON object/array from prose.
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = candidate.find(open_ch)
            end = candidate.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(candidate[start : end + 1]), None
                except json.JSONDecodeError:
                    continue
        return None, f"the reply was not valid JSON ({exc.msg})"


def _json_prompt(prompt: str, schema: dict, previous_error: Optional[str]) -> str:
    parts = [
        prompt,
        "",
        "Respond with ONLY a JSON value (no prose, no code fences) that "
        "matches this JSON Schema:",
        json.dumps(schema, default=str),
    ]
    if previous_error:
        parts += [
            "",
            f"Your previous reply was rejected: {previous_error}. "
            "Correct it and respond again with only the JSON value.",
        ]
    return "\n".join(parts)


def _build_native_runner(llm: Any, schema: dict) -> Optional[Any]:
    """Seam: a ``with_structured_output`` runnable, or None if unsupported."""
    try:
        return llm.with_structured_output(schema)
    except Exception:  # noqa: BLE001 - provider without structured support
        logger.debug("native structured output unavailable", exc_info=True)
        return None


async def ainvoke_structured(llm: Any, prompt: str, schema: dict) -> Any:
    """One structured LLM call with one validation retry (plan-settled).

    At most two model invocations. Raises :class:`VerbError` when the second
    attempt still fails validation or parsing.
    """
    if not isinstance(schema, dict) or not schema:
        raise VerbError("schema must be a JSON Schema object (or a pydantic model class)")

    last_error: Optional[str] = None
    native = _build_native_runner(llm, schema)
    # Construction failure costs no model call, so the prompt path keeps both
    # attempts. A native INVOCATION (success-but-invalid OR raise) was a real
    # model round-trip and consumes attempt one, keeping the two-invocation
    # ceiling true on every branch.
    attempts_left = 2
    if native is not None:
        attempts_left = 1
        try:
            value = await native.ainvoke(prompt, config={"callbacks": []})
            if hasattr(value, "model_dump"):
                value = value.model_dump()
            error = validate_json_value(value, schema)
            if error is None:
                return value
            last_error = error
        except Exception as exc:  # noqa: BLE001 - gateway/tool-support failures
            logger.info("native structured output failed, using prompt path: %s", exc)

    for _ in range(attempts_left):
        reply = await llm.ainvoke(
            _json_prompt(prompt, schema, last_error), config={"callbacks": []}
        )
        value, parse_error = parse_json_reply(normalize_message_content(reply))
        error = parse_error or validate_json_value(value, schema)
        if error is None:
            return value
        last_error = error
    raise VerbError(f"structured output failed validation after retry: {last_error}")
