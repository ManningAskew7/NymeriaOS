"""CLI trigger for Nymeria - used for local testing."""

import sys
import uuid
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live

from .base import BaseTrigger
from ..core.agent import NymeriaAgent


class CLITrigger(BaseTrigger):
    """
    Command-line interface trigger for local testing.

    Provides an interactive REPL for chatting with the agent.
    """

    def __init__(self, agent: NymeriaAgent, thread_id: Optional[str] = None):
        """
        Initialize CLI trigger.

        Args:
            agent: The NymeriaAgent instance
            thread_id: Optional thread ID (generates new one if not provided)
        """
        super().__init__(agent)
        self.thread_id = thread_id or str(uuid.uuid4())[:8]
        self.console = Console()
        self.running = False

    def start(self) -> None:
        """Start the interactive CLI session."""
        self.running = True
        self._print_welcome()

        while self.running:
            try:
                # Get user input
                self.console.print()
                user_input = self.console.input("[bold cyan]You:[/bold cyan] ")

                if not user_input.strip():
                    continue

                # Handle special commands
                if self._handle_command(user_input):
                    continue

                # Process message with streaming
                self._process_with_streaming(user_input)

            except KeyboardInterrupt:
                self.console.print("\n[dim]Interrupted. Type 'exit' to quit.[/dim]")
            except EOFError:
                self.stop()

    def stop(self) -> None:
        """Stop the CLI session."""
        self.running = False
        self.console.print("\n[dim]Goodbye![/dim]")

    def _print_welcome(self) -> None:
        """Print welcome message."""
        self.console.print(
            Panel(
                "[bold blue]Nymeria[/bold blue] - Personal AI Assistant\n\n"
                f"Thread ID: [dim]{self.thread_id}[/dim]\n"
                "Commands: [dim]exit, clear, history, help[/dim]",
                title="Welcome",
                border_style="blue",
            )
        )

    def _handle_command(self, user_input: str) -> bool:
        """
        Handle special CLI commands.

        Returns True if a command was handled, False otherwise.
        """
        cmd = user_input.strip().lower()

        if cmd in ("exit", "quit", "q"):
            self.stop()
            return True

        if cmd == "clear":
            self.console.clear()
            self._print_welcome()
            return True

        if cmd == "history":
            self._show_history()
            return True

        if cmd == "help":
            self._show_help()
            return True

        if cmd == "new":
            self.thread_id = str(uuid.uuid4())[:8]
            self.console.print(f"[dim]Started new conversation: {self.thread_id}[/dim]")
            return True

        if cmd == "/compact":
            self._handle_compact()
            return True

        if cmd == "/context":
            self._show_context_stats()
            return True

        return False

    def _handle_compact(self) -> None:
        """Handle the /compact command."""
        import asyncio
        self.console.print("[dim]Compacting conversation...[/dim]")
        try:
            result = asyncio.run(self.agent.compact_now(self.thread_id))
            if result.get("success"):
                before = result.get('messages_before', '?')
                after = result.get('messages_after', '?')
                removed = result.get('messages_removed', 0)
                self.console.print(
                    f"[green]Compacted! Removed {removed} messages. "
                    f"({before} -> {after})[/green]"
                )
            else:
                self.console.print(f"[yellow]Skipped: {result.get('reason', 'Unknown reason')}[/yellow]")
        except Exception as e:
            self.console.print(f"[red]Error: {e}[/red]")

    def _show_context_stats(self) -> None:
        """Show context window usage statistics."""
        stats = self.agent.get_context_stats(self.thread_id)
        self.console.print(Panel(
            f"Thread: {stats['thread_id']}\n"
            f"Total tokens: {stats['total_tokens']:,} / {stats['context_limit']:,} ({stats['usage_percentage']}%)\n"
            f"  Input: {stats['input_tokens']:,}\n"
            f"  Output: {stats['output_tokens']:,}\n"
            f"Context management: {stats['context_management']}\n"
            f"Compactions: {stats['compaction_count']}\n"
            f"Last compaction: {stats['last_compaction'] or 'Never'}",
            title="Context Stats",
            border_style="dim",
        ))

    def _show_history(self) -> None:
        """Show conversation history."""
        history = self.agent.get_conversation_history(self.thread_id)
        if not history:
            self.console.print("[dim]No conversation history.[/dim]")
            return

        self.console.print(Panel("[bold]Conversation History[/bold]", border_style="dim"))
        for msg in history:
            msg_type = msg.get("type", "Unknown")
            content = msg.get("content", "")[:200]
            if msg_type == "HumanMessage":
                self.console.print(f"[cyan]You:[/cyan] {content}")
            elif msg_type == "AIMessage":
                self.console.print(f"[green]Nymeria:[/green] {content}...")
            elif msg_type == "ToolMessage":
                tool_name = msg.get("tool_name", "unknown")
                self.console.print(f"[yellow]Tool ({tool_name}):[/yellow] {content[:100]}...")

    def _show_help(self) -> None:
        """Show help message."""
        help_text = """
**Commands:**
- `exit` / `quit` / `q` - Exit the CLI
- `clear` - Clear the screen
- `history` - Show conversation history
- `new` - Start a new conversation
- `/compact` - Compact conversation (summarize older messages)
- `/context` - Show context window usage stats
- `help` - Show this help message

**Tips:**
- Just type your message to chat with Nymeria
- The agent can run shell commands, read/write files, and search the web
- Conversation history is preserved within a thread
- Use `/compact` to manage long conversations
        """
        self.console.print(Panel(Markdown(help_text), title="Help", border_style="green"))

    def _process_with_streaming(self, user_input: str) -> None:
        """Process a message with streaming output."""
        self.console.print()
        self.console.print("[bold green]Nymeria:[/bold green]")

        full_response = ""
        tool_calls_shown = set()
        saw_tool_result = False

        try:
            for chunk in self.agent.stream(user_input, thread_id=self.thread_id):
                chunk_type = chunk.get("type")
                content = chunk.get("content", "")

                if chunk_type == "thinking":
                    # Show thinking/planning text in dim italic
                    if content:
                        self.console.print(f"[dim italic]{content}[/dim italic]")

                elif chunk_type == "tool_call":
                    tool_name = chunk.get("name", "unknown")
                    tool_args = chunk.get("args", {})
                    key = f"{tool_name}:{str(tool_args)}"
                    if key not in tool_calls_shown:
                        tool_calls_shown.add(key)
                        self.console.print(
                            f"[yellow]→ Using tool: {tool_name}[/yellow]"
                        )
                        # Show args only if not empty and not too long
                        if tool_args:
                            args_str = str(tool_args)
                            if len(args_str) < 200:
                                self.console.print(f"[dim]  Args: {args_str}[/dim]")

                elif chunk_type == "tool_result":
                    tool_name = chunk.get("name", "unknown")
                    result = chunk.get("result", "")
                    # Show truncated result
                    result_preview = result[:300] + "..." if len(result) > 300 else result
                    self.console.print(f"[dim]  Result: {result_preview}[/dim]")
                    saw_tool_result = True

                elif chunk_type == "response":
                    # Always accumulate response content
                    full_response += content

                elif chunk_type == "error":
                    self.console.print(f"[red]Error: {content}[/red]")

            # Print final response with markdown
            if full_response:
                self.console.print()
                self.console.print(Markdown(full_response))

        except Exception as e:
            self.console.print(f"[red]Error: {str(e)}[/red]")


def run_cli(agent: Optional[NymeriaAgent] = None, thread_id: Optional[str] = None) -> None:
    """
    Run the CLI trigger.

    Args:
        agent: Optional agent instance (creates default if not provided)
        thread_id: Optional thread ID for the conversation
    """
    if agent is None:
        from ..tools import get_all_tools_with_agents

        agent = NymeriaAgent(tools=get_all_tools_with_agents())

    cli = CLITrigger(agent, thread_id=thread_id)
    cli.start()
