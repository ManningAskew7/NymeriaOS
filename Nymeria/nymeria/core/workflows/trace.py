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
    ) -> None:
        self.steps.append(
            StepRecord(
                step=len(self.steps) + 1,
                verb=verb,
                status=status,
                duration_ms=duration_ms,
                args_summary=_summarize(args),
                result_summary=_summarize(result) if result is not None else "",
                error_kind=error_kind,
            )
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "steps": [s.to_dict() for s in self.steps],
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def persist_run_record(trace: StepTrace, envelope_dict: dict) -> Optional[Path]:
    """Write one run record and prune old ones; sync, call off-loop.

    Never raises: run records are observability, and losing one must not fail
    the run that produced it.
    """
    try:
        from ...config import get_settings

        runs_dir = (
            Path(get_settings().data_dir)
            / "workflows"
            / "runs"
            / trace.workflow_id
        )
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{trace.run_id}.json"
        record = {"envelope": envelope_dict, "trace": trace.to_dict()}
        path.write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )
        _prune(runs_dir)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("workflow run record write failed", exc_info=True)
        return None


def _prune(runs_dir: Path, cap: Optional[int] = None) -> None:
    # Read the module constant at call time (not as a bound default) so tests
    # and a future settings override both take effect.
    cap = MAX_RUN_RECORDS_PER_WORKFLOW if cap is None else cap
    # Nanosecond mtime with the filename as a tiebreak, so rapid successive
    # runs (equal coarse mtime) prune deterministically newest-first.
    records = sorted(
        runs_dir.glob("*.json"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    for stale in records[cap:]:
        try:
            stale.unlink()
        except OSError:
            pass  # a race removed it, or it is read-only; retention is best-effort
