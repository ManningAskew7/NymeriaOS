# core/

Platform services and agent orchestration. The authoritative area map, with
per-subsystem traps, is the `core/` rows of the backend agent guide (a maintainer doc).
Start at `agent.py` (`NymeriaAgent`) and its `agent_*.py` extraction family.
Several files here are very large (`agent.py`, `command_service.py`); splitting
is tracked separately and should not be attempted as part of routine changes.
