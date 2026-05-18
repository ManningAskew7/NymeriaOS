"""REST API transport for Nymeria gateway."""

import logging
import threading
from typing import Optional

import uvicorn

from .base import BaseTransport

logger = logging.getLogger(__name__)


class RESTTransport(BaseTransport):
    """
    REST API transport using FastAPI and Uvicorn.

    Wraps a pre-built FastAPI application and runs it in a background
    thread with support for graceful shutdown.
    """

    def __init__(
        self,
        app,
        host: str = "0.0.0.0",
        port: int = 8000,
    ):
        super().__init__(host, port)
        self._app = app
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the REST API server in a background thread."""
        import sys

        if self._running:
            logger.warning("REST transport is already running")
            return

        logger.info(f"Starting REST transport on {self.host}:{self.port}")

        is_service_mode = sys.stdout is None

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=not is_service_mode,
            log_config=None if is_service_mode else uvicorn.config.LOGGING_CONFIG,
        )

        self._server = uvicorn.Server(config)

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
            if self._server is None:
                raise RuntimeError("REST transport server not initialized")
            self._server.run()
        except Exception as e:
            logger.error(f"REST transport error: {e}", exc_info=True)
        finally:
            self._running = False

    def stop(self) -> None:
        """Stop the REST API server gracefully."""
        if not self._running or self._server is None:
            logger.warning("REST transport is not running")
            return

        logger.info("Stopping REST transport...")

        self._server.should_exit = True

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                logger.warning("REST transport thread did not stop within timeout")

        self._running = False
        self._server = None
        self._thread = None

        logger.info("REST transport stopped")
