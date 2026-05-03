"""Backward-compatibility shim — real implementation in agent_compaction.py."""

from .agent_compaction import (
    COMPACT_PROMPT,
    AUTO_RESUME_MESSAGE,
    USER_RESUME_PREFIX,
    CompactionManager,
    estimate_tokens,
)


class ConversationCompactor:
    """Legacy wrapper; new code should use CompactionManager directly."""

    def __init__(self, settings):
        self.settings = settings

    def get_compact_prompt(self) -> str:
        return COMPACT_PROMPT

    def extract_summary(self, summary_message) -> str:
        return CompactionManager.extract_summary(summary_message)

    def format_auto_resume(self, summary: str) -> str:
        return CompactionManager.format_auto_resume(summary)

    def format_user_resume(self, user_message: str, summary: str) -> str:
        return CompactionManager.format_user_resume(user_message, summary)
