"""Per-run step trace and run-record persistence.

Observability ONLY, never replayed (plan: "Durability"). One record per
``nym.*`` call plus the child's captured stdout/stderr, written as a single
JSON run record at run end under ``data_dir/workflows/runs/<workflow_id>/``,
pruned to a per-workflow cap (the hook execution-log retention idiom, sized
the same).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from ..storage_paths import mtime_sort_key

logger = logging.getLogger(__name__)

MAX_RUN_RECORDS_PER_WORKFLOW = 200
_SUMMARY_CAP = 500


def _summarize(value: Any, cap: int = _SUMMARY_CAP) -> str:
    """A bounded, always-serializable one-line summary of args or a result."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - a summary must never fail
        text = str(value)
    text = text.replace("\n", " ")
    if len(text) > cap:
        return text[: cap - 3] + "..."
    return text


@dataclass
class StepRecord:
    step: int
    verb: str
    status: str  # "ok" | "error"
    duration_ms: int
    args_summary: str
    result_summary: str = ""
    error_kind: Optional[str] = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "step": self.step,
            "verb": self.verb,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "args": self.args_summary,
            "result": self.result_summary,
        }
        if self.error_kind:
            out["error_kind"] = self.error_kind
        return out


@dataclass
class StepTrace:
    run_id: str
    workflow_id: str
    steps: List[StepRecord] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""

    def record(
        self,
        *,
        verb: str,
        status: str,
        duration_ms: int,
        args: Any,
        result: Any = None,
        error_kind: Optional[str] = None,
    ) -> StepRecord:
        record = StepRecord(
            step=len(self.steps) + 1,
            verb=verb,
            status=status,
            duration_ms=duration_ms,
            args_summary=_summarize(args),
            result_summary=_summarize(result) if result is not None else "",
            error_kind=error_kind,
        )
        self.steps.append(record)
        return record

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "steps": [s.to_dict() for s in self.steps],
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def persist_run_record(
    trace: StepTrace,
    envelope_dict: dict,
    *,
    user_id: str = "",
    thread_id: str = "",
) -> Optional[Path]:
    """Write one run record and prune old ones; sync, call off-loop.

    Records carry ``user_id``/``thread_id``/``status``/``timestamp`` so the
    read surfaces can scope runs to their owner and sort them without parsing
    the envelope. Never raises: run records are observability, and losing one
    must not fail the run that produced it.
    """
    try:
        from datetime import timezone
        import datetime as _datetime

        from ...config import get_settings

        runs_dir = (
            Path(get_settings().data_dir)
            / "workflows"
            / "runs"
            / trace.workflow_id
        )
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{trace.run_id}.json"
        record = {
            "run_id": trace.run_id,
            "workflow_id": trace.workflow_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "status": str(envelope_dict.get("status") or ""),
            "timestamp": _datetime.datetime.now(timezone.utc).isoformat(),
            "envelope": envelope_dict,
            "trace": trace.to_dict(),
        }
        path.write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )
        _prune(runs_dir)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("workflow run record write failed", exc_info=True)
        return None


def read_run_records(
    workflow_id: str,
    *,
    limit: int = 20,
    user_id: Optional[str] = None,
) -> List[dict]:
    """Read recent run records for a workflow, newest first; sync, call off-loop.

    ``user_id`` filters to one owner's runs (pass None for the admin view).
    Unreadable files are skipped: this is a debugging surface, not a ledger.
    """
    try:
        from ...config import get_settings

        runs_dir = Path(get_settings().data_dir) / "workflows" / "runs" / workflow_id
        if not runs_dir.is_dir():
            return []
        paths = sorted(runs_dir.glob("*.json"), key=mtime_sort_key, reverse=True)
        records: List[dict] = []
        for path in paths:
            if len(records) >= max(1, limit):
                break
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if user_id is not None and record.get("user_id") != user_id:
                continue
            records.append(record)
        return records
    except Exception:  # noqa: BLE001 - a read surface must never raise into a turn
        logger.warning("workflow run record read failed", exc_info=True)
        return []


# Newest-first bound on how many run FILES one aggregate read will open when
# filtering for a specific owner (a user with few runs on a busy multi-user
# store must not force a read+parse of the entire runs tree per request).
MAX_AGGREGATE_SCAN_FILES = 500


def read_recent_run_records(
    *, limit: int = 20, user_id: Optional[str] = None
) -> List[dict]:
    """Read recent run records across ALL workflows, newest first; sync,
    call off-loop.

    The dashboard-feed shape: one owner's (or, for the admin view, every
    user's) latest runs regardless of workflow. Same skip-unreadable
    semantics as ``read_run_records``; additionally the owner filter stops
    scanning after ``MAX_AGGREGATE_SCAN_FILES`` newest files, so a sparse
    owner's feed may miss runs older than that window (a bounded read beats
    an unbounded one for a dashboard surface).
    """
    try:
        from ...config import get_settings

        runs_root = Path(get_settings().data_dir) / "workflows" / "runs"
        if not runs_root.is_dir():
            return []

        paths = sorted(runs_root.glob("*/*.json"), key=mtime_sort_key, reverse=True)
        records: List[dict] = []
        for scanned, path in enumerate(paths):
            if len(records) >= max(1, limit):
                break
            if scanned >= MAX_AGGREGATE_SCAN_FILES:
                break
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if user_id is not None and record.get("user_id") != user_id:
                continue
            records.append(record)
        return records
    except Exception:  # noqa: BLE001 - a read surface must never raise into a turn
        logger.warning("workflow run record aggregate read failed", exc_info=True)
        return []


def _prune(runs_dir: Path, cap: Optional[int] = None) -> None:
    # Read the module constant at call time (not as a bound default) so tests
    # and a future settings override both take effect.
    cap = MAX_RUN_RECORDS_PER_WORKFLOW if cap is None else cap
    # Nanosecond mtime with the filename as a tiebreak, so rapid successive
    # runs (equal coarse mtime) prune deterministically newest-first.
    records = sorted(runs_dir.glob("*.json"), key=mtime_sort_key, reverse=True)
    for stale in records[cap:]:
        try:
            stale.unlink()
        except OSError:
            pass  # a race removed it, or it is read-only; retention is best-effort
