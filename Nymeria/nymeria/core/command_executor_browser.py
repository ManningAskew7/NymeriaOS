"""The ``/browser`` command family: browser-target routing and the human
login handoff.

House style: backend handler families live in their own domain mixin module
(see ``command_executor_aliases.py``). ``_CommandExecutor`` inherits
:class:`BrowserCommandsMixin`, which owns the family: the overview (is a
login handoff open?) and ``/browser login <url>``, the user-started twin of
the agent's ``chrome_request_login`` tool. Both run the ONE orchestration,
``tools.chrome_browser.start_login_handoff``, so the two entry points
cannot drift: a fresh tab is opened at the URL, a session registers in
``core/browser_login_sessions.py``, the extension starts the screencast,
and ``browser_login_started`` on the autonomous stream opens the viewer in
nymeria-desktop, wherever this command was typed from.

Runtime leaf: imports ``command_forms``/``command_params`` at module scope
only; the heavy tool module is imported function-locally at execution time
(it pulls the chrome dispatch stack, which a command catalog build must
never pay for).
"""

from __future__ import annotations

from typing import Optional

from .command_forms import CommandOutput, command_error, command_info, command_success
from .command_params import BoundArgs


class BrowserCommandsMixin:
    """/browser command bodies mixed into ``_CommandExecutor``.

    The host provides ``user_id`` and ``thread_id``; the annotations let
    the static checker see them on the mixin in isolation.
    """

    user_id: str
    thread_id: str

    def _target_summary(self) -> tuple[str, dict]:
        """One line + data dict describing routing state, shared by the
        family overview and `/browser list`."""
        from .browser_targets import (
            REASON_AMBIGUOUS,
            account_default_target,
            describe_browser,
            resolve_target,
            roster_lines,
            thread_target,
        )

        resolution = resolve_target(self.user_id, self.thread_id or "")
        lines = roster_lines(self.user_id)
        if resolution.client_id:
            head = (
                "Commands drive "
                f"{describe_browser(self.user_id, resolution.client_id)} "
                f"(via {resolution.source})."
            )
        elif resolution.reason == REASON_AMBIGUOUS:
            head = (
                "Several browsers are connected and none is chosen: commands "
                "refuse until one is picked (`/browser switch <which>` for this "
                "thread, `/browser default <which>` account-wide)."
            )
        else:
            head = "No browser is connected. " + self._no_browser_hint()
        body = head + (
            (" Known browsers: " + "; ".join(lines) + ".") if lines else ""
        )
        data = {
            "resolved": resolution.client_id,
            "resolved_via": resolution.source or resolution.reason,
            "thread_target": thread_target(self.thread_id or ""),
            "account_default": account_default_target(self.user_id),
            "browsers": lines,
        }
        return body, data

    def _no_browser_hint(self) -> str:
        """User-voice recovery line for the none-connected overview, by what
        the roster and the install say exists. The server browser is headless,
        so it is never told to click a popup; the user's own Chrome is."""
        from .browser_targets import (
            EXTENSION_RELEASES_URL,
            SERVER_BROWSER_INSTALL_COMMAND,
            SERVER_BROWSER_RESTART_COMMAND,
            SERVER_BROWSER_STATUS_COMMAND,
            has_server_browser,
        )
        from .chrome_subscribers import (
            BROWSER_KIND_DESKTOP,
            BROWSER_KIND_SERVER,
            chrome_browser_roster,
        )

        kinds = {record.kind for record in chrome_browser_roster(self.user_id)}
        parts = []
        if BROWSER_KIND_SERVER in kinds or (not kinds and has_server_browser(self.user_id)):
            parts.append(
                "The server browser has no popup: on the Nymeria host, "
                f"`{SERVER_BROWSER_STATUS_COMMAND}` says whether it is running and "
                f"`{SERVER_BROWSER_RESTART_COMMAND}` brings it back."
            )
        if BROWSER_KIND_DESKTOP in kinds:
            parts.append(
                "For your own Chrome, open the Nymeria Browser extension popup "
                "and click Connect."
            )
        if not parts:
            parts.append(
                f"To get one: `{SERVER_BROWSER_INSTALL_COMMAND}` on the Nymeria "
                "host installs the server browser (a headless Chrome the agent "
                "drives, no screen needed), or install the Nymeria Browser "
                f"extension in your own Chrome ({EXTENSION_RELEASES_URL})."
            )
        return " ".join(parts)

    async def _cmd_browser(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare family root = overview (structure rule 1): routing state plus
        # the live handoff.
        from .browser_login_sessions import get_browser_login_registry

        summary, data = self._target_summary()
        active = get_browser_login_registry().active_for_user(self.user_id)
        if not active:
            return command_info(
                f"{summary} No login handoff is open. `/browser login <url>` "
                "opens a window in Nymeria Desktop where you sign the "
                "agent's browser into a site by hand; the agent never sees "
                "that screen.",
                data=data,
            )
        live = active[0].snapshot()
        remaining = int(float(live.get("seconds_remaining") or 0))
        where = str(live.get("url") or "").strip() or "(no url recorded)"
        return command_info(
            f"{summary} A login handoff is open: {where} on tab "
            f"{live.get('tab_id')}, {remaining}s left (session "
            f"{live.get('session_id')}). Drive it in Nymeria Desktop; finish "
            "or cancel there, or ask the agent to cancel it.",
            data={**data, "session": live},
        )

    async def _cmd_browser_list(self, bound: BoundArgs) -> str | CommandOutput:
        summary, data = self._target_summary()
        return command_info(summary, data=data)

    def _resolve_ref_or_clear(self, raw: str) -> tuple[Optional[str], bool, Optional[str]]:
        """Parse a browser argument: (client_id, is_clear, error)."""
        from .browser_targets import CLEAR_WORDS, resolve_browser_ref

        wanted = (raw or "").strip()
        if wanted.casefold() in CLEAR_WORDS:
            return None, True, None
        client_id, error = resolve_browser_ref(self.user_id, wanted)
        return client_id, False, error

    async def _cmd_browser_switch(self, bound: BoundArgs) -> str | CommandOutput:
        from .browser_targets import describe_browser, set_thread_target

        if not self.thread_id:
            return command_error(
                "This surface has no thread. Use `/browser default <which>` "
                "for the account-wide setting."
            )
        client_id, is_clear, error = self._resolve_ref_or_clear(
            str(bound.get("browser") or "")
        )
        if error:
            return command_error(error)
        set_error = set_thread_target(self.user_id, self.thread_id, client_id)
        if set_error:
            return command_error(set_error)
        if is_clear:
            return command_success(
                "Thread browser override cleared: this thread follows the "
                "account default again."
            )
        assert client_id is not None
        return command_success(
            "This thread's browser commands now drive "
            f"{describe_browser(self.user_id, client_id)}. Tab ids from the "
            "previous browser no longer apply."
        )

    async def _cmd_browser_default(self, bound: BoundArgs) -> str | CommandOutput:
        from .browser_targets import (
            account_default_target,
            describe_browser,
            set_account_default,
        )

        raw = str(bound.get("browser") or "").strip()
        if not raw:
            current = account_default_target(self.user_id)
            if not current:
                return command_info(
                    "No account default browser is set. With one browser "
                    "connected it is used automatically; with several, set "
                    "one with `/browser default <which>`."
                )
            return command_info(
                "The account default browser is "
                f"{describe_browser(self.user_id, current)}.",
                data={"default_target": current},
            )
        client_id, is_clear, error = self._resolve_ref_or_clear(raw)
        if error:
            return command_error(error)
        set_error = set_account_default(self.user_id, client_id)
        if set_error:
            return command_error(set_error)
        if is_clear:
            return command_success(
                "Account default browser cleared: threads without their own "
                "target auto-pick only when exactly one browser is connected."
            )
        assert client_id is not None
        return command_success(
            "Account default browser set to "
            f"{describe_browser(self.user_id, client_id)}. Threads with "
            "their own target keep it."
        )

    async def _cmd_browser_rename(self, bound: BoundArgs) -> str | CommandOutput:
        from .browser_targets import (
            describe_browser,
            resolve_browser_ref,
            set_browser_label,
        )

        ref = str(bound.get("browser") or "")
        client_id, error = resolve_browser_ref(self.user_id, ref)
        if error:
            return command_error(error)
        assert client_id is not None
        label = str(bound.get("label") or "").strip() or None
        set_error = set_browser_label(self.user_id, client_id, label)
        if set_error:
            return command_error(set_error)
        if label is None:
            # Removing a name is a decision, and it sticks: the browser
            # announces a name on every reconnect, so without recording the
            # removal the name would be back within the minute.
            return command_success(
                f"Name removed from {describe_browser(self.user_id, client_id)}. "
                "It will not come back on its own when that browser reconnects."
            )
        return command_success(
            f"Browser {client_id[:24]}... is now named '{label}'."
        )

    async def _cmd_browser_login(self, bound: BoundArgs) -> str | CommandOutput:
        url = str(bound.get("url") or "").strip()
        # Heavy import deferred to execution (see module docstring).
        from ..tools.chrome_browser import start_login_handoff

        config = {
            "configurable": {
                "user_id": self.user_id,
                "thread_id": self.thread_id or "",
            }
        }
        snapshot, error = await start_login_handoff(
            url=url, tab_id=None, config=config, origin="command"
        )
        if error is not None:
            # Already sentinel-free: start_login_handoff returns plain
            # reasons so each layer renders its own failure shape.
            return command_error(error)
        live = snapshot or {}
        remaining = int(float(live.get("seconds_remaining") or 0))
        return command_success(
            f"Login window opened for {url} (up to {remaining // 60} "
            "minutes). Sign in from the viewer in Nymeria Desktop; what you "
            "type there never reaches the agent, and the agent stays out of "
            "that tab until you finish.",
            data={"session": live},
        )


__all__ = ["BrowserCommandsMixin"]
