"""Validation and subprocess execution for Python custom tools."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ..config import get_settings
from ..oom import oom_score_preexec
from ..tools.definitions.custom_tool_schema import PythonToolConfig, ToolParameter
from .http_policy import SECRET_PATTERNS
from .time_utils import utc_now

logger = logging.getLogger(__name__)

DEFAULT_VALIDATION_TIMEOUT_SECONDS = 60
MIN_VALIDATION_TIMEOUT_SECONDS = 5
MAX_VALIDATION_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class PythonToolRunResult:
    ok: bool
    result: str = ""
    error_type: str = ""
    error_message: str = ""
    stdout: str = ""
    stderr: str = ""

    def public_text(self) -> str:
        if self.ok:
            return self.result
        prefix = self.error_type or "python_tool_error"
        return f"[Error]: {prefix} - {self.error_message or 'Python tool failed'}"


def clamp_validation_timeout(value: Optional[int]) -> int:
    try:
        seconds = int(value if value is not None else DEFAULT_VALIDATION_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        seconds = DEFAULT_VALIDATION_TIMEOUT_SECONDS
    return max(MIN_VALIDATION_TIMEOUT_SECONDS, min(seconds, MAX_VALIDATION_TIMEOUT_SECONDS))


# --- revision hash and the execution-time approval gate ----------------------
#
# Python tool authoring is admin-only, but the generic file tools can write
# ``data/custom_tools/<id>.json`` directly, so the admin gate is ALSO enforced
# at execution time (mirroring the workflow gate in
# core/workflows/authoring.py). The approval is a content hash an admin stamped
# at publish; the loader recomputes it from live source on every call and runs
# only on a match, so an edited or unapproved record fails closed. These are a
# deliberately separate, co-located parallel to the workflow helpers: this hash
# is its own security boundary (it must not shift if the workflow canonical
# form changes) and the field set differs (no ``continuations``).
#
# Residual (not solved here): the hash lives in the same file it protects, so a
# determined writer who replicates this canonical form could forge a matching
# approval; a hard boundary needs write confinement + subprocess env scrubbing
# (the #75 confinement slice). A bash-capable caller already holds the secret
# key and has code execution regardless.


def compute_python_revision_hash(
    *,
    source: str,
    entrypoint: str,
    parameters: Mapping[str, ToolParameter],
) -> str:
    """Canonical content hash over {source, entrypoint, parameters}."""
    canonical = json.dumps(
        {
            "source": source,
            "entrypoint": entrypoint,
            "parameters": {
                name: parameters[name].model_dump(mode="json") for name in sorted(parameters)
            },
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_python_revision_hash(
    config: PythonToolConfig, parameters: Mapping[str, ToolParameter]
) -> str:
    return compute_python_revision_hash(
        source=config.source_code,
        entrypoint=config.entrypoint,
        parameters=parameters,
    )


def approve_python_revision(
    config: PythonToolConfig,
    parameters: Mapping[str, ToolParameter],
    *,
    approved_by: str,
) -> PythonToolConfig:
    """Return a copy approving the CURRENT content revision (recomputed).

    Called only from the admin-gated publish/create paths, so a valid
    ``approved_revision`` can be produced only by an admin.
    """
    revision = config_python_revision_hash(config, parameters)
    return config.model_copy(
        update={
            "revision_hash": revision,
            "approved_revision": revision,
            "approved_by": approved_by,
            "approved_at": utc_now(),
        }
    )


def python_execution_gate(
    config: PythonToolConfig, parameters: Mapping[str, ToolParameter]
) -> Optional[str]:
    """None when this revision may execute, else the refusal copy (fail closed).

    Recomputes the hash from live content so a stale stored ``revision_hash``
    can never satisfy the gate, and re-reads ``approved_revision`` on every
    call so a revoked approval (or an edited source) takes effect immediately.
    """
    current = config_python_revision_hash(config, parameters)
    if config.approved_revision == current:
        return None
    if config.approved_revision:
        return (
            "this python tool changed since its approval (its source or "
            "parameters differ from the approved revision; raw on-disk edits "
            "count and leave it inert by design). Next: an admin re-publishes "
            "it via tool_create to approve the new revision"
        )
    return (
        "this python tool is not approved; an admin must publish it "
        "(tool_create) before it can run"
    )


def validate_python_tool_static(
    *,
    config: PythonToolConfig,
    parameters: Mapping[str, ToolParameter],
) -> list[str]:
    """Return validation errors that can be detected without running code."""
    errors: list[str] = []
    source = config.source_code or ""
    for pattern in SECRET_PATTERNS:
        if pattern.search(source):
            errors.append("source_code appears to contain a raw secret")
            break

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]

    entrypoint_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.Expr):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == config.entrypoint:
                entrypoint_node = node
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_literal_assignment(node):
            continue
        errors.append(
            f"top-level {type(node).__name__} is not allowed; keep executable work inside {config.entrypoint}()"
        )

    if entrypoint_node is None:
        errors.append(f"entrypoint function {config.entrypoint!r} was not found")
    else:
        errors.extend(_validate_entrypoint_signature(entrypoint_node, parameters))

    return errors


def validate_python_tool_runtime(
    *,
    tool_id: str,
    config: PythonToolConfig,
    parameters: Mapping[str, ToolParameter],
    sample_params: Optional[dict[str, Any]],
    timeout_seconds: Optional[int] = None,
) -> PythonToolRunResult:
    """Run static checks plus one subprocess sample invocation."""
    errors = validate_python_tool_static(config=config, parameters=parameters)
    params = sample_params or {}
    if not isinstance(params, dict):
        errors.append("sample_params must be an object")
        params = {}
    for name, parameter in parameters.items():
        if parameter.required and name not in params:
            errors.append(f"sample_params missing required parameter {name!r}")
    if errors:
        return PythonToolRunResult(
            ok=False,
            error_type="validation_error",
            error_message="; ".join(errors),
        )
    return run_python_tool_subprocess(
        tool_id=tool_id,
        config=config,
        params=params,
        timeout_seconds=clamp_validation_timeout(timeout_seconds),
    )


async def execute_python_tool(
    config: PythonToolConfig,
    params: Dict[str, Any],
    *,
    target_id: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    import asyncio

    return (
        await asyncio.to_thread(
            _sync_execute_python_tool,
            config,
            params,
            target_id=target_id,
            timeout_seconds=timeout_seconds,
        )
    )


def _sync_execute_python_tool(
    config: PythonToolConfig,
    params: Dict[str, Any],
    *,
    target_id: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    timeout = timeout_seconds
    if timeout is None:
        timeout = max(1, int(get_settings().tool_timeout) - 1)
    result = run_python_tool_subprocess(
        tool_id=target_id or "python_custom_tool",
        config=config,
        params=params,
        timeout_seconds=timeout,
    )
    return result.public_text()


def run_python_tool_subprocess(
    *,
    tool_id: str,
    config: PythonToolConfig,
    params: Dict[str, Any],
    timeout_seconds: int,
) -> PythonToolRunResult:
    timeout = max(1, int(timeout_seconds))
    payload = {
        "tool_id": tool_id,
        "source_code": config.source_code,
        "entrypoint": config.entrypoint,
        "params": params,
    }
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "nymeria.core.python_tool_runner"],
            input=json.dumps(payload, ensure_ascii=False),
            cwd=str(get_settings().project_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            # User tool code can allocate heavily; make it the OOM victim, not
            # the API server that runs it (see nymeria/oom.py).
            preexec_fn=oom_score_preexec(),
        )
    except subprocess.TimeoutExpired:
        return PythonToolRunResult(
            ok=False,
            error_type="timeout",
            error_message=f"Python tool timed out after {timeout} seconds",
        )
    except Exception as exc:
        logger.error("Python custom tool subprocess failed", exc_info=True)
        return PythonToolRunResult(
            ok=False,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        return PythonToolRunResult(
            ok=False,
            error_type="process_exit",
            error_message=(
                f"Python tool process exited with code {completed.returncode}: "
                f"{_truncate_text(stderr or stdout, 1000)}"
            ),
            stdout=_truncate_text(stdout, 4000),
            stderr=_truncate_text(stderr, 4000),
        )

    try:
        payload = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError:
        return PythonToolRunResult(
            ok=False,
            error_type="invalid_runner_output",
            error_message=_truncate_text(stdout or stderr, 1000),
            stdout=_truncate_text(stdout, 4000),
            stderr=_truncate_text(stderr, 4000),
        )

    if payload.get("ok") is True:
        return PythonToolRunResult(
            ok=True,
            result=str(payload.get("result", "")),
            stdout=_truncate_text(str(payload.get("stdout", "")), 4000),
            stderr=_truncate_text(str(payload.get("stderr", "")), 4000),
        )

    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    return PythonToolRunResult(
        ok=False,
        error_type=str(error.get("type") or "python_tool_error"),
        error_message=str(error.get("message") or "Python tool failed"),
        stdout=_truncate_text(str(payload.get("stdout", "")), 4000),
        stderr=_truncate_text(str(payload.get("stderr", "")), 4000),
    )


def _is_literal_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    if isinstance(node, ast.Assign):
        value = node.value
    else:
        value = node.value
        if value is None:
            return True
    return _is_literal_node(value)


def _is_literal_node(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal_node(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            (key is None or _is_literal_node(key)) and _is_literal_node(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 32] + "\n[...truncated...]"


def _validate_entrypoint_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    parameters: Mapping[str, ToolParameter],
) -> list[str]:
    errors: list[str] = []
    args = node.args
    if args.vararg is not None:
        errors.append("*args is not supported for Python custom tool entrypoints")
    declared = set(parameters)
    accepts_kwargs = args.kwarg is not None
    positional = list(args.posonlyargs) + list(args.args)
    kwonly = list(args.kwonlyargs)
    function_params = {arg.arg for arg in positional + kwonly}

    if not accepts_kwargs:
        missing = declared - function_params
        if missing:
            errors.append(
                "entrypoint is missing declared parameter(s): " + ", ".join(sorted(missing))
            )

    required_positional_count = max(0, len(positional) - len(args.defaults))
    required = {arg.arg for arg in positional[:required_positional_count]}
    required.update(
        arg.arg
        for arg, default in zip(kwonly, args.kw_defaults)
        if default is None
    )
    undeclared_required = required - declared
    if undeclared_required:
        errors.append(
            "entrypoint has required parameter(s) not declared in schema: "
            + ", ".join(sorted(undeclared_required))
        )

    return errors


__all__ = [
    "DEFAULT_VALIDATION_TIMEOUT_SECONDS",
    "MAX_VALIDATION_TIMEOUT_SECONDS",
    "MIN_VALIDATION_TIMEOUT_SECONDS",
    "PythonToolRunResult",
    "approve_python_revision",
    "clamp_validation_timeout",
    "compute_python_revision_hash",
    "config_python_revision_hash",
    "execute_python_tool",
    "python_execution_gate",
    "run_python_tool_subprocess",
    "validate_python_tool_runtime",
    "validate_python_tool_static",
]
