"""Ordered-execution marker tool (``run_tools_in_order``).

This is an *inert marker*, not a tool that does work. Its presence in a
same-turn tool-call batch tells ``SafeToolNode`` to run that whole batch in the
order the model listed the calls, instead of the default concurrent fan-out.

On the normal sequential path the marker is never dispatched: the ordered loop
in ``SafeToolNode`` synthesizes its result (a batch-aware ack of how many
sibling calls were ordered, or a nudge if it was emitted alone). The function
body below is only a fallback for the rare case where the marker is dispatched
concurrently (e.g. Send-API single-call dispatch, where batch ordering is
meaningless anyway).

The tool name must stay equal to ``SEQUENTIAL_ORDER_TOOL_NAME`` in
``nymeria/vendor/react_agent/nodes.py`` (the dispatcher matches on that exact
string). ``tests/test_tool_node_sequential.py`` asserts the two agree.
"""

from langchain_core.tools import tool


@tool
def run_tools_in_order(stop_on_error: bool = False) -> str:
    """Run this whole batch of tool calls in the order listed, not concurrently.

    Include this call in the SAME response as other tool calls to force that
    entire batch to execute one at a time, in the order you listed them, instead
    of the default concurrent execution. Use this ONLY when a later call depends
    on an earlier call's external effect (create-then-fetch, write-then-read
    across different tools). Ordered batches are slower because the calls cannot
    overlap, so do not use it for independent calls; batching independent calls
    concurrently is the faster, default path.

    For dependent shell steps, prefer chaining them in one `bash_execute` command
    with `&&` or `;` instead: each `bash_execute` call runs in a fresh shell with
    no shared working directory or environment, so separate bash calls cannot
    rely on one another's state regardless of order. Use this tool for
    dependencies that span different tools (for example `file_write` then
    `file_read`).

    Args:
        stop_on_error: When True, if an earlier call in the ordered batch fails
            (raises, or returns a result beginning with "[Error]"), the remaining
            calls in the batch are skipped instead of run. Default False runs the
            whole batch regardless of failures, matching concurrent behavior.

    Returns:
        A confirmation that the batch was ordered.
    """
    # Inert-marker fallback. On the normal sequential path SafeToolNode never
    # dispatches this body; it synthesizes a batch-aware ack instead. This return
    # only surfaces if the marker is dispatched concurrently (no batch context).
    return (
        "Ordered-execution marker received. Include it in the same response as "
        "the dependent tool calls so they run one at a time, in order."
    )
