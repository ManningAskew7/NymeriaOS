"""
Regression suite echo tool.

A simple, deterministic optional tool used by `Nymeria/eval/agent_regression/`
specs (TH-01, CX-01) to verify the tool hot-load pipeline end-to-end without
needing admin privileges.

This tool is intentionally distinct from `hello_test`:
- `hello_test` is in `DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES` and admin-only.
- `regression_echo` is in `OPTIONAL_TOOLS` only — any user can search for it,
  enable it via `tool_manage`, and call it. That makes it the right target for
  testing `tool_search → tool_manage → tool_reload → use` flows from a
  non-admin regression user.

The output prefix `REGRESSION-ECHO:` is stable so spec assertions can use
substring matching without false positives.
"""

from langchain_core.tools import tool


@tool
def regression_echo(text: str) -> str:
    """
    Echo the supplied text back with a fixed prefix.

    Used by the regression suite to verify dynamic tool loading: this tool is
    intentionally not in any skill kit's required_tools, so enabling it via
    `tool_manage` is guaranteed to trigger a real binding change (and a
    `tool_reload` SSE event), not a TTL refresh.

    Args:
        text: The string to echo back.

    Returns:
        ``REGRESSION-ECHO: <text>``
    """
    return f"REGRESSION-ECHO: {text}"
