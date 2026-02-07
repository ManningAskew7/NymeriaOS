"""Base transport abstract class for Nymeria gateway."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent


class BaseTransport(ABC):
    """
    Abstract base class for gateway transports.

    Transports handle incoming requests and route them to the agent.
    Examples include REST API, WebSocket, gRPC, etc.
    """

    def __init__(self, agent: "NymeriaAgent", host: str = "0.0.0.0", port: int = 8000):
        """
        Initialize the transport.

        Args:
            agent: The NymeriaAgent instance to handle requests
            host: Host address to bind to
            port: Port to listen on
        """
        self.agent = agent
        self.host = host
        self.port = port
        self._running = False

    @abstractmethod
    def start(self) -> None:
        """
        Start the transport (non-blocking).

        This method should start the transport in a background thread
        and return immediately.
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        Stop the transport gracefully.

        This method should signal the transport to stop and wait for
        it to shut down cleanly.
        """
        pass

    def is_running(self) -> bool:
        """
        Check if the transport is currently running.

        Returns:
            True if the transport is running, False otherwise
        """
        return self._running
