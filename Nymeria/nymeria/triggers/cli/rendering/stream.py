"""Stream renderer — spinners, tool cards, markdown output."""

from __future__ import annotations

from typing import TYPE_CHECKING, Generator, Dict, Any

from rich.markdown import Markdown

if TYPE_CHECKING:
    from ..state import CLIState


def _truncate(text: str, limit: int = 120) -> str:
    text = text.replace("\n", " ").strip()
    return text[:limit] + "..." if len(text) > limit else text


def _format_args_preview(args: dict) -> str:
    """Produce a compact one-line preview of tool arguments."""
    if not args:
        return ""
    # For common single-arg tools, show the value directly
    if len(args) == 1:
        val = next(iter(args.values()))
        if isinstance(val, str):
            return _truncate(val, 80)
        return _truncate(str(val), 80)
    # Multi-arg: show key=value pairs
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 40:
            v = v[:37] + "..."
        parts.append(f"{k}={v}")
    return _truncate(", ".join(parts), 100)


class StreamRenderer:
    """
    Renders agent streaming events to the terminal.

    Uses Rich console.status() spinners during thinking/tool execution,
    compact one-liner tool cards, and final Markdown rendering.
    """

    def __init__(self, state: "CLIState") -> None:
        self.state = state
        self.console = state.console

    def _flush_response(self, response_buffer: str, after_tools: bool) -> None:
        """Render buffered response text as Markdown."""
        text = response_buffer.strip()
        if not text:
            return
        if after_tools:
            self.console.print()  # blank line after tool one-liners
        self.console.print(Markdown(text))

    def render_stream(
        self, events: Generator[Dict[str, Any], None, None]
    ) -> None:
        """Consume a stream of events and render them to the terminal."""
        response_buffer = ""
        spinner = None
        pending_calls: dict = {}  # call_id → {name, args}
        had_tool_calls = False
        model_info = ""

        try:
            for chunk in events:
                chunk_type = chunk.get("type")
                content = chunk.get("content", "")

                if chunk_type == "thinking":
                    if not spinner:
                        spinner = self.console.status(
                            "[dim]Thinking...[/dim]", spinner="dots"
                        )
                        spinner.start()
                    if content:
                        preview = _truncate(content, 60)
                        spinner.update(f"[dim]{preview}[/dim]")

                elif chunk_type == "tool_call":
                    tool_name = chunk.get("name", "unknown")
                    tool_args = chunk.get("args", {})
                    call_id = chunk.get("id", "")

                    # Flush any buffered preamble text before tool calls
                    if spinner:
                        spinner.stop()
                        spinner = None
                    if response_buffer.strip():
                        self._flush_response(
                            response_buffer, after_tools=had_tool_calls
                        )
                        response_buffer = ""

                    pending_calls[call_id] = {
                        "name": tool_name,
                        "args": tool_args,
                    }

                    # Start/update spinner with all pending tool names
                    names = ", ".join(
                        c["name"] for c in pending_calls.values()
                    )
                    label = f"[dim]Running {names}...[/dim]"
                    spinner = self.console.status(label, spinner="dots")
                    spinner.start()

                elif chunk_type == "tool_result":
                    call_id = chunk.get("id", "")
                    result = chunk.get("result", "")

                    if spinner:
                        spinner.stop()
                        spinner = None

                    # Look up matching call by id, fall back to name
                    call = pending_calls.pop(call_id, None)
                    if call is None:
                        result_name = chunk.get("name", "")
                        for cid, c in list(pending_calls.items()):
                            if c["name"] == result_name:
                                call = pending_calls.pop(cid)
                                break

                    name = call["name"] if call else chunk.get("name", "tool")
                    args = call.get("args", {}) if call else {}

                    args_preview = _format_args_preview(args)
                    result_preview = (
                        _truncate(str(result), 120) if result else ""
                    )
                    is_error = result_preview and "Error:" in str(result)
                    result_style = "red" if is_error else "dim"

                    # Compact one-liner: > name [args] → result
                    parts = [f"  [yellow]>[/yellow] [yellow]{name}[/yellow]"]
                    if args_preview:
                        parts.append(f"[dim]{args_preview}[/dim]")
                    if result_preview:
                        parts.append(
                            f"[dim]→[/dim] [{result_style}]{result_preview}[/{result_style}]"
                        )
                    self.console.print(" ".join(parts))
                    had_tool_calls = True

                    # Restart spinner if more tools still pending
                    if pending_calls:
                        names = ", ".join(
                            c["name"] for c in pending_calls.values()
                        )
                        spinner = self.console.status(
                            f"[dim]Running {names}...[/dim]",
                            spinner="dots",
                        )
                        spinner.start()

                elif chunk_type == "response":
                    if spinner:
                        spinner.stop()
                        spinner = None
                    response_buffer += content

                elif chunk_type == "error":
                    if spinner:
                        spinner.stop()
                        spinner = None
                    code = chunk.get("code", "")
                    if code == "cancelled":
                        self.console.print("[dim]Cancelled.[/dim]")
                        return
                    self.console.print(f"[red]Error: {content}[/red]")

                elif chunk_type == "queued":
                    holder = chunk.get("holder", "unknown")
                    if spinner:
                        spinner.update(
                            f"[dim]Waiting for thread lock ({holder})...[/dim]"
                        )
                    else:
                        spinner = self.console.status(
                            f"[dim]Waiting for thread lock ({holder})...[/dim]",
                            spinner="dots",
                        )
                        spinner.start()

                elif chunk_type == "compacting":
                    if spinner:
                        spinner.update("[dim]Compacting context...[/dim]")
                    else:
                        spinner = self.console.status(
                            "[dim]Compacting context...[/dim]", spinner="dots"
                        )
                        spinner.start()

                elif chunk_type == "compact_result":
                    summary = chunk.get("summary", "")
                    if spinner:
                        spinner.stop()
                        spinner = None
                    if summary:
                        self.console.print(
                            "[green]Context compacted.[/green]"
                        )

                elif chunk_type == "iteration_limit":
                    if spinner:
                        spinner.stop()
                        spinner = None
                    message = chunk.get("content") or "Reached turn safety limit."
                    self.console.print(f"[yellow]{message}[/yellow]")

                elif chunk_type == "done":
                    model_info = chunk.get("model", "")

        except KeyboardInterrupt:
            if spinner:
                spinner.stop()
            # Signal abort to the agent
            self.state.agent.abort_with_cascade(self.state.thread_id)
            self.console.print("[dim]Cancelled.[/dim]")
            return
        finally:
            if spinner:
                spinner.stop()

        # Render any remaining buffered response as Markdown
        self._flush_response(response_buffer, after_tools=had_tool_calls)
