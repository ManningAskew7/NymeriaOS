"""`/tools list` stays printable on the REAL catalog (#101 entry 27b).

The optional view printed every optional tool name: 32k characters once the
integration catalog passed a thousand tools, so the 12k compact budget (the
agent, every chat platform) cut it inside the first category and the other
categories never appeared; `/tools list integrations` was 75k. These tests
feed the real in-process catalog (the same fields `GET /tools/defaults`
serves) into the fake command API, because the shared fixture's two-tool
catalog makes every size assertion vacuous.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from nymeria.core.command_service import OUTPUT_BUDGET_COMPACT
from nymeria.core.tool_listing import INTEGRATIONS
from test_command_service import FakeCommandApi, _run_command

_TRUNCATED = "Output truncated"
# Stand-ins for installed MCP servers, which the test process has none of
# (the live Docker stack carries 74); twice that, so growth is covered.
_FAKE_MCP_TOOLS = 150


def _real_catalog() -> tuple[list[dict[str, Any]], list[str]]:
    """What serialize_default_tools emits for an admin, minus the agent."""
    from nymeria.tools import CATALOG_TOOLS, SEED_TOOLS
    from nymeria.tools.metadata import get_tool_metadata, integration_grouping_fields

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in list(SEED_TOOLS) + list(CATALOG_TOOLS.values()):
        if t.name in seen:
            continue
        seen.add(t.name)
        meta = get_tool_metadata(t.name)
        category = meta.category.value if meta else "general"
        out.append({
            "name": t.name,
            "description": t.description,
            "category": category,
            **integration_grouping_fields(t.name, category),
        })
    for i in range(_FAKE_MCP_TOOLS):
        out.append({
            "name": f"mcp__server{i // 25}__tool_{i}",
            "description": "An installed MCP server tool",
            "category": "mcp_server",
            **integration_grouping_fields("x", "mcp_server"),
        })
    return out, [t.name for t in SEED_TOOLS]


_CATALOG, _SEED = _real_catalog()


class _RealCatalogApi(FakeCommandApi):
    def __init__(self, catalog: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.catalog = list(_CATALOG if catalog is None else catalog)
        self.default_tools = list(_SEED)

    async def get_default_tools(self, user_id: str = "default") -> dict[str, Any]:
        self.calls.append(("get_default_tools", (user_id,), {}))
        return {"default_tools": list(self.default_tools), "available_tools": list(self.catalog)}

    async def get_tool_categories(self) -> dict[str, Any]:
        cats: dict[str, list[str]] = {}
        for t in self.catalog:
            cats.setdefault(t["category"], []).append(t["name"])
        return {"categories": cats}


def _integration(field: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in _CATALOG:
        if t["category"] == INTEGRATIONS:
            out.setdefault(t[field], []).append(t["name"])
    return out


GROUPS = _integration("group")
SERVICES = _integration("service")


def _optional() -> list[dict[str, Any]]:
    return [t for t in _CATALOG if t["name"] not in set(_SEED)]


def test_the_catalog_is_big_enough_to_matter():
    # The premise: without the index the every-name view overflows compact.
    names_chars = sum(len(t["name"]) + 2 for t in _optional())
    assert names_chars > OUTPUT_BUDGET_COMPACT
    assert len(GROUPS) >= 10 and len(SERVICES) >= 100


@pytest.mark.parametrize("surface", ["agent", "telegram", "cli"])
def test_the_optional_index_names_every_category_whole(surface):
    result = _run_command(_RealCatalogApi(), "/tools list optional", surface=surface)
    text = result.markdown
    assert result.success is True, text
    assert _TRUNCATED not in text
    assert len(text) <= OUTPUT_BUDGET_COMPACT
    optional = _optional()
    categories = {t["category"] for t in optional}
    header = f"Optional Tools: {len(optional)} tools in {len(categories)} categories"
    # The command layer renders the first line as a heading.
    assert text.splitlines()[0].lstrip("# ") == header
    counted = 0
    for cat in categories:
        size = sum(1 for t in optional if t["category"] == cat)
        assert f"\n{cat} ({size}" in text, cat
        counted += size
    assert counted == len(optional)
    for group in GROUPS:
        assert f"  {group}: " in text, group
    for service in SERVICES:
        assert service in text, service
    # Examples, capped: a big category shows four names and a count of the rest.
    browser = sorted(t["name"] for t in optional if t["category"] == "browser")
    assert f"browser ({len(browser)}): {', '.join(browser[:4])}, +{len(browser) - 4} more" in text


def test_the_old_spelling_and_the_all_flag():
    api = _RealCatalogApi()
    index = _run_command(api, "/tools optional", surface="agent")
    assert index.success is True and _TRUNCATED not in index.markdown
    every = _run_command(api, "/tools list optional --all", surface="cli")
    assert every.success is True, every.markdown
    assert len(every.markdown) > OUTPUT_BUDGET_COMPACT  # the old wall, on request
    for t in _optional():
        assert t["name"] in every.markdown
    alias = _run_command(api, "/tools optional --all", surface="cli")
    assert alias.markdown == every.markdown


def test_enabled_counts_reach_the_index():
    api = _RealCatalogApi()
    github = sorted(SERVICES["github"])[:2]
    browser = sorted(t["name"] for t in _optional() if t["category"] == "browser")[:1]
    api.thread_config["enabled_tools"] = github + browser
    text = _run_command(api, "/tools list optional", surface="agent").markdown
    assert "(3 enabled on this thread)" in text.splitlines()[0]
    size = len(GROUPS["developer_tools"])
    assert f"  developer_tools: Developer Tools ({size}, 2 enabled here): " in text
    browser_size = sum(1 for t in _optional() if t["category"] == "browser")
    assert f"browser ({browser_size}, 1 enabled here)" in text


@pytest.mark.parametrize("surface", ["agent", "discord"])
def test_the_integrations_listing_is_an_index_that_fits(surface):
    result = _run_command(_RealCatalogApi(), f"/tools list {INTEGRATIONS}", surface=surface)
    text = result.markdown
    assert result.success is True, text
    assert _TRUNCATED not in text and len(text) <= OUTPUT_BUDGET_COMPACT
    for group in GROUPS:
        assert f"\n{group}: " in text, group
    for service, names in SERVICES.items():
        assert f"{service} ({len(names)}" in text, service


def test_a_service_lists_exactly_its_tools():
    api = _RealCatalogApi()
    api.thread_config["enabled_tools"] = [sorted(SERVICES["github"])[0]]
    text = _run_command(api, "/tools list github", surface="agent").markdown
    listed = [line.strip().split("] ", 1)[1].split(":")[0].split(" (")[0].strip()
              for line in text.splitlines() if line.strip().startswith("[")]
    assert sorted(listed) == sorted(SERVICES["github"])
    assert text.count("[on] ") == 1
    assert "integration service 'github'" in text.splitlines()[0]
    assert "Enable them all with `/tools enable github`." in text


def test_a_group_lists_every_tool_of_its_services():
    group = max(GROUPS, key=lambda g: len(GROUPS[g]))  # the largest one
    api = _RealCatalogApi()
    api.thread_config["enabled_tools"] = [sorted(GROUPS[group])[0]]
    text = _run_command(api, f"/tools list {group}", surface="agent").markdown
    assert _TRUNCATED not in text and len(text) <= OUTPUT_BUDGET_COMPACT
    assert f"integration group '{group}'" in text.splitlines()[0]
    for name in GROUPS[group]:
        assert name in text
    assert text.count("[on] ") == 1
    assert f"Enable the whole group with `/tools enable {group}`." in text


def test_every_group_and_service_view_fits_the_compact_budget():
    api = _RealCatalogApi()
    for key in list(GROUPS) + list(SERVICES):
        result = _run_command(api, f"/tools list {key}", surface="agent")
        assert result.success is True, (key, result.markdown)
        assert _TRUNCATED not in result.markdown, key


def test_an_unknown_filter_names_the_categories_and_the_drill_down():
    result = _run_command(_RealCatalogApi(), "/tools list nosuchthing", surface="agent")
    assert result.success is False
    assert "Unknown category 'nosuchthing'" in result.markdown
    assert "browser" in result.markdown
    assert "integration group or service key" in result.markdown


def test_enable_and_disable_take_a_service_or_group():
    api = _RealCatalogApi()
    receipt = _run_command(api, "/tools enable github", surface="agent")
    assert receipt.success is True, receipt.markdown
    # The label is dropped when it only re-cases the key.
    assert f"Enabled integration service 'github' ({len(SERVICES['github'])} tools)." in receipt.markdown
    assert sorted(api.thread_config["enabled_tools"]) == sorted(SERVICES["github"])

    off = _run_command(api, "/tools disable github", surface="agent")
    assert off.success is True, off.markdown
    assert f"Disabled integration service 'github' ({len(SERVICES['github'])} tools)." in off.markdown
    assert api.thread_config["enabled_tools"] == []
    assert set(SERVICES["github"]) <= set(api.thread_config["disabled_tools"])

    # The registered example that used to fail: `productivity` is a GROUP.
    group = _run_command(api, "/tools enable productivity", surface="agent")
    assert group.success is True, group.markdown
    label = next(t["group_label"] for t in _CATALOG if t.get("group") == "productivity")
    assert (
        f"Enabled integration group 'productivity' ({label}, {len(GROUPS['productivity'])} tools)."
        in group.markdown
    )
    assert set(GROUPS["productivity"]) <= set(api.thread_config["enabled_tools"])


def test_a_global_enable_of_a_service_writes_the_account_defaults():
    api = _RealCatalogApi()
    result = _run_command(api, "/tools enable github global", surface="cli")
    assert result.success is True, result.markdown
    assert "integration service 'github'" in result.markdown
    assert set(SERVICES["github"]) <= set(api.default_tools)
    assert set(_SEED) <= set(api.default_tools)
    removed = _run_command(api, "/tools disable github global", surface="cli")
    assert removed.success is True, removed.markdown
    assert f"Removed integration service 'github' ({len(SERVICES['github'])} tools)" in removed.markdown
    assert not set(SERVICES["github"]) & set(api.default_tools)


def test_precedence_category_then_tool_then_integration_key():
    """A category beats a same-named integration key, and an exact tool name
    beats a same-named service. Synthetic, since the registries keep the key
    spaces disjoint (ratcheted below)."""
    catalog = [
        {"name": "email_send", "description": "Send", "category": "email"},
        {"name": "linear", "description": "A plain tool", "category": "general"},
        {"name": "email_x_list", "description": "d", "category": INTEGRATIONS,
         "group": "utilities", "group_label": "Utilities", "service": "email",
         "service_label": "Email"},
        {"name": "linear_list_issues", "description": "d", "category": INTEGRATIONS,
         "group": "project_management", "group_label": "PM", "service": "linear",
         "service_label": "Linear"},
    ]
    api = _RealCatalogApi(catalog)
    listed = _run_command(api, "/tools list email", surface="agent").markdown
    assert "Tools in category 'email'" in listed and "email_x_list" not in listed
    enabled = _run_command(api, "/tools enable linear", surface="agent")
    assert enabled.success is True
    assert api.thread_config["enabled_tools"] == ["linear"]


def test_a_key_outside_the_callers_catalog_does_not_resolve():
    """Keys resolve from the caller's own (role-filtered) available_tools."""
    catalog = [t for t in _CATALOG if t.get("service") != "github"]
    api = _RealCatalogApi(catalog)
    listed = _run_command(api, "/tools list github", surface="agent")
    assert listed.success is False
    enabled = _run_command(api, "/tools enable github", surface="agent")
    assert enabled.success is False
    assert api.thread_config["enabled_tools"] == []


def test_category_group_and_service_keys_never_collide():
    """The registries keep the three key spaces disjoint, and none shadows a
    named view; a collision would make one of them unreachable by name."""
    from nymeria.tools.integration_taxonomy import GROUP_META, SERVICE_REGISTRY
    from nymeria.tools.metadata import ToolCategory

    categories = {c.value for c in ToolCategory}
    groups, services = set(GROUP_META), set(SERVICE_REGISTRY)
    assert not categories & groups
    assert not categories & services
    assert not groups & services
    assert not {"enabled", "optional", "core"} & (categories | groups | services)


def test_a_tool_named_like_a_service_is_that_whole_service():
    """`/tools enable <key>` binds a TOOL of that exact name before any
    service (the tool `calculator` shares the service key `calculator`), so a
    service whose key is also a tool name must hold only that tool, or the
    listing and the enable would name different sets. Add a
    `calculator_*` tool and this fails: rename the tool or the service."""
    tool_names = {t["name"] for t in _CATALOG}
    for key in (set(GROUPS) | set(SERVICES)) & tool_names:
        assert key in SERVICES and SERVICES[key] == [key], key


def test_the_enable_hint_is_withheld_when_it_would_be_false():
    """A service key that is also a tool name, with more members than that
    tool: `/tools enable <key>` binds the one tool, so no enable-them-all hint."""
    catalog = [
        {"name": "calculator", "description": "d", "category": INTEGRATIONS,
         "group": "utilities", "group_label": "Utilities", "service": "calculator",
         "service_label": "Calculator"},
        {"name": "calculator_units", "description": "d", "category": INTEGRATIONS,
         "group": "utilities", "group_label": "Utilities", "service": "calculator",
         "service_label": "Calculator"},
    ]
    api = _RealCatalogApi(catalog)
    listed = _run_command(api, "/tools list calculator", surface="agent").markdown
    assert "calculator_units" in listed
    assert "Enable them all" not in listed
    single = _RealCatalogApi(catalog[:1])
    assert "Enable them all" in _run_command(single, "/tools list calculator", surface="agent").markdown


def test_the_tools_option_set_offers_groups_and_services_once():
    from nymeria.core.command_option_resolvers import resolve_tools
    from nymeria.core.command_service import CommandService, _CommandExecutor
    from test_command_service import run

    api = _RealCatalogApi()
    executor = _CommandExecutor(
        api=api, thread_id="thread-1", user_id="alice", actor="user",
        is_admin=True, service=CommandService(), surface="cli",
    )
    options = run(resolve_tools(executor))
    ids = [o["id"] for o in options]
    assert len(ids) == len(set(ids))
    assert "github" in ids and "productivity" in ids
    github = next(o for o in options if o["id"] == "github")
    assert github["meta"].startswith("integration service")
    # Emitted before the tool rows: the options route keeps the first 25
    # substring matches, and `microsoft_*` tools alone outnumber that.
    first_tool = next(i for i, o in enumerate(options) if o["meta"].startswith(("on", "off")))
    assert ids.index("github") < first_tool
    from nymeria.core.command_option_resolvers import filter_command_options

    microsoft = [o["id"] for o in filter_command_options(options, q="microsoft", limit=25)]
    assert any(SERVICES.get(i) for i in microsoft), microsoft


def test_integrations_all_restores_the_full_listing_on_request():
    text = _run_command(_RealCatalogApi(), f"/tools list {INTEGRATIONS} --all", surface="cli").markdown
    assert f"Tools in category '{INTEGRATIONS}'" in text
    for names in SERVICES.values():
        for name in names:
            assert name in text


def test_a_live_ttl_tool_counts_as_enabled_in_the_index():
    """Kit-bound TTL'd tools are on (`/tools list` computes it the same way)."""
    from datetime import timedelta

    from nymeria.core.time_utils import utc_now

    api = _RealCatalogApi()
    name = sorted(SERVICES["github"])[0]
    api.thread_config["temporary_tools"] = {
        name: {"expires_at": (utc_now() + timedelta(hours=1)).isoformat()},
    }
    text = _run_command(api, "/tools list optional", surface="agent").markdown
    assert "(1 enabled on this thread)" in text.splitlines()[0]


def test_group_and_service_keys_normalize_case_and_dashes():
    api = _RealCatalogApi()
    listed = _run_command(api, "/tools list GitHub", surface="agent")
    assert listed.success is True, listed.markdown
    assert "integration service 'github'" in listed.markdown
    dashed = _run_command(api, "/tools list CRM-Sales", surface="agent")
    assert dashed.success is True, dashed.markdown
    assert "integration group 'crm_sales'" in dashed.markdown
    group = _run_command(api, "/tools enable CRM-Sales", surface="agent")
    assert group.success is True, group.markdown
    assert set(GROUPS["crm_sales"]) <= set(api.thread_config["enabled_tools"])
