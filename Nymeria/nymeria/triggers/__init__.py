"""Nymeria triggers module."""

from .base import BaseTrigger
from .cli import CLITrigger
from .api import create_api_app

__all__ = ["BaseTrigger", "CLITrigger", "create_api_app"]

# Trigger sources are auto-loaded when the sources subpackage is imported.
# Import here so they're ready when the API starts.
from . import sources as trigger_sources  # noqa: F401
