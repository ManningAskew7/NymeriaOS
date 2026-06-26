"""Slice 27 F10: GatewayServer.stop() shuts the ticker via the agent's public
``stop_ticker()`` instead of reaching into ``agent._ticker`` behind a
breakage-masking ``hasattr`` guard.

The log-assertion tests patch the module logger directly rather than relying on
``caplog`` propagation, which is not isolation-proof: suite-wide logging config
(propagate flags / global levels set by other tests) can leave ``caplog`` empty.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from nymeria.gateway.server import GatewayServer


def _running_server(agent) -> GatewayServer:
    server = GatewayServer()
    server._agent = agent
    server._running = True
    return server


def _info_messages(mock_logger) -> list[str]:
    return [call.args[0] for call in mock_logger.info.call_args_list]


def _error_messages(mock_logger) -> list[str]:
    return [call.args[0] for call in mock_logger.error.call_args_list]


def test_stop_calls_agent_stop_ticker_and_tears_down():
    calls: list[bool] = []
    agent = SimpleNamespace(stop_ticker=lambda: (calls.append(True) or True))
    server = _running_server(agent)

    server.stop()

    assert calls == [True]
    assert server._running is False
    assert server._agent is None


def test_stop_logs_when_a_ticker_was_stopped():
    agent = SimpleNamespace(stop_ticker=lambda: True)
    server = _running_server(agent)

    with patch("nymeria.gateway.server.logger") as mock_logger:
        server.stop()

    assert any("Ticker stopped" in m for m in _info_messages(mock_logger))


def test_stop_does_not_log_ticker_when_none_running():
    agent = SimpleNamespace(stop_ticker=lambda: False)
    server = _running_server(agent)

    with patch("nymeria.gateway.server.logger") as mock_logger:
        server.stop()

    assert not any("Ticker stopped" in m for m in _info_messages(mock_logger))


def test_stop_swallows_and_logs_stop_ticker_error():
    def boom() -> bool:
        raise RuntimeError("ticker boom")

    agent = SimpleNamespace(stop_ticker=boom)
    server = _running_server(agent)

    with patch("nymeria.gateway.server.logger") as mock_logger:
        server.stop()  # must not raise

    assert any("Error stopping ticker" in m for m in _error_messages(mock_logger))
    assert server._running is False
    assert server._agent is None
