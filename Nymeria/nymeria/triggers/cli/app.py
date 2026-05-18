"""CLIApp — main REPL loop and orchestrator."""

from __future__ import annotations

import asyncio
import getpass
import math
import signal
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TYPE_CHECKING

from rich.cells import cell_len

from .capabilities import TerminalCapabilities, detect_terminal_capabilities
from .commands import (
    CommandContext,
    ListCommandOutputSink,
    CommandMessage,
    CommandRegistry,
    RichConsoleCommandOutputSink,
)
from .autonomous import AutonomousStreamMonitor
from .header import CLIHeaderSnapshot, build_header_snapshot, concise_connection_label
from .rendering.indicator import FRAME_INTERVAL_SECONDS
from .rendering.slash_panel import (
    slash_panel_fragments,
    slash_panel_height,
    slash_panel_visible,
)
from .rendering.status_bar import StatusBarContext, StatusBarRenderer, StatusNotice
from .rendering.welcome import render_welcome
from .rendering.plain import PlainRenderer, strip_ansi
from .rendering.rich_repl import RichReplRenderer
from .state import CLIState, create_initial_state
from .theme import CLITheme, DEFAULT_CLI_THEME, load_cli_theme, ptk_style
from .transport.base import AgentClient, Attachment
from .transport.disconnected import DISCONNECTED_MESSAGE, is_disconnected_client
from .transport.in_process import InProcessAgentClient

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent
    from .commands import CommandResult


TransportMode = Literal["api", "local", "auto"]
RendererMode = Literal["full", "rich", "plain", "auto"]
ColorMode = Literal["auto", "always", "never"]
_RICH_SCROLL_REGION_MIN_ROWS = 12
_RICH_REPL_COMPOSER_MAX_HEIGHT = 6
_RICH_RESIZE_REDRAW_MIN_INTERVAL_SECONDS = 0.05
_RICH_RESIZE_SETTLE_SECONDS = 0.12


@dataclass(frozen=True, slots=True)
class CLIRuntimeConfig:
    """Launch-time CLI options shared by future transport and renderer tasks."""

    transport: TransportMode = "api"
    renderer: RendererMode = "auto"
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    user_id: str = "default"
    user_id_explicit: bool = False
    alt_screen: bool = True
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
        self.render_lock = threading.RLock()
        self.application: Any | None = None
        self.composer_controller: Any | None = None
        self._footer_height_was_known = False
        self._busy = False
        self._status_notice: StatusNotice | None = None
        self._resize_pending = False
        self._resize_task: asyncio.Task[None] | None = None
        self._terminal_size = self.terminal_size()
        self._resize_requested_at = 0.0
        self._resize_last_redraw_at = 0.0
        self._prev_sigwinch: Any | None = None
        self._follow_footer_transcript_cursor_saved = False
        self._follow_footer_pin_probe_pending = False
        self._pinned_footer_active = False
        self._pinned_footer_height = 0
        self._pinned_scroll_bottom = 0
        self._pinned_terminal_size: tuple[int, int] | None = None
        self._pinned_footer_needs_full_repaint = False
        self._pinned_input_cursor_position: tuple[int, int] | None = None
        self._autonomous_client_id = f"cli-{uuid.uuid4().hex}"
        self._autonomous_task: asyncio.Task[None] | None = None
        self._pending_submissions: deque[Any] = deque()
        self.current_turn_task: asyncio.Task[bool] | None = None
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
        self.application = application
        self.composer_controller = composer_controller
        if self.scroll_region_enabled():
            application._on_resize = self.handle_terminal_resize  # noqa: SLF001
            application._request_absolute_cursor_position = (  # noqa: SLF001
                self._suppress_prompt_toolkit_height_probe
            )

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
            reasoning_label=self.app._repl_reasoning_label,
            fast_mode_active=self.app._repl_fast_active,
            cwd=Path.cwd(),
            queued_count=len(self._pending_submissions),
            notice=self._status_notice,
            busy=self._busy,
            compact_threshold=self.app._compact_threshold(),
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
        if self.app._repl_model_label:
            return self.app._repl_model_label
        with suppress(Exception):
            return self.app.state.get_effective_model()
        return ""

    def terminal_width(self) -> int:
        app = self.application
        if app is not None:
            with suppress(Exception):
                return max(1, int(app.output.get_size().columns))
        return max(1, int(getattr(self.capabilities, "width", 80) or 80))

    def terminal_height(self) -> int:
        app = self.application
        if app is not None:
            with suppress(Exception):
                return max(1, int(app.output.get_size().rows))
        return max(1, int(getattr(self.capabilities, "height", 24) or 24))

    def terminal_size(self) -> tuple[int, int]:
        app = self.application
        if app is not None:
            with suppress(Exception):
                size = app.output.get_size()
                return (
                    max(1, int(size.columns)),
                    max(1, int(size.rows)),
                )
        return (self.terminal_width(), self.terminal_height())

    def scroll_region_enabled(self) -> bool:
        if not bool(getattr(self.app.runtime_config, "rich_scroll_region", False)):
            return False
        if getattr(self.capabilities, "renderer", "") != "rich":
            return False
        if sys.platform == "win32":
            return False
        if not bool(getattr(self.capabilities, "is_interactive", False)):
            return False
        if self.terminal_height() < _RICH_SCROLL_REGION_MIN_ROWS:
            return False
        return True

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
        return self.composer_input_height() + 4 + self.slash_panel_height()

    def _composer_text(self) -> str:
        controller = self.composer_controller
        if controller is None:
            return ""
        buffer = getattr(getattr(controller, "text_area", None), "buffer", None)
        return str(getattr(buffer, "text", "") or "")

    def slash_panel_visible(self) -> bool:
        return slash_panel_visible(self._composer_text())

    def slash_panel_height(self) -> int:
        if not self.slash_panel_visible():
            return 0
        return slash_panel_height(self._composer_text(), self.app.registry)

    def slash_panel_fragments(self):
        return list(
            slash_panel_fragments(
                self._composer_text(),
                self.app.registry,
                width=self.terminal_width(),
            )
        )

    def _reserved_footer_height(self, size: Any) -> int:
        return min(self.footer_height(), max(1, int(size.rows) - 2))

    def footer_is_visible(self) -> bool:
        if self.scroll_region_enabled():
            return True
        return self.footer_height_is_known()

    def pinned_footer_active(self) -> bool:
        return self._pinned_footer_active

    def save_follow_footer_transcript_cursor(self) -> bool:
        """Save the current terminal cursor as the next transcript write point."""

        if not self.scroll_region_enabled():
            return False
        app = self.application
        if app is None:
            return False
        app.output.write_raw("\x1b7")
        app.output.flush()
        self._follow_footer_transcript_cursor_saved = True
        return True

    def prepare_follow_footer_render(self) -> None:
        if not self.scroll_region_enabled():
            return
        if self._pinned_footer_active:
            self._prepare_pinned_footer_render()
            return
        self._maybe_activate_pinned_footer()
        if self._pinned_footer_active:
            self._prepare_pinned_footer_render()
            return
        app = self.application
        renderer = getattr(app, "renderer", None)
        if renderer is not None:
            self._reset_prompt_toolkit_available_height(renderer)

    def _suppress_prompt_toolkit_height_probe(self) -> None:
        """Keep prompt_toolkit from bottom-anchoring the scroll-region footer."""

        app = self.application
        renderer = getattr(app, "renderer", None) if app is not None else None
        if renderer is not None:
            self._reset_prompt_toolkit_available_height(renderer)

    def _reset_prompt_toolkit_available_height(self, renderer: Any) -> None:
        with suppress(Exception):
            renderer._min_available_height = 0  # noqa: SLF001

    def reset_follow_footer(self, *, prepare_shell_cursor: bool = False) -> None:
        self._deactivate_pinned_footer(reset_terminal=True)
        if prepare_shell_cursor:
            self._prepare_shell_cursor_after_footer()
        self._follow_footer_transcript_cursor_saved = False
        self._follow_footer_pin_probe_pending = False

    def _prepare_shell_cursor_after_footer(self) -> None:
        """Move the terminal cursor to a real shell line before shell return."""

        if not self.scroll_region_enabled():
            return
        app = self.application
        output = getattr(app, "output", None)
        if output is None:
            return

        try:
            size = output.get_size()
            output.write_raw("\x1b[r")
            output.write_raw(f"\x1b[{max(1, size.rows)};1H\r\n")
            output.flush()
        except Exception:  # noqa: BLE001 - terminal exit cleanup is best effort.
            return

    def _maybe_activate_pinned_footer(self) -> None:
        if not self._follow_footer_pin_probe_pending:
            return
        rows_below = self._known_rows_below_cursor()
        if rows_below is None:
            return
        self._follow_footer_pin_probe_pending = False
        if rows_below > self.footer_height():
            return
        self._activate_pinned_footer(rows_below=rows_below)

    def _known_rows_below_cursor(self) -> int | None:
        app = self.application
        if app is None:
            return None
        renderer = getattr(app, "renderer", None)
        rows_below = int(getattr(renderer, "_min_available_height", 0) or 0)
        if rows_below > 0:
            return rows_below
        output = app.output
        if not hasattr(output, "get_rows_below_cursor_position"):
            return None
        try:
            return int(output.get_rows_below_cursor_position())
        except (NotImplementedError, OSError, RuntimeError, ValueError):
            return None

    def _request_follow_footer_pin_probe(self) -> None:
        app = self.application
        renderer = getattr(app, "renderer", None)
        if renderer is None:
            return
        with suppress(AttributeError, AssertionError, RuntimeError, OSError, ValueError):
            renderer._min_available_height = 0  # noqa: SLF001
            renderer.request_absolute_cursor_position()

    def _activate_pinned_footer(self, *, rows_below: int | None = None) -> None:
        app = self.application
        if app is None:
            return
        output = app.output
        size = output.get_size()
        size_rows = int(size.rows)
        footer_height = self._reserved_footer_height(size)
        scroll_bottom = size_rows - footer_height
        if scroll_bottom < 1 or not self._follow_footer_transcript_cursor_saved:
            return

        renderer = getattr(app, "renderer", None)
        last_screen = getattr(renderer, "_last_screen", None) if renderer else None
        last_h = int(getattr(last_screen, "height", 0) or 0)
        # rows_below = (T - R_pre) where R_pre is the row that held Rich's
        # last content when \x1b7 was issued. Fall back to footer_height (the
        # boundary value) when caller didn't supply it (e.g. legacy tests).
        rb = int(rows_below) if rows_below is not None else footer_height
        # Total scroll we need (from the \x1b7 moment) to leave the last Rich
        # row at scroll_bottom - 1. Subtract whatever prompt_toolkit's prior
        # follow re-render already scrolled while drawing its layout, so we
        # don't over-scroll and leave slack blank rows in the transcript.
        desired_total_scroll = footer_height - rb + 1
        pt_scroll = max(0, last_h - rb)
        scroll_amount = max(0, desired_total_scroll - pt_scroll)
        output.hide_cursor()
        if renderer is not None:
            with suppress(Exception):
                renderer.erase(leave_alternate_screen=False)
        output.write_raw("\x1b[r")
        # Move to terminal bottom so subsequent `\r\n` always scrolls (the
        # cursor saved by \x1b7 is an absolute row that doesn't track the
        # scrolls prompt_toolkit performed while drawing its layout).
        if scroll_amount > 0:
            output.write_raw(f"\x1b[{size_rows};1H")
            output.write_raw("\r\n" * scroll_amount)
        footer_top = scroll_bottom + 1
        output.write_raw(f"\x1b[{scroll_bottom};1H")
        output.write_raw("\x1b7")
        output.write_raw(f"\x1b[{footer_top};1H\x1b[J")
        output.flush()
        self._follow_footer_transcript_cursor_saved = True
        self._pinned_footer_active = True
        self._pinned_footer_height = footer_height
        self._pinned_scroll_bottom = scroll_bottom
        self._pinned_terminal_size = (size.columns, size.rows)
        self._pinned_footer_needs_full_repaint = True

    def _prepare_pinned_footer_render(self) -> None:
        app = self.application
        if app is None:
            return
        output = app.output
        size = output.get_size()
        if self._pinned_terminal_size != (size.columns, size.rows):
            self._deactivate_pinned_footer(reset_terminal=True)
            return
        footer_height = self._reserved_footer_height(size)
        if self._pinned_footer_height != footer_height:
            if footer_height < self._pinned_footer_height:
                # DECSTBM scroll-down would lose the topmost transcript
                # rows, so replay the transcript onto the larger area.
                # Clear scrollback too — otherwise the previously-visible
                # rows (already pushed up by the scroll region) stay in
                # scrollback and we end up stacking duplicate transcripts.
                self._redraw_follow_footer(rebuild_scrollback=True)
                self._follow_footer_pin_probe_pending = True
                return
            self._resize_pinned_footer(footer_height=footer_height, size=size)
            if not self._pinned_footer_active:
                return

        footer_top = self._pinned_scroll_bottom + 1
        output.hide_cursor()
        output.write_raw("\x1b[r")
        output.write_raw(f"\x1b[{footer_top};1H")
        output.flush()
        renderer = getattr(app, "renderer", None)
        if renderer is not None:
            with suppress(Exception):
                from prompt_toolkit.data_structures import Point

                renderer._cursor_pos = Point(x=0, y=0)  # noqa: SLF001
                renderer._min_available_height = 0  # noqa: SLF001
                if self._pinned_footer_needs_full_repaint:
                    renderer._last_screen = None  # noqa: SLF001
        self._pinned_footer_needs_full_repaint = False

    def _resize_pinned_footer(self, *, footer_height: int, size: Any) -> None:
        app = self.application
        if app is None:
            return
        output = app.output
        old_footer_height = self._pinned_footer_height
        old_scroll_bottom = self._pinned_scroll_bottom
        old_footer_top = old_scroll_bottom + 1
        scroll_bottom = int(size.rows) - footer_height
        if (
            scroll_bottom < 1
            or old_footer_height <= 0
            or old_scroll_bottom <= 0
            or not self._follow_footer_transcript_cursor_saved
        ):
            self._deactivate_pinned_footer(reset_terminal=True)
            return

        output.hide_cursor()
        output.write_raw("\x1b[r")
        if footer_height > old_footer_height:
            output.write_raw(f"\x1b[1;{old_scroll_bottom}r")
            output.write_raw("\x1b8")
            output.write_raw("\r\n" * (footer_height - old_footer_height))
            output.write_raw("\x1b[r")

        footer_top = scroll_bottom + 1
        clear_top = min(old_footer_top, footer_top)
        output.write_raw(f"\x1b[{scroll_bottom};1H")
        output.write_raw("\x1b7")
        output.write_raw(f"\x1b[{clear_top};1H\x1b[J")
        output.flush()
        self._follow_footer_transcript_cursor_saved = True
        self._pinned_footer_height = footer_height
        self._pinned_scroll_bottom = scroll_bottom
        self._pinned_terminal_size = (int(size.columns), int(size.rows))
        self._pinned_footer_needs_full_repaint = True

    def finish_follow_footer_render(self) -> None:
        if not self.scroll_region_enabled() or not self._pinned_footer_active:
            return
        self._remember_pinned_input_cursor_position()
        self._restore_pinned_input_cursor_position()

    def _remember_pinned_input_cursor_position(self) -> None:
        app = self.application
        if app is None or not self._pinned_scroll_bottom:
            self._pinned_input_cursor_position = None
            return
        renderer = getattr(app, "renderer", None)
        point = getattr(renderer, "_cursor_pos", None)
        if point is None:
            return
        output = getattr(app, "output", None)
        if output is None:
            return
        try:
            size = output.get_size()
            row = self._pinned_scroll_bottom + 1 + max(0, int(point.y))
            column = 1 + max(0, int(point.x))
            text_top = self._pinned_scroll_bottom + 4
            text_bottom = text_top + self.composer_input_height() - 1
            if (
                text_top <= row <= min(int(size.rows), text_bottom)
                and 1 <= column <= int(size.columns)
            ):
                self._pinned_input_cursor_position = (row, column)
        except Exception:  # noqa: BLE001 - cursor restore is best effort.
            return

    def _restore_pinned_input_cursor_position(self) -> None:
        app = self.application
        output = getattr(app, "output", None)
        position = self._pinned_input_cursor_position
        if output is None or position is None:
            return
        row, column = position
        with suppress(Exception):
            output.write_raw(f"\x1b[{row};{column}H")
            output.show_cursor()
            output.flush()

    def _deactivate_pinned_footer(self, *, reset_terminal: bool) -> None:
        if reset_terminal:
            app = self.application
            output = getattr(app, "output", None)
            if output is not None:
                with suppress(Exception):
                    output.write_raw("\x1b[r")
                    output.flush()
        self._pinned_footer_active = False
        self._pinned_footer_height = 0
        self._pinned_scroll_bottom = 0
        self._pinned_terminal_size = None
        self._pinned_footer_needs_full_repaint = False
        self._pinned_input_cursor_position = None
        self._follow_footer_pin_probe_pending = False

    def _on_sigwinch(self, signum: int, frame: Any) -> None:
        self.handle_terminal_resize()
        previous = self._prev_sigwinch
        if callable(previous):
            with suppress(Exception):
                previous(signum, frame)

    def install_resize_handler(self) -> None:
        if self.scroll_region_enabled():
            return
        sigwinch = getattr(signal, "SIGWINCH", None)
        if sigwinch is None or self._prev_sigwinch is not None:
            return
        try:
            self._prev_sigwinch = signal.getsignal(sigwinch)
            signal.signal(sigwinch, self._on_sigwinch)
        except (OSError, RuntimeError, ValueError):
            self._prev_sigwinch = None

    def uninstall_resize_handler(self) -> None:
        sigwinch = getattr(signal, "SIGWINCH", None)
        previous = self._prev_sigwinch
        self._prev_sigwinch = None
        if sigwinch is None or previous is None:
            return
        with suppress(OSError, RuntimeError, ValueError):
            signal.signal(sigwinch, previous)

    def handle_terminal_resize(self) -> None:
        """Resize hook for prompt_toolkit in scroll-region mode."""

        self._resize_pending = True
        self._resize_requested_at = time.monotonic()
        self.schedule_resize_redraw()

    def schedule_resize_redraw(self) -> None:
        """Schedule a transcript redraw from prompt_toolkit's render cycle."""

        if not self._resize_pending:
            with suppress(Exception):
                current_size = self.terminal_size()
                renderer_width = int(getattr(self.renderer, "width", current_size[0]))
                if (
                    current_size == self._terminal_size
                    and current_size[0] == renderer_width
                ):
                    return
            self._resize_pending = True
            self._resize_requested_at = time.monotonic()
        task = self._resize_task
        if task is not None and not task.done():
            return
        app = self.application
        if app is not None and getattr(app, "is_running", False):
            redraw = self._maybe_resize_redraw()
            try:
                self._resize_task = app.create_background_task(
                    redraw
                )
                return
            except Exception:
                redraw.close()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._resize_task = loop.create_task(
            self._maybe_resize_redraw(),
            name="NymeriaCLIRichResizeRedraw",
        )

    async def _maybe_resize_redraw(self) -> None:
        while True:
            new_size = self.terminal_size()
            renderer_width = int(getattr(self.renderer, "width", new_size[0]))
            if (
                not self._resize_pending
                and new_size == self._terminal_size
                and new_size[0] == renderer_width
            ):
                return
            delay = (
                _RICH_RESIZE_REDRAW_MIN_INTERVAL_SECONDS
                - (time.monotonic() - self._resize_last_redraw_at)
            )
            if self._resize_last_redraw_at > 0 and delay > 0:
                await asyncio.sleep(delay)
                new_size = self.terminal_size()
            self._resize_pending = False
            old_size = self._terminal_size
            width_changed = self.renderer.update_terminal_width(new_size[0])
            self._terminal_size = new_size
            size_changed = new_size != old_size
            if width_changed or (self.scroll_region_enabled() and size_changed):
                await self.redraw(
                    rebuild_scrollback=self.scroll_region_enabled() and size_changed
                )
                self._resize_last_redraw_at = time.monotonic()
            quiet_delay = (
                self._resize_requested_at
                + _RICH_RESIZE_SETTLE_SECONDS
                - time.monotonic()
            )
            if quiet_delay > 0:
                await asyncio.sleep(quiet_delay)
                if self.terminal_size() != self._terminal_size:
                    self._resize_pending = True
                    self._resize_requested_at = time.monotonic()
            if not self._resize_pending:
                return

    def footer_height_is_known(self) -> bool:
        """Keep the Rich footer visible after prompt_toolkit has placed it once."""

        app = self.application
        if app is not None:
            with suppress(Exception):
                if app.renderer.height_is_known:
                    self._footer_height_was_known = True
        return self._footer_height_was_known

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

    async def render_above_prompt(self, callback: Callable[[], Any]) -> Any:
        """Run terminal output above the live Rich status/composer area."""

        app = self.application
        if app is not None and self.scroll_region_enabled():
            with self.render_lock:
                return self._render_in_follow_footer(callback)

        def run_locked() -> Any:
            with self.render_lock:
                return callback()

        if app is not None and getattr(app, "is_running", False):
            from prompt_toolkit.application import run_in_terminal

            result = run_in_terminal(run_locked, render_cli_done=False)
            return await result
        return run_locked()

    def _render_in_follow_footer(self, callback: Callable[[], Any]) -> Any:
        app = self.application
        if app is None:
            return callback()
        if self._pinned_footer_active:
            return self._render_in_pinned_footer(callback)
        output = app.output
        renderer = getattr(app, "renderer", None)
        output.hide_cursor()
        if renderer is not None:
            with suppress(Exception):
                renderer.erase(leave_alternate_screen=False)
        try:
            return callback()
        finally:
            self._flush_renderer_output()
            output.flush()
            self._request_follow_footer_pin_probe()
            output.write_raw("\x1b7")
            output.flush()
            self._follow_footer_transcript_cursor_saved = True
            self._follow_footer_pin_probe_pending = True

    def _render_in_pinned_footer(self, callback: Callable[[], Any]) -> Any:
        app = self.application
        if app is None:
            return callback()
        output = app.output
        if not self._pinned_scroll_bottom or not self._follow_footer_transcript_cursor_saved:
            self._deactivate_pinned_footer(reset_terminal=True)
            return self._render_in_follow_footer(callback)

        output.hide_cursor()
        output.write_raw(f"\x1b[1;{self._pinned_scroll_bottom}r")
        output.write_raw("\x1b8")
        output.flush()
        try:
            return callback()
        finally:
            self._flush_renderer_output()
            output.write_raw("\x1b7")
            output.write_raw("\x1b[r")
            output.flush()
            self._follow_footer_transcript_cursor_saved = True
            self._restore_pinned_input_cursor_position()

    def _flush_renderer_output(self) -> None:
        for console_name in ("console", "error_console"):
            console = getattr(self.renderer, console_name, None)
            file = getattr(console, "file", None)
            flush = getattr(file, "flush", None)
            if callable(flush):
                with suppress(Exception):
                    flush()

    async def render_event_above_prompt(self, event: Any) -> None:
        await self._maybe_resize_redraw()
        await self.render_above_prompt(
            lambda: self.renderer.render_event(event, now=time.monotonic())
        )
        if self.scroll_region_enabled() and not self._pinned_footer_active:
            self.invalidate()

    async def render_from_screen_top(self, callback: Callable[[], Any]) -> Any:
        if not self.scroll_region_enabled() or self.application is None:
            def clear_and_render() -> Any:
                self.app.state.console.clear()
                return callback()

            return await self.render_above_prompt(clear_and_render)
        with self.render_lock:
            self._clear_follow_footer_screen_for_replay()
            result = callback()
            self.save_follow_footer_transcript_cursor()
        self.invalidate()
        return result

    async def redraw(self, *, rebuild_scrollback: bool = False) -> None:
        """Clear visible terminal cells, replay reducer transcript, and repaint."""

        if self.scroll_region_enabled() and self.application is not None:
            with self.render_lock:
                self._redraw_follow_footer(rebuild_scrollback=rebuild_scrollback)
            self.invalidate()
            return

        def repaint() -> None:
            self._clear_prompt_toolkit_screen()
            self.renderer.reset_state(self.renderer.state)
            self.renderer.render_state()

        await self.render_above_prompt(repaint)
        self.invalidate()

    def _redraw_follow_footer(self, *, rebuild_scrollback: bool = False) -> None:
        app = self.application
        if app is None:
            return
        output = app.output
        self._terminal_size = self.terminal_size()
        self.renderer.update_terminal_width(self._terminal_size[0])
        self._deactivate_pinned_footer(reset_terminal=True)
        output.hide_cursor()
        output.erase_screen()
        if rebuild_scrollback:
            output.write_raw("\x1b[3J")
        output.cursor_goto(0, 0)
        output.flush()
        with suppress(Exception):
            app.renderer.reset(leave_alternate_screen=False)
        self._follow_footer_transcript_cursor_saved = False
        if rebuild_scrollback:
            with suppress(Exception):
                self.app._render_current_header(
                    replace(
                        self.capabilities,
                        width=self._terminal_size[0],
                        height=self._terminal_size[1],
                    )
                )
        self.renderer.reset_state(self.renderer.state)
        self.renderer.render_state()
        self.save_follow_footer_transcript_cursor()

    def _clear_follow_footer_screen_for_replay(self) -> None:
        app = self.application
        if app is None:
            return
        output = app.output
        self._deactivate_pinned_footer(reset_terminal=True)
        output.erase_screen()
        output.cursor_goto(0, 0)
        output.flush()
        with suppress(Exception):
            app.renderer.reset(leave_alternate_screen=False)
        self._follow_footer_transcript_cursor_saved = False

    def _clear_prompt_toolkit_screen(self) -> None:
        app = self.application
        if app is None:
            self.app.state.console.clear()
            return
        try:
            self._deactivate_pinned_footer(reset_terminal=True)
            renderer = app.renderer
            output = renderer.output
            output.reset_attributes()
            output.erase_screen()
            output.cursor_goto(0, 0)
            output.flush()
            renderer.reset(leave_alternate_screen=False)
        except Exception:  # noqa: BLE001 - redraw recovery is best effort.
            self.app.state.console.clear()

    def invalidate(self) -> None:
        app = self.application
        if app is not None:
            with suppress(Exception):
                app.invalidate()

    def exit(self) -> None:
        app = self.application
        if app is not None:
            with suppress(Exception):
                app.exit()

    async def _run_autonomous_listener(self) -> None:
        await self._autonomous_monitor.run_forever()

    async def _consume_autonomous_stream(self) -> None:
        await self._autonomous_monitor.consume_once()

    async def _apply_autonomous_event(self, normalized: Any) -> bool:
        await self.render_event_above_prompt(normalized)
        return True


class _RichReplPromptToolkitShell:
    """Non-full-screen prompt_toolkit shell for Rich REPL status/composer."""

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
                [transcript_gap, status_bar, input_area, slash_panel],
                height=lambda: Dimension.exact(self.runtime.footer_height()),
            )
        else:
            body = HSplit(
                [footer_spacer, transcript_gap, status_bar, input_area, slash_panel]
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
        try:
            pre_run = None
            if self.runtime.scroll_region_enabled():
                def pre_run_follow_footer() -> None:
                    self.runtime.save_follow_footer_transcript_cursor()

                pre_run = pre_run_follow_footer
            await app.run_async(pre_run=pre_run)
        finally:
            await self.runtime.stop_autonomous_listener_async()
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
        if capabilities.renderer == "full":
            self._run_full_screen(capabilities)
            return

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
                events = client.stream_chat(
                    message.strip(),
                    self.state.thread_id,
                    self.state.user_id,
                )
                return await renderer.consume(events)
            finally:
                await self._close_selected_client()

        return asyncio.run(_run())

    def _run_full_screen(self, capabilities: TerminalCapabilities) -> None:
        """Run the retained full-screen TUI shell."""
        from .rendering.full_screen import (
            FullScreenPromptToolkitShell,
            FullScreenShellConfig,
        )
        from .transport.api import APITransportStartupError

        async def launch() -> None:
            try:
                client = await self._select_agent_client()
            except APITransportStartupError as exc:
                self.state.console.print(f"[red]Error: {exc.message}[/red]")
                return
            self._client = client
            if await self._handle_startup_thread_intents_async(capabilities):
                await self._close_selected_client()
                return

            on_turn_complete = (
                self._maybe_auto_title if client is self._local_client else None
            )
            shell = FullScreenPromptToolkitShell(
                client=client,
                capabilities=capabilities,
                config=FullScreenShellConfig(
                    thread_id=self.state.thread_id,
                    user_id=self.state.user_id,
                    model=self.state.get_effective_model(),
                    thread_label=self.state.get_thread_title(),
                ),
                on_turn_complete=on_turn_complete,
                command_registry=self.registry,
                history_path=self.state.settings.data_dir / "cli_history",
                theme=self.theme,
            )
            if is_disconnected_client(client):
                shell.set_status_notice(DISCONNECTED_MESSAGE, level="warning")
            await shell.run_async()

        asyncio.run(launch())

    def _run_legacy_repl(self) -> None:
        """Compatibility wrapper for callers that still expect the REPL method."""

        self._run_repl(detect_terminal_capabilities(self.runtime_config))

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
                            DISCONNECTED_MESSAGE,
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
            return False

        resolved = await self._resolve_startup_thread_ref(ref)
        if resolved is None:
            return True
        self.state.switch_thread(resolved["thread_id"])
        self._repl_thread_label = resolved["title"]
        self._startup_history_thread_id = resolved["thread_id"]
        return False

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

    async def _resolve_startup_thread_ref(self, ref: str) -> dict[str, str] | None:
        from .commands.system import CommandClientMethodUnavailable, call_client_method
        from .commands.threads import (
            _format_thread_resolution_ambiguity,
            resolve_thread_reference,
        )
        from .commands.system import normalize_thread_id, thread_title

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
                f"Cannot open thread '{ref}': missing client method {exc.method_name}.",
                self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
            )
            return None
        except Exception as exc:  # noqa: BLE001 - startup selection should explain and exit.
            self._render_startup_error(
                f"Cannot open thread '{ref}': {exc or exc.__class__.__name__}",
                self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
            )
            return None

        if not isinstance(raw_threads, Sequence) or isinstance(raw_threads, (str, bytes)):
            raw_threads = []
        threads = [thread for thread in raw_threads if isinstance(thread, Mapping)]
        resolution = resolve_thread_reference(threads, ref)
        if resolution.matched and resolution.thread is not None:
            thread_id = normalize_thread_id(resolution.thread)
            return {"thread_id": thread_id, "title": thread_title(resolution.thread)}
        if resolution.status == "ambiguous":
            message = _format_thread_resolution_ambiguity(ref, resolution.matches)
        else:
            message = f"No thread matching '{ref}'."
        self._render_startup_error(
            message,
            self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
        )
        return None

    async def _resolve_most_recent_thread(self) -> dict[str, str] | None:
        """Find the most recently updated thread for ``--continue``."""
        from .commands.system import CommandClientMethodUnavailable, call_client_method
        from .commands.system import normalize_thread_id, thread_title

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
                f"Cannot continue: missing client method {exc.method_name}.",
                self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
            )
            return None
        except Exception as exc:  # noqa: BLE001 - startup selection should explain and exit.
            self._render_startup_error(
                f"Cannot continue: {exc or exc.__class__.__name__}",
                self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
            )
            return None

        if not isinstance(raw_threads, Sequence) or isinstance(raw_threads, (str, bytes)):
            raw_threads = []
        threads = [thread for thread in raw_threads if isinstance(thread, Mapping)]
        if not threads:
            self._render_startup_error(
                "No threads found to continue.",
                self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
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
                self._active_capabilities or detect_terminal_capabilities(self.runtime_config),
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

    def _compact_threshold(self) -> float | None:
        try:
            return float(self.state.settings.compact_threshold)
        except Exception:  # noqa: BLE001
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
        if self._client is None:
            self._header_snapshot = None
            return None
        snapshot = asyncio.run(
            build_header_snapshot(
                self.state,
                self._client,
                runtime_config=self.runtime_config,
            )
        )
        self._header_snapshot = snapshot
        self._header_refresh_pending = False
        runtime = self._active_rich_runtime
        if runtime is not None:
            runtime.invalidate()
        return snapshot

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
        close = getattr(client, "close", None)
        if callable(close):
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
        )

    async def _repl_loop_async(
        self,
        session: Any,
        capabilities: TerminalCapabilities,
        renderer: _ReplRenderer,
        runtime: _RichReplRuntime,
    ) -> None:
        """Async Rich REPL loop that keeps the prompt/status bar active."""

        try:
            while self.state.running:
                try:
                    user_input = await session.prompt_async(
                        runtime.prompt_fragments,
                        **runtime.prompt_kwargs(),
                    )

                    if not user_input.strip():
                        continue

                    stripped = user_input.strip()

                    if stripped.startswith("/"):
                        await self._dispatch_command_async(
                            stripped,
                            capabilities,
                            renderer,
                            session=session,
                            runtime=runtime,
                        )
                        continue

                    lower = stripped.lower()
                    if lower in ("exit", "quit", "q"):
                        self.state.running = False
                        self.state.console.print("[dim]Goodbye![/dim]")
                        continue
                    if lower == "clear":
                        self.state.console.clear()
                        if capabilities.renderer != "plain":
                            await self._refresh_header_snapshot_async(capabilities)
                            self._render_current_header(capabilities)
                        self._reset_active_repl_state()
                        continue
                    if lower == "help":
                        await self._dispatch_command_async(
                            "/help",
                            capabilities,
                            renderer,
                            session=session,
                            runtime=runtime,
                        )
                        continue

                    await self._submit_repl_message_async(
                        stripped,
                        renderer,
                        runtime=runtime,
                    )

                except KeyboardInterrupt:
                    if runtime.busy:
                        await self._stop_current_turn_async()
                        runtime.set_status_notice("Stop requested")
                    else:
                        self.state.console.print()
                    continue
                except EOFError:
                    self.state.running = False
                    self.state.console.print("[dim]Goodbye![/dim]")
        finally:
            task = runtime.current_turn_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

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
                        DISCONNECTED_MESSAGE,
                        level="warning",
                    )
                runtime.restart_autonomous_listener()
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

    async def _submit_repl_message_async(
        self,
        raw_input: str,
        renderer: _ReplRenderer,
        *,
        runtime: _RichReplRuntime,
    ) -> None:
        from .input import parse_composer_submission

        submission = parse_composer_submission(raw_input, cwd=Path.cwd())
        if submission.attachment_errors:
            runtime.set_status_notice(submission.attachment_errors[0], level="warning")
            return

        await self._submit_rich_submission_async(
            submission,
            renderer,
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
        from .commands.system import call_client_method, mapping_get

        if self._client is None:
            raise RuntimeError("CLI agent client has not been selected")
        context = CommandContext(
            client=self._client,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
        )
        config = await call_client_method(
            context,
            "get_thread_config",
            self.state.thread_id,
            user_id=self.state.user_id,
        )
        settings = await call_client_method(
            context,
            "get_settings",
            user_id=self.state.user_id,
        )
        llm_config = mapping_get(config, "llm_config", None)
        llm_config_present = isinstance(llm_config, Mapping)
        model_present = llm_config_present and "model" in llm_config
        previous_model = llm_config.get("model") if model_present else None
        default_model = str(mapping_get(settings, "llm_model", "") or "")
        effective_model = str(previous_model or default_model)

        await call_client_method(
            context,
            "update_thread_config",
            self.state.thread_id,
            user_id=self.state.user_id,
            llm_config={"model": model},
        )
        return {
            "llm_config_present": llm_config_present,
            "model_present": model_present,
            "previous_model": previous_model,
            "effective_model": effective_model,
        }

    async def _restore_temporary_thread_model(self, restore: Mapping[str, Any]) -> None:
        from .commands.system import call_client_method

        if self._client is None:
            return
        context = CommandContext(
            client=self._client,
            thread_id=self.state.thread_id,
            user_id=self.state.user_id,
            registry=self.registry,
        )
        if not bool(restore.get("llm_config_present", False)):
            await call_client_method(
                context,
                "update_thread_config",
                self.state.thread_id,
                user_id=self.state.user_id,
                clear_llm_config=True,
            )
            return

        model_value = restore.get("previous_model") if restore.get("model_present") else None
        await call_client_method(
            context,
            "update_thread_config",
            self.state.thread_id,
            user_id=self.state.user_id,
            llm_config={"model": model_value},
        )

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
        if self._client is None:
            return

        async def stop() -> None:
            assert self._client is not None
            await self._client.stop(self.state.thread_id, self.state.user_id)

        try:
            asyncio.run(stop())
        except Exception:  # noqa: BLE001 - cancellation feedback is best effort.
            return

    async def _stop_current_turn_async(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop(self.state.thread_id, self.state.user_id)
        except Exception:  # noqa: BLE001 - cancellation feedback is best effort.
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
            close = getattr(old_client, "close", None)
            if callable(close):
                await close()

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
        startup_error = str(getattr(client, "startup_error", "") or "")
        message = startup_error or DISCONNECTED_MESSAGE
        if capabilities.renderer == "plain":
            sys.stderr.write(f"{strip_ansi(message)}\n")
            sys.stderr.flush()
            return
        self.state.console.print(f"[yellow]{message}[/yellow]")


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
        "slash-panel": ptk_style(theme, "status_fg"),
        "slash-panel.name": ptk_style(theme, "status_accent", bold=True),
        "slash-panel.desc": ptk_style(theme, "status_fg"),
        "slash-panel.empty": ptk_style(theme, "status_fg"),
        "slash-panel.more": ptk_style(theme, "separator"),
    }


def _queued_notice(count: int) -> str:
    if count == 1:
        return "Queued message (1)"
    return f"Queued messages ({count})"


def _fast_prompt_from_result(result: Any) -> tuple[str, str] | None:
    from .commands.fast import fast_prompt_payload

    return fast_prompt_payload(result)


def _chat_stream_command_from_result(result: Any) -> str:
    payload = getattr(result, "payload", {}) or {}
    command = payload.get("chat_stream_command")
    return str(command or "").strip()


def _is_chat_stream_command(registry: Any, raw_input: str) -> bool:
    try:
        match = registry.resolve(raw_input)
    except Exception:  # noqa: BLE001 - fall back to normal command handling.
        return False
    command = getattr(match, "command", None)
    metadata = getattr(command, "metadata", {}) or {}
    return str(metadata.get("execution_kind") or "") == "chat_stream"


def _set_renderer_active_model(renderer: Any, model: str) -> None:
    state = getattr(renderer, "state", None)
    if state is None or not hasattr(state, "active_model"):
        return
    with suppress(Exception):
        renderer.state = replace(state, active_model=str(model or ""))
