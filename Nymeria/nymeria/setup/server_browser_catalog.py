"""TUI-free facts about the server-browser wizard pick.

Shared by the step screen (`steps/server_browser.py`), the review summary,
hydrate, the runner's `--no-server-browser` flag, and finalize, none of which
may import the Textual step package (finalize must import without a TUI, the
way `voice_catalog` and `rag_catalog` keep their facts out of `steps/`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import WizardState

SERVER_BROWSER_STEP_ID = "server_browser"
INSTALL = "install"
SKIP = "skip"


def server_browser_selected(state: "WizardState") -> bool:
    """The pick, defaulting to install: the out-of-the-box path, and what a
    `--quick` run (which never shows the screen) or a hydrated reconfigure of
    a rig-less install applies. `--no-server-browser` and the screen's Skip
    both store `skip`."""
    return state.extras.get(SERVER_BROWSER_STEP_ID, INSTALL) == INSTALL
