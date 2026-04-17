"""Gateway server managing agent lifecycle and transports for Nymeria."""

import logging
import threading
from typing import List, Optional

from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from ..tools import get_all_tools_with_agents
from .transports.base import BaseTransport
from .transports.rest import RESTTransport

logger = logging.getLogger(__name__)


class GatewayServer:
    """
    Gateway server that manages the NymeriaAgent and its transports.

    The GatewayServer is responsible for:
    - Creating and configuring the NymeriaAgent with all tools
    - Starting configured transports (REST API, etc.)
    - Coordinating graceful shutdown of all components
    """

    def __init__(self, settings: Optional[Settings] = None):
        """
        Initialize the gateway server.

        Args:
            settings: Application settings (uses default if not provided)
        """
        self.settings = settings or get_settings()
        self._agent: Optional[NymeriaAgent] = None
        self._transports: List[BaseTransport] = []
        self._running = False
        self._stop_event = threading.Event()

    def start(self) -> None:
        """
        Start the gateway server.

        Creates the agent and starts all configured transports.
        This method is non-blocking - call wait_for_stop() to block until shutdown.
        """
        if self._running:
            logger.warning("Gateway server is already running")
            return

        logger.info("Starting Nymeria Gateway Server...")

        # Create the agent with all tools
        logger.info("Initializing NymeriaAgent...")
        self._agent = NymeriaAgent(settings=self.settings, tools=get_all_tools_with_agents())

        # Create and start transports
        self._start_transports()

        self._running = True
        self._stop_event.clear()

        logger.info("Nymeria Gateway Server started successfully")
        logger.info(f"  REST API: http://{self.settings.api_host}:{self.settings.api_port}")
        logger.info(f"  API Docs: http://{self.settings.api_host}:{self.settings.api_port}/docs")

    def _start_transports(self) -> None:
        """Start all configured transports."""
        # REST transport (always enabled)
        rest_transport = RESTTransport(
            agent=self._agent,
            host=self.settings.api_host,
            port=self.settings.api_port,
        )
        rest_transport.start()
        self._transports.append(rest_transport)

        logger.info(f"Started {len(self._transports)} transport(s)")

    def stop(self) -> None:
        """
        Stop the gateway server gracefully.

        Stops all transports first, then shuts down the agent's background threads.
        """
        if not self._running:
            logger.warning("Gateway server is not running")
            return

        logger.info("Stopping Nymeria Gateway Server...")

        # Stop all transports
        logger.info("Stopping transports...")
        for transport in self._transports:
            try:
                transport.stop()
            except Exception as e:
                logger.error(f"Error stopping transport: {e}", exc_info=True)

        self._transports.clear()

        # Stop agent background threads
        if self._agent is not None:
            logger.info("Stopping agent background threads...")

            # Stop the ticker
            if hasattr(self._agent, '_ticker') and self._agent._ticker is not None:
                try:
                    self._agent._ticker.stop()
                    logger.info("Ticker stopped")
                except Exception as e:
                    logger.error(f"Error stopping ticker: {e}", exc_info=True)

            # Watchdog now runs as an external thin-client service (run.py watchdog),
            # so there's nothing for the gateway to stop here.

        self._running = False
        self._stop_event.set()
        self._agent = None

        logger.info("Nymeria Gateway Server stopped")

    def wait_for_stop(self) -> None:
        """
        Block until the server is stopped.

        Call this after start() to keep the main thread alive until shutdown.
        """
        self._stop_event.wait()

    def is_running(self) -> bool:
        """
        Check if the gateway server is running.

        Returns:
            True if the server is running, False otherwise
        """
        return self._running

    @property
    def agent(self) -> Optional[NymeriaAgent]:
        """Get the NymeriaAgent instance (None if not started)."""
        return self._agent
