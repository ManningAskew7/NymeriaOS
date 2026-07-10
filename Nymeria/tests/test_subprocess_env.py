"""Tests for the shared subprocess env allowlist and its use at exec surfaces.

Regression cover for the credential-containment hardening: the Python custom
tool runner and MCP stdio launch used to start from ``os.environ.copy()`` and
handed children the full secret-bearing environment. Every exec surface now
routes through the shared allowlist in ``nymeria/subprocess_env.py``.
"""

from nymeria.subprocess_env import (
    BASE_SUBPROCESS_ENV_PASSTHROUGH,
    scrubbed_subprocess_env,
)

_SECRET_NAMES = (
    "NYMERIA_SECRETS_KEY",
    "POSTGRES_URI",
    "NYMERIA_SERVICE_TOKEN",
    "ANTHROPIC_API_KEY",
    "REDIS_PASSWORD",
)


def test_scrubbed_env_drops_secrets_keeps_base(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    for name in _SECRET_NAMES:
        monkeypatch.setenv(name, "super-secret-value")
    env = scrubbed_subprocess_env()
    assert env.get("PATH") == "/usr/bin"
    for name in _SECRET_NAMES:
        assert name not in env


def test_scrubbed_env_passes_named_extras_only_when_set(monkeypatch):
    monkeypatch.setenv("MY_EXTRA", "1")
    monkeypatch.delenv("NOT_SET_EXTRA", raising=False)
    env = scrubbed_subprocess_env(["MY_EXTRA", "NOT_SET_EXTRA"])
    assert env["MY_EXTRA"] == "1"
    assert "NOT_SET_EXTRA" not in env


def test_base_allowlist_has_no_secret_shaped_names():
    for name in BASE_SUBPROCESS_ENV_PASSTHROUGH:
        assert not any(
            marker in name for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "URI", "URL")
        )


def test_python_tool_subprocess_env_is_scrubbed_and_launched_by_path(monkeypatch):
    """The Python custom tool child gets no API secrets and is launched by file
    path (not ``-m nymeria.core.python_tool_runner``)."""
    from nymeria.core import python_custom_tools
    from nymeria.tools.definitions.custom_tool_schema import PythonToolConfig

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", "master-key")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://u:p@h/db")

    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = '{"ok": true, "result": "x", "stdout": "", "stderr": ""}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _Completed()

    monkeypatch.setattr(python_custom_tools.subprocess, "run", _fake_run)

    config = PythonToolConfig(source_code="def run():\n    return 'x'\n")
    result = python_custom_tools.run_python_tool_subprocess(
        tool_id="t", config=config, params={}, timeout_seconds=5
    )

    assert result.ok is True
    env = captured["env"]
    assert "NYMERIA_SECRETS_KEY" not in env
    assert "POSTGRES_URI" not in env
    assert env.get("PYTHONUNBUFFERED") == "1"
    assert "-m" not in captured["cmd"]
    assert captured["cmd"][-1].endswith("python_tool_runner.py")


def test_mcp_stdio_env_scrubbed_but_keeps_declared(monkeypatch):
    """MCP stdio launch drops the API secrets but keeps the server's declared
    (credential-resolved) env vars and the non-secret base."""
    from nymeria.core import mcp_manager
    from nymeria.tools.definitions.mcp_schema import MCPToolConfig

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", "master-key")
    monkeypatch.setenv("POSTGRES_URI", "postgresql://u:p@h/db")
    monkeypatch.setenv("PATH", "/usr/bin")
    # A non-secret network var the server (npx/uvx) legitimately needs and used
    # to inherit via os.environ.copy(); it must survive the scrub.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")

    config = MCPToolConfig(
        server_command="npx",
        server_args=["-y", "some-server"],
        tool_name="__discovery__",
        env_vars={"NPM_CONFIG_CACHE": "/tmp/npm"},
    )
    env = mcp_manager._build_stdio_env(config)

    assert env.get("PATH") == "/usr/bin"
    assert env.get("NPM_CONFIG_CACHE") == "/tmp/npm"
    assert env.get("HTTPS_PROXY") == "http://proxy:3128"
    assert "NYMERIA_SECRETS_KEY" not in env
    assert "POSTGRES_URI" not in env
