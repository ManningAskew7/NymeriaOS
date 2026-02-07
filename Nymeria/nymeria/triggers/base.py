"""Base trigger interface for Nymeria."""

from abc import ABC, abstractmethod

from ..core.agent import NymeriaAgent


class BaseTrigger(ABC):
    """
    Abstract base class for Nymeria triggers.

    Triggers are input interfaces that route messages to the agent.
    Examples: CLI, REST API, Discord bot, Slack integration, etc.
    """

    def __init__(self, agent: NymeriaAgent):
        """
        Initialize the trigger.

        Args:
            agent: The NymeriaAgent instance to route messages to
        """
        self.agent = agent

    @abstractmethod
    def start(self) -> None:
        """Start the trigger and begin listening for input."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the trigger gracefully."""
        ...

    def process_message(
        self,
        message: str,
        thread_id: str = "default",
    ) -> str:
        """
        Process an incoming message through the agent.

        Args:
            message: The user's message
            thread_id: Conversation thread ID for persistence

        Returns:
            Agent's response
        """
        return self.agent.chat(message, thread_id=thread_id)

    async def aprocess_message(
        self,
        message: str,
        thread_id: str = "default",
    ):
        """
        Async version for streaming responses.

        Args:
            message: The user's message
            thread_id: Conversation thread ID

        Yields:
            Response chunks from the agent
        """
        async for chunk in self.agent.astream(message, thread_id=thread_id):
            yield chunk
