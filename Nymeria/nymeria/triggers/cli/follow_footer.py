"""Follow-footer/scroll-region terminal engine for the Rich REPL.

Extracted from ``_RichReplRuntime`` (Phase 2a of
``docs/private/plans/cli-modernization.md``). This module owns every piece of
raw terminal control behind the Rich REPL's pinned footer: DEC scroll margins,
cursor save/restore bookkeeping, prompt_toolkit height-probe suppression,
sigwinch/resize debouncing, and the render-above-prompt orchestration that
every transcript write funnels through.

The engine is deliberately renderer-facing and app-agnostic: everything it
needs from the wider CLI (the footer height, the composer height, the header
rebuild used by scrollback replay, the fallback console) is injected as a
callback, and the prompt_toolkit ``Application`` handle is bound late via
``bind_application``. ``_RichReplRuntime`` exposes the engine as
``runtime.footer`` and keeps thin delegates for the public surface so
production call sites are unchanged.

All prompt_toolkit private-attribute pokes (the ``# noqa: SLF001`` sites) are
confined to this module by design.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities import TerminalCapabilities

_RICH_SCROLL_REGION_MIN_ROWS = 12
_RICH_RESIZE_REDRAW_MIN_INTERVAL_SECONDS = 0.05
_RICH_RESIZE_SETTLE_SECONDS = 0.12


class FollowFooterEngine:
    """Pinned-footer/scroll-region terminal engine behind the Rich REPL."""

    def __init__(
        self,
        *,
        renderer_getter: Callable[[], Any],
        capabilities: "TerminalCapabilities",
        scroll_region_flag: Callable[[], bool],
        footer_height: Callable[[], int],
        composer_input_height: Callable[[], int],
        rebuild_header: Callable[["TerminalCapabilities"], None],
        console_getter: Callable[[], Any],
    ) -> None:
        self._renderer_getter = renderer_getter
        self.capabilities = capabilities
        self._scroll_region_flag = scroll_region_flag
        self._footer_height = footer_height
        self._composer_input_height = composer_input_height
        self._rebuild_header = rebuild_header
        self._console = console_getter
        self.application: Any | None = None
        self.render_lock = threading.RLock()
        self._footer_height_was_known = False
        self._resize_pending = False
        self._resize_task: asyncio.Task[None] | None = None
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
        self._terminal_size = self.terminal_size()

    @property
    def renderer(self) -> Any:
        """Resolve the Rich renderer at call time (tests swap it live)."""

        return self._renderer_getter()

    # ----- application binding and geometry ----------------------------- #

    def bind_application(self, application: Any) -> None:
        self.application = application
        if self.scroll_region_enabled():
            application._on_resize = self.handle_terminal_resize  # noqa: SLF001
            application._request_absolute_cursor_position = (  # noqa: SLF001
                self._suppress_prompt_toolkit_height_probe
            )

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
        if not self._scroll_region_flag():
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

    def _reserved_footer_height(self, size: Any) -> int:
        return min(self._footer_height(), max(1, int(size.rows) - 2))

    def footer_is_visible(self) -> bool:
        if self.scroll_region_enabled():
            return True
        return self.footer_height_is_known()

    def pinned_footer_active(self) -> bool:
        return self._pinned_footer_active

    def footer_height_is_known(self) -> bool:
        """Keep the Rich footer visible after prompt_toolkit has placed it once."""

        app = self.application
        if app is not None:
            with suppress(Exception):
                if app.renderer.height_is_known:
                    self._footer_height_was_known = True
        return self._footer_height_was_known

    # ----- follow-footer lifecycle --------------------------------------- #

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
        if rows_below > self._footer_height():
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
            text_bottom = text_top + self._composer_input_height() - 1
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

    # ----- resize handling ------------------------------------------------ #

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

    # ----- render orchestration ------------------------------------------ #

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
                self._console().clear()
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
                self._rebuild_header(
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
            self._console().clear()
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
            self._console().clear()

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
