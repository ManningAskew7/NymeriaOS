"""Bundled hook-template catalog: canned recipes installable in one command.

Templates ship with Nymeria as JSON files in ``nymeria/hooks_bundled/``
(mirroring how bundled skills ship in ``skills_bundled/``). Nothing fires
until a user explicitly installs a template, which is a plain
``HookManager.add_hook`` with the template's pre-filled fields, so every
authoring gate and validation path applies unchanged. Installs are
idempotent: a hook already installed from the same template (same scope and
binding) is returned instead of duplicated, keyed by the ``template``
provenance field on ``HookDefinition``.

This module stays store-shaped (no engine imports), like ``hook_manager``:
the three authoring surfaces (the ``/hook`` command, the ``hook_config``
tool, and the REST router) all install through :func:`install_template`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from .hook_manager import (
    HookDefinition,
    HookManager,
    run_command_authoring_error,
)

logger = logging.getLogger(__name__)

# Dummy id used only for dry-run validation at scan time (never persisted).
_VALIDATION_ID = "00000000"


class HookTemplate(BaseModel):
    """One bundled hook recipe: list metadata plus ready-to-create hook fields.

    ``hook`` carries the ``add_hook`` field values (name, event, action,
    params, matcher, fire_conditions, once, scope, enabled). It is kept as a
    plain dict so a template file may only use fields that already exist on
    the authoring surface; :func:`_validate_template` dry-builds a
    ``HookDefinition`` from it at scan time so a broken bundled file is
    skipped with a log line instead of surfacing at install time.
    """

    id: str = Field(..., min_length=1, max_length=100)
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=4_000)
    hook: dict


def _definition_payload(template: HookTemplate) -> dict:
    """The ``HookDefinition.model_validate`` payload for a template's hook."""
    hook = template.hook
    action = (hook.get("action") or "inject_context").strip()
    params = hook.get("params") or {}
    return {
        "id": _VALIDATION_ID,
        "name": hook.get("name") or template.id,
        "event": hook.get("event") or "",
        "matcher": hook.get("matcher"),
        "fire_conditions": hook.get("fire_conditions") or [],
        "once": bool(hook.get("once", False)),
        "single_use": bool(hook.get("single_use", False)),
        "logic": {"action": action, **params},
        "enabled": bool(hook.get("enabled", True)),
        "scope": hook.get("scope") or "global",
        "thread_id": "",
        "template": template.id,
        "created_by": "user",
    }


def _validate_template(template: HookTemplate) -> None:
    """Raise if the template's hook payload would not survive ``add_hook``."""
    HookDefinition.model_validate(_definition_payload(template))


def load_templates(directory: Optional[Path] = None) -> List[HookTemplate]:
    """All valid bundled templates, sorted by id.

    ``directory`` defaults to ``settings.bundled_hooks_dir``. Invalid or
    unparseable files are skipped with a warning (bundled files are
    repo-shipped, so a bad one is a packaging bug, not user data; no
    quarantine). Never raises. The catalog is tiny and read only from
    authoring surfaces (never the per-turn path), so a fresh scan per call
    is fine.
    """
    if directory is None:
        from ..config import get_settings

        directory = get_settings().bundled_hooks_dir
    templates: List[HookTemplate] = []
    root = Path(directory)
    if not root.is_dir():
        return templates
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            template = HookTemplate.model_validate(data)
            _validate_template(template)
        except Exception:  # noqa: BLE001 - a bad bundled file must not break authoring
            logger.warning("Skipping invalid bundled hook template %s", path, exc_info=True)
            continue
        templates.append(template)
    templates.sort(key=lambda t: t.id)
    return templates


def get_template(
    template_id: str, directory: Optional[Path] = None
) -> Optional[HookTemplate]:
    """One template by id, or None."""
    for template in load_templates(directory):
        if template.id == template_id:
            return template
    return None


def find_installed(
    manager: HookManager,
    user_id: str,
    template_id: str,
    *,
    scope: str,
    thread_id: str,
) -> Optional[HookDefinition]:
    """An existing hook installed from ``template_id`` with the same binding."""
    for hook in manager.get_hooks(user_id):
        if hook.template != template_id or hook.scope != scope:
            continue
        if scope == "global" or hook.thread_id == thread_id:
            return hook
    return None


def install_template(
    manager: HookManager,
    user_id: str,
    template_id: str,
    *,
    scope: Optional[str] = None,
    thread_id: Optional[str] = None,
    text: Optional[str] = None,
    enabled: Optional[bool] = None,
    created_by: str = "user",
    is_admin: Optional[bool] = None,
    directory: Optional[Path] = None,
) -> Tuple[Optional[HookDefinition], bool]:
    """Install a bundled template as a real hook for ``user_id``.

    Returns ``(hook, created)``: ``created=False`` means an identical install
    already existed and is returned unchanged (idempotent re-install);
    ``hook=None`` means the per-user hook cap was hit. Overrides: ``scope``
    (+ ``thread_id`` for a thread binding, which the SURFACE must have
    access-checked), ``text`` (the text-action body / approval prompt), and
    ``enabled``. Raises ``ValueError`` on an unknown template, an illegal
    override, or a gated action the caller may not author (``is_admin``
    follows ``run_command_authoring_error`` semantics: None = trusted local
    context). Nothing in the bundled catalog uses a gated action today; the
    check exists so a future template cannot bypass the create-time gate.
    """
    template = get_template(template_id, directory)
    if template is None:
        known = ", ".join(t.id for t in load_templates(directory)) or "(none)"
        raise ValueError(f"Unknown template '{template_id}'. Available: {known}.")
    hook_fields = dict(template.hook)
    action = (hook_fields.get("action") or "inject_context").strip()
    gate_reason = run_command_authoring_error(action, is_admin=is_admin)
    if gate_reason:
        raise ValueError(gate_reason)
    effective_scope = (scope or hook_fields.get("scope") or "global").strip().lower()
    if effective_scope not in ("thread", "global"):
        raise ValueError("scope must be 'thread' or 'global'.")
    effective_thread = (thread_id or "") if effective_scope == "thread" else ""
    if effective_scope == "thread" and not effective_thread:
        raise ValueError("A thread-scoped install requires a thread binding.")
    existing = find_installed(
        manager, user_id, template.id, scope=effective_scope, thread_id=effective_thread
    )
    if existing is not None:
        return existing, False
    params = dict(hook_fields.get("params") or {})
    if text is not None:
        # The same bare-text alias every authoring surface uses.
        params["prompt" if action == "require_approval" else "text"] = text
    hook = manager.add_hook(
        user_id,
        name=hook_fields.get("name") or template.id,
        event=hook_fields.get("event") or "",
        action=action,
        params=params or None,
        matcher=hook_fields.get("matcher"),
        fire_conditions=hook_fields.get("fire_conditions") or None,
        once=bool(hook_fields.get("once", False)),
        single_use=bool(hook_fields.get("single_use", False)),
        scope=effective_scope,
        thread_id=effective_thread,
        enabled=(bool(hook_fields.get("enabled", True)) if enabled is None else bool(enabled)),
        created_by=created_by,
        template=template.id,
    )
    return hook, True
