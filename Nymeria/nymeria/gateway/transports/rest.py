"""REST API transport for Nymeria gateway."""

import logging
import threading
from typing import TYPE_CHECKING, Optional

import uvicorn

from .base import BaseTransport
from ...triggers.api import create_api_app

if TYPE_CHECKING:
    from ...core.agent import NymeriaAgent

logger = logging.getLogger(__name__)


class RESTTransport(BaseTransport):
    """
    REST API transport using FastAPI and Uvicorn.

    Wraps the existing FastAPI application and runs it in a background thread
    with support for graceful shutdown.
    """

    def __init__(
        self,
        agent: "NymeriaAgent",
        host: str = "0.0.0.0",
        port: int = 8000,
    ):
        """
        Initialize the REST transport.

        Args:
            agent: The NymeriaAgent instance to handle requests
            host: Host address to bind to
            port: Port to listen on
        """
        super().__init__(agent, host, port)
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """
        Start the REST API server in a background thread.

        This method creates the FastAPI app, configures Uvicorn, and starts
        it in a daemon thread so it doesn't block the main thread.
        """
        import sys

        if self._running:
            logger.warning("REST transport is already running")
            return

        logger.info(f"Starting REST transport on {self.host}:{self.port}")

        # Create the FastAPI app with the agent
        app = create_api_app(self.agent)

        # Check if we're running in service mode (no stdout)
        # In service mode, disable Uvicorn's default logging to avoid isatty() errors
        is_service_mode = sys.stdout is None

        # Configure Uvicorn
        config = uvicorn.Config(
            app=app,
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=not is_service_mode,  # Disable access log in service mode
            log_config=None if is_service_mode else uvicorn.config.LOGGING_CONFIG,  # Disable default logging config in service mode
        )

        # Create server instance
        self._server = uvicorn.Server(config)

        # Start server in a background thread
        self._thread = threading.Thread(
            target=self._run_server,
            name="rest-transport",
            daemon=True,
        )
        self._thread.start()
        self._running = True

        logger.info(f"REST transport started on http://{self.host}:{self.port}")

    def _run_server(self) -> None:
        """Run the Uvicorn server (called from background thread)."""
        try:
            self._server.run()
        except Exception as e:
            logger.error(f"REST transport error: {e}", exc_info=True)
        finally:
            self._running = False

    def stop(self) -> None:
        """
        Stop the REST API server gracefully.

        Signals Uvicorn to exit and waits for the background thread to finish.
        """
        if not self._running or self._server is None:
            logger.warning("REST transport is not running")
            return

        logger.info("Stopping REST transport...")

        # Signal Uvicorn to exit
        self._server.should_exit = True

        # Wait for the thread to finish (with timeout)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                logger.warning("REST transport thread did not stop within timeout")

        self._running = False
        self._server = None
        self._thread = None

        logger.info("REST transport stopped")
