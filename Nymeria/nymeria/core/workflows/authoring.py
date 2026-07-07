"""Workflow custom-tool authoring: schema derivation, static validation,
revision hashing, the execution gate, and source retention (plan:
"Workflow as a callable tool" + "Authoring gate").

Design (settled in docs/private/plans/workflow-tools.md):

- The definition's ``parameters`` map is DERIVED from the entrypoint
  signature at draft time (type hints, literal defaults, docstring
  descriptions). Generation replaces cross-validation, so signature and
  schema cannot drift. Supported hint set: str/int/float/bool/list/dict
  plus Optional (``Optional[T]``, ``Union[T, None]``, ``T | None``).
- ``compute_revision_hash`` covers {source, entrypoint, continuations,
  parameters}. Any behavior-changing edit produces a new hash and therefore
  resets approval by construction; name/enabled edits never touch it.
- ``workflow_execution_gate`` is the per-revision approval check, evaluated
  AT EXECUTION TIME (the run_command precedent: a revoked approval takes
  effect immediately). Fail closed, with distinct copy for "not approved
  yet" vs "revision changed since approval".
- Static validation extends the Python custom-tool checks (secret scan,
  top-level AST allowlist, entrypoint presence) with continuation-signature
  checks: each declared continuation must exist with the fixed
  ``(state, decision)`` shape (nym.approve resume, phase 4).
- ``retain_source_revision`` keeps the last N source revisions beside the
  definitions so an approval request can be diffed against what was
  previously approved. Full history/rollback is deliberately deferred.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from ..http_policy import SECRET_PATTERNS
from ..time_utils import utc_now
from .budget import WorkflowBudget

if TYPE_CHECKING:
    from ...tools.definitions.custom_tool_schema import ToolParameter, WorkflowToolConfig

# The tools-package imports (ToolParameter construction, the literal-assignment
# helper) stay function-local: this module must be importable before the tools
# package finishes initializing (tools/__init__ -> tool_create -> here), the
# repo's standard circular-import dodge.

logger = logging.getLogger(__name__)

MAX_SOURCE_REVISIONS = 10

SUPPORTED_HINTS_HELP = "str, int, float, bool, list, dict, or Optional[...] of those"

_HINT_TO_TYPE = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "List": "array",
    "dict": "object",
    "Dict": "object",
}

_SPHINX_PARAM_RE = re.compile(r"^\s*:param\s+(\w+)\s*:\s*(.+?)\s*$")
_ARGS_HEADING_RE = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$")
_SECTION_HEADING_RE = re.compile(
    r"^\s*(Returns|Raises|Yields|Examples?|Notes?|Attributes|Warns|See Also)\s*:\s*$"
)
_ARGS_ENTRY_RE = re.compile(r"^\s+(\w+)\s*(\([^)]*\))?\s*:\s*(.+?)\s*$")

# Names an entrypoint parameter may not use: ``config`` is claimed by the
# langchain RunnableConfig injection on the bound tool coroutine (a model-
# supplied value would be silently swallowed), and pydantic rejects or
# reserves leading-underscore and ``model_``-prefixed field names when the
# args schema is built.
RESERVED_PARAM_NAMES = frozenset({"config"})


# --- docstring descriptions ---------------------------------------------------


def _param_descriptions(docstring: Optional[str]) -> Dict[str, str]:
    """Best-effort parameter descriptions from a Google or Sphinx docstring."""
    if not docstring:
        return {}
    descriptions: Dict[str, str] = {}
    in_args = False
    for line in docstring.splitlines():
        sphinx = _SPHINX_PARAM_RE.match(line)
        if sphinx:
            descriptions.setdefault(sphinx.group(1), sphinx.group(2))
            continue
        if _ARGS_HEADING_RE.match(line):
            in_args = True
            continue
        if in_args:
            if not line.strip() or _SECTION_HEADING_RE.match(line):
                in_args = False
                continue
            entry = _ARGS_ENTRY_RE.match(line)
            if entry:
                descriptions.setdefault(entry.group(1), entry.group(3))
    return descriptions


# --- annotation mapping -------------------------------------------------------


def _annotation_to_type(node: ast.expr) -> Tuple[Optional[str], bool, Optional[str]]:
    """Map an annotation AST node to ``(json_type, optional, error)``.

    ``optional`` is True for Optional/Union-with-None/``| None`` shapes.
    """
    if isinstance(node, ast.Name) and node.id in _HINT_TO_TYPE:
        return _HINT_TO_TYPE[node.id], False, None
    if isinstance(node, ast.Attribute) and node.attr in _HINT_TO_TYPE:
        # e.g. typing.List
        return _HINT_TO_TYPE[node.attr], False, None
    if isinstance(node, ast.Subscript):
        head = node.value
        head_name = (
            head.id
            if isinstance(head, ast.Name)
            else head.attr
            if isinstance(head, ast.Attribute)
            else None
        )
        if head_name in ("Optional",):
            inner_type, _, error = _annotation_to_type(node.slice)
            return inner_type, True, error
        if head_name in ("Union",):
            elements = (
                list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            )
            none_free = [
                el
                for el in elements
                if not (isinstance(el, ast.Constant) and el.value is None)
            ]
            if len(none_free) == len(elements) or len(none_free) != 1:
                return None, False, "only Union[T, None] (Optional) unions are supported"
            inner_type, _, error = _annotation_to_type(none_free[0])
            return inner_type, True, error
        if head_name in ("list", "List"):
            return "array", False, None
        if head_name in ("dict", "Dict"):
            return "object", False, None
        return None, False, None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        sides = [node.left, node.right]
        none_free = [
            side
            for side in sides
            if not (isinstance(side, ast.Constant) and side.value is None)
        ]
        if len(none_free) != 1:
            return None, False, "only 'T | None' unions are supported"
        inner_type, _, error = _annotation_to_type(none_free[0])
        return inner_type, True, error
    return None, False, None


# --- parameter derivation -----------------------------------------------------


def derive_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Tuple[Dict[str, ToolParameter], List[str]]:
    """Derive the tool ``parameters`` map from the entrypoint signature.

    Every parameter needs a supported type hint; defaults must be literals.
    ``Optional[...]`` (with or without a default) makes a parameter optional.
    ``*args``/``**kwargs`` are rejected: the schema must fully describe the
    call surface.
    """
    from ...tools.definitions.custom_tool_schema import ToolParameter

    errors: List[str] = []
    args = node.args
    if args.vararg is not None:
        errors.append("*args is not supported for workflow entrypoints")
    if args.kwarg is not None:
        errors.append(
            "**kwargs is not supported for workflow entrypoints; declare each parameter"
        )

    descriptions = _param_descriptions(ast.get_docstring(node))
    parameters: Dict[str, ToolParameter] = {}

    positional = list(args.posonlyargs) + list(args.args)
    default_offset = len(positional) - len(args.defaults)
    entries: List[Tuple[ast.arg, Optional[ast.expr]]] = []
    for index, arg in enumerate(positional):
        default = args.defaults[index - default_offset] if index >= default_offset else None
        entries.append((arg, default))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        entries.append((arg, default))

    for arg, default in entries:
        name = arg.arg
        if name in RESERVED_PARAM_NAMES or name.startswith("_") or name.startswith("model_"):
            errors.append(
                f"parameter {name!r} uses a reserved name; rename it "
                "('config', leading underscores, and the 'model_' prefix are reserved)"
            )
            continue
        if arg.annotation is None:
            errors.append(
                f"parameter {name!r} needs a type hint ({SUPPORTED_HINTS_HELP})"
            )
            continue
        json_type, optional, hint_error = _annotation_to_type(arg.annotation)
        if hint_error:
            errors.append(f"parameter {name!r}: {hint_error}")
            continue
        if json_type is None:
            errors.append(
                f"parameter {name!r} has an unsupported type hint; use {SUPPORTED_HINTS_HELP}"
            )
            continue
        default_value: Any = None
        has_default = default is not None
        if has_default:
            try:
                default_value = ast.literal_eval(default)
            except (ValueError, SyntaxError):
                errors.append(f"default for parameter {name!r} must be a literal")
                continue
        parameters[name] = ToolParameter(
            type=json_type,  # type: ignore[arg-type]
            description=descriptions.get(name, ""),
            required=not (optional or has_default),
            default=default_value,
        )

    return parameters, errors


# --- static validation --------------------------------------------------------


def validate_workflow_static(
    *, config: WorkflowToolConfig
) -> Tuple[Dict[str, ToolParameter], List[str]]:
    """Static checks plus parameter derivation in one pass; runs no code.

    Returns ``(parameters, errors)``. The parameters are only meaningful when
    ``errors`` is empty. Mirrors ``validate_python_tool_static`` (secret scan,
    top-level allowlist, entrypoint presence) and adds continuation-signature
    checks for the declared ``nym.approve`` continuations.
    """
    from ..python_custom_tools import _is_literal_assignment

    errors: List[str] = []
    source = config.source_code or ""
    for pattern in SECRET_PATTERNS:
        if pattern.search(source):
            errors.append("source_code appears to contain a raw secret")
            break

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {}, [f"syntax error at line {exc.lineno}: {exc.msg}"]

    functions: Dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Expr):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                continue
        if isinstance(
            node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, node)
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_literal_assignment(node):
            continue
        errors.append(
            f"top-level {type(node).__name__} is not allowed; "
            f"keep executable work inside {config.entrypoint}()"
        )

    parameters: Dict[str, ToolParameter] = {}
    entrypoint_node = functions.get(config.entrypoint)
    if entrypoint_node is None:
        errors.append(f"entrypoint function {config.entrypoint!r} was not found")
    else:
        parameters, derive_errors = derive_parameters(entrypoint_node)
        errors.extend(derive_errors)

    seen: set[str] = set()
    for continuation in config.continuations:
        if continuation in seen:
            errors.append(f"duplicate continuation entrypoint {continuation!r}")
            continue
        seen.add(continuation)
        if continuation == config.entrypoint:
            errors.append("a continuation cannot be the main entrypoint")
            continue
        cont_node = functions.get(continuation)
        if cont_node is None:
            errors.append(f"continuation function {continuation!r} was not found")
            continue
        errors.extend(_validate_continuation_signature(continuation, cont_node))

    return parameters, errors


def _validate_continuation_signature(
    name: str, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> List[str]:
    """A continuation has the fixed shape ``(state, decision)``."""
    args = node.args
    problems: List[str] = []
    if args.vararg is not None or args.kwarg is not None:
        problems.append(f"continuation {name!r} must not use *args or **kwargs")
    if args.kwonlyargs:
        problems.append(f"continuation {name!r} must not declare keyword-only parameters")
    positional = list(args.posonlyargs) + list(args.args)
    if len(positional) != 2:
        problems.append(
            f"continuation {name!r} must accept exactly (state, decision); "
            f"it declares {len(positional)} parameter(s)"
        )
    elif [arg.arg for arg in positional] != ["state", "decision"]:
        # The resume path invokes the continuation by name, so the names are
        # part of the contract, not a style choice.
        problems.append(
            f"continuation {name!r} parameters must be named (state, decision); "
            f"it declares ({positional[0].arg}, {positional[1].arg})"
        )
    return problems


# --- revision hash and the approval gate --------------------------------------


def compute_revision_hash(
    *,
    source: str,
    entrypoint: str,
    continuations: List[str],
    parameters: Mapping[str, ToolParameter],
) -> str:
    """Canonical content hash over {source, entrypoint, continuations, parameters}."""
    canonical = json.dumps(
        {
            "source": source,
            "entrypoint": entrypoint,
            "continuations": sorted(continuations),
            "parameters": {
                name: parameters[name].model_dump(mode="json") for name in sorted(parameters)
            },
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_revision_hash(
    config: WorkflowToolConfig, parameters: Mapping[str, ToolParameter]
) -> str:
    return compute_revision_hash(
        source=config.source_code,
        entrypoint=config.entrypoint,
        continuations=list(config.continuations),
        parameters=parameters,
    )


def stamp_revision(
    config: WorkflowToolConfig, parameters: Mapping[str, ToolParameter]
) -> WorkflowToolConfig:
    """Return a copy with ``revision_hash`` recomputed from content."""
    return config.model_copy(update={"revision_hash": config_revision_hash(config, parameters)})


def approve_revision(
    config: WorkflowToolConfig,
    parameters: Mapping[str, ToolParameter],
    *,
    approved_by: str,
) -> WorkflowToolConfig:
    """Approve the CURRENT content revision (recomputed, never trusted stored)."""
    revision = config_revision_hash(config, parameters)
    return config.model_copy(
        update={
            "revision_hash": revision,
            "approved_revision": revision,
            "approved_by": approved_by,
            "approved_at": utc_now(),
            "declined_by": None,
            "declined_at": None,
            "decline_note": None,
            "declined_revision": None,
        }
    )


def decline_revision(
    config: WorkflowToolConfig,
    parameters: Mapping[str, ToolParameter],
    *,
    declined_by: str,
    note: Optional[str] = None,
) -> WorkflowToolConfig:
    """Decline the current content revision (pins the declined hash).

    Declining REVOKES a prior approval of this same revision (the
    run_command re-gate precedent: revocation takes effect immediately); an
    approval of some older, different revision is left alone since the gate
    already refuses the current hash against it.
    """
    revision = config_revision_hash(config, parameters)
    update: Dict[str, Any] = {
        "revision_hash": revision,
        "declined_by": declined_by,
        "declined_at": utc_now(),
        "decline_note": (note or "")[:500] or None,
        "declined_revision": revision,
    }
    if config.approved_revision == revision:
        update.update(
            {"approved_revision": None, "approved_by": None, "approved_at": None}
        )
    return config.model_copy(update=update)


def workflow_execution_gate(
    config: WorkflowToolConfig, parameters: Mapping[str, ToolParameter]
) -> Optional[str]:
    """None when this revision may execute, else the refusal copy (fail closed).

    Recomputes the hash from content so a stale stored ``revision_hash`` can
    never satisfy the gate, and re-reads the approval fields at every
    execution so a revoked approval takes effect immediately.
    """
    current = config_revision_hash(config, parameters)
    if config.approved_revision == current:
        return None
    if config.declined_revision == current:
        note = f" ({config.decline_note})" if config.decline_note else ""
        return (
            "this workflow revision was declined by an admin"
            f"{note}; edit the workflow and request approval again"
        )
    if config.approved_revision:
        return (
            "this workflow changed since its approval (its source or parameters "
            "differ from the approved revision; raw on-disk edits count and "
            "leave it inert by design). Next: an admin approves the new "
            "revision (tool_create publish, /workflows approvals, or the "
            "dashboard Workflows card) before it can run"
        )
    return (
        "this workflow revision is not approved yet; an admin must approve it "
        "before it can run (admins approve their own saves automatically)"
    )


def approval_state(
    config: WorkflowToolConfig, parameters: Mapping[str, ToolParameter]
) -> str:
    """One-word approval status for list/detail surfaces."""
    current = config_revision_hash(config, parameters)
    if config.approved_revision == current:
        return "approved"
    if config.declined_revision == current:
        return "declined"
    return "pending"


# --- budget overrides ---------------------------------------------------------


def budget_from_config(config: WorkflowToolConfig) -> WorkflowBudget:
    """A WorkflowBudget applying the definition's clamped overrides."""
    overrides: Dict[str, Any] = {}
    if config.wall_clock_seconds is not None:
        overrides["wall_clock_seconds"] = float(config.wall_clock_seconds)
    if config.max_calls is not None:
        overrides["max_calls"] = int(config.max_calls)
    if config.max_ai_calls is not None:
        overrides["max_ai_calls"] = int(config.max_ai_calls)
    return WorkflowBudget(**overrides)


# --- source retention ---------------------------------------------------------


def retain_source_revision(tool_id: str, revision_hash: str, source: str) -> None:
    """Keep the last N source revisions beside the definitions for diffing.

    Best effort: retention failure never blocks a save. Files land under
    ``custom_tools_dir/revisions/<tool_id>/<hash-prefix>.py`` (the loader
    only globs ``*.json`` at its top level, so this cannot collide).
    """
    try:
        from ...config.settings import get_settings

        base = get_settings().custom_tools_dir / "revisions" / tool_id
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{revision_hash[:16]}.py"
        if not path.exists():
            path.write_text(source, encoding="utf-8")
        revisions = sorted(
            base.glob("*.py"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True
        )
        for stale in revisions[MAX_SOURCE_REVISIONS:]:
            stale.unlink(missing_ok=True)
    except Exception:
        logger.warning("workflow source retention failed for %s", tool_id, exc_info=True)


__all__ = [
    "MAX_SOURCE_REVISIONS",
    "RESERVED_PARAM_NAMES",
    "SUPPORTED_HINTS_HELP",
    "approval_state",
    "approve_revision",
    "budget_from_config",
    "compute_revision_hash",
    "config_revision_hash",
    "decline_revision",
    "derive_parameters",
    "retain_source_revision",
    "stamp_revision",
    "validate_workflow_static",
    "workflow_execution_gate",
]
