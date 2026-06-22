"""Registry-vs-executor coverage guard (slice 03 F7).

``CommandService.execute`` resolves every executable command's handler purely by
name string::

    method_name = "_cmd_" + "_".join(definition.path)
    method = getattr(executor, method_name, None)

Nothing at construction time guarantees that each registered executable command
has a matching ``_cmd_*`` method, so a renamed or dropped handler (or, under the
deferred F1/F2 package/mixin split, a forgotten executor section) is otherwise
only discovered when a user runs that exact command. These tests assert the
binding holds for every executable command at test time instead.
"""
from __future__ import annotations

import pytest

from nymeria.core.command_service import _CommandExecutor, get_command_service


def _executable_commands() -> list:
    service = get_command_service()
    return [cmd for cmd in service._commands.values() if cmd.executable]


_EXECUTABLE = _executable_commands()


def test_registry_exposes_executable_commands() -> None:
    """Guard against the parametrized list silently going empty.

    If a registry refactor changed ``_commands`` or ``executable`` so that the
    list below became empty, the per-command binding test would pass vacuously.
    The floor is well under the current count (75) but high enough to catch an
    accidental collapse.
    """
    assert len(_EXECUTABLE) >= 50


@pytest.mark.parametrize(
    "command",
    _EXECUTABLE,
    ids=[cmd.id for cmd in _EXECUTABLE],
)
def test_executable_command_has_executor_method(command) -> None:
    method_name = "_cmd_" + "_".join(command.path)
    method = getattr(_CommandExecutor, method_name, None)
    assert method is not None, (
        f"Command /{command.name} (id={command.id}) is registered as executable "
        f"but _CommandExecutor has no {method_name} method. Either add the "
        f"handler, register the command as a non-command execution_kind, or fix "
        f"the command path."
    )
    assert callable(method), f"{method_name} is not callable."
