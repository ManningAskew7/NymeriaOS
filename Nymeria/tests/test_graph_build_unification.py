"""Tests for AGENT-002: sync and async graph-build path unification.

Validates that _select_tools_for_graph is the single source of truth for
tool selection and that sync/async paths differ only in checkpointer config.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

from nymeria.core.thread_config import ThreadConfig
from nymeria.tools import (
    ADMIN_ONLY_TOOL_NAMES,
    SEED_TOOLS,
    DEVELOPER_ONLY_TOOL_NAMES,
    CATALOG_TOOLS,
    static_tool_catalog,
)
from nymeria.vendor.react_agent.config import CheckpointerConfig


def _method_tree(method):
    return ast.parse(textwrap.dedent(inspect.getsource(method)))


def _called_method_names(method) -> set[str]:
    names = set()
    for node in ast.walk(_method_tree(method)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _graph_rebuild_ops_in(node) -> list[str]:
    """Collect inline graph-rebuild operations within an AST subtree.

    Reports direct ``_user_graphs``/``_async_user_graphs`` clears, default-graph
    builder calls, and ``_default_graph``/``_default_async_graph`` assignments,
    so a test can assert where (or whether) they appear.
    """
    ops = []
    for node in ast.walk(node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if (
                func.attr == "clear"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr in {"_user_graphs", "_async_user_graphs"}
            ):
                ops.append(f"{func.value.attr}.clear")
            if func.attr in {"_build_graph_with_prompt", "_build_async_graph_with_prompt"}:
                ops.append(func.attr)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"_default_graph", "_default_async_graph"}
                ):
                    ops.append(f"assign {target.attr}")
    return ops


def _direct_graph_rebuild_ops(method) -> list[str]:
    return _graph_rebuild_ops_in(_method_tree(method))


def _make_agent():
    """Create a minimal NymeriaAgent mock with the real graph-build methods."""
    from nymeria.core.agent import NymeriaAgent

    with patch.object(NymeriaAgent, "__init__", lambda self: None):
        agent = NymeriaAgent()

    agent.settings = MagicMock()
    agent.settings.tool_timeout = 300
    agent.settings.tool_output_max_chars = 100000
    agent.settings.log_level = "INFO"
    # These tests assert the legacy build path; opt out of dynamic mode
    # explicitly so the MagicMock's truthy attribute access doesn't flip
    # them onto the per-step resolver path.
    agent.settings.dynamic_tool_binding = False
    agent.thread_config_manager = MagicMock()
    agent.thread_config_manager.get_config.return_value = None
    agent.profile_manager = MagicMock()
    agent.profile_manager.get_profile.return_value = MagicMock(
        tool_preferences=MagicMock(default_thread_tools=None),
    )
    agent.accounts_repo = MagicMock()
    agent.tool_registry = MagicMock()
    agent._callable_tool_thread_map = {}
    agent._on_tool_timeout = None
    agent.MAIN_AGENT_MAX_ITERATIONS = 500
    agent.CALLABLE_DEFAULT_MAX_ITERATIONS = 10
    agent.TURN_SAME_TOOL_RESULT_LIMIT = 5

    agent._checkpointer_config = CheckpointerConfig(backend="memory")
    agent._async_checkpointer_config = CheckpointerConfig(backend="sqlite_async", sqlite_path="/tmp/test.db")
    agent.skill_manager = None
    agent._get_llm_config_for_thread = MagicMock(return_value=MagicMock())

    return agent


def _ordinary_optional_tool_name(exclude: set[str] | None = None) -> str:
    excluded = set(exclude or set())
    blocked = ADMIN_ONLY_TOOL_NAMES | DEVELOPER_ONLY_TOOL_NAMES
    for name in CATALOG_TOOLS:
        if name not in blocked and name not in excluded:
            return name
    raise AssertionError("no ordinary optional tool found")


def test_apply_role_gates_strips_admin_and_developer_for_non_admin():
    """``_apply_role_gates`` removes admin- and developer-gated names for a
    non-admin role and returns their union as the blocked set."""
    from nymeria.core.agent_graph import _apply_role_gates

    admin_tool = next(iter(ADMIN_ONLY_TOOL_NAMES))
    dev_tool = next(iter(DEVELOPER_ONLY_TOOL_NAMES))
    # Any name not in the gated frozensets passes through untouched.
    ordinary = "ordinary_tool_not_gated"
    names = {admin_tool, dev_tool, ordinary}

    allowed, blocked = _apply_role_gates(names, "user")

    assert ordinary in allowed
    assert admin_tool not in allowed
    assert dev_tool not in allowed
    assert blocked == {admin_tool, dev_tool}
    assert isinstance(allowed, set)


def test_apply_role_gates_admin_keeps_everything():
    """Admins keep all names; nothing is blocked."""
    from nymeria.core.agent_graph import _apply_role_gates

    names = {
        next(iter(ADMIN_ONLY_TOOL_NAMES)),
        next(iter(DEVELOPER_ONLY_TOOL_NAMES)),
        "ordinary_tool_not_gated",
    }

    allowed, blocked = _apply_role_gates(names, "admin")

    assert allowed == names
    assert blocked == set()


def test_resolve_owner_role_defaults_to_user_for_unknown_or_empty():
    """``_resolve_owner_role`` returns the conservative ``"user"`` default for
    an empty id (no lookup) or an unknown account, and the real role otherwise."""
    from nymeria.core.agent_graph import _resolve_owner_role

    agent = MagicMock()

    agent.accounts_repo.get_user_by_id.return_value = None
    assert _resolve_owner_role(agent, "") == "user"
    agent.accounts_repo.get_user_by_id.assert_not_called()

    assert _resolve_owner_role(agent, "ghost") == "user"

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="admin")
    assert _resolve_owner_role(agent, "u1") == "admin"


def test_select_tools_strips_admin_default_for_non_admin_keeps_for_admin():
    """Defense-in-depth: an admin-only tool promoted into default_thread_tools
    is stripped from the bound list for a non-admin owner but kept for an admin."""
    catalog = static_tool_catalog()
    admin_tool = next((n for n in ADMIN_ONLY_TOOL_NAMES if n in catalog), None)
    assert admin_tool is not None, "expected an admin-only tool present in the catalog"
    default_core = SEED_TOOLS[0].name

    agent = _make_agent()
    agent.profile_manager.get_profile.return_value = SimpleNamespace(
        tool_preferences=SimpleNamespace(
            default_thread_tools=[default_core, admin_tool],
        ),
    )
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])
    agent.thread_config_manager.get_config.return_value = None

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="user")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    names = {t.name for t in tools}
    assert default_core in names
    assert admin_tool not in names

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="admin")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    names = {t.name for t in tools}
    assert default_core in names
    assert admin_tool in names


def test_select_tools_strips_admin_extra_for_non_admin_keeps_for_admin():
    """Defense-in-depth on the extras gate: an admin-only tool enabled on a
    thread is stripped for a non-admin owner but kept for an admin."""
    catalog = static_tool_catalog()
    admin_tool = next((n for n in ADMIN_ONLY_TOOL_NAMES if n in catalog), None)
    assert admin_tool is not None, "expected an admin-only tool present in the catalog"

    agent = _make_agent()
    # Default profile (default_thread_tools=None) means the core gate is
    # skipped, so this isolates the enabled_tools (extras) gate.
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])
    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1",
        enabled_tools=[admin_tool],
    )

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="user")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    assert admin_tool not in {t.name for t in tools}

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="admin")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    assert admin_tool in {t.name for t in tools}


def test_select_tools_strips_developer_only_extra_for_non_admin():
    """The developer-only axis of the extras gate.

    Load-bearing since #326: the thread-config writer now judges only what a
    write ADDS, so a developer-only name already on a thread is RETAINED in
    `enabled_tools`. That is only safe while graph build keeps stripping it.
    """
    catalog = static_tool_catalog()
    dev_tool = next((n for n in DEVELOPER_ONLY_TOOL_NAMES if n in catalog), None)
    assert dev_tool is not None, "expected a developer-only tool present in the catalog"

    agent = _make_agent()
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])
    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1",
        enabled_tools=[dev_tool],
    )

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="user")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    assert dev_tool not in {t.name for t in tools}

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="admin")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    assert dev_tool in {t.name for t in tools}


def test_select_tools_strips_a_gated_temporary_tool_for_non_admin():
    """`temporary_tools` rides the same extras merge and must be gated too.

    The extras set is `enabled_tools | live temporary_tools`, so a TTL binding
    is the other way a gated name reaches the merge. Unpinned before #326.
    """
    catalog = static_tool_catalog()
    admin_tool = next((n for n in ADMIN_ONLY_TOOL_NAMES if n in catalog), None)
    assert admin_tool is not None, "expected an admin-only tool present in the catalog"

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    agent = _make_agent()
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])
    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1",
        temporary_tools={admin_tool: {"expires_at": future}},
    )

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="user")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    assert admin_tool not in {t.name for t in tools}

    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="admin")
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    assert admin_tool in {t.name for t in tools}


def test_select_tools_resolves_owner_role_once_across_core_and_extras():
    """The core and extras role gates share a single owner-role lookup, so the
    common (defaults + enabled extras) path does one get_user_by_id, not two."""
    agent = _make_agent()
    default_core = SEED_TOOLS[0].name
    thread_extra = _ordinary_optional_tool_name()

    agent.profile_manager.get_profile.return_value = SimpleNamespace(
        tool_preferences=SimpleNamespace(default_thread_tools=[default_core]),
    )
    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="user")
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])
    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1",
        enabled_tools=[thread_extra],
    )

    agent._select_tools_for_graph("u1", "t1")

    assert cast(MagicMock, agent.accounts_repo.get_user_by_id).call_count == 1


def test_select_tools_shared_by_both_build_paths():
    """Both _build_graph_with_prompt and _build_async_graph_with_prompt
    call _select_tools_for_graph — the tool list is identical."""
    agent = _make_agent()

    calls = []
    original_select = agent._select_tools_for_graph.__func__

    def tracking_select(self, user_id, thread_id):
        result = original_select(self, user_id, thread_id)
        calls.append(("select", user_id, thread_id, [t.name for t in result[0]]))
        return result

    with patch.object(type(agent), "_select_tools_for_graph", tracking_select):
        with patch("nymeria.core.agent_graph.create_graph", return_value=MagicMock()):
            agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")
            agent._build_async_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert len(calls) == 2
    assert calls[0][1:] == calls[1][1:]


def test_select_tools_resolves_defaults_plus_thread_overrides_without_double_counting():
    agent = _make_agent()
    default_core = SEED_TOOLS[0].name
    promoted_optional = _ordinary_optional_tool_name()
    thread_extra = _ordinary_optional_tool_name({promoted_optional})

    agent.profile_manager.get_profile.return_value = SimpleNamespace(
        tool_preferences=SimpleNamespace(
            default_thread_tools=[default_core, promoted_optional],
        ),
    )
    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="user")
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])

    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1",
        enabled_tools=[promoted_optional, thread_extra],
    )
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    names = [tool.name for tool in tools]

    assert default_core in names
    assert thread_extra in names
    assert names.count(promoted_optional) == 1

    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1",
        enabled_tools=[promoted_optional, thread_extra],
        disabled_tools=[promoted_optional],
    )
    tools, _ = agent._select_tools_for_graph("u1", "t1")
    names = [tool.name for tool in tools]

    assert default_core in names
    assert thread_extra in names
    assert promoted_optional not in names


def test_sync_async_differ_only_in_checkpointer():
    """Sync and async graph builds produce AgentConfigs that differ only
    in the checkpointer field."""
    agent = _make_agent()

    configs_seen = []

    def capture_create_graph(config=None, tools=None):
        configs_seen.append(config)
        return MagicMock()

    with patch("nymeria.core.agent_graph.create_graph", side_effect=capture_create_graph):
        agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")
        agent._build_async_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert len(configs_seen) == 2
    sync_cfg, async_cfg = configs_seen

    assert sync_cfg.checkpointer.backend == "memory"
    assert async_cfg.checkpointer.backend == "sqlite_async"

    assert sync_cfg.system_prompt == async_cfg.system_prompt
    assert sync_cfg.max_iterations == async_cfg.max_iterations
    assert sync_cfg.tool_timeout == async_cfg.tool_timeout
    assert sync_cfg.tool_output_max_chars == async_cfg.tool_output_max_chars
    assert sync_cfg.verbose == async_cfg.verbose


def test_default_graph_includes_core_tools_and_excludes_capability_expansion_tools():
    agent = _make_agent()

    tools, _ = agent._select_tools_for_graph("u1", "t1")
    names = {tool.name for tool in tools}

    from nymeria.tools import CAPABILITY_EXPANSION_TOOL_NAMES

    assert {"bash_execute", "file_read", "notify"}.issubset(names)
    assert names.isdisjoint(CAPABILITY_EXPANSION_TOOL_NAMES)


def test_build_agent_config_uses_callable_iteration_limit():
    """Callable threads get a lower iteration limit."""
    agent = _make_agent()
    from nymeria.core.thread_config import ThreadConfig

    tc = ThreadConfig(thread_id="t1")
    tc.callable = True
    tc.callable_name = "helper"
    tc.callable_max_iterations = 7

    config = agent._build_agent_config(
        "prompt", agent._checkpointer_config, "t1", tc
    )
    assert config.max_iterations == 7

    config_main = agent._build_agent_config(
        "prompt", agent._checkpointer_config, "t1", None
    )
    assert config_main.max_iterations == 500


def test_get_graph_for_user_delegates_to_impl():
    """Both _get_graph_for_user and _get_async_graph_for_user go through
    _get_graph_for_user_impl."""
    agent = _make_agent()
    agent._user_graphs = {}
    agent._async_user_graphs = {}
    agent._graph_cache_lock = __import__("threading").Lock()
    agent._GRAPH_CACHE_MAX = 100

    calls = []
    def mock_impl(
        self,
        user_id,
        thread_id,
        cache,
        build_fn,
        cache_key_fn=None,
    ):
        calls.append((cache is self._user_graphs, cache is self._async_user_graphs))
        return MagicMock()

    with patch.object(type(agent), "_get_graph_for_user_impl", mock_impl):
        agent._get_graph_for_user("u1", thread_id="t1")
        agent._get_async_graph_for_user("u1", thread_id="t1")

    assert calls[0] == (True, False)
    assert calls[1] == (False, True)


def test_async_graph_cache_is_scoped_to_running_event_loop():
    agent = _make_agent()
    agent._async_user_graphs = {}
    agent._graph_cache_lock = __import__("threading").Lock()
    agent._GRAPH_CACHE_MAX = 100
    agent._base_system_prompt = "base"
    agent._get_memory_hash = MagicMock(return_value="hash")
    agent._build_full_system_prompt = MagicMock(return_value="full")
    agent.profile_manager.get_profile.return_value = SimpleNamespace(
        memories={},
        personality_overrides={},
        tool_preferences=SimpleNamespace(default_thread_tools=[]),
    )
    todo_list = MagicMock()
    todo_list.get_active_todos_for_thread.return_value = []
    agent.todo_manager = MagicMock()
    agent.todo_manager.get_todos.return_value = todo_list

    first_graph = object()
    second_graph = object()
    agent._build_async_graph_with_prompt = MagicMock(
        side_effect=[first_graph, second_graph]
    )

    async def get_graph():
        return agent._get_async_graph_for_user("u1", thread_id="t1")

    loop_one = asyncio.new_event_loop()
    loop_two = asyncio.new_event_loop()
    try:
        graph_one = loop_one.run_until_complete(get_graph())
        graph_one_again = loop_one.run_until_complete(get_graph())
        graph_two = loop_two.run_until_complete(get_graph())
    finally:
        loop_one.close()
        loop_two.close()

    assert graph_one is first_graph
    assert graph_one_again is first_graph
    assert graph_two is second_graph
    assert agent._build_async_graph_with_prompt.call_count == 2


def test_graph_cache_hit_moves_entry_to_lru_tail():
    agent = _make_agent()
    agent._graph_cache_lock = __import__("threading").Lock()

    first_graph = object()
    second_graph = object()
    cache = {
        ("u1", "t1"): ("hash", first_graph),
        ("u2", "t2"): ("hash", second_graph),
    }

    cached = agent._get_cached_graph_entry(cache, ("u1", "t1"), "hash")

    assert cached is first_graph
    assert list(cache) == [("u2", "t2"), ("u1", "t1")]


def test_graph_cache_eviction_uses_least_recently_used_entry():
    agent = _make_agent()
    agent._graph_cache_lock = __import__("threading").Lock()
    agent._GRAPH_CACHE_MAX = 2

    first_graph = object()
    second_graph = object()
    third_graph = object()
    cache = {
        ("u1", "t1"): ("hash", first_graph),
        ("u2", "t2"): ("hash", second_graph),
    }

    assert agent._get_cached_graph_entry(cache, ("u1", "t1"), "hash") is first_graph
    agent._store_cached_graph_entry(cache, ("u3", "t3"), "hash", third_graph)

    assert list(cache) == [("u1", "t1"), ("u3", "t3")]
    assert ("u2", "t2") not in cache


def test_graph_cache_hash_mismatch_removes_stale_entry():
    agent = _make_agent()
    agent._graph_cache_lock = __import__("threading").Lock()
    cache = {("u1", "t1"): ("old-hash", object())}

    cached = agent._get_cached_graph_entry(cache, ("u1", "t1"), "new-hash")

    assert cached is None
    assert cache == {}


def test_toolset_mutators_delegate_default_graph_rebuild():
    """Tool-set mutation paths go through the shared cache rebuild helper.

    ``register_tool``/``register_tools`` stay on ``NymeriaAgent``; the other
    four mutators now live as module-level functions in ``agent_tools`` and
    are exposed on the class as thin facades. Inspect the implementation
    location, not the facade.
    """
    from nymeria.core.agent import NymeriaAgent
    from nymeria.core import agent_tools

    implementations = [
        NymeriaAgent.register_tool,
        NymeriaAgent.register_tools,
        NymeriaAgent.reload_base_system_prompt,
        agent_tools.sync_agent_tools,
        agent_tools.reload_mcp_server_tools,
        agent_tools.reload_custom_tools,
        agent_tools.reload_tools,
    ]

    for impl in implementations:
        assert "_rebuild_default_graphs" in _called_method_names(impl)
        assert _direct_graph_rebuild_ops(impl) == []


def test_no_inline_graph_cache_rebuild_outside_canonical_helper():
    """Graph-cache invalidation is centralized in ``rebuild_default_graphs``.

    Every mutation that needs to drop the per-thread graph caches and refresh
    the defaults must call ``agent._rebuild_default_graphs()`` rather than
    inlining a ``_user_graphs``/``_async_user_graphs`` clear plus a
    ``_default_graph`` reassignment. This gate would have caught the copies
    that had drifted across the API routers and skill tools (each carrying its
    own, sometimes unlocked, clear). Targeted per-thread eviction
    (``del _user_graphs[key]`` in ``agent_tools``/``thread_deletion``) is a
    different operation and is intentionally not matched here.
    """
    import pathlib

    import nymeria

    pkg_root = pathlib.Path(nymeria.__file__).resolve().parent
    allowed = {
        pkg_root / "core" / "agent.py",          # one-time __init__ build
        pkg_root / "core" / "agent_tools.py",    # the canonical helper itself
    }

    offenders: list[tuple[str, list[str]]] = []
    for path in pkg_root.rglob("*.py"):
        if path.resolve() in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inline = [
            op
            for op in _graph_rebuild_ops_in(tree)
            if op.endswith(".clear") or op.startswith("assign _default")
        ]
        if inline:
            offenders.append((str(path.relative_to(pkg_root)), inline))

    assert offenders == [], (
        "Inline graph-cache rebuild found; route it through "
        "agent._rebuild_default_graphs() instead: " + repr(offenders)
    )


def test_rebuild_default_graphs_clears_caches_under_lock():
    """The canonical rebuild clears the per-thread caches under
    ``_graph_cache_lock``, then rebuilds the defaults outside it.

    Clearing under the lock matters: ``store_cached_graph_entry`` mutates the
    same dicts under that lock (including an LRU ``next(iter)``/``del``
    eviction), so a concurrent unlocked ``clear()`` could raise. The
    non-reentrant lock must NOT be held across the default-graph rebuild.
    """
    from nymeria.core import agent_tools

    func = _method_tree(agent_tools.rebuild_default_graphs).body[0]

    lock_blocks = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Attribute)
            and item.context_expr.attr == "_graph_cache_lock"
            for item in node.items
        )
    ]
    assert len(lock_blocks) == 1, "expected exactly one _graph_cache_lock block"
    lock_block = lock_blocks[0]

    # Both cache clears happen inside the lock; nothing builds/assigns there.
    assert set(_graph_rebuild_ops_in(lock_block)) == {
        "_user_graphs.clear",
        "_async_user_graphs.clear",
    }

    # The default-graph rebuilds happen outside the lock block (and the clears
    # are not duplicated there).
    outside_ops: list[str] = []
    for stmt in func.body:
        if stmt is not lock_block:
            outside_ops.extend(_graph_rebuild_ops_in(stmt))
    assert "_build_graph_with_prompt" in outside_ops
    assert "_build_async_graph_with_prompt" in outside_ops
    assert "assign _default_graph" in outside_ops
    assert "assign _default_async_graph" in outside_ops
    assert "_user_graphs.clear" not in outside_ops
    assert "_async_user_graphs.clear" not in outside_ops


# ---------------------------------------------------------------------------
# #327: a role change must not be served a stale compiled graph.
#
# Graph build gates admin-only and developer-only tools by role, but that gate
# runs only on a cache MISS. `memory_hash` is the freshness token in front of
# it, so role had to become one of its inputs: otherwise a demotion left the
# previously-compiled graph, and every tool bound into it, exactly as the
# account was before. Under `dynamic_tool_binding=False` the tool node holds
# that bound list with no second gate, so the stale binding was executable.
# ---------------------------------------------------------------------------


def _roles_by_id(**roles: str):
    """A `get_user_by_id` that answers per ACCOUNT ID, never a flat value.

    A `return_value` fake ignores its argument, so a resolver that looked up
    the wrong identity (the thread id, say) would still be handed the role
    under test and the assertion would pass for the wrong reason. Discriminating
    here is what pins WHICH account's role the hash reads.
    """
    return MagicMock(side_effect=lambda account_id: (
        SimpleNamespace(role=roles[account_id]) if account_id in roles else None
    ))


def _role_hash_agent(role: str):
    """An agent whose only interesting property is the role it reports."""
    agent = _make_agent()
    agent.accounts_repo.get_user_by_id = _roles_by_id(u1=role)
    agent._skills_fingerprint = MagicMock(return_value="")
    agent._resolve_temporary_tools = MagicMock(return_value=set())
    return agent


def test_memory_hash_changes_when_the_owner_role_changes():
    admin = _role_hash_agent("admin")
    demoted = _role_hash_agent("user")

    assert admin._get_memory_hash("u1", "") != demoted._get_memory_hash("u1", "")


def test_memory_hash_is_stable_when_the_role_is_unchanged():
    # Guards two over-corrections at once: a term that varied per CALL would
    # defeat the cache entirely, and a term that varied per HOST (an object id,
    # say) would pass the test above while folding no role at all. Two distinct
    # agents reporting the same role must therefore agree.
    assert _role_hash_agent("admin")._get_memory_hash("u1", "") == (
        _role_hash_agent("admin")._get_memory_hash("u1", "")
    )


def test_role_change_evicts_the_cached_graph_and_forces_a_rebuild():
    """The end-to-end property, and the one that failed before #327.

    Demote the account between two graph fetches and the second must not be
    served the first's compiled graph. The rebuild is what matters: it is the
    only thing that re-runs `select_tools_for_graph`, whose role stripping the
    tests above already pin. Without the rebuild an admin-only tool bound
    while the account was an admin stays bound, and on the static path
    `SafeToolNode` has no second gate to catch the call.
    """
    admin_only = sorted(ADMIN_ONLY_TOOL_NAMES)[0]

    agent = _make_agent()
    agent._graph_cache_lock = __import__("threading").Lock()
    agent._user_graphs = {}
    agent._async_user_graphs = {}
    agent._GRAPH_CACHE_MAX = 50
    agent._base_system_prompt = "sys"
    agent._build_full_system_prompt = MagicMock(return_value="sys")
    agent._skills_fingerprint = MagicMock(return_value="")
    agent._resolve_temporary_tools = MagicMock(return_value=set())
    agent.thread_config_manager.get_config.return_value = None
    agent.profile_manager.get_profile.return_value = MagicMock(
        memories=[],
        personality_overrides={},
        tool_preferences=MagicMock(default_thread_tools=[admin_only]),
    )
    agent.todo_manager = MagicMock()
    agent.todo_manager.get_todos.return_value = MagicMock(
        get_active_todos_for_thread=MagicMock(return_value=[]),
        get_active_todos=MagicMock(return_value=[]),
    )
    agent._build_graph_with_prompt = MagicMock(side_effect=lambda *a, **k: object())

    agent.accounts_repo.get_user_by_id = _roles_by_id(u1="admin")
    as_admin = agent._get_graph_for_user("u1", "t1")
    # A second fetch at the SAME role must still hit the cache, or this test
    # would pass for a hash that simply varies per call.
    assert agent._get_graph_for_user("u1", "t1") is as_admin

    agent.accounts_repo.get_user_by_id = _roles_by_id(u1="user")
    as_user = agent._get_graph_for_user("u1", "t1")

    assert as_admin is not as_user, "the demotion was served the admin's cached graph"
    assert agent._build_graph_with_prompt.call_count == 2


def test_memory_hash_folds_the_role_on_the_callable_thread_path_too():
    """`get_memory_hash` returns from TWO places and both bind tools.

    The callable-thread branch returns early, skipping memory/TODO/personality,
    but it still hashes `enabled_tools`/`disabled_tools`/`temporary_tools`, so a
    graph built from it is just as role-gated and just as cacheable. Leaving the
    role out of that return would have left callable threads with exactly the
    staleness #327 fixed everywhere else.
    """
    def _agent_at(role: str):
        agent = _make_agent()
        agent.accounts_repo.get_user_by_id = _roles_by_id(u1=role)
        agent._skills_fingerprint = MagicMock(return_value="")
        agent._resolve_temporary_tools = MagicMock(return_value=set())
        agent.thread_config_manager.get_config.return_value = ThreadConfig(
            thread_id="t1",
            callable=True,
            system_prompt="you are a callable thread",
        )
        return agent

    admin = _agent_at("admin")._get_memory_hash("u1", "t1")
    demoted = _agent_at("user")._get_memory_hash("u1", "t1")

    assert admin != demoted
