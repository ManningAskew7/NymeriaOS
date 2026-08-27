"""Reload-integrity tests for the tools package and registry assembly (#277).

The 2026-08-27 production incident: deleting one custom tool fired
``reload_tools()``, whose importlib storm re-executed ``tools/registry.py``
mid-sequence and wiped ``_TOOL_GROUPS`` (catalog 1256 -> 263 names, 72 -> 27
groups); the rebuilt ToolRegistry also dropped every MCP wrapper tool. The
process served a collapsed tool surface for 17 hours until a restart.

Two layers of coverage:

- A SUBPROCESS test replays the verbatim reload storm against the real
  package and asserts zero loss across every cross-module push-accumulator
  (tool groups, catalog, credential specs, plugin metadata overrides). A
  subprocess is the point: reloading a hundred modules in-process would
  poison module identity for every later test in the worker (same rationale
  as test_cli_startup_imports.py).
- Fast in-process tests pin the shared registry assembly
  (``rebuild_tool_registry``: seed + callable + custom + MCP, in that
  order, on a fresh registry) and the honest logging contract of
  ``sync_default_thread_tools`` (no "Removed core tools" line without a
  real core diff, while the capability-expansion strip still applies).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

from nymeria.core import agent_tools
from nymeria.vendor.react_agent.tool_registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_tool(name: str) -> StructuredTool:
    def helper(value: str = "") -> str:
        return value

    return StructuredTool.from_function(
        helper,
        name=name,
        description=f"{name} helper",
    )


# ---------------------------------------------------------------------------
# Subprocess: the verbatim reload storm must lose nothing
# ---------------------------------------------------------------------------

_STORM_PROBE = """
import importlib, pkgutil, sys
from pathlib import Path

import nymeria.tools as T
from nymeria.tools import registry as reg
from nymeria.tools import credential_registry as cred
from nymeria.tools import metadata as meta

groups0 = {g.name for g in reg.all_tool_groups()}
cat0 = set(T.CATALOG_TOOLS)
static0 = set(T.static_tool_catalog())
specs0 = set(cred._SPECS)

assert "outlook" in groups0, "sentinel group missing before reload"
assert "outlook_send_email" in cat0, "sentinel tool missing before reload"
assert "http_api" in groups0
assert specs0, "expected provider credential specs registered at import"

# A plugin-style override, registered the way plugins/ do at their own
# import time; nothing re-registers it during a tools-package storm.
meta.register_plugin_tool_category(
    "qa_probe_tool_277", next(iter(meta.ToolCategory))
)

# 1) Reloading an accumulator module alone must not wipe sibling
#    contributions.
importlib.reload(reg)
importlib.reload(cred)
importlib.reload(meta)
assert {g.name for g in reg.all_tool_groups()} == groups0, (
    "registry module reload wiped tool groups"
)
assert set(cred._SPECS) == specs0, (
    "credential_registry module reload wiped provider specs"
)
assert "qa_probe_tool_277" in meta._PLUGIN_TOOL_CATEGORIES, (
    "metadata module reload wiped plugin category overrides"
)

# 2) A family module reloaded AFTER the accumulator re-registers exactly
#    its own entry with fresh objects.
import nymeria.tools.http_api as http_api_mod
importlib.reload(http_api_mod)
assert {g.name for g in reg.all_tool_groups()} == groups0
g = reg.get_tool_group("http_api")
assert g is not None and any(t.name == "http_request" for t in g.tools)

# 3) The verbatim storm from core/agent_tools.py::reload_tools.
tools_path = Path(T.__file__).parent
for _, name, _ in pkgutil.iter_modules([str(tools_path)]):
    full = f"nymeria.tools.{name}"
    if full in sys.modules:
        try:
            importlib.reload(sys.modules[full])
        except Exception as e:  # noqa: BLE001
            print("RELOAD-FAIL", full, repr(e))
    else:
        try:
            importlib.import_module(full)
        except Exception as e:  # noqa: BLE001
            print("IMPORT-FAIL", full, repr(e))
importlib.reload(T)
from nymeria.tools.metadata import refresh_builtin_tool_metadata
refresh_builtin_tool_metadata()

groups1 = {g.name for g in reg.all_tool_groups()}
cat1 = set(T.CATALOG_TOOLS)
static1 = set(T.static_tool_catalog())

missing_groups = groups0 - groups1
missing_cat = cat0 - cat1
missing_static = static0 - static1
assert not missing_groups, (
    f"storm lost {len(missing_groups)} groups, e.g. {sorted(missing_groups)[:8]}"
)
assert not missing_cat, (
    f"storm lost {len(missing_cat)} catalog names, e.g. {sorted(missing_cat)[:8]}"
)
assert not missing_static, (
    f"storm lost {len(missing_static)} static names, e.g. {sorted(missing_static)[:8]}"
)
assert set(cred._SPECS) >= specs0, "storm lost credential specs"
for probe in (
    "outlook_send_email",
    "calendar_list_events",
    "http_request",
    "api_discover",
    "tool_search",
):
    assert probe in static1, f"{probe} unresolvable after storm"
print(f"STORM-OK groups={len(groups1)} catalog={len(cat1)}")
"""


@pytest.mark.timeout(600)
def test_reload_storm_preserves_catalog_and_registries():
    result = subprocess.run(
        [sys.executable, "-c", _STORM_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=540,
    )
    detail = f"stdout:\n{result.stdout[-3000:]}\nstderr:\n{result.stderr[-3000:]}"
    assert result.returncode == 0, detail
    # Guard against a vacuous pass: the probe must have reached its verdict.
    assert "STORM-OK" in result.stdout, detail


# ---------------------------------------------------------------------------
# In-process: shared registry assembly and honest logging
# ---------------------------------------------------------------------------


class _StubTCM:
    """thread_config_manager stand-in: no callable threads."""

    def list_callable_threads(self):
        return []

    def list_configured_threads(self):
        return []

    def get_config(self, thread_id):
        return None


class _StubAgent:
    """Just enough NymeriaAgent surface for the assembly helpers."""

    def __init__(self):
        self.tool_registry = ToolRegistry()
        self.thread_config_manager = _StubTCM()
        self.graph_rebuilds = 0
        self.default_sync_calls = []
        self._callable_tool_thread_map = {}

    # The loaders register into whatever registry the agent holds at call
    # time, mirroring the real facades; the assertions below therefore
    # observe ORDER (swap-then-load) as an outcome, not as a call trace.
    def _load_custom_tools(self):
        self.tool_registry.register(_make_tool("fake_custom_277"))
        return 1

    def _load_mcp_server_tools(self):
        self.tool_registry.register(_make_tool("mcp__fake__t277"))
        return 1

    def _rebuild_default_graphs(self):
        self.graph_rebuilds += 1

    def _sync_default_thread_tools(self, old_core, new_core):
        self.default_sync_calls.append((set(old_core), set(new_core)))


def _registry_names(agent) -> set:
    return {t["name"] for t in agent.tool_registry.list_tools()}


def test_rebuild_tool_registry_includes_all_four_sources():
    agent = _StubAgent()
    # Pre-poison the registry: the rebuild must START FRESH, not accrete.
    agent.tool_registry.register(_make_tool("stale_leftover_277"))

    agent_tools.rebuild_tool_registry(agent)

    names = _registry_names(agent)
    assert "bash_execute" in names, "seed tools missing from rebuilt registry"
    assert "fake_custom_277" in names, "custom tools missing from rebuilt registry"
    assert "mcp__fake__t277" in names, "MCP wrapper tools missing from rebuilt registry"
    assert "stale_leftover_277" not in names, "rebuild accreted instead of swapping"


def test_reload_tools_registry_includes_mcp_tools(monkeypatch):
    """The incident's second half: reload_tools left MCP wrappers behind."""
    agent = _StubAgent()
    # The storm itself is covered by the subprocess test; reloading a
    # hundred modules in-process would poison sibling tests.
    monkeypatch.setattr(agent_tools, "_reload_tools_package", lambda: None)

    names = set(agent_tools.reload_tools(agent))

    assert "mcp__fake__t277" in names, "reload_tools dropped MCP wrapper tools"
    assert "fake_custom_277" in names
    assert "bash_execute" in names
    assert agent.graph_rebuilds == 1
    assert agent.default_sync_calls, "default_thread_tools sync did not run"


class _StubProfileManager:
    def __init__(self, default_thread_tools):
        self._profile = SimpleNamespace(
            tool_preferences=SimpleNamespace(
                default_thread_tools=default_thread_tools
            )
        )

    def list_users(self):
        return ["u1"]

    def get_profile(self, user_id):
        return self._profile

    @contextmanager
    def atomic_update(self, user_id):
        yield self._profile


def test_sync_default_thread_tools_quiet_without_core_diff(caplog):
    """No core diff -> no 'Removed core tools' noise; the capability strip
    still applies and is reported per user."""
    agent = _StubAgent()
    agent.profile_manager = _StubProfileManager(["bash_execute", "tool_search"])
    core = {"bash_execute", "file_read"}

    with caplog.at_level(logging.INFO, logger="nymeria.core.agent_tools"):
        agent_tools.sync_default_thread_tools(agent, set(core), set(core))

    assert "Removed core tools detected" not in caplog.text, (
        "no-diff reload must not claim core tools were removed"
    )
    # tool_search is capability-expansion: stripped from defaults, and the
    # change is what gets logged.
    updated = agent.profile_manager._profile.tool_preferences.default_thread_tools
    assert "tool_search" not in updated
    assert "bash_execute" in updated
    assert "Updated default_thread_tools" in caplog.text


def test_sync_default_thread_tools_reports_real_core_diff(caplog):
    agent = _StubAgent()
    agent.profile_manager = _StubProfileManager(["bash_execute", "gone_tool"])

    with caplog.at_level(logging.INFO, logger="nymeria.core.agent_tools"):
        agent_tools.sync_default_thread_tools(
            agent, {"bash_execute", "gone_tool"}, {"bash_execute"}
        )

    assert "Removed core tools detected" in caplog.text
    updated = agent.profile_manager._profile.tool_preferences.default_thread_tools
    assert "gone_tool" not in updated
