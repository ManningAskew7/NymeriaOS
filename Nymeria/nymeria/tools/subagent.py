"""Compatibility shim — real implementation moved to runtime_admin.py."""

from .runtime_admin import (  # noqa: F401
    reload_all,
    self_modify_rollback,
    RUNTIME_ADMIN_TOOLS,
    RUNTIME_ADMIN_TOOLS as SUBAGENT_TOOLS,
)
