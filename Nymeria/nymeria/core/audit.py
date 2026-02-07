"""Audit logging for Nymeria agent tool executions."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class AuditLogger:
    """Simple audit logger for tool executions."""

    def __init__(self, log_dir: Path, enabled: bool = True):
        self.log_dir = log_dir
        self.enabled = enabled
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_tool_call(
        self,
        tool_name: str,
        inputs: Dict[str, Any],
        output: Any,
        thread_id: str,
        duration_ms: float,
    ) -> None:
        """Log a tool execution to the audit log."""
        if not self.enabled:
            return

        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "thread_id": thread_id,
            "tool_name": tool_name,
            "inputs": inputs,
            "output": str(output)[:1000],  # Truncate large outputs
            "duration_ms": duration_ms,
        }

        log_file = self.log_dir / f"audit_{datetime.utcnow().strftime('%Y%m%d')}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
