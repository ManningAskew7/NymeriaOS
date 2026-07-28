"""``nym.state``: per-workflow persistent scratch (phase 5).

The cross-run memory a watcher workflow needs ("did the page change since
the last fire?") without abusing the user's semantic memory store. One JSON
document per (workflow_id, user_id): keyed per workflow so workflows never
collide, per user so two users' bindings of the same shared workflow tool
keep separate state. A draft and its published workflow share one tool id,
so state written during a test run carries into live fires.

Saved workflows only: an adhoc run has no durable identity to key on, so
the verbs refuse (the ``nym.approve`` precedent). The whole document is
capped at ``budget.state_cap_bytes`` (the approval-state cap), written
atomically (temp + replace, the approvals idiom), and all I/O runs
off-loop. Read-modify-write is serialized by an in-process lock, which is
sufficient because the API process is the only workflow runtime; a corrupt
document reads as empty (scratch state is derived: a watcher re-baselines).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from ...config import get_settings
from .registry import VerbContext, VerbError, register_verb

logger = logging.getLogger(__name__)

# Serializes read-modify-write across concurrent runs in this process (the
# single agent-runtime invariant makes an in-process lock the whole story).
_state_write_lock = threading.Lock()


def state_dir() -> Path:
    return get_settings().data_dir / "workflows" / "state"


def _state_path(workflow_id: str, user_id: str) -> Path:
    # Directory per workflow, file per user: the runs/<workflow_id>/ layout,
    # with no delimiter ambiguity between the two sanitized names. Uses the
    # canonical sanitizer (storage_paths is the single home for that rule).
    from ..storage_paths import safe_path_segment

    return (
        state_dir()
        / safe_path_segment(workflow_id)
        / f"{safe_path_segment(user_id)}.json"
    )


def _load_document(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise VerbError(f"workflow state store unreadable: {e}") from e
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("workflow state file %s is corrupt; reading as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def _store_document(path: Path, document: dict, cap_bytes: int) -> None:
    payload = json.dumps(document, ensure_ascii=False, default=str)
    size = len(payload.encode("utf-8"))
    if size > cap_bytes:
        # A SHRINKING write is always allowed: if the cap was lowered after
        # data was written, delete/set must still offer a recovery path out
        # of an over-cap document instead of locking it in place.
        try:
            old_size = path.stat().st_size
        except OSError:
            old_size = 0
        if size >= old_size:
            raise VerbError(
                f"workflow state exceeds the {cap_bytes}-byte cap; store "
                "less or delete unused keys"
            )
    from ..storage_paths import write_text_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, payload)


def _require_saved_workflow(ctx: VerbContext, verb: str) -> str:
    workflow_id = str(ctx.workflow_id or "").strip()
    if not workflow_id or workflow_id == "adhoc":
        raise VerbError(
            f"nym.{verb} needs a saved workflow: an adhoc run has no durable "
            "identity to key state on"
        )
    return workflow_id


def _clean_key(args: dict, verb: str) -> str:
    key = str(args.get("key") or "").strip()
    if not key:
        raise VerbError(f"nym.{verb} requires a non-empty string key")
    return key


@register_verb(
    "state.get",
    positional=("key",),
    description=(
        "Read a value from this workflow's persistent per-user state; "
        "returns default= (None if omitted) when the key is absent."
    ),
)
async def _state_get_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    key = _clean_key(args, "state.get")
    workflow_id = _require_saved_workflow(ctx, "state.get")
    document = await asyncio.to_thread(
        _load_document, _state_path(workflow_id, ctx.user_id)
    )
    return document.get(key, args.get("default"))


@register_verb(
    "state.set",
    side_effect=True,
    positional=("key", "value"),
    description=(
        "Store a JSON value in this workflow's persistent per-user state "
        "(survives across runs; document capped at the state byte cap)."
    ),
)
async def _state_set_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    key = _clean_key(args, "state.set")
    if "value" not in args:
        raise VerbError(
            "nym.state.set requires a value (use nym.state.delete to remove a key)"
        )
    value = args.get("value")
    workflow_id = _require_saved_workflow(ctx, "state.set")
    path = _state_path(workflow_id, ctx.user_id)
    cap = ctx.budget.state_cap_bytes

    def _apply() -> None:
        with _state_write_lock:
            document = _load_document(path)
            document[key] = value
            _store_document(path, document, cap)

    await asyncio.to_thread(_apply)
    return {"key": key, "stored": True}


@register_verb(
    "state.delete",
    side_effect=True,
    positional=("key",),
    description=(
        "Remove one key from this workflow's persistent per-user state; "
        "returns whether the key existed."
    ),
)
async def _state_delete_verb(ctx: VerbContext, verb: str, args: dict) -> Any:
    key = _clean_key(args, "state.delete")
    workflow_id = _require_saved_workflow(ctx, "state.delete")
    path = _state_path(workflow_id, ctx.user_id)
    cap = ctx.budget.state_cap_bytes

    def _apply() -> bool:
        with _state_write_lock:
            document = _load_document(path)
            existed = key in document
            if existed:
                del document[key]
                _store_document(path, document, cap)
            return existed

    deleted = await asyncio.to_thread(_apply)
    return {"key": key, "deleted": deleted}
