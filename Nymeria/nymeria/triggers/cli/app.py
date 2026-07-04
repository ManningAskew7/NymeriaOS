"""CLIApp: main REPL loop and orchestrator."""

from __future__ import annotations

import asyncio
import getpass
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TYPE_CHECKING


from .capabilities import TerminalCapabilities, detect_terminal_capabilities
from .command_routing import (
    chat_stream_command_from_result as _chat_stream_command_from_result,
)
from .commands import (
    CommandContext,
    ListCommandOutputSink,
    CommandMessage,
    CommandRegistry,
    RichConsoleCommandOutputSink,
)
from .follow_footer import _RICH_SCROLL_REGION_MIN_ROWS
from .repl_runtime import (
    _RichReplPromptToolkitShell,
    _RichReplRuntime,
    _disconnected_notice_text,
    _repl_prompt_style,
)
from .header import CLIHeaderSnapshot, build_header_snapshot, concise_connection_label
from .rendering import form_panel
from .rendering.welcome import render_welcome
from .rendering.plain import PlainRenderer, strip_ansi
from .rendering.rich_repl import RichReplRenderer
from .state import CLIState, create_initial_state
from .temporary_model import apply_temporary_model, restore_temporary_model
from .theme import CLITheme, load_cli_theme
from .transport.base import AgentClient, Attachment
from .transport.disconnected import is_disconnected_client
from .transport.in_process import InProcessAgentClient

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent
    from .commands import CommandResult


logger = logging.getLogger(__name__)

TransportMode = Literal["api", "local", "auto"]
RendererMode = Literal["rich", "plain", "auto"]
ColorMode = Literal["auto", "always", "never"]


@dataclass(frozen=True, slots=True)
class CLIRuntimeConfig:
    """Launch-time CLI options shared by future transport and renderer tasks."""

    transport: TransportMode = "api"
    renderer: RendererMode = "auto"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    user_id: str = "default"
    user_id_explicit: bool = False
    animation: bool = True
    ascii_only: bool = False
    color: ColorMode = "auto"
    rich_scroll_region: bool = False
    startup_thread_ref: str | None = None
    list_threads_on_startup: bool = False
    continue_last: bool = False
    resume_ref: str | None = None
    oneshot_message: str | None = None
    oneshot_format: str = "plain"


class _ReplRenderer(Protocol):
    def start_turn(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        attachments: Any = None,
        now: float | None = None,
    ) -> Any:
        """Start a visible chat turn."""

    async def render_async_events(
        self,
        events: Any,
        *,
        now: float | None = None,
    ) -> Any:
        """Render an async normalized event stream."""


class PlainCommandOutputSink:
    """Plain output adapter for slash-command results in non-Rich modes."""

    def __init__(self, file: Any = None) -> None:
        self.file = file or sys.stderr

    def emit(self, message: CommandMessage) -> None:
        content = strip_ansi(str(message.content or ""))
        if content:
            self.file.write(f"{content}\n")
            self.file.flush()


class CLIApp:
    """
    The main CLI application.

    Manages the REPL loop, dispatching commands and chat messages.
    """

    def __init__(
        self,
        agent: "NymeriaAgent | None",
        thread_id: Optional[str] = None,
        user_id: str = "default",
        runtime_config: CLIRuntimeConfig | None = None,
    ) -> None:
        self.runtime_config = runtime_config or CLIRuntimeConfig(user_id=user_id)
        self._startup_thread_id_was_provided = thread_id is not None
        self.state = CLIState(
            agent,
            thread_id=thread_id,
            user_id=self.runtime_config.user_id,
        )
        self.registry = CommandRegistry()
        self._local_client: InProcessAgentClient | None = None
        self._client: AgentClient | None = None
        self._active_repl_renderer: _ReplRenderer | None = None
        self._active_rich_runtime: _RichReplRuntime | None = None
        self._active_capabilities: TerminalCapabilities | None = None
        self._repl_thread_label: str | None = None
        self._repl_model_label: str | None = None
        self._repl_reasoning_label: str = ""
        self._repl_fast_active = False
        self._header_snapshot: CLIHeaderSnapshot | None = None
        self._header_refresh_pending = False
        self._startup_history_thread_id: str | None = None
        self.theme = load_cli_theme()
        self._register_all_commands()

    def _register_all_commands(self) -> None:
        """Import and register all command modules."""
        from .commands import (
            account,
            activity,
            artifacts,
            backend,
            clipboard,
            connection,
            context,
            conversation,
            doctor,
            export,
            fallback,
            fast,
            reasoning,
            mcp,
            memory,
            model,
            provider,
            skills,
            smart,
            system,
            theme,
            threads,
            todos,
            tools,
            triggers,
            usage,
        )

        # Backend commands register first so they always win at the root
        # level. Local CLI commands either target a name the backend never
        # claims (truly frontend-local: theme, clipboard, etc.) or contribute
        # subcommands that get merged under a backend-owned root. See
        # CommandRegistry.register for the merge rules.
        backend.register(self.registry)

        system.register(self.registry)
        connection.register(self.registry)
        context.register(self.registry)
        threads.register(self.registry)
        model.register(self.registry)
        tools.register(self.registry)
        skills.register(self.registry)
        mcp.register(self.registry)
        todos.register(self.registry)
        memory.register(self.registry)
        account.register(self.registry)
        triggers.register(self.registry)
        activity.register(self.registry)
        artifacts.register(self.registry)
        doctor.register(self.registry)
        theme.register(self.registry)
        export.register(self.registry)
        fallback.register(self.registry)
        fast.register(self.registry)
        smart.register(self.registry)
        provider.register(self.registry)
        clipboard.register(self.registry)
        conversation.register(self.registry)
        reasoning.register(self.registry)
        usage.register(self.registry)

    def run(self) -> None:
        """Main REPL loop, or oneshot mode if a message was provided."""
        if self.runtime_config.oneshot_message is not None:
            sys.exit(0 if self.run_oneshot() else 1)

        capabilities = detect_terminal_capabilities(self.runtime_config)
        self._run_repl(capabilities)

    def run_oneshot(self) -> bool:
        """Send a single message, stream the response to stdout, and return success."""
        from .rendering.oneshot import OneshotRenderer

        message = self.runtime_config.oneshot_message or ""
        if message == "-":
            message = sys.stdin.read()
        if not message.strip():
            sys.stderr.write("Error: empty message\n")
            return False

        output_format = self.runtime_config.oneshot_format
        if output_format not in ("plain", "json"):
            output_format = "plain"

        renderer = OneshotRenderer(
            output_format=output_format,
            verbose=True,
        )

        async def _run() -> bool:
            try:
                client = await self._select_agent_client()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"Error: {exc}\n")
                return False
            self._client = client
            try:
                if self._session_resume_requested():
                    resolved = await self._resolve_session_resume_flags()
                    if resolved is None:
                        return False
                    self.state.switch_thread(resolved["thread_id"])
                else:
                    await self._ensure_new_cli_thread_metadata()
                events = client.stream_chat(
                    message.strip(),
                    self.state.thread_id,
                    self.state.user_id,
                )
                return await renderer.consume(events)
            finally:
                await self._close_selected_client()

        return asyncio.run(_run())

    def _run_repl(self, capabilities: TerminalCapabilities) -> None:
        """Run the prompt_toolkit REPL with reducer-backed rich/plain rendering."""
        from .transport.api import APITransportStartupError

        self._active_capabilities = capabilities
        try:
            self._client = asyncio.run(self._select_agent_client())
        except APITransportStartupError as exc:
            self._render_startup_error(exc.message, capabilities)
            self._active_capabilities = None
            return
        if asyncio.run(self._handle_startup_thread_intents_async(capabilities)):
            asyncio.run(self._close_selected_client())
            self._active_capabilities = None
            return

        # Try to use prompt_toolkit; fall back to basic input if unavailable
        try:
            from .input import create_session

            data_dir = self.state.settings.data_dir
            session = create_session(
                data_dir,
                self.registry,
                erase_when_done=capabilities.renderer == "rich",
            )
            use_prompt_toolkit = True
        except ImportError:
            session = None
            use_prompt_toolkit = False

        if capabilities.renderer != "plain":
            self._refresh_header_snapshot(capabilities)
            render_welcome(
                self.state,
                self._header_snapshot,
                theme=self.theme,
                capabilities=capabilities,
            )
        self._render_disconnected_notice(self._client, capabilities)

        # patch_stdout intercepts background-thread writes (ticker, watchdog)
        # and redraws the prompt after they finish.
        use_follow_footer_stdout = (
            use_prompt_toolkit
            and bool(getattr(self.runtime_config, "rich_scroll_region", False))
            and capabilities.renderer == "rich"
            and sys.platform != "win32"
            and capabilities.is_interactive
            and capabilities.height >= _RICH_SCROLL_REGION_MIN_ROWS
        )
        if use_prompt_toolkit and not use_follow_footer_stdout:
            from prompt_toolkit.patch_stdout import patch_stdout

            stdout_ctx = patch_stdout(raw=True)
        else:
            stdout_ctx = nullcontext()

        renderer: _ReplRenderer | None = None
        runtime: _RichReplRuntime | None = None
        try:
            with stdout_ctx:
                renderer = self._create_repl_renderer(capabilities)
                if isinstance(renderer, RichReplRenderer):
                    runtime = _RichReplRuntime(
                        app=self,
                        renderer=renderer,
                        capabilities=capabilities,
                    )
                    runtime.install_resize_handler()
                    if is_disconnected_client(self._client):
                        runtime.set_status_notice(
                            _disconnected_notice_text(
                                self._client,
                                auto_reconnect=True,
                            ),
                            level="warning",
                        )
                self._active_repl_renderer = renderer
                self._active_rich_runtime = runtime
                if isinstance(renderer, RichReplRenderer):
                    asyncio.run(self._load_startup_history_into_renderer(renderer))
                if use_prompt_toolkit and runtime is not None:
                    shell = _RichReplPromptToolkitShell(
                        cli_app=self,
                        runtime=runtime,
                        renderer=renderer,
                        capabilities=capabilities,
                        history_path=self.state.settings.data_dir / "cli_history",
                    )
                    asyncio.run(
                        shell.run_async()
                    )
                else:
                    self._repl_loop(
                        session,
                        use_prompt_toolkit,
                        capabilities,
                        renderer,
                        runtime=runtime,
                    )
        finally:
            if runtime is not None:
                runtime.uninstall_resize_handler()
                runtime.stop_autonomous_listener()
                runtime.stop_reconnect_watcher()
            self._active_capabilities = None
            self._active_rich_runtime = None
            self._active_repl_renderer = None
            asyncio.run(self._close_selected_client())

    async def _select_agent_client(self) -> AgentClient:
        from .transport.api import APITransportStartupError, DEFAULT_API_URL, select_agent_client

        local_client = None
        if self.runtime_config.transport == "local":
            if self.state.agent is None:
                raise APITransportStartupError(
                    "Local transport requested without a NymeriaAgent.",
                    code="local_transport_unavailable",
                    api_url=DEFAULT_API_URL,
                )
            self._local_client = InProcessAgentClient(
                self.state.agent,
                default_user_id=self.state.user_id,
            )
            local_client = self._local_client
        selected = await select_agent_client(
            self.runtime_config,
            local_client=local_client,
        )
        self._apply_selected_client_user(selected)
        return selected

    async def _handle_startup_thread_intents_async(
        self,
        capabilities: TerminalCapabilities,
    ) -> bool:
        """Handle ``nymeria cli list`` and startup thread refs before the REPL."""

        if self.runtime_config.list_threads_on_startup:
            await self._render_startup_thread_list_async(capabilities)
            return True

        if self.runtime_config.continue_last:
            resolved = await self._resolve_most_recent_thread()
            if resolved is None:
                return True
            self.state.switch_thread(resolved["thread_id"])
            self._repl_thread_label = resolved["title"]
            self._startup_history_thread_id = resolved["thread_id"]
            return False

        resume_ref = str(self.runtime_config.resume_ref or "").strip()
        if resume_ref:
            resolved = await self._resolve_startup_thread_ref(resume_ref)
            if resolved is None:
                return True
            self.state.switch_thread(resolved["thread_id"])
            self._repl_thread_label = resolved["title"]
            self._startup_history_thread_id = resolved["thread_id"]
            return False

        ref = str(self.runtime_config.startup_thread_ref or "").strip()
        if not ref:
            await self._ensure_new_cli_thread_metadata()
            return False

        resolved = await self._resolve_startup_thread_ref(ref)
        if resolved is None:
            return True
        self.state.switch_thread(resolved["thread_id"])
        self._repl_thread_label = resolved["title"]
        self._startup_history_thread_id = resolved["thread_id"]
        return False

    async def _ensure_new_cli_thread_metadata(self) -> None:
        """Persist metadata for the CLI-generated startup thread."""

        if self._startup_thread_id_was_provided:
            return
        if self._client is None or is_disconnected_client(self._client):
            return
        create_thread_attr = getattr(self._client, "create_thread", None)
        if not callable(create_thread_attr):
            return
        create_thread: Any = create_thread_attr
        try:
            await create_thread(
                self.state.user_id,
                thread_id=self.state.thread_id,
            )
        except Exception:  # noqa: BLE001 - startup metadata persistence is best effort.
            logger.debug("Failed to persist startup thread metadata", exc_info=True)
            return

    async def _render_startup_thread_list_async(
        self,
        capabilities: TerminalCapabilities,
    ) -> None:
        output = ListCommandOutputSink()
        context = CommandContext(
            client=self._client,
            output=output,
            dispatch_state=self._dispatch_repl_action,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
            metadata={"capabilities": capabilities},
        )
        result = await self.registry.dispatch_async(context, "/thread list")
        sink = self._command_output_sink(capabilities)
        for message in result.messages or output.messages:
            sink.emit(message)
        if not result.messages and not output.messages and not result.ok:
            sink.emit(CommandMessage("Thread list failed.", level="error"))

    def _startup_capabilities(self) -> TerminalCapabilities:
        """Resolve capabilities for a startup-error render, lazily.

        Prefers the active REPL capabilities and only probes the terminal when
        none are active, preserving the prior inline ``self._active_capabilities
        or detect_terminal_capabilities(...)`` short-circuit that each startup
        error branch used.
        """
        return self._active_capabilities or detect_terminal_capabilities(self.runtime_config)

    async def _load_threads_for_startup(self, error_prefix: str) -> list[Mapping[str, Any]] | None:
        """Fetch and coerce the thread list for startup selection.

        Shared preamble of ``_resolve_startup_thread_ref`` and
        ``_resolve_most_recent_thread``: builds the command context, calls
        ``list_threads``, and on a missing client method or any failure renders a
        startup error (using ``error_prefix`` for the message) and returns
        ``None``. On success returns the coerced list of Mapping threads (possibly
        empty); each caller applies its own selection.
        """
        from .commands._shared import CommandClientMethodUnavailable, call_client_method

        context = CommandContext(
            client=self._client,
            output=ListCommandOutputSink(),
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
        )
        try:
            raw_threads = await call_client_method(context, "list_threads", self.state.user_id)
        except CommandClientMethodUnavailable as exc:
            self._render_startup_error(
                f"{error_prefix}: missing client method {exc.method_name}.",
                self._startup_capabilities(),
            )
            return None
        except Exception as exc:  # noqa: BLE001 - startup selection should explain and exit.
            self._render_startup_error(
                f"{error_prefix}: {exc or exc.__class__.__name__}",
                self._startup_capabilities(),
            )
            return None

        if not isinstance(raw_threads, Sequence) or isinstance(raw_threads, (str, bytes)):
            raw_threads = []
        return [thread for thread in raw_threads if isinstance(thread, Mapping)]

    async def _resolve_startup_thread_ref(self, ref: str) -> dict[str, str] | None:
        from .commands.threads import (
            _format_thread_resolution_ambiguity,
            resolve_thread_reference,
        )
        from .commands._shared import normalize_thread_id, thread_title

        threads = await self._load_threads_for_startup(f"Cannot open thread '{ref}'")
        if threads is None:
            return None
        resolution = resolve_thread_reference(threads, ref)
        if resolution.matched and resolution.thread is not None:
            thread_id = normalize_thread_id(resolution.thread)
            return {"thread_id": thread_id, "title": thread_title(resolution.thread)}
        if resolution.status == "ambiguous":
            message = _format_thread_resolution_ambiguity(ref, resolution.matches)
        else:
            message = f"No thread matching '{ref}'."
        self._render_startup_error(message, self._startup_capabilities())
        return None

    async def _resolve_most_recent_thread(self) -> dict[str, str] | None:
        """Find the most recently updated thread for ``--continue``."""
        from .commands._shared import normalize_thread_id, thread_title

        threads = await self._load_threads_for_startup("Cannot continue")
        if threads is None:
            return None
        if not threads:
            self._render_startup_error(
                "No threads found to continue.",
                self._startup_capabilities(),
            )
            return None

        most_recent = max(
            threads,
            key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""),
        )
        tid = normalize_thread_id(most_recent)
        if not tid:
            self._render_startup_error(
                "No threads found to continue.",
                self._startup_capabilities(),
            )
            return None
        return {"thread_id": tid, "title": thread_title(most_recent)}

    def _session_resume_requested(self) -> bool:
        return self.runtime_config.continue_last or bool(self.runtime_config.resume_ref)

    async def _resolve_session_resume_flags(self) -> dict[str, str] | None:
        """Resolve ``--continue`` or ``--resume`` to a thread, if either was set."""
        if self.runtime_config.continue_last:
            return await self._resolve_most_recent_thread()
        resume_ref = str(self.runtime_config.resume_ref or "").strip()
        if resume_ref:
            return await self._resolve_startup_thread_ref(resume_ref)
        return None

    def _compact_settings(self) -> Any | None:
        """Settings-like object for resolving the compact trigger in displays."""
        try:
            return self.state.settings
        except Exception:  # noqa: BLE001
            logger.debug("Failed to resolve settings for compact display", exc_info=True)
            return None

    def _status_connection_label(self) -> str:
        return concise_connection_label(self._header_snapshot, self._client)

    def _render_current_header(self, capabilities: TerminalCapabilities | None = None) -> None:
        selected_capabilities = capabilities or self._active_capabilities
        render_welcome(
            self.state,
            self._header_snapshot,
            theme=self.theme,
            capabilities=selected_capabilities,
        )

    def _refresh_header_snapshot(
        self,
        capabilities: TerminalCapabilities | None = None,
    ) -> CLIHeaderSnapshot | None:
        """Sync wrapper over the async twin (plain REPL path, no running loop)."""
        return asyncio.run(self._refresh_header_snapshot_async(capabilities))

    async def _refresh_header_snapshot_async(
        self,
        capabilities: TerminalCapabilities | None = None,
    ) -> CLIHeaderSnapshot | None:
        del capabilities
        if self._client is None:
            self._header_snapshot = None
            return None
        snapshot = await build_header_snapshot(
            self.state,
            self._client,
            runtime_config=self.runtime_config,
        )
        self._header_snapshot = snapshot
        self._header_refresh_pending = False
        runtime = self._active_rich_runtime
        if runtime is not None:
            runtime.invalidate()
        return snapshot

    def _mark_header_refresh_pending(self) -> None:
        self._header_refresh_pending = True

    def _refresh_and_render_pending_header(
        self,
        capabilities: TerminalCapabilities,
    ) -> None:
        if not self._header_refresh_pending or capabilities.renderer == "plain":
            return
        self._refresh_header_snapshot(capabilities)
        self._render_current_header(capabilities)

    async def _refresh_and_render_pending_header_async(
        self,
        runtime: _RichReplRuntime,
    ) -> None:
        if not self._header_refresh_pending or runtime.capabilities.renderer == "plain":
            return
        await self._refresh_header_snapshot_async(runtime.capabilities)
        await runtime.render_above_prompt(
            lambda: self._render_current_header(runtime.capabilities)
        )

    async def _close_selected_client(self) -> None:
        client = self._client
        self._client = None
        close_attr = getattr(client, "close", None)
        if callable(close_attr):
            close: Any = close_attr
            await close()

    def _create_repl_renderer(
        self,
        capabilities: TerminalCapabilities,
    ) -> _ReplRenderer:
        width = getattr(capabilities, "width", None)
        if capabilities.renderer == "plain":
            return PlainRenderer(width=width)
        return RichReplRenderer(
            capabilities=capabilities,
            width=width,
            theme=self.theme,
            stream_rich_response_lines=bool(
                getattr(self.runtime_config, "rich_scroll_region", False)
            ),
            download_base_url=getattr(self._client, "base_url", "") or "",
        )

    def _repl_loop(
        self,
        session,
        use_prompt_toolkit: bool,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        """Inner REPL loop, run inside patch_stdout context."""
        while self.state.running:
            try:
                if use_prompt_toolkit:
                    from .input import get_prompt

                    prompt = get_prompt(
                        self.state,
                        busy=bool(runtime and runtime.busy),
                        theme=self.theme,
                    )
                    prompt_kwargs = runtime.prompt_kwargs() if runtime else {}
                    user_input = session.prompt(prompt, **prompt_kwargs)
                else:
                    user_input = self.state.console.input(
                        "[bold cyan]You:[/bold cyan] "
                    )

                if not user_input.strip():
                    continue

                stripped = user_input.strip()

                # Dispatch /commands
                if stripped.startswith("/"):
                    self._dispatch_command(
                        stripped,
                        capabilities,
                        renderer,
                        session=session if use_prompt_toolkit else None,
                    )
                    continue

                # Legacy bare commands (no / prefix)
                lower = stripped.lower()
                if lower in ("exit", "quit", "q"):
                    self.state.running = False
                    self.state.console.print("[dim]Goodbye![/dim]")
                    continue
                if lower == "clear":
                    self.state.console.clear()
                    if capabilities.renderer != "plain":
                        self._refresh_header_snapshot(capabilities)
                        self._render_current_header(capabilities)
                    continue
                if lower == "help":
                    self._dispatch_command("/help", capabilities, renderer)
                    continue

                # Chat message
                self._submit_repl_message(stripped, renderer, runtime=runtime)

            except KeyboardInterrupt:
                # At the prompt, Ctrl+C clears input (prompt_toolkit handles it).
                # If we get here, just continue.
                self.state.console.print()
                continue
            except EOFError:
                self.state.running = False
                self.state.console.print("[dim]Goodbye![/dim]")

    def _dispatch_command(
        self,
        raw_input: str,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        session: Any | None = None,
    ) -> None:
        """Dispatch one slash command through the v2 command context."""

        context = CommandContext(
            client=self._client,
            output=self._command_output_sink(capabilities),
            dispatch_state=self._dispatch_repl_action,
            prompt_handler=lambda prompt: self._prompt_for_input(
                prompt,
                session=session,
            ),
            secret_prompt_handler=lambda prompt: self._prompt_for_input(
                prompt,
                secret=True,
                session=session,
            ),
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
            metadata={
                "capabilities": capabilities,
                "ui_state": getattr(renderer, "state", None),
                "transcript_verbose": bool(
                    getattr(renderer, "transcript_verbose", False)
                ),
            },
        )
        result = asyncio.run(self.registry.dispatch_async(context, raw_input))
        self._apply_repl_command_result(result, capabilities)

        fast_prompt = _fast_prompt_from_result(result)
        if fast_prompt is not None:
            prompt, model = fast_prompt
            self._send_temporary_model_message(
                prompt,
                model,
                renderer,
                capabilities=capabilities,
                runtime=None,
            )
            return

        chat_stream_command = _chat_stream_command_from_result(result)
        if chat_stream_command:
            self._send_message(chat_stream_command, renderer)
            return

        retry_message = result.payload.get("retry_message")
        if retry_message and isinstance(retry_message, str):
            self._send_message(retry_message, renderer)

    async def _dispatch_command_async(
        self,
        raw_input: str,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        *,
        session: Any | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        """Dispatch one slash command from the async Rich prompt loop."""

        output_sink = (
            ListCommandOutputSink()
            if runtime is not None and capabilities.renderer != "plain"
            else self._command_output_sink(capabilities)
        )
        context = CommandContext(
            client=self._client,
            output=output_sink,
            dispatch_state=self._dispatch_repl_action,
            prompt_handler=lambda prompt: self._prompt_for_input_async(
                prompt,
                session=session,
                runtime=runtime,
            ),
            secret_prompt_handler=lambda prompt: self._prompt_for_input_async(
                prompt,
                secret=True,
                session=session,
                runtime=runtime,
            ),
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
            metadata={
                "capabilities": capabilities,
                "ui_state": getattr(renderer, "state", None),
                "transcript_verbose": bool(
                    getattr(renderer, "transcript_verbose", False)
                ),
            },
        )
        result = await self.registry.dispatch_async(context, raw_input)
        if runtime is not None:
            await self._apply_repl_command_result_async(result, capabilities, runtime)
        else:
            self._apply_repl_command_result(result, capabilities)
        if runtime is not None and bool(result.payload.get("thread_switched")):
            if result.messages:
                runtime.set_status_notice(result.messages[0].content)
            await self._render_current_thread_history_above_prompt(runtime)
            return
        if runtime is not None and isinstance(output_sink, ListCommandOutputSink):
            await self._render_command_messages_above_prompt(output_sink.messages, runtime)
        if runtime is not None:
            await self._refresh_and_render_pending_header_async(runtime)

        fast_prompt = _fast_prompt_from_result(result)
        if fast_prompt is not None:
            prompt, model = fast_prompt
            if runtime is not None:
                runtime.set_status_notice(f"Fast turn ({model})")
                await self._send_temporary_model_message_async(
                    prompt,
                    model,
                    renderer,
                    capabilities=capabilities,
                    runtime=runtime,
                )
            else:
                self._send_temporary_model_message(
                    prompt,
                    model,
                    renderer,
                    capabilities=capabilities,
                    runtime=None,
                )
            return

        chat_stream_command = _chat_stream_command_from_result(result)
        if chat_stream_command:
            if runtime is not None:
                await self._send_message_async(
                    chat_stream_command,
                    renderer,
                    runtime=runtime,
                )
            else:
                self._send_message(chat_stream_command, renderer)
            return

        retry_message = result.payload.get("retry_message")
        if retry_message and isinstance(retry_message, str):
            if runtime is not None:
                await self._send_message_async(retry_message, renderer, runtime=runtime)
            else:
                self._send_message(retry_message, renderer)

    def _command_output_sink(self, capabilities: TerminalCapabilities):
        if capabilities.renderer == "plain":
            return PlainCommandOutputSink()
        return RichConsoleCommandOutputSink(self.state.console)

    async def _dispatch_repl_action(self, action: Any) -> None:
        if not isinstance(action, dict):
            return
        action_type = action.get("type")
        refresh_header = False
        if action_type == "switch_thread":
            thread_id = str(action.get("thread_id") or self.state.thread_id)
            self.state.switch_thread(thread_id)
            self._repl_thread_label = str(action.get("thread_label") or thread_id)
            if action.get("model"):
                self._repl_model_label = str(action.get("model"))
            self._reset_active_repl_state()
            refresh_header = True
        elif action_type == "switch_user":
            user_id = str(action.get("user_id") or self.state.user_id)
            self.state.user_id = user_id
            self._repl_thread_label = None
            self._repl_model_label = None
            if self._local_client is not None:
                self._local_client.default_user_id = user_id
            self.runtime_config = replace(
                self.runtime_config,
                user_id=user_id,
                user_id_explicit=True,
            )
            self._reset_active_repl_state()
            refresh_header = True
        elif action_type == "replace_client":
            await self._replace_repl_client(action)
            runtime = self._active_rich_runtime
            if runtime is not None:
                if is_disconnected_client(self._client):
                    runtime.set_status_notice(
                        _disconnected_notice_text(
                            self._client,
                            auto_reconnect=True,
                        ),
                        level="warning",
                    )
                runtime.restart_autonomous_listener()
                runtime.start_reconnect_watcher()
            refresh_header = True
        elif action_type == "set_thread_label":
            self._repl_thread_label = str(
                action.get("thread_label")
                or self._repl_thread_label
                or self.state.thread_id
            )
            refresh_header = True
        elif action_type == "set_model":
            self._repl_model_label = str(action.get("model") or "")
            self._repl_fast_active = bool(action.get("fast_mode", False))
            refresh_header = True
        elif action_type == "set_fast_mode":
            self._repl_fast_active = bool(action.get("active", False))
            refresh_header = True
        elif action_type == "set_reasoning":
            enabled = bool(action.get("enabled"))
            effort = str(action.get("effort") or "")
            if enabled:
                self._repl_reasoning_label = f"thinking: {effort}" if effort else "thinking"
            else:
                self._repl_reasoning_label = ""
            refresh_header = True
        elif action_type == "thread_config_updated":
            refresh_header = True
        elif action_type in {
            "thread_metadata_updated",
            "thread_context_updated",
            "tools_updated",
            "skills_updated",
            "mcp_updated",
            "triggers_updated",
        }:
            refresh_header = True
        elif action_type == "set_transcript_verbose":
            renderer = self._active_repl_renderer
            if renderer is not None and hasattr(renderer, "transcript_verbose"):
                setattr(renderer, "transcript_verbose", bool(action.get("enabled")))
        elif action_type == "theme_updated":
            theme = action.get("theme")
            if isinstance(theme, CLITheme):
                self.theme = theme
                renderer = self._active_repl_renderer
                if renderer is not None and hasattr(renderer, "set_theme"):
                    renderer.set_theme(theme)
                runtime = self._active_rich_runtime
                if runtime is not None and runtime.application is not None:
                    runtime.application.style = _repl_prompt_style(
                        runtime.capabilities,
                        theme=theme,
                    )
                    runtime.invalidate()
                refresh_header = True
        elif action_type == "clear_transcript":
            self._reset_active_repl_state()
            runtime = self._active_rich_runtime
            if runtime is not None:
                await runtime.render_from_screen_top(lambda: None)
            else:
                self.state.console.clear()
            refresh_header = True
        elif action_type == "undo_last_exchange":
            self._undo_last_exchange_from_renderer()
            refresh_header = True
        elif action_type == "redraw":
            runtime = self._active_rich_runtime
            if runtime is not None:
                await runtime.redraw()
            return
        elif action_type == "open_form":
            spec = action.get("spec")
            runtime = self._active_rich_runtime
            if runtime is not None and isinstance(spec, form_panel.FormSpec):
                runtime.open_form(spec)
            return
        elif action_type == "close_form":
            runtime = self._active_rich_runtime
            if runtime is not None:
                runtime.close_form()
                runtime.invalidate()
            return
        if refresh_header:
            self._mark_header_refresh_pending()

    def _apply_repl_command_result(
        self,
        result: "CommandResult",
        capabilities: TerminalCapabilities,
    ) -> None:
        if result.status == "exit":
            self.state.running = False
        elif result.status == "clear" and capabilities.renderer != "plain":
            self.state.console.clear()
            self._refresh_header_snapshot(capabilities)
            self._render_current_header(capabilities)
        self._refresh_and_render_pending_header(capabilities)

    async def _apply_repl_command_result_async(
        self,
        result: "CommandResult",
        capabilities: TerminalCapabilities,
        runtime: _RichReplRuntime,
    ) -> None:
        if result.status == "exit":
            self.state.running = False
            runtime.exit()
            return
        if result.status == "clear" and capabilities.renderer != "plain":
            self._reset_active_repl_state()

            def clear_and_welcome() -> None:
                self._render_current_header(capabilities)

            await self._refresh_header_snapshot_async(capabilities)
            await runtime.render_from_screen_top(clear_and_welcome)
            return

    async def _render_command_messages_above_prompt(
        self,
        messages: Sequence[CommandMessage],
        runtime: _RichReplRuntime,
    ) -> None:
        if not messages:
            return

        def render_messages() -> None:
            sink = RichConsoleCommandOutputSink(self.state.console)
            for message in messages:
                sink.emit(message)

        await runtime.render_above_prompt(render_messages)

    async def _load_startup_history_into_renderer(
        self,
        renderer: RichReplRenderer,
    ) -> None:
        if self._startup_history_thread_id != self.state.thread_id:
            return
        try:
            history_state = await self._load_current_thread_history_state()
        except Exception as exc:  # noqa: BLE001 - history load must not kill REPL.
            self.state.console.print(
                f"[yellow]Could not load thread history: "
                f"{exc or exc.__class__.__name__}[/yellow]"
            )
            return
        renderer.reset_state(history_state)
        renderer.render_state()
        self._startup_history_thread_id = None

    async def _render_current_thread_history_above_prompt(
        self,
        runtime: _RichReplRuntime,
    ) -> None:
        try:
            history_state = await self._load_current_thread_history_state()
        except Exception as exc:  # noqa: BLE001 - keep the interactive prompt alive.
            runtime.set_status_notice(
                f"Could not load history: {exc or exc.__class__.__name__}",
                level="warning",
            )
            history_state = create_initial_state(
                thread_id=self.state.thread_id,
                user_id=self.state.user_id,
                now=time.monotonic(),
            )

        await self._refresh_header_snapshot_async(runtime.capabilities)

        def render_thread() -> None:
            self._render_current_header(runtime.capabilities)
            runtime.renderer.reset_state(history_state)
            runtime.renderer.render_state()

        await runtime.render_from_screen_top(render_thread)
        runtime.invalidate()

    async def _load_current_thread_history_state(self):
        from .history import cli_state_from_history

        if self._client is None:
            return create_initial_state(
                thread_id=self.state.thread_id,
                user_id=self.state.user_id,
                now=time.monotonic(),
            )
        history = await self._client.get_history(
            self.state.thread_id,
            self.state.user_id,
        )
        return cli_state_from_history(
            history,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
        )

    def _submit_repl_message(
        self,
        raw_input: str,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        from .input import parse_composer_submission

        submission = parse_composer_submission(raw_input, cwd=Path.cwd())
        if submission.attachment_errors:
            message = submission.attachment_errors[0]
            if runtime is not None:
                runtime.set_status_notice(message, level="warning")
            elif isinstance(renderer, PlainRenderer):
                sys.stderr.write(f"{message}\n")
                sys.stderr.flush()
            else:
                self.state.console.print(f"[yellow]{message}[/yellow]")
            return
        self._send_message(
            submission.message,
            renderer,
            attachments=submission.attachments,
            runtime=runtime,
        )

    async def _submit_rich_submission_async(
        self,
        submission: Any,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime,
    ) -> None:
        active_task = runtime.current_turn_task
        if runtime.busy or (active_task is not None and not active_task.done()):
            runtime.queue_submission(submission)
            return

        runtime.current_turn_task = asyncio.create_task(
            self._run_rich_submission_chain(
                submission,
                renderer,
                runtime=runtime,
            ),
            name="NymeriaCLIRichSubmission",
        )

    async def _run_rich_submission_chain(
        self,
        submission: Any,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime,
    ) -> bool:
        current = submission
        ran_turn = False
        try:
            while current is not None:
                ran_turn = await self._send_message_async(
                    current.message,
                    renderer,
                    attachments=current.attachments,
                    runtime=runtime,
                )
                current = runtime.next_queued_submission()
        finally:
            runtime.clear_queued_notice_if_idle()
            runtime.current_turn_task = None
        return ran_turn

    def _send_temporary_model_message(
        self,
        message: str,
        model: str,
        renderer: _ReplRenderer,
        *,
        capabilities: TerminalCapabilities,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        previous_fast_active = self._repl_fast_active
        try:
            restore = asyncio.run(self._set_temporary_thread_model(model))
        except Exception as exc:  # noqa: BLE001 - command feedback should be visible.
            self._render_startup_error(
                f"Could not start fast turn: {exc or exc.__class__.__name__}",
                capabilities,
            )
            return

        self._repl_fast_active = True
        self._repl_model_label = model
        _set_renderer_active_model(renderer, model)
        self._mark_header_refresh_pending()
        self._refresh_and_render_pending_header(capabilities)
        try:
            self._send_message(message, renderer, runtime=runtime)
        finally:
            try:
                asyncio.run(self._restore_temporary_thread_model(restore))
            except Exception as exc:  # noqa: BLE001 - restore failures must be surfaced.
                self._render_startup_error(
                    f"Fast turn ended, but model restore failed: "
                    f"{exc or exc.__class__.__name__}",
                    capabilities,
                )
                return
            self._repl_fast_active = previous_fast_active
            self._repl_model_label = restore["effective_model"]
            _set_renderer_active_model(renderer, restore["effective_model"])
            self._mark_header_refresh_pending()
            self._refresh_and_render_pending_header(capabilities)

    async def _send_temporary_model_message_async(
        self,
        message: str,
        model: str,
        renderer: _ReplRenderer,
        *,
        capabilities: TerminalCapabilities,
        runtime: _RichReplRuntime,
    ) -> bool:
        previous_fast_active = self._repl_fast_active
        try:
            restore = await self._set_temporary_thread_model(model)
        except Exception as exc:  # noqa: BLE001 - keep the prompt alive.
            runtime.set_status_notice(
                f"Could not start fast turn: {exc or exc.__class__.__name__}",
                level="error",
            )
            return False

        self._repl_fast_active = True
        self._repl_model_label = model
        _set_renderer_active_model(renderer, model)
        runtime.invalidate()
        try:
            return await self._send_message_async(message, renderer, runtime=runtime)
        finally:
            try:
                await self._restore_temporary_thread_model(restore)
            except Exception as exc:  # noqa: BLE001 - restore failures must be visible.
                runtime.set_status_notice(
                    "Fast turn ended, but model restore failed: "
                    f"{exc or exc.__class__.__name__}",
                    level="error",
                )
                return False
            self._repl_fast_active = previous_fast_active
            self._repl_model_label = restore["effective_model"]
            _set_renderer_active_model(renderer, restore["effective_model"])
            self._mark_header_refresh_pending()
            await self._refresh_and_render_pending_header_async(runtime)

    async def _set_temporary_thread_model(self, model: str) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")
        context = CommandContext(
            client=self._client,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
        )
        return await apply_temporary_model(context, model)

    async def _restore_temporary_thread_model(self, restore: Mapping[str, Any]) -> None:
        if self._client is None:
            return
        context = CommandContext(
            client=self._client,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
        )
        await restore_temporary_model(context, restore)

    def _send_message(
        self,
        message: str,
        renderer: _ReplRenderer,
        *,
        attachments: Sequence[Attachment] | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        """Send a chat message through the selected client and render it."""

        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")

        render_context = runtime.render_lock if runtime is not None else nullcontext()
        if runtime is not None:
            runtime.set_busy(True)
        try:
            with render_context:
                if not isinstance(renderer, PlainRenderer):
                    self.state.console.print()

                renderer.start_turn(
                    message,
                    thread_id=self.state.thread_id,
                    user_id=self.state.user_id,
                    attachments=attachments,
                )
                try:
                    asyncio.run(
                        self._render_message_stream(
                            message,
                            renderer,
                            attachments=attachments,
                        )
                    )
                except KeyboardInterrupt:
                    self._stop_current_turn()
                    self._render_cancelled(renderer)
                    return
        finally:
            if runtime is not None:
                runtime.set_busy(False)

        # Auto-title on first message
        if self._client is self._local_client:
            self._maybe_auto_title(message)

    async def _send_message_async(
        self,
        message: str,
        renderer: _ReplRenderer,
        *,
        attachments: Sequence[Attachment] | None = None,
        runtime: _RichReplRuntime,
    ) -> bool:
        """Send a chat message while the Rich prompt remains active."""

        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")

        runtime.set_busy(True)
        try:
            def start_visible_turn() -> None:
                if not isinstance(renderer, PlainRenderer):
                    self.state.console.print()
                renderer.start_turn(
                    message,
                    thread_id=self.state.thread_id,
                    user_id=self.state.user_id,
                    attachments=attachments,
                )

            await runtime.render_above_prompt(start_visible_turn)
            await self._render_message_stream(
                message,
                renderer,
                attachments=attachments,
                runtime=runtime,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the interactive prompt alive.
            runtime.set_status_notice(
                f"Stream failed: {exc or exc.__class__.__name__}",
                level="error",
            )
            return False
        finally:
            runtime.set_busy(False)

        if self._client is self._local_client:
            self._maybe_auto_title(message)
        return True

    async def _render_message_stream(
        self,
        message: str,
        renderer: _ReplRenderer,
        *,
        attachments: Sequence[Attachment] | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> None:
        assert self._client is not None
        events = self._client.stream_chat(
            message,
            self.state.thread_id,
            self.state.user_id,
            attachments=attachments,
        )
        if runtime is not None:
            async for event in events:
                await runtime.render_event_above_prompt(event)
            return
        await renderer.render_async_events(events)

    def _reset_active_repl_state(self) -> None:
        renderer = self._active_repl_renderer
        if renderer is None or not hasattr(renderer, "reset_state"):
            return
        renderer.reset_state(
            create_initial_state(
                thread_id=self.state.thread_id,
                user_id=self.state.user_id,
                now=time.monotonic(),
            )
        )

    def _undo_last_exchange_from_renderer(self) -> None:
        """Remove the last user+assistant message pair from the renderer state."""
        from .state.model import AssistantMessage, UserMessage

        renderer = self._active_repl_renderer
        if renderer is None or not hasattr(renderer, "state") or not hasattr(renderer, "reset_state"):
            return
        ui_state = renderer.state
        messages = list(ui_state.messages)
        found_assistant = False
        cut_index = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if isinstance(msg, AssistantMessage) and not found_assistant:
                found_assistant = True
                cut_index = i
            elif isinstance(msg, UserMessage) and found_assistant:
                cut_index = i
                break
        if cut_index < len(messages):
            new_state = replace(ui_state, messages=tuple(messages[:cut_index]))
            renderer.reset_state(new_state)


    def _stop_current_turn(self) -> None:
        """Sync wrapper over the async twin (plain REPL path, no running loop)."""
        try:
            asyncio.run(self._stop_current_turn_async())
        except Exception:  # noqa: BLE001 - cancellation feedback is best effort.
            logger.debug("Failed to stop the current turn", exc_info=True)
            return

    async def _stop_current_turn_async(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop(self.state.thread_id, self.state.user_id)
        except Exception:  # noqa: BLE001 - cancellation feedback is best effort.
            logger.debug("Failed to stop the current turn", exc_info=True)
            return

    def _render_cancelled(self, renderer: _ReplRenderer) -> None:
        if isinstance(renderer, PlainRenderer):
            sys.stderr.write("Cancelled.\n")
            sys.stderr.flush()
            return
        self.state.console.print("[dim]Cancelled.[/dim]")

    def _render_startup_error(
        self,
        message: str,
        capabilities: TerminalCapabilities,
    ) -> None:
        if capabilities.renderer == "plain":
            sys.stderr.write(f"Error: {strip_ansi(message)}\n")
            sys.stderr.flush()
            return
        self.state.console.print(f"[red]Error: {message}[/red]")

    def _maybe_auto_title(self, first_message: str) -> None:
        """Auto-generate a thread title if this looks like the first message."""
        store = self.state.thread_metadata_manager.get_store(self.state.user_id)
        meta = store.threads.get(self.state.thread_id)
        if meta and meta.title_source != "default":
            return  # Already titled

        # Use the first message as a rough title
        title = first_message[:50].strip()
        if len(first_message) > 50:
            title += "..."

        self.state.thread_metadata_manager.upsert_thread(
            self.state.user_id,
            self.state.thread_id,
            title=title,
            title_source="auto",
        )

    async def _replace_repl_client(self, action: Any) -> None:
        next_client = action.get("client") if isinstance(action, dict) else None
        if next_client is None:
            return
        old_client = self._client
        self._client = next_client
        self._apply_selected_client_user(next_client, action.get("user_id"))
        if old_client is not None and old_client is not next_client:
            close_attr = getattr(old_client, "close", None)
            if callable(close_attr):
                close: Any = close_attr
                await close()

    async def _apply_reconnected_client(
        self,
        client: AgentClient,
        *,
        runtime: "_RichReplRuntime | None" = None,
    ) -> None:
        """Swap in a live client after a background reconnect, and surface it.

        Reuses the same ``replace_client`` path as /login so the old placeholder
        is closed, the header refreshes, and the autonomous stream restarts; then
        announces the recovered connection in the status bar.
        """

        user_id = str(
            getattr(client, "default_user_id", None) or self.state.user_id or "default"
        )
        api_url = str(getattr(client, "base_url", "") or "")
        await self._dispatch_repl_action(
            {
                "type": "replace_client",
                "client": client,
                "user_id": user_id,
                "api_url": api_url,
            }
        )
        if runtime is not None:
            runtime.set_status_notice(
                f"Reconnected to {api_url or 'backend'} as {user_id}.",
                level="info",
            )
            await self._refresh_and_render_pending_header_async(runtime)

    def _apply_selected_client_user(
        self,
        client: Any,
        user_id: Any | None = None,
    ) -> None:
        selected_user_id = str(
            user_id
            or getattr(client, "default_user_id", None)
            or self.state.user_id
            or "default"
        )
        self.state.user_id = selected_user_id
        if self._local_client is not None:
            self._local_client.default_user_id = selected_user_id
        self.runtime_config = replace(
            self.runtime_config,
            user_id=selected_user_id,
            user_id_explicit=True,
        )

    def _prompt_for_input(
        self,
        prompt: str,
        *,
        secret: bool = False,
        session: Any | None = None,
    ) -> str:
        if session is not None:
            return session.prompt(prompt, is_password=secret)
        if secret:
            return getpass.getpass(prompt)
        return self.state.console.input(prompt)

    async def _prompt_for_input_async(
        self,
        prompt: str,
        *,
        secret: bool = False,
        session: Any | None = None,
        runtime: _RichReplRuntime | None = None,
    ) -> str:
        if session is not None:
            prompt_kwargs = runtime.prompt_kwargs() if runtime is not None else {}
            prompt_message = (
                runtime.prompt_fragments(prompt)
                if runtime is not None
                else prompt
            )
            return await session.prompt_async(
                prompt_message,
                is_password=secret,
                **prompt_kwargs,
            )
        if runtime is not None and runtime.application is not None:
            from prompt_toolkit.application import run_in_terminal

            def read_prompt() -> str:
                if secret:
                    return getpass.getpass(prompt)
                return input(prompt)

            return str(await run_in_terminal(read_prompt, in_executor=True))
        if secret:
            return await asyncio.to_thread(getpass.getpass, prompt)
        return await asyncio.to_thread(self.state.console.input, prompt)

    def _render_disconnected_notice(
        self,
        client: AgentClient | None,
        capabilities: TerminalCapabilities,
    ) -> None:
        if not is_disconnected_client(client):
            return
        # Only the Rich REPL runs the auto-reconnect watcher, so only it should
        # promise automatic recovery; plain/full-screen point at /reconnect.
        message = _disconnected_notice_text(
            client,
            auto_reconnect=capabilities.renderer == "rich",
        )
        if capabilities.renderer == "plain":
            sys.stderr.write(f"{strip_ansi(message)}\n")
            sys.stderr.flush()
            return
        self.state.console.print(f"[yellow]{message}[/yellow]")


def _fast_prompt_from_result(result: Any) -> tuple[str, str] | None:
    from .commands.fast import fast_prompt_payload

    return fast_prompt_payload(result)


def _set_renderer_active_model(renderer: Any, model: str) -> None:
    state = getattr(renderer, "state", None)
    if state is None or not hasattr(state, "active_model"):
        return
    with suppress(Exception):
        renderer.state = replace(state, active_model=str(model or ""))
