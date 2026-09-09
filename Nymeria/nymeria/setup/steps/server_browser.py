"""The server-browser step: install a headless Chrome the agent can drive.

One yes/no screen after the skill kits. "Install" (the default, and what a
quick run applies without showing the screen) has finalize download Chrome
for Testing plus the pinned Nymeria extension release, connect it to the local
API as the installing user, and run it as a background service
(`nymeria/server_browser.py`, `finalize._maybe_install_server_browser`). The
browser-control kit then works on the first request instead of refusing with
"no extension connected". Skipping leaves the user to connect their own
Chrome later; on a reconfigure it leaves an existing rig alone.
"""

from __future__ import annotations

from ..nav import Step
from ..server_browser_catalog import (
    INSTALL,
    SERVER_BROWSER_STEP_ID,
    SKIP,
    server_browser_selected,
)
from ..state import WizardState
from .base import Choice, single_select_step

__all__ = ["make_server_browser_step", "server_browser_selected"]


def make_server_browser_step() -> Step:
    def get_initial(state: WizardState) -> str:
        value = state.extras.get(SERVER_BROWSER_STEP_ID)
        return value if value in (INSTALL, SKIP) else INSTALL

    def store(state: WizardState, value: str) -> None:
        state.extras[SERVER_BROWSER_STEP_ID] = value

    return single_select_step(
        step_id=SERVER_BROWSER_STEP_ID,
        title="Server browser",
        note=(
            "Give the assistant a browser of its own: a headless Chrome running "
            "beside Nymeria with the Nymeria extension loaded and already "
            "connected, so browsing tasks work right away (about 200 MB to "
            "download; runs as a background service). It starts signed into "
            "nothing; you can sign it into sites later from the desktop app, "
            "or connect your own Chrome instead or as well."
        ),
        choices=[
            Choice(
                INSTALL,
                "Install the server browser (recommended)",
                "Download Chrome for Testing and the extension, connect them to "
                "this install, and keep the browser running in the background.",
            ),
            Choice(
                SKIP,
                "Skip",
                "No browser until you install the Nymeria extension in your own "
                "Chrome (or run `nymeria browser install` later).",
            ),
        ],
        get_initial=get_initial,
        store=store,
    )
