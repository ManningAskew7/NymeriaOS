"""Bundled workflow-template catalog: shipped nym-SDK recipes installable in
one command.

Templates ship with Nymeria as JSON files in ``nymeria/workflows_bundled/``
(mirroring bundled skills in ``skills_bundled/`` and bundled hook templates in
``hooks_bundled/``). Nothing exists until a user installs a template, which
publishes the template's source as a real workflow custom tool through the
ordinary authoring path (``tools/tool_create.py::install_workflow_template``),
so every validation and approval gate applies unchanged.

Unlike per-user hook templates, a published workflow tool lives in the GLOBAL
custom-tool registry and its execution is gated on an admin-approved revision,
so INSTALL is admin-gated (the install engine enforces this): the analogue of
"any user installs a per-user hook" is "an admin publishes an approved global
workflow tool". See ``install_workflow_template`` for the runtime half.

This module stays store-shaped (no agent/loader imports), like
``hook_templates``: it only loads and validates the bundled catalog. The
authoring-surface imports (the workflow config schema, the static validator)
are function-local, the repo's standard circular-import dodge, since this
module can be imported before the tools package finishes initializing.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# The installed tool id is the template id, so it must satisfy the workflow
# custom-tool id contract (lowercase, digits, underscores; 3-64 chars), the
# same pattern ``tool_create._normalize_tool_id`` enforces on authored tools.
_TEMPLATE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

_BUDGET_KEYS = ("wall_clock_seconds", "max_calls", "max_ai_calls")


class WorkflowTemplate(BaseModel):
    """One bundled workflow recipe: list metadata plus ready-to-publish source.

    ``workflow`` carries the fields the install path needs (``source_code``,
    ``entrypoint``, ``continuations``, ``budget``). It is a plain dict so a
    template file may only use fields that already exist on the authoring
    surface; :func:`_validate_template` dry-builds and statically validates the
    workflow config at scan time so a broken bundled file is skipped with a log
    line instead of surfacing at install time.
    """

    id: str = Field(..., min_length=3, max_length=64)
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1000)
    notes: str = Field(default="", max_length=4_000)
    workflow: dict

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _TEMPLATE_ID_PATTERN.match(value or ""):
            raise ValueError(
                "template id must be lowercase letters, digits, and underscores "
                "(3-64 chars, leading letter): it becomes the installed tool id"
            )
        return value


def _build_config(template: WorkflowTemplate) -> Any:
    """The ``WorkflowToolConfig`` a template installs (validated by pydantic)."""
    from ..tools.definitions.custom_tool_schema import WorkflowToolConfig

    workflow = template.workflow or {}
    budget = workflow.get("budget") or {}
    if not isinstance(budget, dict):
        raise ValueError("workflow.budget must be an object")
    unknown = set(budget) - set(_BUDGET_KEYS)
    if unknown:
        raise ValueError(
            f"unknown budget key(s): {', '.join(sorted(map(str, unknown)))}"
        )
    overrides = {k: budget[k] for k in _BUDGET_KEYS if budget.get(k) is not None}
    return WorkflowToolConfig(
        # Strip to match the install path (``_coerce_workflow_config`` strips),
        # so load-time validation and install validate the same bytes.
        source_code=str(workflow.get("source_code") or "").strip(),
        entrypoint=str(workflow.get("entrypoint") or "run"),
        continuations=[str(c) for c in (workflow.get("continuations") or [])],
        **overrides,
    )


def _validate_template(template: WorkflowTemplate) -> Dict[str, Any]:
    """Raise if the template would not survive publishing; return derived params.

    Runs the same static validation the authoring path runs at draft time
    (secret scan, top-level AST allowlist, entrypoint presence, continuation
    signatures) plus parameter derivation, so a bundled recipe is proven
    well-formed at load time rather than at install time.
    """
    from .workflows.authoring import validate_workflow_static

    config = _build_config(template)
    parameters, errors = validate_workflow_static(config=config)
    if errors:
        raise ValueError("; ".join(errors))
    return parameters


def load_templates(directory: Optional[Path] = None) -> List[WorkflowTemplate]:
    """All valid bundled workflow templates, sorted by id.

    ``directory`` defaults to ``settings.bundled_workflows_dir``. Invalid or
    unparseable files are skipped with a warning (bundled files are
    repo-shipped, so a bad one is a packaging bug, not user data; no
    quarantine). Never raises. The catalog is tiny and read only from
    authoring surfaces (never the per-turn path), so a fresh scan per call is
    fine.
    """
    if directory is None:
        from ..config import get_settings

        directory = get_settings().bundled_workflows_dir
    templates: List[WorkflowTemplate] = []
    root = Path(directory)
    if not root.is_dir():
        # Say so rather than presenting an empty catalog as "nothing shipped":
        # the dir is repo-shipped, so its absence is a packaging bug (e.g. the
        # glob missing from pyproject's package-data), and silence there reads
        # like the feature simply does not exist.
        logger.warning(
            "Bundled workflow-template directory %s does not exist; the "
            "catalog will be empty (packaging bug?)",
            root,
        )
        return templates
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        logger.warning("Cannot read bundled workflow templates in %s", root, exc_info=True)
        return templates
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            template = WorkflowTemplate.model_validate(data)
            _validate_template(template)
        except Exception:  # noqa: BLE001 - a bad bundled file must not break authoring
            logger.warning(
                "Skipping invalid bundled workflow template %s", path, exc_info=True
            )
            continue
        templates.append(template)
    templates.sort(key=lambda t: t.id)
    return templates


def get_template(
    template_id: str, directory: Optional[Path] = None
) -> Optional[WorkflowTemplate]:
    """One template by id, or None."""
    for template in load_templates(directory):
        if template.id == template_id:
            return template
    return None


def template_parameter_names(template: WorkflowTemplate) -> List[str]:
    """The derived parameter names of a template (for list surfaces).

    Best effort: returns ``[]`` if the template cannot be validated (it would
    already have been skipped by :func:`load_templates`). Derived from the
    template's own source, so it needs no directory.
    """
    try:
        return sorted(_validate_template(template).keys())
    except Exception:  # noqa: BLE001 - list enrichment must not raise
        return []
