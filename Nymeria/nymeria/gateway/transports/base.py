"""Base transport abstract class for Nymeria gateway."""

from abc import ABC, abstractmethod


class BaseTransport(ABC):
    """
    Abstract base class for gateway transports.

    Transports handle incoming requests via a pre-built application
    (e.g. FastAPI) and run them on a specific host/port.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8000):
        self.host = host
        self.port = port
        self._running = False

    @abstractmethod
    def start(self) -> None:
        """Start the transport (non-blocking)."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the transport gracefully."""
        pass

    def is_running(self) -> bool:
        return self._running
