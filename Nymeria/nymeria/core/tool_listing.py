"""Tool listings that stay inside the command output budget (#101 entry 27b).

The optional-tools view used to print every optional tool name, grouped by
category: 32k characters once the integration catalog grew past a thousand
tools, so every compact surface (the agent, the chat platforms) truncated it
inside the first category and never showed the rest. ``/tools list
integrations`` was worse (75k). A clipped listing is silently wrong, so these
views are indexes that drill down instead: category, then integration group,
then integration service, each small enough to print whole.

Everything here is a pure function over the ``available_tools`` payload of
``GET /tools/defaults`` (``serialize_default_tools``), whose integration
entries carry ``group``/``group_label``/``service``/``service_label``. The
keys resolve from the CALLER's payload, which is already role-filtered, so a
group or service a caller cannot see does not resolve for them either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

INTEGRATIONS = "integrations"
# Example names shown per category in the index; the rest is a count.
_INDEX_EXAMPLES = 4


@dataclass(frozen=True)
class IntegrationTarget:
    """An integration group or service key and the tools it names."""

    kind: str  # "group" or "service"
    key: str
    label: str
    tools: tuple[Mapping[str, Any], ...]

    @property
    def names(self) -> list[str]:
        return [str(t["name"]) for t in self.tools]

    def describe(self, *, count: bool = False) -> str:
        """``integration group 'productivity' (Productivity & Collaboration)``;
        with ``count``, the tool count joins the same parenthetical. The label
        is dropped when it only re-cases the key (``github``/``GitHub``)."""
        parts = []
        if self.label and self.label.lower() != self.key:
            parts.append(self.label)
        if count:
            parts.append(f"{len(self.tools)} tools")
        suffix = f" ({', '.join(parts)})" if parts else ""
        return f"integration {self.kind} '{self.key}'{suffix}"


def normalize_key(value: str) -> str:
    return value.lower().strip().replace("-", "_")


def _integration_tools(available: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [t for t in available if t.get("category") == INTEGRATIONS and t.get("name")]


def integration_targets(available: Iterable[Mapping[str, Any]]) -> list[IntegrationTarget]:
    """Every group, then every service, in the caller's catalog (sorted by key
    within each kind): the option resolver's rows and the index's buckets."""
    tools = _integration_tools(available)
    out: list[IntegrationTarget] = []
    for kind in ("group", "service"):
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for t in tools:
            key = str(t.get(kind) or "")
            if key:
                buckets.setdefault(key, []).append(t)
        for key in sorted(buckets):
            members = tuple(buckets[key])
            label = str(members[0].get(f"{kind}_label") or key)
            out.append(IntegrationTarget(kind=kind, key=key, label=label, tools=members))
    return out


def resolve_integration_target(
    available: Iterable[Mapping[str, Any]], key: str
) -> IntegrationTarget | None:
    """The integration group, else service, that ``key`` names, or None.

    Group before service: the registries keep the two key spaces disjoint
    (``tests/test_tool_listing.py`` ratchets it), so the order only matters
    for a fallback service key synthesized from an unregistered tool prefix.
    """
    wanted = normalize_key(key)
    if not wanted:
        return None
    tools = _integration_tools(available)
    for kind in ("group", "service"):
        matched = tuple(t for t in tools if str(t.get(kind) or "") == wanted)
        if matched:
            label = str(matched[0].get(f"{kind}_label") or wanted)
            return IntegrationTarget(kind=kind, key=wanted, label=label, tools=matched)
    return None


def _by(tools: Iterable[Mapping[str, Any]], field: str) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {}
    for t in tools:
        out.setdefault(str(t.get(field) or "other"), []).append(t)
    return out


def _enabled_note(tools: Sequence[Mapping[str, Any]], enabled: set[str]) -> str:
    on = sum(1 for t in tools if t["name"] in enabled)
    return f", {on} enabled here" if on else ""


def first_line(text: Any, limit: int = 60) -> str:
    """A description's first line, clipped: the listings' one-line form."""
    return str(text or "").split("\n")[0][:limit]


def render_optional_index(
    available: Sequence[Mapping[str, Any]],
    default_names: set[str],
    enabled: set[str],
) -> str:
    """Every optional category with counts and a few example names;
    ``integrations`` expands one level into its groups and their service keys."""
    optional = [t for t in available if t.get("name") and t["name"] not in default_names]
    cats = _by(optional, "category")
    enabled_total = sum(1 for t in optional if t["name"] in enabled)
    lines = [
        f"Optional Tools: {len(optional)} tools in {len(cats)} categories"
        + (f" ({enabled_total} enabled on this thread)" if enabled_total else ""),
        "Drill down with `/tools list <category>`, an integration group, or a service"
        " (`/tools list github`); `/tools list optional --all` prints every name.",
    ]
    for cat_name in sorted(cats):
        entries = cats[cat_name]
        lines.append("")
        head = f"{cat_name} ({len(entries)}{_enabled_note(entries, enabled)})"
        if cat_name == INTEGRATIONS:
            groups = _by(entries, "group")
            lines.append(f"{head}: {len(groups)} groups")
            for group_key in sorted(groups):
                members = groups[group_key]
                label = str(members[0].get("group_label") or group_key)
                services = sorted(_by(members, "service"))
                lines.append(
                    f"  {group_key}: {label} ({len(members)}"
                    f"{_enabled_note(members, enabled)}): {', '.join(services)}"
                )
            continue
        names = sorted(t["name"] for t in entries)
        shown = ", ".join(names[:_INDEX_EXAMPLES])
        more = f", +{len(names) - _INDEX_EXAMPLES} more" if len(names) > _INDEX_EXAMPLES else ""
        lines.append(f"{head}: {shown}{more}")
    return "\n".join(lines)


def render_optional_all(
    available: Sequence[Mapping[str, Any]],
    default_names: set[str],
    enabled: set[str],
) -> str:
    """The pre-index view: every optional tool name, by category. Only on
    request (``--all``); roomy surfaces print it whole."""
    optional = [t for t in available if t.get("name") and t["name"] not in default_names]
    cats = _by(optional, "category")
    lines = [f"Optional Tools: {len(optional)} tools in {len(cats)} categories"]
    for cat_name in sorted(cats):
        entries = cats[cat_name]
        active = sum(1 for t in entries if t["name"] in enabled)
        tag = f" ({active} enabled on this thread)" if active else ""
        lines.append("")
        lines.append(f"{cat_name} ({len(entries)}){tag}")
        lines.append(f"  {', '.join(t['name'] for t in entries)}")
    return "\n".join(lines)


def render_integrations_index(
    available: Sequence[Mapping[str, Any]], enabled: set[str]
) -> str:
    """``/tools list integrations``: group, then service key and tool count."""
    tools = _integration_tools(available)
    groups = _by(tools, "group")
    service_count = len(_by(tools, "service"))
    lines = [
        f"Tools in category '{INTEGRATIONS}': {len(tools)} tools in {len(groups)} groups,"
        f" {service_count} services",
        "List one with `/tools list <group>` or `/tools list <service>`; enable one the"
        " same way (`/tools enable github`).",
    ]
    for group_key in sorted(groups):
        members = groups[group_key]
        label = str(members[0].get("group_label") or group_key)
        lines.append("")
        lines.append(f"{group_key}: {label} ({len(members)}{_enabled_note(members, enabled)})")
        services = _by(members, "service")
        parts = []
        for service_key in sorted(services):
            svc = services[service_key]
            on = sum(1 for t in svc if t["name"] in enabled)
            parts.append(f"{service_key} ({len(svc)}{f', {on} on' if on else ''})")
        lines.append(f"  {', '.join(parts)}")
    return "\n".join(lines)


def render_integration_target(
    target: IntegrationTarget,
    enabled: set[str],
    default_names: set[str],
    tool_names: set[str] | None = None,
) -> str:
    """A service lists its tools with descriptions; a group (up to ~120
    tools) lists names per service, to stay printable whole.

    ``tool_names`` is the caller's whole catalog: `/tools enable <key>` binds
    a TOOL of that exact name first (the tool `calculator` shares the service
    key `calculator`), so the enable-them-all hint is only printed when it is
    true."""
    count = len(target.tools)
    key_is_a_tool = target.key in (tool_names or set())
    hint_is_true = not key_is_a_tool or target.names == [target.key]
    if target.kind == "service":
        lines = [f"Tools in {target.describe()}: {count} tools"]
        for t in sorted(target.tools, key=lambda t: t["name"]):
            mark = "[on] " if t["name"] in enabled else "[off]"
            tag = " (core)" if t["name"] in default_names else ""
            desc = first_line(t.get("description"))
            lines.append(f"  {mark} {t['name']}{tag}" + (f": {desc}" if desc else ""))
        if hint_is_true:
            lines.append(f"Enable them all with `/tools enable {target.key}`.")
        return "\n".join(lines)
    services = _by(target.tools, "service")
    lines = [
        f"Tools in {target.describe()}: {count} tools in {len(services)} services",
        "Descriptions: `/tools list <service>`. Enabled tools are marked `[on]`.",
    ]
    for service_key in sorted(services):
        svc = sorted(services[service_key], key=lambda t: t["name"])
        names = ", ".join(
            ("[on] " if t["name"] in enabled else "") + t["name"] for t in svc
        )
        lines.append("")
        lines.append(f"{service_key} ({len(svc)}): {names}")
    if hint_is_true:
        lines.append("")
        lines.append(f"Enable the whole group with `/tools enable {target.key}`.")
    return "\n".join(lines)


__all__ = [
    "INTEGRATIONS",
    "IntegrationTarget",
    "first_line",
    "integration_targets",
    "normalize_key",
    "render_integration_target",
    "render_integrations_index",
    "render_optional_all",
    "render_optional_index",
    "resolve_integration_target",
]
