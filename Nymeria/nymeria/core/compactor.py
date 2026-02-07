"""Conversation compaction via in-context summarization.

Instead of re-sending messages to a separate LLM call, we ask the agent
(which already has the full context) to summarize and save important facts.
"""

import logging
from typing import List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


# Prompt asking agent to generate a summary (it already has full context)
COMPACT_PROMPT = """**System Request: Context Compaction**

The conversation is getting long and needs to be summarized. After your response, older messages will be removed and only your summary will remain.

**Your summary must include:**

1. **What we were just doing** - Be specific about the last few turns:
   - What did the user ask for most recently?
   - What action were you in the middle of?
   - Any pending questions or decisions?

2. **Key context** - Important facts from the conversation:
   - User info, project details, preferences established
   - Decisions made and their reasoning
   - Significant outcomes from tool operations

3. **Files to read** - If continuing work, list specific files I should read to get back up to speed:
   - Code files being worked on
   - Config files referenced
   - Any notes or checklists created during this session

4. **Save persistent facts** - Use `memory_save` for anything that should be remembered across ALL future conversations:
   - User's name, role, occupation
   - Project names and key technical details
   - Strong preferences or constraints

**Keep it concise but complete** - this summary will be my only context for continuing the conversation.

**Do NOT include:**
- Routine greetings or small talk
- Failed attempts that were later corrected
- Verbose tool outputs (just summarize outcomes)
- Information already saved to memory"""

# Message injected for auto-compact (agent continues immediately)
# Format is designed to be parseable by frontend for collapsible display
AUTO_RESUME_MESSAGE = """[Auto-compact: Context limit reached, conversation summarized]

---
*Context Summary (auto-compact):*

{summary}

---

Continue where you left off. If you were in the middle of a task, proceed with it."""

# Prefix added to user's message when they have a pending summary (manual /compact)
USER_RESUME_PREFIX = """---
*This conversation is resuming from a previous session that exceeded context limits. Summary of prior context:*

{summary}

---"""


class ConversationCompactor:
    """
    Handles conversation compaction by asking the agent to summarize in-context.

    Instead of making a separate LLM call, this triggers a summarization turn
    where the agent (which already has full context) generates a summary and
    can save important facts to persistent memory.
    """

    def __init__(self, settings):
        """
        Initialize the compactor.

        Args:
            settings: Application settings
        """
        self.settings = settings

    def get_compact_prompt(self) -> str:
        """
        Get the prompt to inject for compaction.

        Returns:
            The compaction prompt string
        """
        return COMPACT_PROMPT

    def extract_summary(self, summary_message: AIMessage) -> str:
        """
        Extract the summary text from the agent's response.

        Args:
            summary_message: The agent's summary response

        Returns:
            Summary text content
        """
        return summary_message.content if isinstance(summary_message.content, str) else str(summary_message.content)

    def format_auto_resume(self, summary: str) -> str:
        """
        Format summary for auto-compact resume (agent continues immediately).

        Args:
            summary: The summary text

        Returns:
            Formatted prompt for agent to continue
        """
        from . import compactor as module
        return module.AUTO_RESUME_MESSAGE.format(summary=summary)

    def format_user_resume(self, user_message: str, summary: str) -> str:
        """
        Format user's message with attached summary (manual /compact).

        Args:
            user_message: The user's actual message
            summary: The summary text

        Returns:
            User message with summary appended
        """
        from . import compactor as module
        return f"{user_message}\n\n{module.USER_RESUME_PREFIX.format(summary=summary)}"


def estimate_tokens(text: str) -> int:
    """
    Rough estimate of token count from text.

    Uses ~4 chars per token as a rough approximation.

    Args:
        text: Text to estimate

    Returns:
        Estimated token count
    """
    return len(text) // 4
