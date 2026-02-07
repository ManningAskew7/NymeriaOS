#!/usr/bin/env python
"""
Nymeria Windows Service Runner

This script is the entry point for the Windows Service. It sets up the
Python path correctly before starting the service.

Usage (run as Administrator):
    python service_runner.py install   - Install the service
    python service_runner.py remove    - Remove the service
    python service_runner.py start     - Start the service
    python service_runner.py stop      - Stop the service

Or use the main entry point:
    python run.py service install/start/stop/status
"""

import os
import sys
import traceback

# Set up paths BEFORE any other imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
if not _script_dir:
    # When run by pythonservice.exe, __file__ might be weird
    # Use the actual project location
    _script_dir = "C:\\NymeriaOS\\Nymeria"

sys.path.insert(0, _script_dir)
sys.path.insert(0, "C:/LangGraph/src")

# Add user site-packages for packages installed with --user
# This is needed because pythonservice.exe runs as LocalSystem which has different env vars
# We hardcode the path since LocalSystem's %APPDATA% points elsewhere
_user_site_packages = r"C:\Users\user\AppData\Roaming\Python\Python312\site-packages"
if os.path.exists(_user_site_packages) and _user_site_packages not in sys.path:
    sys.path.insert(0, _user_site_packages)

# Set working directory to project root
os.chdir(_script_dir)

# Early error logging for debugging service startup issues
def _log_error(msg):
    """Write error to a debug file for service debugging."""
    try:
        debug_file = os.path.join(_script_dir, "data", "logs", "service_debug.log")
        os.makedirs(os.path.dirname(debug_file), exist_ok=True)
        with open(debug_file, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"{datetime.datetime.now()}: {msg}\n")
    except:
        pass

# Log immediately when module is loaded
_log_error(f"service_runner.py loaded. script_dir={_script_dir}, sys.executable={sys.executable}")

try:
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_script_dir, ".env"))
except Exception as e:
    _log_error(f"Failed to load dotenv: {e}\n{traceback.format_exc()}")

# Windows service imports
try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    from logging.handlers import RotatingFileHandler
    import logging

    from nymeria.config import get_settings
except Exception as e:
    _log_error(f"Import error: {e}\n{traceback.format_exc()}")
    raise


def setup_service_logging():
    """Configure logging for service mode."""
    settings = get_settings()
    log_dir = settings.logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / settings.service_log_file

    handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.service_log_max_bytes,
        backupCount=settings.service_log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper()))
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


def _find_pythonservice_exe():
    """Find pythonservice.exe in common locations."""
    import site

    # Check user site-packages first (common when pip install --user)
    user_paths = [
        os.path.join(site.getusersitepackages(), "win32", "pythonservice.exe"),
        os.path.join(site.getusersitepackages(), "pywin32_system32", "pythonservice.exe"),
    ]

    # Check system site-packages
    for sp in site.getsitepackages():
        user_paths.append(os.path.join(sp, "win32", "pythonservice.exe"))
        user_paths.append(os.path.join(sp, "pywin32_system32", "pythonservice.exe"))

    # Check next to python.exe
    python_dir = os.path.dirname(sys.executable)
    user_paths.append(os.path.join(python_dir, "pythonservice.exe"))

    for path in user_paths:
        if os.path.exists(path):
            return path

    return None


class NymeriaService(win32serviceutil.ServiceFramework):
    """Windows Service for Nymeria."""

    _svc_name_ = "NymeriaService"
    _svc_display_name_ = "Nymeria AI Assistant"
    _svc_description_ = "Personal AI Assistant service providing REST API access"

    # Explicitly set the exe path to handle user-installed pywin32
    _exe_name_ = _find_pythonservice_exe()

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.gateway = None
        self.logger = None

    def SvcStop(self):
        """Handle service stop."""
        if self.logger:
            self.logger.info("Service stop requested")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

        if self.gateway:
            try:
                self.gateway.stop()
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error stopping gateway: {e}")

    def SvcDoRun(self):
        """Main service entry point."""
        try:
            _log_error("SvcDoRun started")

            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )

            # Setup logging
            self.logger = setup_service_logging()
            self.logger.info("Nymeria service starting...")
            _log_error("Logging setup complete")

            # Import and start gateway
            from nymeria.gateway.server import GatewayServer
            _log_error("GatewayServer imported")

            settings = get_settings()
            self.gateway = GatewayServer(settings=settings)
            _log_error("GatewayServer created")

            self.gateway.start()
            _log_error("GatewayServer started")

            self.logger.info("Nymeria service started successfully")

            # Wait for stop signal
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

            self.logger.info("Nymeria service stopped")

        except Exception as e:
            _log_error(f"Service error: {e}\n{traceback.format_exc()}")
            if self.logger:
                self.logger.error(f"Service error: {e}", exc_info=True)
            servicemanager.LogErrorMsg(f"Nymeria service error: {e}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Called by Windows SCM
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(NymeriaService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # Called from command line (install, remove, start, stop, etc.)
        win32serviceutil.HandleCommandLine(NymeriaService)
