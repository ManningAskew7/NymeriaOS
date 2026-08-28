"""The ``/browser`` command family: the human login handoff, user-started.

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

from .command_forms import CommandOutput, command_error, command_info, command_success
from .command_params import BoundArgs


class BrowserCommandsMixin:
    """/browser command bodies mixed into ``_CommandExecutor``.

    The host provides ``user_id`` and ``thread_id``; the annotations let
    the static checker see them on the mixin in isolation.
    """

    user_id: str
    thread_id: str

    async def _cmd_browser(self, bound: BoundArgs) -> str | CommandOutput:
        # Bare family root = overview (structure rule 1): the live handoff.
        from .browser_login_sessions import get_browser_login_registry

        active = get_browser_login_registry().active_for_user(self.user_id)
        if not active:
            return command_info(
                "No login handoff is open. `/browser login <url>` opens a "
                "window in Nymeria Desktop where you sign the agent's browser "
                "into a site by hand; the agent never sees that screen."
            )
        live = active[0].snapshot()
        remaining = int(float(live.get("seconds_remaining") or 0))
        where = str(live.get("url") or "").strip() or "(no url recorded)"
        return command_info(
            f"A login handoff is open: {where} on tab {live.get('tab_id')}, "
            f"{remaining}s left (session {live.get('session_id')}). Drive it "
            "in Nymeria Desktop; finish or cancel there, or ask the agent to "
            "cancel it.",
            data={"session": live},
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
