"""Shared helpers, fakes, and step-index constants for the `nymeria init`
wizard test modules (test_setup_wizard_*.py).

Every helper here is a plain function/class called imperatively by the area
modules (the suite uses no pytest fixtures for these); the _*_STEP constants
are shared by the port and interactive Pilot modules.
"""


from __future__ import annotations

import re
from nymeria.onboarding import HostingOption
from nymeria.setup import finalize as finalize_mod
from nymeria.setup import runner as runner_mod
from nymeria.setup.environment import EnvironmentReport
from nymeria.setup.providers import LLMConnectionResult
from nymeria.setup.runner import main as setup_main


__all__ = [
    "_env_report",
    "_conditional_steps",
    "_stub_llm",
    "_active_auth",
    "_FakeCLIProxyClient",
    "_fake_cliproxy_client",
    "_init_state",
    "_read_secrets_key",
    "_capture_console",
    "_first_run",
    "_no_models",
    "_TIER_STEP",
    "_HOSTING_STEP",
    "_API_PORT_STEP",
    "_SECURITY_STEP",
    "_AUTH_STEP",
    "_PROVIDER_STEP",
    "_CONNECTION_STEP",
    "_advance_to_provider",
    "_FakeModelsClient",
    "_install_fake_models_client",
    "_serve_chat_sync",
    "_env_line",
    "_no_checkout",
    "_cliproxy_first_run",
]


def _env_report(**overrides) -> EnvironmentReport:
    """A crafted detection report with benign defaults for gating tests."""
    base: dict = dict(
        os_label="Linux test",
        is_windows=False,
        docker_available=True,
        recommended_hosting=HostingOption.LOCAL,
    )
    base.update(overrides)
    return EnvironmentReport(**base)


def _conditional_steps():
    from nymeria.setup.nav import Step

    return [
        Step("a", lambda _s: True, lambda *_a: None),
        Step("b", lambda _s: True, lambda *_a: None),
        Step("c", lambda s: s.provider == "openai", lambda *_a: None),
        Step("d", lambda _s: True, lambda *_a: None),
    ]


def _stub_llm(monkeypatch):
    calls = []

    def fake(spec, model, api_key, *, base_url=None):
        calls.append((spec.id, model, api_key))
        return LLMConnectionResult(model=model)

    monkeypatch.setattr(finalize_mod, "check_llm_connection_for_spec", fake)
    return calls


def _active_auth(provider: str, *, account: str = "user@example.com") -> dict:
    """An enabled, available auth-file entry as `list_auth_files` reports it.

    `provider` is the proxy's auth-file provider name (claude/codex/xai/...),
    not necessarily the catalog spec id (grok's entries report as xai).
    """
    return {
        "provider": provider,
        "name": f"{provider}-test.json",
        "account": account,
        "disabled": False,
        "unavailable": False,
    }


class _FakeCLIProxyClient:
    """Scripted stand-in for CLIProxyManagementClient in headless wizard tests.

    The non-interactive CLIProxy branch now verifies the proxy login through
    the management API, so every test that runs it must patch this in (via
    `_fake_cliproxy_client`) or the suite would hit whatever real proxy
    answers on the test URL; the real proxy IP-bans after 5 bad management
    attempts. Class-level knobs script the behavior; `calls` records traffic.
    """

    auth_files: list[dict] = []
    knobs: dict = {}
    start_result: dict = {"url": "https://auth.example/login", "state": "s1"}
    status_script: list[str] = []
    login_lands: dict | None = None
    raise_on_list: Exception | None = None
    calls: list[tuple] = []

    def __init__(self, base_url: str, secret: str, **kwargs) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self._secret = secret

    async def list_auth_files(self):
        cls = type(self)
        cls.calls.append(("list_auth_files", None))
        if cls.raise_on_list is not None:
            raise cls.raise_on_list
        return [dict(entry) for entry in cls.auth_files]

    async def start_oauth(self, spec, *, project_id=None):
        cls = type(self)
        cls.calls.append(("start_oauth", spec.id))
        return dict(cls.start_result)

    async def oauth_callback(self, spec, *, redirect_url=None, code=None, state=None):
        type(self).calls.append(("oauth_callback", (spec.id, redirect_url)))

    async def auth_status(self, state):
        cls = type(self)
        cls.calls.append(("auth_status", state))
        status = cls.status_script.pop(0) if cls.status_script else "wait"
        if status == "ok" and cls.login_lands is not None:
            # The login landing server-side makes the auth file appear.
            cls.auth_files = [*cls.auth_files, dict(cls.login_lands)]
        return status

    async def upload_auth_file(self, name, content):
        cls = type(self)
        cls.calls.append(("upload_auth_file", (name, content)))
        if cls.login_lands is not None:
            # An upload lands under the UPLOADED name (verified live:
            # multipart filenames round-trip verbatim into the list); the
            # OAuth path above keeps login_lands verbatim because the
            # proxy names its own OAuth files.
            cls.auth_files = [
                *cls.auth_files,
                {**cls.login_lands, "name": name},
            ]

    async def ensure_tool_prefix_disabled(self, name):
        type(self).calls.append(("ensure_tool_prefix_disabled", name))

    async def get_config_knobs(self, paths=None):
        type(self).calls.append(("get_config_knobs", paths))
        return dict(type(self).knobs)

    async def set_config_knob(self, path, value):
        cls = type(self)
        cls.calls.append(("set_config_knob", (path, value)))
        cls.knobs[path] = value


def _fake_cliproxy_client(
    monkeypatch,
    *,
    auth_files=None,
    knobs=None,
    status_script=None,
    login_lands=None,
    raise_on_list=None,
    start_result=None,
):
    """Reset and patch the fake client into the headless CLIProxy path."""
    from nymeria.setup import cliproxy_login as cliproxy_login_mod

    cls = _FakeCLIProxyClient
    cls.auth_files = [dict(entry) for entry in (auth_files or [])]
    cls.knobs = dict(knobs or {})
    cls.status_script = list(status_script or [])
    cls.login_lands = dict(login_lands) if login_lands else None
    cls.raise_on_list = raise_on_list
    cls.start_result = dict(
        start_result or {"url": "https://auth.example/login", "state": "s1"}
    )
    cls.calls = []
    monkeypatch.setattr(cliproxy_login_mod, "CLIProxyManagementClient", cls)
    return cls


def _init_state(*extra_args):
    parser = runner_mod.build_parser()
    args = parser.parse_args(
        ["--provider", "anthropic", "--model", "m", "--api-key", "k", *extra_args]
    )
    return runner_mod._build_state(args)


def _read_secrets_key(text: str) -> str | None:
    match = re.search(r"^NYMERIA_SECRETS_KEY=(\S+)$", text, re.MULTILINE)
    return match.group(1) if match else None


def _capture_console():
    import io

    from rich.console import Console

    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False), buf


def _first_run(monkeypatch, root, *extra):
    """Write a first-run config + bootstrap profile under ``root`` via the flag path."""
    _stub_llm(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    args = [
        "--provider", "anthropic", "--model", "claude-test-model",
        "--api-key", "sk-ant-x", "--root", str(root),
        "--non-interactive", "--skip-llm-test", *extra,
    ]
    assert setup_main(args) == 0


async def _no_models(*_args, **_kwargs):
    """Stand-in for the live model fetch so Pilot tests never hit the network."""
    return []


# Step indices in the default flow (welcome, tier, hosting, api_port,
# docker_stack, security, auth, the five cliproxy_* branch steps, provider,
# connection, model, ...). docker_stack (4) only applies to a Docker host and
# the cliproxy_* steps (7-11) only to the subscription branch, so on the
# default local API-key path the provider step is index 12. The pilots pick
# "Full setup" on the tier chooser (down + enter) so every step stays
# applicable.
_TIER_STEP = 1


_HOSTING_STEP = 2


_API_PORT_STEP = 3


_SECURITY_STEP = 5


_AUTH_STEP = 6


_PROVIDER_STEP = 12


_CONNECTION_STEP = 13


async def _advance_to_provider(pilot) -> None:
    """Walk welcome -> hosting -> api_port -> security -> auth on the default
    (local) path.

    Accepts every default (local hosting, port 8000, Unleashed security
    profile, Direct API key) and leaves the provider picker focused.
    docker_stack is skipped because the default hosting is not Docker.
    """
    await pilot.press("enter")  # welcome -> tier chooser
    await pilot.pause()
    await pilot.press("down")  # focus Full setup
    await pilot.press("enter")  # pick Full setup -> hosting
    await pilot.pause()
    await pilot.press("enter")  # accept default hosting (local) -> api port
    await pilot.pause()
    await pilot.press("enter")  # accept default port (8000) -> security
    await pilot.pause()
    await pilot.press("enter")  # accept default security profile -> auth method
    await pilot.pause()
    await pilot.press("enter")  # accept default auth (Direct API key) -> provider
    await pilot.pause()


class _FakeModelsClient:
    response_status = 200
    response_body: dict | None = None
    calls: list[dict] = []

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get(self, url, *, headers):
        import httpx

        type(self).calls.append({"url": url, "headers": headers})
        return httpx.Response(
            self.response_status,
            json=self.response_body or {"data": []},
            request=httpx.Request("GET", url),
        )


def _install_fake_models_client(monkeypatch, *, status=200, body=None):
    import httpx

    _FakeModelsClient.calls = []
    _FakeModelsClient.response_status = status
    _FakeModelsClient.response_body = body
    monkeypatch.setattr(httpx, "AsyncClient", _FakeModelsClient)


def _serve_chat_sync(respond):
    """A throwaway local /chat/sync server. Returns (base_url, seen, shutdown).

    `respond(payload)` maps the POSTed body to (status, response_text); DELETEs
    are recorded and answered 204. Runs on an ephemeral port so nothing touches
    a real backend on :8000.
    """
    import http.server
    import json

    seen: list[tuple[str, str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # type: ignore[bad-override]  # noqa: ARG002 (quiet test server)
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            seen.append(("POST", self.path, self.headers.get("Authorization")))
            status, response_text = respond(payload)
            data = json.dumps(
                {
                    "response": response_text,
                    "thread_id": payload.get("thread_id"),
                    "tool_call_count": 0,
                }
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            seen.append(("DELETE", self.path, self.headers.get("Authorization")))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    import threading

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _stop() -> None:
        server.shutdown()
        server.server_close()

    return f"http://127.0.0.1:{server.server_address[1]}", seen, _stop


def _env_line(text: str, key: str) -> str | None:
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith(f"{key}="):
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def _no_checkout(monkeypatch):
    """Simulate a clone-free (pip/uv) install: no source checkout anywhere."""
    monkeypatch.setattr(finalize_mod, "source_checkout_root", lambda: None)


def _cliproxy_first_run(monkeypatch, root, *, provider="claude", auth_provider=None):
    """A completed CLIProxy install under ``root`` (local hosting)."""
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(
        monkeypatch, auth_files=[_active_auth(auth_provider or provider)]
    )
    assert setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", provider,
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", "cpx-gate",
         "--hosting", "local", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    ) == 0
