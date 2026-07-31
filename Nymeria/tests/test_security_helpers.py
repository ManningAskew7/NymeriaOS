"""Focused coverage for security-adjacent helper modules."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import nymeria.config as config_module
import nymeria.core.secrets as secrets
from nymeria.core import http_policy
from nymeria.core.validator import CodeValidator


def test_secrets_encrypt_decrypt_roundtrip_reload_and_wrong_key(monkeypatch):
    first_key = secrets.generate_key()
    second_key = secrets.generate_key()
    monkeypatch.setenv(secrets.SECRETS_KEY_ENV_VAR, first_key)

    ciphertext = secrets.encrypt("token-\u2603")

    assert ciphertext != "token-\u2603"
    assert secrets.decrypt(ciphertext) == "token-\u2603"

    monkeypatch.setenv(secrets.SECRETS_KEY_ENV_VAR, second_key)
    with pytest.raises(secrets.InvalidToken):
        secrets.decrypt(ciphertext)


def test_secrets_missing_invalid_and_type_errors(monkeypatch):
    monkeypatch.delenv(secrets.SECRETS_KEY_ENV_VAR, raising=False)

    assert secrets.has_secrets_key() is False
    with pytest.raises(secrets.SecretsKeyMissing, match=secrets.SECRETS_KEY_ENV_VAR):
        secrets.encrypt("value")

    monkeypatch.setenv(secrets.SECRETS_KEY_ENV_VAR, "not-a-fernet-key")
    assert secrets.has_secrets_key() is True
    with pytest.raises(secrets.SecretsKeyInvalid):
        secrets.decrypt("not-a-token")

    with pytest.raises(TypeError):
        secrets.encrypt(b"bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        secrets.decrypt(b"bytes")  # type: ignore[arg-type]


def test_http_audit_writes_redacted_jsonl(monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(audit_log_enabled=True, logs_dir=logs_dir),
    )

    http_policy.audit_http_event(
        {
            "tool": "http_request",
            "url": "https://api.example.com",
            "headers": {
                "Authorization": "Bearer secret-token",
                "X-API-Key": "sk-test_secretsecretsecret",
                "X-Trace": "public",
            },
            "body": {"note": "github token ghp_abcdefghijklmnopqrstuvwxyz"},
        }
    )

    log_files = list(logs_dir.glob("audit_*.jsonl"))
    assert len(log_files) == 1
    entry = json.loads(log_files[0].read_text(encoding="utf-8"))

    assert entry["event_type"] == "http_tool_call"
    assert entry["tool"] == "http_request"
    assert entry["headers"]["Authorization"] == "[REDACTED]"
    assert entry["headers"]["X-API-Key"] == "[REDACTED]"
    assert entry["headers"]["X-Trace"] == "public"
    assert entry["body"]["note"] == "github token [REDACTED]"
    assert "timestamp" in entry


def test_http_audit_write_failure_logs_error(monkeypatch, tmp_path, caplog):
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(
            audit_log_enabled=True,
            logs_dir=blocking_file / "audit",
        ),
    )

    caplog.set_level(logging.ERROR, logger="nymeria.core.http_policy")

    http_policy.audit_http_event({"tool": "http_request"})

    assert "Failed to write HTTP audit event" in caplog.text


def test_requests_get_with_policy_blocks_private_redirect():
    class FakeResponse:
        status_code = 302
        headers = {"location": "https://127.0.0.1/admin"}
        url = "https://api.example.com/start"
        is_redirect = True

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return FakeResponse()

    session = FakeSession()

    with pytest.raises(http_policy.HTTPPolicyViolation) as exc_info:
        http_policy.requests_get_with_policy(
            "https://api.example.com/start",
            session=session,
            config=http_policy.HTTPPolicyConfig(resolve_dns=False),
        )

    assert session.calls == 1
    assert exc_info.value.decision.reason == "loopback_network"
    assert exc_info.value.redirect_chain[0]["redirect_url"] == "https://127.0.0.1/admin"


def test_requests_get_with_policy_pins_dns_between_check_and_connect(monkeypatch):
    hostile_ip = "10.0.0.5"
    pinned_ip = "93.184.216.34"
    seen_addrinfo = {}

    def hostile_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (hostile_ip, port))]

    class FakeResponse:
        status_code = 200
        headers = {}
        url = "https://api.example.com/data"
        is_redirect = False

    class FakeSession:
        def get(self, *args, **kwargs):
            seen_addrinfo["during_request"] = socket.getaddrinfo(
                "api.example.com",
                443,
                type=socket.SOCK_STREAM,
            )
            return FakeResponse()

    # The pin installs a transparent dispatcher onto socket.getaddrinfo, which
    # delegates non-pinned lookups to the system resolver captured at import.
    # Inject the hostile resolver there to simulate DNS rebinding during connect.
    monkeypatch.setattr(http_policy, "_SYSTEM_GETADDRINFO", hostile_getaddrinfo)

    response, _redirect_chain, decision = http_policy.requests_get_with_policy(
        "https://api.example.com/data",
        session=FakeSession(),
        config=http_policy.HTTPPolicyConfig(),
        resolver=lambda host, port: [pinned_ip],
    )

    assert response.status_code == 200
    assert decision.resolved_ips == (pinned_ip,)
    assert seen_addrinfo["during_request"][0][4][0] == pinned_ip
    assert socket.getaddrinfo("api.example.com", 443)[0][4][0] == hostile_ip


def test_httpx_request_with_policy_pins_dns_between_check_and_connect(monkeypatch):
    hostile_ip = "10.0.0.5"
    pinned_ip = "93.184.216.34"
    seen_addrinfo = {}

    def hostile_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (hostile_ip, port))]

    class FakeResponse:
        is_redirect = False
        status_code = 200
        headers = {}
        url = "https://api.example.com/data"

    class FakeClient:
        def request(self, *args, **kwargs):
            seen_addrinfo["during_request"] = socket.getaddrinfo(
                "api.example.com",
                443,
                type=socket.SOCK_STREAM,
            )
            return FakeResponse()

    # The pin installs a transparent dispatcher onto socket.getaddrinfo, which
    # delegates non-pinned lookups to the system resolver captured at import.
    # Inject the hostile resolver there to simulate DNS rebinding during connect.
    monkeypatch.setattr(http_policy, "_SYSTEM_GETADDRINFO", hostile_getaddrinfo)

    response, _redirect_chain, decision = http_policy.httpx_request_with_policy(
        "GET",
        "https://api.example.com/data",
        client=FakeClient(),
        config=http_policy.HTTPPolicyConfig(),
        resolver=lambda host, port: [pinned_ip],
    )

    assert response.status_code == 200
    assert decision.resolved_ips == (pinned_ip,)
    assert seen_addrinfo["during_request"][0][4][0] == pinned_ip
    assert socket.getaddrinfo("api.example.com", 443)[0][4][0] == hostile_ip


@pytest.mark.parametrize(
    "env_var", ["http_proxy", "https_proxy", "all_proxy"]
)
def test_requests_policy_path_does_not_inherit_an_env_proxy(monkeypatch, env_var):
    """A proxy would make the whole policy advisory, on the requests side too.

    The proxy resolves the hostname ITSELF and opens the socket, so neither the
    private-address verdict nor the resolver pin describes what the connection
    reaches. This path carries ``web_fetch`` and ``browser``, whose URL is a
    plain tool argument, so it is the higher-exposure half of the two.

    Asserts the MECHANISM rather than the argument: it feeds whatever the code
    passed back through requests' own environment merge and checks that no proxy
    survives. An assertion on the literal dict would pass while ``ALL_PROXY``
    still routed, which is exactly the bug the third key exists to prevent.
    """
    import requests
    from requests.utils import select_proxy

    url = "https://example.invalid/x"
    captured: dict = {}

    class FakeSession:
        def get(self, target, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(is_redirect=False, status_code=200, headers={}, url=target)

    monkeypatch.setenv(env_var, "http://attacker-proxy.invalid:3128")

    http_policy.requests_get_with_policy(
        url,
        session=FakeSession(),
        config=http_policy.HTTPPolicyConfig(resolve_dns=False),
    )

    merged = requests.Session().merge_environment_settings(
        url, dict(captured.get("proxies") or {}), None, None, None
    )
    assert select_proxy(url, merged["proxies"]) is None, (
        f"{env_var} still routes this request through a proxy the policy cannot see"
    )


def test_pinned_dns_resolution_covers_the_punycode_spelling(monkeypatch):
    """An internationalized hostname must not slip the pin.

    A decision carries the hostname as ``urlparse`` reports it, which for an IDN
    is the UNICODE form; httpx and requests connect with the IDNA (punycode)
    form. Keyed on one spelling, the dispatcher matched neither the client's
    lookup nor anything else, fell through to the system resolver, and the check
    and the connect performed independent lookups. That is the rebinding window
    the pin exists to close, and it failed SILENTLY: the screen still ran, so
    only the pin's own behaviour shows it.

    Asserted through the hostile-resolver seam its sibling above uses, so a
    regression looks like a rebind rather than a missing key.
    """
    pinned_ip, hostile_ip = "93.184.216.34", "10.0.0.5"

    def hostile_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (hostile_ip, port))]

    monkeypatch.setattr(http_policy, "_SYSTEM_GETADDRINFO", hostile_getaddrinfo)

    # The second pair is the sharp one. "ß" is an IDNA DEVIATION character: the
    # stdlib codec (IDNA2003) maps it to "fass.example.test" while httpx and
    # requests, which both use the `idna` package (IDNA2008/UTS46), ask for
    # "xn--fa-hia.example.test". Those are different domains, so keying on the
    # stdlib spelling would have left the policy judging one host and the client
    # connecting to another. The first pair alone cannot catch that, because the
    # two standards agree on "ü".
    for unicode_host, wire_host in (
        ("münchen.example.test", "xn--mnchen-3ya.example.test"),
        ("faß.example.test", "xn--fa-hia.example.test"),
    ):
        decision = http_policy.evaluate_http_url(
            f"https://{unicode_host}/x",
            config=http_policy.HTTPPolicyConfig(),
            resolver=lambda host, port: [pinned_ip],
        )
        assert decision.allowed and decision.resolved_ips == (pinned_ip,)
        # The verdict must be ABOUT the host the client will reach, not merely
        # pinned to it: a decision carrying the unicode (or IDNA2003) spelling
        # would have resolved a different name than the socket asks for.
        assert decision.host == wire_host

        with http_policy.pinned_dns_resolution(decision):
            for spelling in (unicode_host, wire_host):
                resolved = socket.getaddrinfo(spelling, 443, type=socket.SOCK_STREAM)
                assert resolved[0][4][0] == pinned_ip, f"{spelling} escaped the pin"

        # Outside the pin the hostile answer is what a second lookup would get,
        # which is what makes the assertion above meaningful.
        assert socket.getaddrinfo(wire_host, 443)[0][4][0] == hostile_ip


def test_pinned_dns_resolution_isolates_concurrent_threads():
    """Two threads pinning different hosts at the same time each resolve their
    own pinned IP.

    The old design held a process-global RLock across the entire request, so two
    pins could not coexist: this barrier setup (each thread waits for the other
    while inside its pinned context) would have deadlocked, since the second
    thread could never acquire the lock to reach the barrier. The thread-local
    pin makes the two contexts independent, so both resolve concurrently.
    """
    decision_a = http_policy.evaluate_http_url(
        "https://a.example.test/x",
        config=http_policy.HTTPPolicyConfig(),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    decision_b = http_policy.evaluate_http_url(
        "https://b.example.test/x",
        config=http_policy.HTTPPolicyConfig(),
        resolver=lambda host, port: ["8.8.8.8"],
    )
    assert decision_a.resolved_ips == ("93.184.216.34",)
    assert decision_b.resolved_ips == ("8.8.8.8",)

    barrier = threading.Barrier(2, timeout=10)
    results: dict[str, str] = {}
    errors: list[Exception] = []

    def run(name: str, decision: http_policy.HTTPPolicyDecision, host: str) -> None:
        try:
            with http_policy.pinned_dns_resolution(decision):
                # Both threads are now inside their own pinned context at once.
                barrier.wait()
                infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                results[name] = str(infos[0][4][0])
        except Exception as exc:  # noqa: BLE001 - surfaced via the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("a", decision_a, "a.example.test")),
        threading.Thread(target=run, args=("b", decision_b, "b.example.test")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert results == {"a": "93.184.216.34", "b": "8.8.8.8"}


def test_validate_http_egress_url_blocks_literal_private_base_url():
    with pytest.raises(ValueError, match="blocked by HTTP egress policy"):
        http_policy.validate_http_egress_url(
            "http://127.0.0.1:8000/api",
            label="base URL",
            resolve_dns=False,
        )


def test_validator_accepts_sync_and_async_tool_definitions(tmp_path):
    validator = CodeValidator(tmp_path)

    sync_valid, sync_msg = validator.validate_tool_definition(
        "from langchain_core.tools import tool\n\n"
        "@tool\n"
        "def lookup_price(symbol: str) -> str:\n"
        "    return symbol\n"
    )
    async_valid, async_msg = validator.validate_tool_definition(
        "from langchain_core.tools import tool\n\n"
        "@tool\n"
        "async def fetch_price(symbol: str) -> str:\n"
        "    return symbol\n"
    )

    assert sync_valid is True
    assert "lookup_price" in sync_msg
    assert async_valid is True
    assert "fetch_price" in async_msg


def test_validator_rejects_bad_syntax_missing_tool_and_disallowed_path(tmp_path):
    validator = CodeValidator(tmp_path)
    tools_dir = tmp_path / "nymeria" / "tools"
    tool_file = tools_dir / "example_tool.py"

    valid_syntax, syntax_msg = validator.validate_python_syntax("def broken(:\n")
    valid_tool, tool_msg = validator.validate_modification(
        tool_file,
        "def helper() -> str:\n"
        "    return 'missing decorator'\n",
    )
    valid_path, path_msg = validator.validate_modification(
        tmp_path / "outside.py",
        "@tool\n"
        "def outside() -> str:\n"
        "    return 'nope'\n",
    )

    assert valid_syntax is False
    assert "Syntax error" in syntax_msg
    assert valid_tool is False
    assert "No @tool decorated function found" in tool_msg
    assert valid_path is False
    assert "Path not allowed" in path_msg


def test_tool_import_success_runs_seed_tools_import_in_subprocess(tmp_path, monkeypatch):
    """The post-modification guarantee is an import smoke test: it shells out to a
    fresh interpreter importing ``SEED_TOOLS`` from ``project_root`` and reports the
    trimmed stdout on a clean exit."""
    validator = CodeValidator(tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="Loaded 42 tools\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    success, msg = validator.test_tool_import()

    assert success is True
    assert msg == "Loaded 42 tools"
    assert len(captured["cmd"]) == 3
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1] == "-c"
    assert "SEED_TOOLS" in captured["cmd"][2]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["timeout"] == 30
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_tool_import_failure_returns_stderr(tmp_path, monkeypatch):
    validator = CodeValidator(tmp_path)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="ImportError: boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    success, msg = validator.test_tool_import()

    assert success is False
    assert "Import failed:" in msg
    assert "ImportError: boom" in msg


def test_tool_import_timeout_is_reported(tmp_path, monkeypatch):
    validator = CodeValidator(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    success, msg = validator.test_tool_import()

    assert success is False
    assert "timed out" in msg


def test_tool_import_unexpected_error_is_reported(tmp_path, monkeypatch):
    validator = CodeValidator(tmp_path)

    def fake_run(cmd, **kwargs):
        raise OSError("no interpreter")

    monkeypatch.setattr(subprocess, "run", fake_run)

    success, msg = validator.test_tool_import()

    assert success is False
    assert "Import test error:" in msg
    assert "no interpreter" in msg


def test_validator_has_no_dead_quick_test_runner(tmp_path):
    """``run_quick_tests`` referenced a never-built ``test_nymeria.py --quick``
    harness and had no callers; it was removed (slice 08 F12). Guard against
    re-introduction."""
    validator = CodeValidator(tmp_path)
    assert not hasattr(validator, "run_quick_tests")
