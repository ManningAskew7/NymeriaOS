"""Tool-group registry for catalog auto-discovery (backlog #51, Workstream A).

Each tool family self-registers a frozen :class:`ToolGroup` on import: the
``register_tool_group(ToolGroup(...))`` call sits at the bottom of the family
module and fires when ``tools/__init__.py`` imports it, exactly the way each
``triggers/sources/*.py`` file calls ``register_source`` and
``triggers/sources/__init__.py`` derives ``AVAILABLE_SOURCES`` from it.
``tools/__init__.py`` then derives ``CATALOG_TOOLS``, ``__all__``, the tool
count, and the role-gate sets (``ADMIN_ONLY_TOOL_NAMES`` /
``DEVELOPER_ONLY_TOOL_NAMES``) from :func:`all_tool_groups` instead of
hand-maintaining them.

Scope boundary: this registry owns LIST ASSEMBLY and the two ROLE FLAGS
(``admin_only`` / ``developer_only``) only. Category and security-level
metadata stay in ``tools/metadata.py`` (populated from the generated tool
objects, plus per-plugin overrides via ``register_plugin_tool_category``), so
there is no second source of truth for a tool's category.

The registry does not change how tools RESOLVE at graph build. The name->tool
mapping ``CATALOG_TOOLS`` produces is identical to the old hand-maintained
comprehension; only its ASSEMBLY moves from a hand-listed union to a derived
one. Group registration order becomes the dict's insertion order, which no
consumer's behavior depends on (every read is a keyed lookup, a membership
test, or an iterate-then-sort-by-name path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class ToolGroup:
    """A self-registered tool family.

    ``name`` is a short internal identifier for the family (for example
    ``"web_fetch"`` or ``"microsoft_graph"``); it is used only to key the
    registry and never surfaces to the model. ``tools`` is the ordered,
    immutable tuple of tool objects the family contributes to the catalog.
    ``admin_only`` / ``developer_only`` are the role flags that feed the
    ``ADMIN_ONLY_TOOL_NAMES`` / ``DEVELOPER_ONLY_TOOL_NAMES`` chokepoint.
    """

    name: str
    tools: Tuple[BaseTool, ...]
    admin_only: bool = False
    developer_only: bool = False


# Registry: group name -> ToolGroup. Keyed by name so a family that
# re-registers (for example after a module reload) replaces its own entry in
# place rather than duplicating it, and dict insertion order is preserved.
#
# Reload-survivable on purpose (#277): ``importlib.reload`` re-executes this
# body inside the SAME module namespace, so a plain ``= {}`` here wipes every
# family registered before this module's turn in a package-wide reload storm
# (the 2026-08-27 incident collapsed the catalog from 1256 to 263 names that
# way). Reusing the existing dict object keeps sibling registrations intact
# regardless of reload order; per-name replacement above keeps re-registration
# idempotent. A family module DELETED from disk therefore keeps its group
# until process restart, and a RENAMED group doubles up (the old name
# persists beside the new until restart): deleting or renaming builtin tool
# modules is a deploy operation.
_TOOL_GROUPS: Dict[str, ToolGroup] = globals().get("_TOOL_GROUPS", {})


def register_tool_group(group: ToolGroup) -> None:
    """Register (or replace, by name) a tool family's group.

    Called at module level by each family; last registration under a given
    name wins, which keeps a module reload idempotent (the same family
    re-registering an equivalent group replaces its entry in place).

    A re-registration may never RELAX a role gate, though. Silently replacing
    an ``admin_only`` or ``developer_only`` group with an ungated one under the
    same name would drop that family's tools out of the role chokepoint
    (``ADMIN_ONLY_TOOL_NAMES`` / ``DEVELOPER_ONLY_TOOL_NAMES``), a privilege
    escalation. That case raises at import time instead of shadowing, turning a
    silent gate-drop into a loud failure; the golden catalog tests are the
    backstop, this is defense in depth at the source. Tightening a gate
    (ungated -> gated) and same-flags reloads are both allowed.
    """
    existing = _TOOL_GROUPS.get(group.name)
    if existing is not None:
        if existing.admin_only and not group.admin_only:
            raise ValueError(
                f"tool group {group.name!r} is already registered admin_only; "
                "refusing to re-register it without the admin gate"
            )
        if existing.developer_only and not group.developer_only:
            raise ValueError(
                f"tool group {group.name!r} is already registered "
                "developer_only; refusing to re-register it without the "
                "developer gate"
            )
    _TOOL_GROUPS[group.name] = group


def all_tool_groups() -> Tuple[ToolGroup, ...]:
    """Every registered tool group, in registration (import) order."""
    return tuple(_TOOL_GROUPS.values())


def get_tool_group(name: str) -> Optional[ToolGroup]:
    """Return the group registered under ``name``, or ``None``."""
    return _TOOL_GROUPS.get(name)


def clear_tool_groups() -> None:
    """Drop every registered group. Test-support only."""
    _TOOL_GROUPS.clear()
