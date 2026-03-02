"""Shared mutable state for the CLI session."""

from __future__ import annotations

import uuid
from typing import Optional

from rich.console import Console

from ...core.agent import NymeriaAgent
from ...config.settings import get_settings


class CLIState:
    """Holds all mutable state shared across the CLI session."""

    def __init__(
        self,
        agent: NymeriaAgent,
        thread_id: Optional[str] = None,
        user_id: str = "default",
    ) -> None:
        self.agent = agent
        self.console = Console()
        self.thread_id = thread_id or str(uuid.uuid4())[:8]
        self.user_id = user_id
        self.running = True

    # ── Manager shortcuts ──────────────────────────────────────────

    @property
    def settings(self):
        return get_settings()

    @property
    def todo_manager(self):
        return self.agent.todo_manager

    @property
    def thread_config_manager(self):
        return self.agent.thread_config_manager

    @property
    def thread_metadata_manager(self):
        return self.agent.thread_metadata_manager

    @property
    def profile_manager(self):
        return self.agent.profile_manager

    # ── Helpers ────────────────────────────────────────────────────

    def get_thread_title(self) -> str:
        """Return title for the current thread, or a truncated ID."""
        store = self.thread_metadata_manager.get_store(self.user_id)
        meta = store.threads.get(self.thread_id)
        if meta and meta.title and meta.title != "New Chat":
            # Truncate long titles
            return meta.title[:30] + "..." if len(meta.title) > 30 else meta.title
        return self.thread_id[:8]

    def get_effective_model(self) -> str:
        """Resolve per-thread model override vs global default."""
        tc = self.thread_config_manager.get_config(self.thread_id)
        if tc and tc.llm_config and tc.llm_config.model:
            return tc.llm_config.model
        return self.settings.llm_model

    def switch_thread(self, new_thread_id: str) -> None:
        """Switch the active thread."""
        self.thread_id = new_thread_id

    def new_thread(self, title: Optional[str] = None) -> str:
        """Create a new thread and switch to it."""
        new_id = str(uuid.uuid4())[:8]
        self.thread_id = new_id
        if title:
            self.thread_metadata_manager.upsert_thread(
                self.user_id, new_id, title=title, title_source="user"
            )
        return new_id
