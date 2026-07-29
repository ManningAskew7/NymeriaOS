"""CLI transport clients."""

from .base import AgentClient

# `InProcessAgentClient` is deliberately NOT re-exported here. Only the fat CLI
# (`--transport local`) ever builds one, and `.in_process` reaches into
# `nymeria.core`; before that package's init went lazy, this one line cost
# every importer of this package the entire agent stack. Nothing imported the
# name from here anyway (`cli/app.py` and both test modules import
# `.in_process` directly), so the re-export was pure liability.
__all__ = ["AgentClient"]
