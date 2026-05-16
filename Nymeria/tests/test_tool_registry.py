"""Regression tests for the LangGraph tool registry wrapper."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from langchain_core.tools import StructuredTool

from nymeria.vendor.react_agent.tool_registry import ToolRegistry


def _make_tool(name: str) -> StructuredTool:
    def helper(value: str = "") -> str:
        return value

    return StructuredTool.from_function(
        helper,
        name=name,
        description=f"{name} helper",
    )


def test_tool_registry_basic_enable_disable_flow():
    registry = ToolRegistry()
    first = _make_tool("first_tool")
    second = _make_tool("second_tool")

    registry.register(first).register(second)
    registry.disable("first_tool")

    assert registry.get_tool("first_tool") is first
    assert [tool.name for tool in registry.get_enabled_tools()] == ["second_tool"]
    assert {
        row["name"]: row["enabled"] for row in registry.list_tools()
    } == {
        "first_tool": False,
        "second_tool": True,
    }

    registry.enable("first_tool")
    registry.unregister("second_tool")

    assert "second_tool" not in registry
    assert [tool.name for tool in registry.get_enabled_tools()] == ["first_tool"]


def test_tool_registry_allows_concurrent_reads_and_mutations():
    registry = ToolRegistry()
    tools = [_make_tool(f"tool_{index}") for index in range(64)]

    def mutate(index: int) -> None:
        tool = tools[index]
        registry.register(tool)
        if index % 2 == 0:
            registry.disable(tool.name)
        if index % 3 == 0:
            registry.enable(tool.name)
        if index % 5 == 0:
            registry.unregister(tool.name)
            registry.register(tool)

    def read() -> None:
        for _ in range(64):
            registry.get_all_tools()
            registry.get_enabled_tools()
            registry.list_tools()

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(mutate, index) for index in range(len(tools))]
        futures.extend(executor.submit(read) for _ in range(12))
        for future in futures:
            future.result()

    assert len(registry.get_all_tools()) == len(tools)
    assert {tool.name for tool in registry.get_enabled_tools()} <= {
        tool.name for tool in tools
    }
