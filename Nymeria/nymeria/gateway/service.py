"""Windows Service implementation for Nymeria using pywin32."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Windows-only imports
try:
    import win32service
    import win32serviceutil
    HAS_PYWIN32 = True
except ImportError:
    HAS_PYWIN32 = False

from ..config import get_settings

logger = logging.getLogger(__name__)


def _get_python_executable() -> str:
    """Get the path to the Python executable."""
    return sys.executable


def _get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


def _get_service_runner_script() -> str:
    """Get the path to the service runner script."""
    return str(_get_project_root() / "service_runner.py")


def _run_service_command(command: str) -> tuple[bool, str]:
    """
    Run a service command via service_runner.py.

    Args:
        command: The command (install, remove, start, stop)

    Returns:
        Tuple of (success, output_message)
    """
    python_exe = _get_python_executable()
    service_script = _get_service_runner_script()

    if not os.path.exists(service_script):
        return False, f"Service runner script not found: {service_script}"

    cmd = [python_exe, service_script, command]
    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout + result.stderr
    success = result.returncode == 0 or "already" in output.lower()

    return success, output


def install_service(auto_start: bool = True) -> bool:
    """
    Install Nymeria as a Windows service.

    Args:
        auto_start: Whether to configure the service for automatic startup

    Returns:
        True if installation was successful, False otherwise
    """
    if not HAS_PYWIN32:
        logger.error("pywin32 is required for Windows service support")
        return False

    settings = get_settings()

    try:
        logger.info(f"Installing service: {settings.service_name}")

        # Use service_runner.py to install via HandleCommandLine
        success, output = _run_service_command("install")

        if success:
            logger.info(f"Service '{settings.service_name}' installed successfully")
            # Configure recovery options (restart on failure)
            _configure_recovery(settings.service_name)
            return True
        else:
            logger.error(f"Failed to install service: {output}")
            return False

    except Exception as e:
        logger.error(f"Failed to install service: {e}", exc_info=True)
        return False


def _configure_recovery(service_name: str) -> None:
    """
    Configure service recovery options to auto-restart on failure.

    Uses sc.exe to set failure actions:
    - First failure: Restart after 60 seconds
    - Second failure: Restart after 60 seconds
    - Subsequent failures: Restart after 60 seconds
    """
    try:
        # Configure automatic restart on failure
        cmd = [
            "sc.exe",
            "failure",
            service_name,
            "reset=",
            "86400",  # Reset failure count after 1 day
            "actions=",
            "restart/60000/restart/60000/restart/60000",  # Restart after 60s each time
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        logger.info(f"Recovery options configured for service '{service_name}'")

    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to configure recovery options: {e}")
    except Exception as e:
        logger.warning(f"Failed to configure recovery options: {e}")


def uninstall_service() -> bool:
    """
    Remove the Nymeria Windows service.

    Returns:
        True if uninstallation was successful, False otherwise
    """
    if not HAS_PYWIN32:
        logger.error("pywin32 is required for Windows service support")
        return False

    settings = get_settings()

    try:
        # Stop the service first if it's running
        try:
            stop_service()
        except Exception:
            pass  # Service might not be running

        # Remove the service using service_runner.py
        logger.info(f"Uninstalling service: {settings.service_name}")
        success, output = _run_service_command("remove")

        if success or "does not exist" in output.lower():
            logger.info(f"Service '{settings.service_name}' uninstalled successfully")
            return True
        else:
            logger.error(f"Failed to uninstall service: {output}")
            return False

    except Exception as e:
        logger.error(f"Failed to uninstall service: {e}", exc_info=True)
        return False


def start_service() -> bool:
    """
    Start the Nymeria Windows service.

    Returns:
        True if the service was started successfully, False otherwise
    """
    if not HAS_PYWIN32:
        logger.error("pywin32 is required for Windows service support")
        return False

    settings = get_settings()

    try:
        logger.info(f"Starting service: {settings.service_name}")
        success, output = _run_service_command("start")

        if success or "already" in output.lower():
            logger.info(f"Service '{settings.service_name}' started")
            return True
        else:
            logger.error(f"Failed to start service: {output}")
            return False

    except Exception as e:
        logger.error(f"Failed to start service: {e}", exc_info=True)
        return False


def stop_service() -> bool:
    """
    Stop the Nymeria Windows service.

    Returns:
        True if the service was stopped successfully, False otherwise
    """
    if not HAS_PYWIN32:
        logger.error("pywin32 is required for Windows service support")
        return False

    settings = get_settings()

    try:
        logger.info(f"Stopping service: {settings.service_name}")
        success, output = _run_service_command("stop")

        if success or "not started" in output.lower():
            logger.info(f"Service '{settings.service_name}' stopped")
            return True
        else:
            logger.error(f"Failed to stop service: {output}")
            return False

    except Exception as e:
        logger.error(f"Failed to stop service: {e}", exc_info=True)
        return False


def get_service_status() -> Optional[str]:
    """
    Get the current status of the Nymeria Windows service.

    Returns:
        Service status string ("running", "stopped", "starting", "stopping",
        "not_installed", or "unknown"), or None if an error occurred
    """
    if not HAS_PYWIN32:
        logger.error("pywin32 is required for Windows service support")
        return None

    settings = get_settings()

    try:
        status = win32serviceutil.QueryServiceStatus(settings.service_name)
        state = status[1]

        state_map = {
            win32service.SERVICE_STOPPED: "stopped",
            win32service.SERVICE_START_PENDING: "starting",
            win32service.SERVICE_STOP_PENDING: "stopping",
            win32service.SERVICE_RUNNING: "running",
            win32service.SERVICE_CONTINUE_PENDING: "continuing",
            win32service.SERVICE_PAUSE_PENDING: "pausing",
            win32service.SERVICE_PAUSED: "paused",
        }

        return state_map.get(state, "unknown")

    except Exception as e:
        error_str = str(e).lower()
        if "does not exist" in error_str or "1060" in str(e):
            return "not_installed"
        logger.error(f"Failed to get service status: {e}", exc_info=True)
        return None


