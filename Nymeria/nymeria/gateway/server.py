"""Gateway server managing agent lifecycle and transports for Nymeria."""

import logging
import threading
from typing import List, Optional

from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from .transports.base import BaseTransport
from .transports.rest import RESTTransport

logger = logging.getLogger(__name__)


class GatewayServer:
    """
    Gateway server that manages transports and coordinates shutdown.

    Agent creation, Redis event bus, FCM, ticker-disable logic, and tool
    sync are all owned by ``create_api_app()`` — the same factory used by
    the direct ``run.py api`` path. The gateway only orchestrates startup
    and graceful shutdown around that shared factory.
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._agent: Optional[NymeriaAgent] = None
        self._transports: List[BaseTransport] = []
        self._running = False
        self._stop_event = threading.Event()

    def start(self) -> None:
        """
        Start the gateway server.

        Delegates agent/app creation to ``create_api_app()`` so all
        startup logic (Redis, FCM, ticker) is shared with ``run.py api``.
        """
        if self._running:
            logger.warning("Gateway server is already running")
            return

        logger.info("Starting Nymeria Gateway Server...")

        from ..triggers.api import create_api_app, get_agent

        app = create_api_app()
        self._agent = get_agent()

        self._start_transports(app)

        self._running = True
        self._stop_event.clear()

        logger.info("Nymeria Gateway Server started successfully")
        logger.info(f"  REST API: http://{self.settings.api_host}:{self.settings.api_port}")
        logger.info(f"  API Docs: http://{self.settings.api_host}:{self.settings.api_port}/docs")

    def _start_transports(self, app) -> None:
        """Start all configured transports."""
        rest_transport = RESTTransport(
            app=app,
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

            if hasattr(self._agent, '_ticker') and self._agent._ticker is not None:
                try:
                    self._agent._ticker.stop()
                    logger.info("Ticker stopped")
                except Exception as e:
                    logger.error(f"Error stopping ticker: {e}", exc_info=True)

        self._running = False
        self._stop_event.set()
        self._agent = None

        logger.info("Nymeria Gateway Server stopped")

    def wait_for_stop(self) -> None:
        """Block until the server is stopped."""
        self._stop_event.wait()

    def is_running(self) -> bool:
        return self._running

    @property
    def agent(self) -> Optional[NymeriaAgent]:
        """Get the NymeriaAgent instance (None if not started)."""
        return self._agent
