"""Base class for trigger event sources.

Each trigger source watches for a specific type of external event
(webhooks, email arrival, GitHub activity, etc.) and returns events
when they occur. Sources must be lightweight -- no LLM calls.

Config schema fields support these keys:
    type         str   "string"|"integer"|"number"|"boolean"|"object"|"array"
    description  str   Field description shown in UI
    required     bool  Whether the field is mandatory
    default      Any   Prefill value in the UI
    placeholder  str   Hint text shown in empty inputs
    enum         list  Dropdown options (strings)
    group        str   Visual grouping label in the setup wizard
    order        int   Display order within the form
    secret       bool  Mask the input (password-style)
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from ...core.time_utils import utc_now

logger = logging.getLogger(__name__)


class BaseTriggerSource(ABC):
    """Abstract base class for trigger event sources.

    Subclasses represent a category of event (e.g. "webhook", "github_issues").
    Each trigger *instance* stores its own ``config`` and ``state``; the source
    class is stateless and shared across all triggers of the same type.
    """

    name: str = ""
    description: str = ""
    config_schema: Dict[str, Any] = {}

    # Rich metadata for frontend catalog / setup wizard
    category: str = "general"
    icon: str = "bolt"
    setup_guide: str = ""
    template_variables: List[str] = []
    example_config: dict = {}
    requires_auth: Optional[str] = None

    @abstractmethod
    def check(self, config: dict, state: dict, user_id: str = "") -> List[dict]:
        """Check for new events.

        Called by the polling loop (ticker).  Must be lightweight -- no LLM
        calls, no expensive I/O unless truly necessary.

        Args:
            config: Source-specific configuration from the trigger definition.
            state:  Mutable state dict persisted between checks.  Sources can
                    store cursors, timestamps, etc. here.
            user_id: Owning Nymeria user. Required by sources that hit
                    per-user credential stores (outlook, teams). Default
                    ``""`` keeps sources that don't need it unchanged.

        Returns:
            List of event dicts (empty = no new events).  Each dict contains
            event-specific data available as ``{template_vars}`` in actions.
        """
        ...

    def get_sample_event(self, config: dict) -> dict:
        """Return a realistic sample event dict for testing/preview.

        Override per source to provide meaningful sample data that matches
        the structure returned by ``check()``.
        """
        return {
            "trigger_name": "test",
            "fired_at": utc_now().isoformat(),
        }

    def validate_config(self, config: dict) -> Tuple[bool, str]:
        """Validate source config against the declared schema.

        The default implementation checks that all required fields are present
        and that types match if specified.  Subclasses may override for custom
        validation logic.

        Returns:
            (ok, message) tuple.
        """
        for field_name, field_def in self.config_schema.items():
            required = field_def.get("required", False)
            if required and field_name not in config:
                return False, f"Missing required field: {field_name}"
            if field_name in config:
                expected_type = field_def.get("type")
                value = config[field_name]
                type_map = {
                    "string": str,
                    "integer": int,
                    "number": (int, float),
                    "boolean": bool,
                    "object": dict,
                    "array": list,
                }
                py_type = type_map.get(expected_type)
                if py_type and not isinstance(value, py_type):
                    return False, f"Field '{field_name}' must be {expected_type}, got {type(value).__name__}"
        return True, "ok"

    def on_register(self) -> None:
        """Called once when the source is registered. Optional setup hook."""
        pass
