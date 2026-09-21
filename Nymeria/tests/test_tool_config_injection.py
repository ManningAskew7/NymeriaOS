"""Build-failing gate: every `@tool` in `nymeria/tools/` that names a `config`
parameter must spell it so LangChain injects the run config.

`langchain_core.tools.base._get_runnable_config_param` injects `config` only
when the resolved type hint IS `RunnableConfig`; the
`Annotated[Optional[RunnableConfig], InjectedToolArg]` spelling resolves to a
Union, is skipped, and the tool silently runs with `config=None`, so
`get_user_id(config)` returns "default" and every per-user vault lookup and
per-thread store resolves as the owner account. Backlog #380 found 200 tools
in four service-integration modules in that state (the Twitch family before
them, live, posted into the wrong channel). The per-family tests call
`tool.func(config=...)` directly and cannot see the difference, hence this
gate over the real injection predicate. Behaviour twin:
`test_marketing_contact_service_integrations.py::test_convertkit_subscribe_reaches_the_callers_vault_through_tool_invoke`.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from langchain_core.tools import BaseTool
from langchain_core.tools.base import _get_runnable_config_param


def _tool_functions():
    import nymeria.tools as tools_pkg

    seen: set[int] = set()
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"nymeria.tools.{info.name}")
        for name in dir(module):
            obj = getattr(module, name)
            if not isinstance(obj, BaseTool):
                continue
            func = getattr(obj, "func", None) or getattr(obj, "coroutine", None)
            if func is None or id(func) in seen:
                continue
            seen.add(id(func))
            yield f"{info.name}.{obj.name}", func


def test_every_tool_naming_config_receives_the_run_config():
    offenders = []
    checked = 0
    for label, func in _tool_functions():
        if "config" not in inspect.signature(func).parameters:
            continue
        checked += 1
        if _get_runnable_config_param(func) != "config":
            offenders.append(label)
    assert checked >= 1000, checked
    assert offenders == [], (
        f"{len(offenders)} tool(s) declare `config` in a spelling LangChain never injects "
        f"(use `config: Annotated[RunnableConfig, InjectedToolArg] = None`): {offenders[:20]}"
    )
