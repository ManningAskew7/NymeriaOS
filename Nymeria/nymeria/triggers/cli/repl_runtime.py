"""Rich REPL runtime and prompt_toolkit shell.

Moved out of ``app.py`` in Phase 2b of
``docs/private/plans/cli-modernization.md``. ``_RichReplRuntime`` holds the
Rich REPL's live state (busy/notice, the submission queue, slash/form panel
state, hook-approval forms, the autonomous and reconnect background tasks)
and delegates all scroll-region/pinned-footer terminal control to the
``FollowFooterEngine`` it owns (``runtime.footer``, see ``follow_footer.py``).
``_RichReplPromptToolkitShell`` hosts the prompt_toolkit ``Application`` that
drives the composer and key bindings.

``app.py`` re-exports the public names so existing imports keep working;
``CLIApp`` remains the orchestrator and the only production constructor of
these classes.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from rich.cells import cell_len

from .autonomous import AutonomousStreamMonitor
from .capabilities import TerminalCapabilities
from .command_routing import (
    is_chat_stream_command as _is_chat_stream_command,
    queued_notice as _queued_notice,
)
from .commands import RichConsoleCommandOutputSink
from .follow_footer import FollowFooterEngine
from .rendering import form_panel
from .rendering.indicator import FRAME_INTERVAL_SECONDS
from .rendering.rich_repl import RichReplRenderer
from .rendering.slash_panel import (
    filter_commands,
    slash_panel_fragments,
    slash_panel_height,
    slash_panel_visible,
)
from .rendering.status_bar import StatusBarContext, StatusBarRenderer, StatusNotice
from .theme import CLITheme, DEFAULT_CLI_THEME, ptk_style
from .transport.disconnected import DISCONNECTED_MESSAGE, is_disconnected_client

if TYPE_CHECKING:
    from .app import CLIApp, _ReplRenderer


_RICH_REPL_COMPOSER_MAX_HEIGHT = 6
# How often the Rich REPL re-probes a saved-but-unreachable backend so it can
# auto-reconnect once the backend comes up (e.g. CLI started before the API).
_RECONNECT_POLL_INTERVAL_SECONDS = 3.0


class _RichReplRuntime:
    """Runtime services that only exist for the scrollback-native Rich REPL."""

    def __init__(
        self,
        *,
        app: "CLIApp",
        renderer: RichReplRenderer,
        capabilities: TerminalCapabilities,
    ) -> None:
        self.app = app
        self.renderer = renderer
        self.capabilities = capabilities
        self.status_bar_renderer = StatusBarRenderer()
        self.footer = FollowFooterEngine(
            renderer_getter=lambda: self.renderer,
            capabilities=capabilities,
            scroll_region_flag=lambda: bool(
                getattr(app.runtime_config, "rich_scroll_region", False)
            ),
            footer_height=self.footer_height,
            composer_input_height=self.composer_input_height,
            rebuild_header=app._render_current_header,
            console_getter=lambda: app.state.console,
        )
        self.composer_controller: Any | None = None
        self._busy = False
        self._status_notice: StatusNotice | None = None
        self._autonomous_client_id = f"cli-{uuid.uuid4().hex}"
        self._autonomous_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._pending_submissions: deque[Any] = deque()
        self.current_turn_task: asyncio.Task[bool] | None = None
        self._slash_panel_filter_text = ""
        self._slash_panel_match_count = 0
        self._slash_panel_selected_index = 0
        self._active_form: form_panel.FormSpec | None = None
        self._form_state: form_panel.FormState | None = None
        # record_id of the require_approval hold whose decision form is open.
        self._pending_hook_approval_record: str | None = None
        self._autonomous_monitor = AutonomousStreamMonitor(
            client_getter=lambda: self.app._client,
            user_id_getter=lambda: self.app.state.user_id,
            thread_id_getter=lambda: self.app.state.thread_id,
            client_id=self._autonomous_client_id,
            apply_event=self._apply_autonomous_event,
            set_notice=lambda message, level, ttl_seconds: self.set_status_notice(
                message,
                level=level,
                ttl_seconds=ttl_seconds,
            ),
        )

    @property
    def busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.invalidate()

    @property
    def queued_count(self) -> int:
        return len(self._pending_submissions)

    def queue_submission(self, submission: Any) -> None:
        self._pending_submissions.append(submission)
        self.set_status_notice(_queued_notice(len(self._pending_submissions)))

    def next_queued_submission(self) -> Any | None:
        if not self._pending_submissions:
            return None
        return self._pending_submissions.popleft()

    def clear_queued_notice_if_idle(self) -> None:
        if (
            self._status_notice is not None
            and self._status_notice.message.startswith("Queued message")
        ):
            self._status_notice = None
            self.invalidate()

    def set_status_notice(
        self,
        message: str,
        *,
        level: str = "info",
        ttl_seconds: float | None = None,
    ) -> None:
        self._status_notice = StatusNotice(
            message=message,
            level=level if level in {"info", "warning", "error"} else "info",
            created_at=time.monotonic(),
            ttl_seconds=ttl_seconds,
        )
        self.invalidate()

    def bind_application(self, application: Any, composer_controller: Any) -> None:
        self.composer_controller = composer_controller
        self.footer.bind_application(application)

    # ----- follow-footer engine delegation (see follow_footer.py) ------- #
    # The engine owns the prompt_toolkit Application handle, the render
    # lock, and all scroll-region/pinned-footer/resize state. These thin
    # delegates keep the runtime as the single facade production callers
    # and the prompt_toolkit shell talk to.

    @property
    def application(self) -> Any | None:
        return self.footer.application

    @application.setter
    def application(self, value: Any | None) -> None:
        self.footer.application = value

    @property
    def render_lock(self) -> threading.RLock:
        return self.footer.render_lock

    def terminal_width(self) -> int:
        return self.footer.terminal_width()

    def terminal_height(self) -> int:
        return self.footer.terminal_height()

    def terminal_size(self) -> tuple[int, int]:
        return self.footer.terminal_size()

    def scroll_region_enabled(self) -> bool:
        return self.footer.scroll_region_enabled()

    def footer_is_visible(self) -> bool:
        return self.footer.footer_is_visible()

    def pinned_footer_active(self) -> bool:
        return self.footer.pinned_footer_active()

    def footer_height_is_known(self) -> bool:
        return self.footer.footer_height_is_known()

    def save_follow_footer_transcript_cursor(self) -> bool:
        return self.footer.save_follow_footer_transcript_cursor()

    def prepare_follow_footer_render(self) -> None:
        self.footer.prepare_follow_footer_render()

    def finish_follow_footer_render(self) -> None:
        self.footer.finish_follow_footer_render()

    def reset_follow_footer(self, *, prepare_shell_cursor: bool = False) -> None:
        self.footer.reset_follow_footer(prepare_shell_cursor=prepare_shell_cursor)

    def install_resize_handler(self) -> None:
        self.footer.install_resize_handler()

    def uninstall_resize_handler(self) -> None:
        self.footer.uninstall_resize_handler()

    def handle_terminal_resize(self) -> None:
        self.footer.handle_terminal_resize()

    def schedule_resize_redraw(self) -> None:
        self.footer.schedule_resize_redraw()

    async def render_above_prompt(self, callback: Callable[[], Any]) -> Any:
        return await self.footer.render_above_prompt(callback)

    async def render_event_above_prompt(self, event: Any) -> None:
        await self.footer.render_event_above_prompt(event)

    async def render_from_screen_top(self, callback: Callable[[], Any]) -> Any:
        return await self.footer.render_from_screen_top(callback)

    async def redraw(self, *, rebuild_scrollback: bool = False) -> None:
        await self.footer.redraw(rebuild_scrollback=rebuild_scrollback)

    def invalidate(self) -> None:
        self.footer.invalidate()

    def exit(self) -> None:
        self.footer.exit()

    def bottom_toolbar(self):
        """Return prompt_toolkit toolbar fragments for the live Rich prompt."""

        return list(self.status_fragments())

    def status_fragments(self):
        return list(
            self.status_bar_renderer.render_fragments(
                self.renderer.state,
                capabilities=self.capabilities,
                context=self._status_context(),
                width=self.terminal_width(),
                now=time.monotonic(),
            )
        )

    def prompt_fragments(self, prompt: str | None = None):
        label = str(prompt or ("Busy: " if self._busy else "You: "))
        prompt_style = "class:prompt.busy" if self._busy else "class:prompt"
        return list(self.status_fragments()) + [("", "\n"), (prompt_style, label)]

    def status_text(self) -> str:
        return self.status_bar_renderer.render_text(
            self.renderer.state,
            capabilities=self.capabilities,
            context=self._status_context(),
            width=self.terminal_width(),
            now=time.monotonic(),
        )

    def _status_context(self) -> StatusBarContext:
        return StatusBarContext(
            connection_label=self.app._status_connection_label(),
            thread_label=self._thread_label(),
            model=self._model_label(),
            disconnected=is_disconnected_client(self.app._client),
            reasoning_label=self.app._repl_reasoning_label,
            fast_mode_active=self.app._repl_fast_active,
            cwd=Path.cwd(),
            queued_count=len(self._pending_submissions),
            notice=self._status_notice,
            busy=self._busy,
            compact_settings=self.app._compact_settings(),
        )

    def prompt_kwargs(self) -> dict[str, Any]:
        return {
            "refresh_interval": FRAME_INTERVAL_SECONDS,
            "style": _repl_prompt_style(self.capabilities, theme=self.app.theme),
        }

    def _thread_label(self) -> str:
        if self.app._repl_thread_label:
            return self.app._repl_thread_label
        with suppress(Exception):
            return self.app.state.get_thread_title()
        return self.app.state.thread_id

    def _model_label(self) -> str:
        # Explicit /fast or /model override wins everywhere.
        if self.app._repl_model_label:
            return self.app._repl_model_label
        # Disconnected: no backend to report a model. Show nothing rather than
        # the client's local Settings default (dev-todo #37).
        if is_disconnected_client(self.app._client):
            return ""
        # Remote (thin client, no in-process agent): the model is backend state.
        # Report the fetched header-snapshot model; never the local default.
        if self.app.state.agent is None:
            snapshot = self.app._header_snapshot
            if snapshot is not None and snapshot.model:
                return snapshot.model
            return ""
        # Local-agent mode (slim): the in-process Settings model is authoritative.
        with suppress(Exception):
            return self.app.state.get_effective_model()
        return ""

    def composer_input_height(self) -> int:
        controller = self.composer_controller
        if controller is None:
            return 1
        buffer = getattr(getattr(controller, "text_area", None), "buffer", None)
        text = str(getattr(buffer, "text", "") or "")
        prompt_width = 0
        with suppress(Exception):
            prompt_width = sum(
                cell_len(fragment)
                for _style, fragment in controller.prompt_fragments()
            )
        content_width = max(1, self.terminal_width() - prompt_width)
        height = 0
        for line in text.split("\n"):
            cells = cell_len(line)
            height += max(1, math.ceil(cells / content_width))
        return max(1, min(_RICH_REPL_COMPOSER_MAX_HEIGHT, height))

    def footer_height(self) -> int:
        return (
            self.composer_input_height()
            + 4
            + self.slash_panel_height()
            + self.form_height()
        )

    def _composer_text(self) -> str:
        controller = self.composer_controller
        if controller is None:
            return ""
        buffer = getattr(getattr(controller, "text_area", None), "buffer", None)
        return str(getattr(buffer, "text", "") or "")

    def slash_panel_visible(self) -> bool:
        if self.form_is_active():
            return False
        return slash_panel_visible(self._composer_text())

    def slash_panel_height(self) -> int:
        if not self.slash_panel_visible():
            return 0
        return slash_panel_height(self._composer_text(), self.app.registry)

    def slash_panel_fragments(self):
        self._sync_slash_panel_selection()
        return list(
            slash_panel_fragments(
                self._composer_text(),
                self.app.registry,
                width=self.terminal_width(),
                selected_index=self._slash_panel_selected_index,
            )
        )

    def slash_panel_selectable(self) -> bool:
        if self.form_is_active():
            return False
        return bool(self._sync_slash_panel_selection())

    def move_slash_panel_selection(self, delta: int) -> bool:
        matches = self._sync_slash_panel_selection()
        if not matches:
            return False
        self._slash_panel_selected_index = (
            self._slash_panel_selected_index + delta
        ) % len(matches)
        self.invalidate()
        return True

    def accept_slash_panel_selection(self, buffer: Any) -> bool:
        matches = self._sync_slash_panel_selection()
        if not matches:
            return False
        selected = matches[self._slash_panel_selected_index].text
        buffer.text = selected
        buffer.cursor_position = len(selected)
        self.invalidate()
        return True

    def _sync_slash_panel_selection(self) -> list[Any]:
        text = self._composer_text()
        matches = filter_commands(text, self.app.registry)
        if text != self._slash_panel_filter_text:
            self._slash_panel_filter_text = text
            self._slash_panel_selected_index = 0
        if len(matches) != self._slash_panel_match_count:
            self._slash_panel_match_count = len(matches)
        if not matches:
            self._slash_panel_selected_index = 0
            return []
        self._slash_panel_selected_index = max(
            0,
            min(self._slash_panel_selected_index, len(matches) - 1),
        )
        return matches

    # ----- Interactive config form (see rendering/form_panel.py) -------- #

    def form_is_active(self) -> bool:
        return self._active_form is not None

    def form_visible(self) -> bool:
        return self._active_form is not None and self._form_state is not None

    def form_tab_enabled(self) -> bool:
        return self._active_form is not None and len(self._active_form.tabs) > 1

    def active_field_is_checkbox(self) -> bool:
        if self._active_form is None or self._form_state is None:
            return False
        return form_panel.active_field_is_checkbox(self._active_form, self._form_state)

    def form_height(self) -> int:
        if self._active_form is None or self._form_state is None:
            return 0
        self._sync_form_filter()
        return form_panel.form_panel_height(self._active_form, self._form_state)

    def form_fragments(self) -> list[Any]:
        if self._active_form is None or self._form_state is None:
            return []
        self._sync_form_filter()
        return list(
            form_panel.form_panel_fragments(
                self._active_form,
                self._form_state,
                width=self.terminal_width(),
            )
        )

    def _sync_form_filter(self) -> None:
        if self._active_form is None or self._form_state is None:
            return
        form_panel.sync_filter(self._active_form, self._form_state, self._composer_text())

    def _reset_composer_buffer(self) -> None:
        controller = self.composer_controller
        buffer = getattr(getattr(controller, "text_area", None), "buffer", None)
        if buffer is not None:
            buffer.text = ""
            buffer.cursor_position = 0

    def open_form(self, spec: form_panel.FormSpec) -> None:
        self._active_form = spec
        self._form_state = form_panel.init_state(spec)
        self._reset_composer_buffer()
        self.invalidate()

    def close_form(self) -> None:
        self._active_form = None
        self._form_state = None

    def move_form_selection(self, delta: int) -> bool:
        if self._active_form is None or self._form_state is None:
            return False
        self._sync_form_filter()
        moved = form_panel.move_selection(self._active_form, self._form_state, delta)
        if moved:
            self._maybe_form_change()
            self.invalidate()
        return moved

    def move_form_tab(self, delta: int) -> bool:
        if self._active_form is None or self._form_state is None:
            return False
        moved = form_panel.move_tab(self._active_form, self._form_state, delta)
        if moved:
            self._reset_composer_buffer()
            self.invalidate()
        return moved

    def toggle_form_option(self) -> bool:
        if self._active_form is None or self._form_state is None:
            return False
        self._sync_form_filter()
        toggled = form_panel.toggle_current(self._active_form, self._form_state)
        if toggled:
            self.invalidate()
        return toggled

    def schedule_form_submit(self) -> bool:
        if self._active_form is None or self._form_state is None:
            return False
        app = self.application
        if app is None:
            return False
        app.create_background_task(self._submit_form_async())
        return True

    def request_form_cancel(self) -> bool:
        if self._active_form is None:
            return False
        title = self._active_form.title
        self.close_form()
        self._reset_composer_buffer()
        self.invalidate()
        app = self.application
        if app is not None:
            app.create_background_task(self._render_form_note(f"{title} — dismissed"))
        return True

    def _maybe_form_change(self) -> None:
        spec = self._active_form
        state = self._form_state
        if spec is None or state is None or spec.on_change is None:
            return
        try:
            outcome = spec.on_change(form_panel.build_result(spec, state))
        except Exception:  # noqa: BLE001 - a form callback must not kill the REPL.
            return
        if inspect.isawaitable(outcome):
            app = self.application
            if app is not None:
                app.create_background_task(outcome)

    async def _submit_form_async(self) -> None:
        spec = self._active_form
        state = self._form_state
        if spec is None or state is None:
            return
        self._sync_form_filter()
        result = form_panel.build_result(spec, state)
        self.close_form()
        self._reset_composer_buffer()
        self.invalidate()
        try:
            command_result = await spec.on_confirm(result)
        except Exception as exc:  # noqa: BLE001 - a form callback must not kill the REPL.
            await self._render_form_note(f"Form error: {exc.__class__.__name__}: {exc}")
            return
        messages = list(getattr(command_result, "messages", ()) or ())
        if messages:
            await self._render_form_messages(messages)

    async def _render_form_messages(self, messages: Sequence[Any]) -> None:
        def render() -> None:
            sink = RichConsoleCommandOutputSink(self.app.state.console)
            for message in messages:
                sink.emit(message)

        await self.render_above_prompt(render)

    async def _render_form_note(self, text: str) -> None:
        def render() -> None:
            self.app.state.console.print(f"[dim]{text}[/dim]")

        await self.render_above_prompt(render)

    def start_autonomous_listener(self) -> None:
        if self._autonomous_task is not None and not self._autonomous_task.done():
            return
        if not self._autonomous_monitor.can_start():
            return
        with suppress(RuntimeError):
            self._autonomous_task = asyncio.create_task(
                self._run_autonomous_listener(),
                name="NymeriaCLIRichAutonomousStream",
            )

    async def stop_autonomous_listener_async(self) -> None:
        task = self._autonomous_task
        self._autonomous_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def stop_autonomous_listener(self) -> None:
        task = self._autonomous_task
        self._autonomous_task = None
        if task is not None and not task.done():
            task.cancel()

    def restart_autonomous_listener(self) -> None:
        self.stop_autonomous_listener()
        self.start_autonomous_listener()

    def start_reconnect_watcher(self) -> None:
        """Poll a saved-but-unreachable backend so the CLI auto-reconnects.

        Only runs while the active client is a placeholder that retained a saved
        profile (CLI started before the backend). It self-exits the moment a
        live client is in place, so it is a no-op once connected.
        """

        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        client = self.app._client
        if not getattr(client, "can_reconnect", False):
            return
        try:
            self._reconnect_task = asyncio.create_task(
                self._run_reconnect_watcher(),
                name="NymeriaCLIRichReconnectWatcher",
            )
        except RuntimeError:
            return
        # Only the Rich REPL reaches here, so the "automatically" promise is made
        # exactly where the watcher backs it up (the shared startup_error stays
        # neutral for the plain/full-screen paths, which rely on /reconnect).
        self.set_status_notice(
            _disconnected_notice_text(client, auto_reconnect=True),
            level="warning",
        )

    async def stop_reconnect_watcher_async(self) -> None:
        task = self._reconnect_task
        self._reconnect_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def stop_reconnect_watcher(self) -> None:
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _run_reconnect_watcher(self) -> None:
        from .transport.api import APITransportStartupError, attempt_saved_reconnect

        while True:
            await asyncio.sleep(_RECONNECT_POLL_INTERVAL_SECONDS)
            client = self.app._client
            if not getattr(client, "can_reconnect", False):
                return
            try:
                reconnected = await attempt_saved_reconnect(client)
            except asyncio.CancelledError:
                raise
            except APITransportStartupError as exc:
                # Saved token is no longer valid (e.g. revoked while we waited):
                # stop polling and tell the user to re-authenticate.
                self.set_status_notice(
                    f"{exc.message} Run /login to reconnect.",
                    level="error",
                )
                return
            except Exception:  # noqa: BLE001 - a background retry must never crash the REPL.
                continue
            if reconnected is not None:
                # The probe above awaited network I/O, during which the user may
                # have run /login, /logout, or switched connection. If the active
                # client is no longer the placeholder we set out to replace, drop
                # this result rather than clobbering their action.
                if self.app._client is not client:
                    with suppress(Exception):
                        await reconnected.close()
                    return
                await self.app._apply_reconnected_client(reconnected, runtime=self)
                return

    async def _run_autonomous_listener(self) -> None:
        await self._autonomous_monitor.run_forever()

    async def _consume_autonomous_stream(self) -> None:
        await self._autonomous_monitor.consume_once()

    async def _apply_autonomous_event(self, normalized: Any) -> bool:
        event_type = getattr(normalized, "type", "")
        if event_type == "hook_approval":
            await self._on_hook_approval_event(normalized)
            return True
        if event_type == "hook_approval_resolved":
            await self._on_hook_approval_resolved_event(normalized)
            return True
        await self.render_event_above_prompt(normalized)
        return True

    # -- require_approval holds (backlog #77) --------------------------------
    #
    # These two events never reach the transcript reducer: the hold is a live
    # decision, not turn output. The prompt renders as a dim note above the
    # prompt, the decision surface is the form panel (Approve/Deny radio,
    # Enter confirms, Esc leaves it for `/hook approvals`), and the resolved
    # event closes a stale form + notes the outcome from any surface.

    async def _on_hook_approval_event(self, event: Any) -> None:
        record_id = str(getattr(event, "record_id", "") or "")
        if not record_id:
            return
        tool_name = str(getattr(event, "tool_name", "") or "a tool")
        prompt = str(getattr(event, "prompt", "") or "").strip()
        preview = str(getattr(event, "tool_args_preview", "") or "").strip()
        note = f"Approval needed: {tool_name}"
        if prompt:
            note += f" ({prompt})"
        await self._render_form_note(note)
        self._pending_hook_approval_record = record_id
        self.open_form(self._hook_approval_form_spec(
            record_id=record_id,
            tool_name=tool_name,
            prompt=prompt,
            preview=preview,
        ))

    def _hook_approval_form_spec(
        self,
        *,
        record_id: str,
        tool_name: str,
        prompt: str,
        preview: str,
    ) -> form_panel.FormSpec:
        from .rendering.markdown import truncate_cell_width

        title = prompt or f"Approve tool call {tool_name}?"
        meta = truncate_cell_width(preview, 60) if preview else ""

        async def _confirm(result: form_panel.FormResult) -> Any:
            from .commands import CommandResult

            verdict = (result.radio_value or "").strip()
            if verdict not in ("approve", "deny"):
                return CommandResult.completed()
            self._pending_hook_approval_record = None
            await self.app._dispatch_command_async(  # noqa: SLF001 - runtime helper
                f"/hook {verdict} {record_id}",
                self.capabilities,
                self.renderer,
                runtime=self,
            )
            return CommandResult.completed()

        return form_panel.FormSpec(
            title=title,
            tabs=(
                form_panel.FormTab(
                    label="Approval",
                    fields=(
                        form_panel.FormField(
                            kind="radio",
                            key="decision",
                            options=(
                                form_panel.FormOption(
                                    id="approve",
                                    label=f"Approve {tool_name}",
                                    meta=meta,
                                ),
                                form_panel.FormOption(
                                    id="deny",
                                    label="Deny",
                                    description="The agent is told not to retry.",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            on_confirm=_confirm,
            footer_hint=(
                "Enter decide · Esc later (/hook approvals) · no answer = deny"
            ),
        )

    async def _on_hook_approval_resolved_event(self, event: Any) -> None:
        record_id = str(getattr(event, "record_id", "") or "")
        if record_id and self._pending_hook_approval_record == record_id:
            self._pending_hook_approval_record = None
            if self._active_form is not None:
                self.close_form()
                self._reset_composer_buffer()
                self.invalidate()
        outcome = str(getattr(event, "outcome", "") or "")
        tool_name = str(getattr(event, "tool_name", "") or "tool call")
        resolved_by = str(getattr(event, "resolved_by", "") or "").strip()
        if outcome == "approved":
            line = f"Approved {tool_name}" + (f" ({resolved_by})" if resolved_by else "")
        elif outcome == "denied":
            line = f"Denied {tool_name}" + (f" ({resolved_by})" if resolved_by else "")
        elif outcome == "timeout":
            line = f"Approval timed out; {tool_name} was denied."
        elif outcome == "aborted":
            line = f"Turn cancelled; {tool_name} was denied."
        else:
            line = f"Approval for {tool_name} is no longer pending."
        await self._render_form_note(line)


class _RichReplPromptToolkitShell:
    """Prompt_toolkit shell for the Rich REPL status bar and composer."""

    def __init__(
        self,
        *,
        cli_app: "CLIApp",
        runtime: _RichReplRuntime,
        renderer: _ReplRenderer,
        capabilities: TerminalCapabilities,
        history_path: Path,
    ) -> None:
        self.cli_app = cli_app
        self.runtime = runtime
        self.renderer = renderer
        self.capabilities = capabilities
        self.history_path = history_path
        self.composer_controller: Any | None = None
        self.application: Any | None = None

    def build_application(self) -> Any:
        from prompt_toolkit.application import Application
        from prompt_toolkit.filters import Condition, is_done
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        from prompt_toolkit.layout import ConditionalContainer, Dimension, HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        from .input import create_rich_repl_composer

        controller = create_rich_repl_composer(
            command_registry=self.cli_app.registry,
            history_path=self.history_path,
            cwd=Path.cwd(),
            on_submit=self._handle_submission,
            on_error=self._handle_composer_error,
            on_stop=self._request_stop,
            is_busy=lambda: self.runtime.busy,
            queued_count=lambda: self.runtime.queued_count,
            slash_panel_is_active=self.runtime.slash_panel_selectable,
            on_slash_panel_move=self.runtime.move_slash_panel_selection,
            on_slash_panel_accept=self.runtime.accept_slash_panel_selection,
            form_is_active=self.runtime.form_is_active,
            form_tab_enabled=self.runtime.form_tab_enabled,
            active_field_is_checkbox=self.runtime.active_field_is_checkbox,
            on_form_move=self.runtime.move_form_selection,
            on_form_tab=self.runtime.move_form_tab,
            on_form_toggle=self.runtime.toggle_form_option,
            on_form_submit=self.runtime.schedule_form_submit,
            on_form_cancel=self.runtime.request_form_cancel,
        )
        if self.runtime.scroll_region_enabled():
            controller.text_area.window.height = lambda: Dimension.exact(
                self.runtime.composer_input_height()
            )
        footer_visible = Condition(self.runtime.footer_is_visible) & ~is_done
        transcript_gap = ConditionalContainer(
            Window(
                height=Dimension.exact(1),
                dont_extend_height=True,
                char=" ",
            ),
            filter=footer_visible,
        )
        status_bar = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: self.runtime.status_fragments()),
                height=Dimension.exact(1),
                dont_extend_height=True,
                style="class:status",
                wrap_lines=False,
                char=" ",
            ),
            filter=footer_visible,
        )
        slash_panel_filter = (
            Condition(self.runtime.slash_panel_visible) & footer_visible
        )
        slash_panel = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: self.runtime.slash_panel_fragments()),
                height=lambda: Dimension.exact(max(1, self.runtime.slash_panel_height())),
                dont_extend_height=True,
                style="class:slash-panel",
                wrap_lines=False,
                char=" ",
            ),
            filter=slash_panel_filter,
        )
        form_panel_filter = Condition(self.runtime.form_visible) & footer_visible
        form_panel_container = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: self.runtime.form_fragments()),
                height=lambda: Dimension.exact(max(1, self.runtime.form_height())),
                dont_extend_height=True,
                style="class:form-panel",
                wrap_lines=False,
                char=" ",
            ),
            filter=form_panel_filter,
        )
        top_border = ConditionalContainer(
            Window(
                height=Dimension.exact(1),
                dont_extend_height=True,
                char="─",
                style="class:input-border",
            ),
            filter=footer_visible,
        )
        bottom_border = ConditionalContainer(
            Window(
                height=Dimension.exact(1),
                dont_extend_height=True,
                char="─",
                style="class:input-border",
            ),
            filter=footer_visible,
        )
        input_area = HSplit(
            [top_border, controller.text_area, bottom_border],
            style="class:input-area",
        )
        footer_spacer = Window(height=Dimension(weight=1), char=" ")
        if self.runtime.scroll_region_enabled():
            body = HSplit(
                [transcript_gap, status_bar, input_area, slash_panel, form_panel_container],
                height=lambda: Dimension.exact(self.runtime.footer_height()),
            )
        else:
            body = HSplit(
                [
                    footer_spacer,
                    transcript_gap,
                    status_bar,
                    input_area,
                    slash_panel,
                    form_panel_container,
                ]
            )
        bindings = KeyBindings()

        @bindings.add("c-d")
        def _exit(event: Any) -> None:
            self.cli_app.state.running = False
            event.app.exit()

        @bindings.add("c-l")
        def _redraw(event: Any) -> None:
            event.app.create_background_task(self.runtime.redraw())

        def _before_render(_app: Any) -> None:
            self.runtime.prepare_follow_footer_render()
            self.runtime.schedule_resize_redraw()

        def _after_render(_app: Any) -> None:
            self.runtime.finish_follow_footer_render()

        app = Application(
            layout=Layout(body, focused_element=controller.text_area),
            key_bindings=merge_key_bindings([bindings, controller.key_bindings]),
            full_screen=False,
            mouse_support=False,
            refresh_interval=FRAME_INTERVAL_SECONDS,
            style=_repl_prompt_style(self.capabilities, theme=self.cli_app.theme),
            before_render=_before_render,
            after_render=_after_render,
        )
        self.composer_controller = controller
        self.application = app
        self.runtime.bind_application(app, controller)
        return app

    async def run_async(self) -> None:
        app = self.application or self.build_application()
        self.runtime.start_autonomous_listener()
        self.runtime.start_reconnect_watcher()
        try:
            pre_run = None
            if self.runtime.scroll_region_enabled():
                def pre_run_follow_footer() -> None:
                    self.runtime.save_follow_footer_transcript_cursor()

                pre_run = pre_run_follow_footer
            await app.run_async(pre_run=pre_run)
        finally:
            await self.runtime.stop_autonomous_listener_async()
            await self.runtime.stop_reconnect_watcher_async()
            task = self.runtime.current_turn_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self.runtime.reset_follow_footer(prepare_shell_cursor=True)
            self._write_resume_hint_after_exit()

    def _resume_hint_text(self) -> str | None:
        thread_id = str(getattr(self.cli_app.state, "thread_id", "") or "").strip()
        if not thread_id:
            return None
        return f"Use nymeria cli --resume {thread_id} to return to this thread."

    def _write_resume_hint_after_exit(self) -> None:
        hint = self._resume_hint_text()
        if not hint:
            return

        output = getattr(self.runtime.application, "output", None)
        if output is not None:
            try:
                output.write_raw(f"{hint}\r\n")
                output.flush()
                return
            except Exception:  # noqa: BLE001 - terminal shutdown hint is best effort.
                pass

        sys.stdout.write(f"{hint}\n")
        sys.stdout.flush()

    def _handle_submission(self, submission: Any) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(
            self._handle_submission_async(submission),
            name="NymeriaCLIRichComposerSubmission",
        )
        return True

    async def _handle_submission_async(self, submission: Any) -> None:
        message = str(getattr(submission, "message", "") or "").strip()
        if not message:
            return
        if message.startswith("/"):
            if _is_chat_stream_command(self.cli_app.registry, message):
                attachments = tuple(getattr(submission, "attachments", ()) or ())
                await self.cli_app._send_message_async(
                    message,
                    self.renderer,
                    attachments=attachments,
                    runtime=self.runtime,
                )
                return
            await self.cli_app._dispatch_command_async(
                message,
                self.capabilities,
                self.renderer,
                runtime=self.runtime,
            )
            return

        lower = message.lower()
        if lower in {"exit", "quit", "q"}:
            self.cli_app.state.running = False
            await self.runtime.render_above_prompt(
                lambda: self.cli_app.state.console.print("[dim]Goodbye![/dim]")
            )
            self.runtime.exit()
            return
        if lower == "clear":
            self.cli_app._reset_active_repl_state()
            await self.cli_app._refresh_header_snapshot_async(self.capabilities)
            if self.capabilities.renderer != "plain":
                await self.runtime.render_from_screen_top(
                    lambda: self.cli_app._render_current_header(self.capabilities)
                )
            else:
                await self.runtime.render_above_prompt(self.cli_app.state.console.clear)
            return
        if lower == "help":
            await self.cli_app._dispatch_command_async(
                "/help",
                self.capabilities,
                self.renderer,
                runtime=self.runtime,
            )
            return

        await self.cli_app._submit_rich_submission_async(
            submission,
            self.renderer,
            runtime=self.runtime,
        )

    def _handle_composer_error(self, message: str) -> None:
        self.runtime.set_status_notice(message, level="warning")

    def _request_stop(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(
            self._stop_current_turn(),
            name="NymeriaCLIRichStopTurn",
        )
        return True

    async def _stop_current_turn(self) -> None:
        await self.cli_app._stop_current_turn_async()
        self.runtime.set_status_notice("Stop requested")


def _repl_prompt_style(
    capabilities: TerminalCapabilities,
    *,
    theme: CLITheme | None = None,
):
    from prompt_toolkit.styles import Style

    selected_theme = theme or DEFAULT_CLI_THEME
    style_keys = _repl_prompt_style_dict(selected_theme)
    if not bool(getattr(capabilities, "color_enabled", False)):
        return Style.from_dict({key: "" for key in style_keys})
    return Style.from_dict(style_keys)


def _repl_prompt_style_dict(theme: CLITheme) -> dict[str, str]:
    return {
        "status": ptk_style(theme, "status_fg"),
        "status.separator": ptk_style(theme, "separator"),
        "status.accent": ptk_style(theme, "status_accent"),
        "status.spinner": ptk_style(theme, "spinner"),
        "status.notice.warning": ptk_style(theme, "prompt_busy"),
        "status.notice.error": ptk_style(theme, "error"),
        "prompt": ptk_style(theme, "prompt", bold=True),
        "prompt.busy": ptk_style(theme, "prompt_busy", bold=True),
        "prompt.error": ptk_style(theme, "prompt_error", bold=True),
        "composer": ptk_style(theme, "prompt", bold=True),
        "composer.busy": ptk_style(theme, "prompt_busy", bold=True),
        "composer.error": ptk_style(theme, "prompt_error", bold=True),
        "composer.queued": ptk_style(theme, "prompt_busy", bold=True),
        "input-border": ptk_style(theme, "input_border"),
        "input-area": "",
        "text-area": "",
        "text-area.prompt": "",
        "slash-hint": ptk_style(theme, "separator"),
        "slash-panel": ptk_style(theme, "status_fg"),
        "slash-panel.name": ptk_style(theme, "status_accent", bold=True),
        "slash-panel.desc": ptk_style(theme, "status_fg"),
        "slash-panel.selected": ptk_style(theme, "status_fg", bg_slot="input_border"),
        "slash-panel.selected.name": ptk_style(
            theme,
            "status_accent",
            bg_slot="input_border",
            bold=True,
        ),
        "slash-panel.selected.desc": ptk_style(
            theme,
            "status_fg",
            bg_slot="input_border",
        ),
        "slash-panel.empty": ptk_style(theme, "status_fg"),
        "slash-panel.more": ptk_style(theme, "separator"),
        "form-panel": ptk_style(theme, "status_fg"),
        "form-panel.title": ptk_style(theme, "status_accent", bold=True),
        "form-panel.tab": ptk_style(theme, "status_fg"),
        "form-panel.tab.active": ptk_style(
            theme,
            "status_accent",
            bg_slot="input_border",
            bold=True,
        ),
        "form-panel.search": ptk_style(theme, "status_fg"),
        "form-panel.placeholder": ptk_style(theme, "separator"),
        "form-panel.footer": ptk_style(theme, "separator"),
        "form-panel.more": ptk_style(theme, "separator"),
    }


def _disconnected_notice_text(client: Any, *, auto_reconnect: bool = False) -> str:
    """Notice text for a disconnected client.

    Precedence: a detected alternate backend (``suggested_url``) wins everywhere,
    since "your backend moved to <url>" is the most actionable thing to say. Then,
    when ``auto_reconnect`` is set (the Rich REPL, where the reconnect watcher
    runs) and the client retained a saved profile, promise the automatic retry so
    the user knows they can just wait. Otherwise fall back to the client's own
    ``startup_error`` (which, for the plain/full-screen paths, points at
    ``/reconnect``), or the generic disconnect prompt.
    """

    suggested = str(getattr(client, "suggested_url", "") or "")
    if suggested:
        failed = str(getattr(client, "reconnect_api_url", "") or "")
        where = f" (your saved connection points at {failed})" if failed else ""
        return (
            f"A Nymeria backend is running at {suggested}{where}. "
            f"Run /login {suggested} to switch to it."
        )
    if auto_reconnect and getattr(client, "can_reconnect", False):
        url = str(getattr(client, "reconnect_api_url", "") or "")
        return (
            f"Backend at {url} is not reachable yet; reconnecting automatically "
            "when it comes up (or run /reconnect)."
        )
    return str(getattr(client, "startup_error", "") or "") or DISCONNECTED_MESSAGE
