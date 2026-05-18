from __future__ import annotations

from typing import Dict, List

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from nymeria.tools.tool_search import tool_enable
from nymeria.vendor.react_agent.nodes import SafeToolNode


def test_safe_tool_node_decodes_tool_enable_tools_argument():
    node = SafeToolNode([tool_enable])
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "tool_enable",
                "args": {
                    "action": "enable",
                    "tools": '["random_cat_fact"]',
                    "ttl": "30m",
                },
                "id": "call-1",
            }
        ],
    )

    tool_calls, _ = node._parse_input({"messages": [message]})

    assert tool_calls[0]["args"]["tools"] == ["random_cat_fact"]


def test_safe_tool_node_decodes_json_encoded_list_arguments():
    @tool
    def collect_names(names: List[str]) -> str:
        """Collect names."""
        return ",".join(names)

    node = SafeToolNode([collect_names])
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "collect_names",
                "args": {"names": '["alpha", "beta"]'},
                "id": "call-1",
            }
        ],
    )

    tool_calls, _ = node._parse_input({"messages": [message]})

    assert tool_calls[0]["args"]["names"] == ["alpha", "beta"]
    assert collect_names.invoke(tool_calls[0]["args"]) == "alpha,beta"
    assert message.tool_calls[0]["args"]["names"] == '["alpha", "beta"]'


def test_safe_tool_node_decodes_json_encoded_object_arguments():
    @tool
    def summarize_payload(payload: Dict[str, int]) -> str:
        """Summarize a payload."""
        return str(payload["count"])

    node = SafeToolNode([summarize_payload])
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "summarize_payload",
                "args": {"payload": '{"count": 3}'},
                "id": "call-1",
            }
        ],
    )

    tool_calls, _ = node._parse_input({"messages": [message]})

    assert tool_calls[0]["args"]["payload"] == {"count": 3}
    assert summarize_payload.invoke(tool_calls[0]["args"]) == "3"


def test_safe_tool_node_keeps_json_looking_scalar_strings():
    @tool
    def echo_query(query: str) -> str:
        """Echo a query."""
        return query

    node = SafeToolNode([echo_query])
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "echo_query",
                "args": {"query": '["not", "a", "list", "field"]'},
                "id": "call-1",
            }
        ],
    )

    tool_calls, _ = node._parse_input({"messages": [message]})

    assert tool_calls[0]["args"]["query"] == '["not", "a", "list", "field"]'
    assert echo_query.invoke(tool_calls[0]["args"]) == '["not", "a", "list", "field"]'
