"""Tests for OPS-003: unified API and gateway startup paths.

Both ``run.py api`` and ``run.py service`` (gateway foreground) must
converge on ``create_api_app()`` as the single startup factory for
agent creation, Redis event bus, FCM, and ticker-disable logic.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Static analysis: verify run_api() does NOT create NymeriaAgent directly
# ---------------------------------------------------------------------------

def test_run_api_does_not_instantiate_agent():
    """run_api() must not create a NymeriaAgent — create_api_app() owns that."""
    from run import run_api

    source = textwrap.dedent(inspect.getsource(run_api))
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "NymeriaAgent":
                pytest.fail(
                    "run_api() still instantiates NymeriaAgent directly — "
                    "agent creation should be delegated to create_api_app()"
                )


def test_run_api_does_not_call_set_event_bus():
    """run_api() must not init the Redis event bus — create_api_app() owns that."""
    from run import run_api

    source = textwrap.dedent(inspect.getsource(run_api))
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "set_event_bus":
                pytest.fail(
                    "run_api() still calls set_event_bus() directly — "
                    "Redis init should be delegated to create_api_app()"
                )


# ---------------------------------------------------------------------------
# Static analysis: GatewayServer.start() delegates to create_api_app
# ---------------------------------------------------------------------------

def test_gateway_start_does_not_instantiate_agent():
    """GatewayServer.start() must not create a NymeriaAgent directly."""
    from nymeria.gateway.server import GatewayServer

    source = textwrap.dedent(inspect.getsource(GatewayServer.start))
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "NymeriaAgent":
                pytest.fail(
                    "GatewayServer.start() still instantiates NymeriaAgent — "
                    "agent creation should be delegated to create_api_app()"
                )


def test_gateway_start_calls_create_api_app():
    """GatewayServer.start() must call create_api_app() for unified startup."""
    from nymeria.gateway.server import GatewayServer

    source = textwrap.dedent(inspect.getsource(GatewayServer.start))
    tree = ast.parse(source)

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "create_api_app":
                found = True
                break

    assert found, (
        "GatewayServer.start() does not call create_api_app() — "
        "both startup paths must converge on the same factory"
    )


# ---------------------------------------------------------------------------
# RESTTransport no longer needs an agent constructor arg
# ---------------------------------------------------------------------------

def test_rest_transport_accepts_app_not_agent():
    """RESTTransport constructor takes an app, not an agent."""
    from nymeria.gateway.transports.rest import RESTTransport

    sig = inspect.signature(RESTTransport.__init__)
    params = list(sig.parameters.keys())

    assert "app" in params, "RESTTransport should accept an 'app' parameter"
    assert "agent" not in params, (
        "RESTTransport should not accept an 'agent' parameter — "
        "the app is built by create_api_app() before the transport sees it"
    )


def test_base_transport_has_no_agent_param():
    """BaseTransport should not require an agent."""
    from nymeria.gateway.transports.base import BaseTransport

    sig = inspect.signature(BaseTransport.__init__)
    params = list(sig.parameters.keys())

    assert "agent" not in params


# ---------------------------------------------------------------------------
# Runtime: gateway calls create_api_app and retrieves agent
# ---------------------------------------------------------------------------

def test_gateway_retrieves_agent_from_create_api_app():
    """GatewayServer.start() should get its agent ref from get_agent()."""
    from nymeria.gateway.server import GatewayServer

    mock_app = MagicMock()
    mock_agent = MagicMock()
    mock_transport = MagicMock()

    with (
        patch(
            "nymeria.triggers.api.create_api_app", return_value=mock_app
        ) as patched_create,
        patch(
            "nymeria.triggers.api.get_agent", return_value=mock_agent
        ) as patched_get,
        patch(
            "nymeria.gateway.server.RESTTransport",
            return_value=mock_transport,
        ),
    ):
        gw = GatewayServer.__new__(GatewayServer)
        gw.settings = MagicMock(api_host="127.0.0.1", api_port=8000)
        gw._agent = None
        gw._transports = []
        gw._running = False
        gw._stop_event = MagicMock()

        gw.start()

        patched_create.assert_called_once()
        patched_get.assert_called_once()
        assert gw._agent is mock_agent


def test_create_api_app_is_the_single_factory():
    """Both run_api() and GatewayServer.start() converge on create_api_app.

    This is a documentation-style assertion: if someone re-adds agent
    creation outside create_api_app(), the static tests above will catch it.
    This test simply confirms the function exists and is importable.
    """
    from nymeria.triggers.api import create_api_app, get_agent

    assert callable(create_api_app)
    assert callable(get_agent)
