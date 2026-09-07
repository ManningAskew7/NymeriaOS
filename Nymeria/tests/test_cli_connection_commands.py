from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from cli_fixtures import run
from nymeria.triggers.cli.commands import CommandContext, CommandRegistry, ListCommandOutputSink
from nymeria.triggers.cli.commands import connection
from nymeria.triggers.cli.transport.api import APIAgentClient
from nymeria.triggers.cli.transport.disconnected import DisconnectedAgentClient


def http_status_error(status_code: int, detail: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://api/me")
    response = httpx.Response(status_code, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(detail, request=request, response=response)


class FakeLoginAPI:
    instances: list["FakeLoginAPI"] = []
    health_result = True
    me_result: dict[str, Any] | Exception = {"id": "alice", "role": "user"}
    # Per-token identity overrides: a key listed here fails /me with the given
    # exception (so a freshly issued token can be scripted to fail validation).
    me_result_by_key: dict[str, Exception] = {}
    # The account's token records as GET /me/tokens returns them; by default a
    # long-lived record for the pasted token so the exchange stays inert.
    tokens_result: list[dict[str, Any]] | Exception = []
    issue_result: dict[str, Any] | Exception = {}
    revoke_result: dict[str, Any] | Exception = {"revoked": True}

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.__class__.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.health_result = True
        cls.me_result = {"id": "alice", "role": "user"}
        cls.me_result_by_key = {}
        cls.tokens_result = [token_record("nym_secret", expires_in=timedelta(days=60))]
        cls.issue_result = {}
        cls.revoke_result = {"revoked": True}

    async def health(self) -> bool:
        self.calls.append(("health", {}))
        return self.health_result

    async def get_me(self, act_as: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_me", {"act_as": act_as}))
        if self.api_key in self.me_result_by_key:
            raise self.me_result_by_key[self.api_key]
        if isinstance(self.me_result, Exception):
            raise self.me_result
        return self.me_result

    async def list_my_tokens(self, user_id: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_my_tokens", {"user_id": user_id}))
        if isinstance(self.tokens_result, Exception):
            raise self.tokens_result
        return [dict(record) for record in self.tokens_result]

    async def issue_my_token(
        self, label: str | None = None, user_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("issue_my_token", {"label": label, "user_id": user_id}))
        if isinstance(self.issue_result, Exception):
            raise self.issue_result
        return dict(self.issue_result)

    async def revoke_my_token(
        self, token_hash_prefix: str, user_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("revoke_my_token", {"token_hash_prefix": token_hash_prefix, "user_id": user_id})
        )
        if isinstance(self.revoke_result, Exception):
            raise self.revoke_result
        return dict(self.revoke_result)

    async def close(self) -> None:
        self.closed = True


def token_record(
    raw_token: str,
    *,
    expires_in: timedelta,
    label: str | None = "bootstrap",
) -> dict[str, Any]:
    """A GET /me/tokens record for ``raw_token``, keyed the way the API keys
    it: by the leading characters of the token's sha256 hash."""
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return {
        "token_hash_prefix": digest[:8],
        "label": label,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + expires_in).isoformat(timespec="seconds"),
        "last_used_at": None,
        "revoked_at": None,
    }


def _entered_expiry(record: dict[str, Any]) -> str:
    """``record``'s expiry rendered the way the success line names it."""
    return f"{datetime.fromisoformat(record['expires_at']).astimezone(timezone.utc):%Y-%m-%d %H:%M UTC}"


def exchange_calls(api: FakeLoginAPI) -> list[tuple[str, dict[str, Any]]]:
    return [
        call
        for call in api.calls
        if call[0] in {"list_my_tokens", "issue_my_token", "revoke_my_token"}
    ]


def make_registry() -> CommandRegistry:
    registry = CommandRegistry(include_builtins=False)
    connection.register(registry)
    return registry


def make_context(
    *,
    config_path,
    actions: list[Any],
    prompts: list[str] | None = None,
    token: str = "nym_secret",
    secret_calls: list[str] | None = None,
    prompt_calls: list[str] | None = None,
    client: Any | None = None,
) -> CommandContext:
    prompt_values = iter(["http://api"] if prompts is None else prompts)

    def prompt_handler(prompt: str) -> str:
        if prompt_calls is not None:
            prompt_calls.append(prompt)
        return next(prompt_values)

    def secret_handler(prompt: str) -> str:
        if secret_calls is not None:
            secret_calls.append(prompt)
        return token

    return CommandContext(
        client=client,
        output=ListCommandOutputSink(),
        dispatch_state=actions.append,
        prompt_handler=prompt_handler,
        secret_prompt_handler=secret_handler,
        thread_id="thread-1",
        user_id="default",
        metadata={
            "api_client_factory": FakeLoginAPI,
            "cli_config_path": config_path,
        },
    )


def test_login_validates_saves_profile_and_replaces_client(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    assert "nym_secret" not in result.messages[0].content
    assert actions[0]["type"] == "replace_client"
    assert isinstance(actions[0]["client"], APIAgentClient)
    assert actions[0]["user_id"] == "alice"
    # Validation, then the token-lifetime check that decides whether to swap
    # the pasted token for a long-lived one (a no-op for this 60-day record).
    assert FakeLoginAPI.instances[0].calls == [
        ("health", {}),
        ("get_me", {"act_as": "alice"}),
        ("list_my_tokens", {"user_id": None}),
    ]
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_url"] == "http://api"
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert data["profiles"]["default"]["user_id"] == "alice"


def test_connect_alias_uses_login_handler(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    registry = make_registry()
    context = make_context(
        config_path=tmp_path / ".nymeria" / "cli.json",
        actions=actions,
        prompts=[],
    )

    result = run(registry.dispatch_async(context, "/connect http://api --user-id alice"))

    assert result.ok is True
    assert actions[0]["type"] == "replace_client"


def test_login_failure_does_not_save_profile(tmp_path) -> None:
    FakeLoginAPI.reset()
    FakeLoginAPI.health_result = False
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is False
    assert result.error_code == "api_unavailable"
    assert actions == []
    assert not config_path.exists()
    assert FakeLoginAPI.instances[0].closed is True


def test_login_reuses_saved_token_for_local_url(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    # First login saves a loopback profile (prompts for the token: none saved yet).
    ctx1 = make_context(
        config_path=config_path,
        actions=actions,
        prompts=["http://127.0.0.1:8098"],
    )
    assert run(registry.dispatch_async(ctx1, "/login --user-id alice")).ok is True

    # Change only the port to another local URL: the saved token must be reused.
    actions.clear()
    FakeLoginAPI.instances.clear()
    secret_calls: list[str] = []
    ctx2 = make_context(
        config_path=config_path,
        actions=actions,
        prompts=[],
        secret_calls=secret_calls,
    )
    result = run(
        registry.dispatch_async(ctx2, "/login http://127.0.0.1:8000 --user-id alice")
    )

    assert result.ok is True
    assert secret_calls == []  # reused the saved token; no prompt
    assert actions[0]["type"] == "replace_client"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_url"] == "http://127.0.0.1:8000"
    assert data["profiles"]["default"]["api_key"] == "nym_secret"


def test_login_remote_url_still_prompts_for_token(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    ctx1 = make_context(
        config_path=config_path,
        actions=actions,
        prompts=["http://127.0.0.1:8098"],
    )
    assert run(registry.dispatch_async(ctx1, "/login")).ok is True

    actions.clear()
    FakeLoginAPI.instances.clear()
    secret_calls: list[str] = []
    ctx2 = make_context(
        config_path=config_path,
        actions=actions,
        prompts=[],
        secret_calls=secret_calls,
    )
    result = run(registry.dispatch_async(ctx2, "/login https://remote.example.com:8000"))

    assert result.ok is True
    # A new remote host must still prompt: never silently resend the saved token.
    assert secret_calls != []


def test_login_cancelled_at_a_prompt_reports_cancellation(tmp_path) -> None:
    """EOF (Ctrl+D) at the URL or token prompt used to surface as an empty
    "Command failed:" (the EOFError's empty str); it is a cancellation. Ctrl+C
    never reaches the command inside the REPL (its keymap clears the line)."""
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()

    def eof(prompt: str) -> str:
        raise EOFError

    ctx = make_context(config_path=config_path, actions=actions, prompts=[])
    ctx.secret_prompt_handler = eof
    result = run(registry.dispatch_async(ctx, "/login https://remote.example.com:8000"))
    assert result.ok is False
    assert result.error_code == "login_cancelled"
    assert "cancelled" in str(result.messages[0].content).lower()
    assert FakeLoginAPI.instances == []

    ctx = make_context(config_path=config_path, actions=actions, prompts=[])
    ctx.prompt_handler = eof
    result = run(registry.dispatch_async(ctx, "/login"))
    assert result.error_code == "login_cancelled"


def test_login_reuse_falls_back_to_prompt_on_auth_error(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    ctx1 = make_context(
        config_path=config_path,
        actions=actions,
        prompts=["http://127.0.0.1:8098"],
    )
    assert run(registry.dispatch_async(ctx1, "/login")).ok is True

    # The saved token is now rejected by the backend on the new port.
    actions.clear()
    FakeLoginAPI.instances.clear()
    FakeLoginAPI.me_result = http_status_error(401, "Token revoked")
    secret_calls: list[str] = []
    ctx2 = make_context(
        config_path=config_path,
        actions=actions,
        prompts=[],
        secret_calls=secret_calls,
    )
    result = run(registry.dispatch_async(ctx2, "/login http://127.0.0.1:8000"))

    # Reuse hit a 401, so it falls back to prompting for a fresh token.
    assert secret_calls != []
    assert result.ok is False
    assert result.error_code == "api_auth_error"


def test_reconnect_reuses_saved_profile_without_reprompting(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)
    assert run(registry.dispatch_async(context, "/login --user-id alice")).ok is True

    actions.clear()
    FakeLoginAPI.instances.clear()
    result = run(registry.dispatch_async(context, "/reconnect"))

    assert result.ok is True
    assert "Reconnected to http://api as alice" in result.messages[0].content
    assert actions[0]["type"] == "replace_client"
    assert isinstance(actions[0]["client"], APIAgentClient)
    assert actions[0]["user_id"] == "alice"
    # Reuses the saved url + token directly: validates, never prompts.
    assert FakeLoginAPI.instances[0].calls == [
        ("health", {}),
        ("get_me", {"act_as": "alice"}),
    ]


def test_reconnect_without_saved_profile_fails(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    registry = make_registry()
    context = make_context(
        config_path=tmp_path / ".nymeria" / "cli.json",
        actions=actions,
    )

    result = run(registry.dispatch_async(context, "/reconnect"))

    assert result.ok is False
    assert result.error_code == "reconnect_no_profile"
    assert actions == []
    assert FakeLoginAPI.instances == []


def test_reconnect_reports_backend_still_down(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)
    assert run(registry.dispatch_async(context, "/login --user-id alice")).ok is True

    actions.clear()
    FakeLoginAPI.instances.clear()
    FakeLoginAPI.health_result = False
    result = run(registry.dispatch_async(context, "/reconnect"))

    assert result.ok is False
    assert result.error_code == "api_unavailable"
    assert actions == []
    assert FakeLoginAPI.instances[0].closed is True


def test_logout_removes_active_profile_and_disconnects(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)
    assert run(registry.dispatch_async(context, "/login --user-id alice")).ok is True

    actions.clear()
    result = run(registry.dispatch_async(context, "/logout"))

    assert result.ok is True
    assert actions[0]["type"] == "replace_client"
    assert isinstance(actions[0]["client"], DisconnectedAgentClient)
    assert "nym_secret" not in config_path.read_text(encoding="utf-8")


# --- bootstrap-token exchange (spec A1-A3) ------------------------------------


def test_login_exchanges_short_lived_token_for_long_lived_one(tmp_path, monkeypatch) -> None:
    FakeLoginAPI.reset()
    # The pasted token is the 24h bootstrap; a decoy long-lived record sits
    # first in the list so the match must be by hash, not by position.
    FakeLoginAPI.tokens_result = [
        token_record("nym_someone_elses", expires_in=timedelta(days=80), label="desktop"),
        token_record("nym_secret", expires_in=timedelta(hours=20)),
    ]
    FakeLoginAPI.issue_result = {
        "raw_token": "nym_durable",
        "metadata": token_record("nym_durable", expires_in=timedelta(days=90), label="x"),
    }
    monkeypatch.setattr(connection.socket, "gethostname", lambda: "devbox")
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    # The long-lived token is what lands on disk; the bootstrap never does.
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_key"] == "nym_durable"
    assert "nym_secret" not in config_path.read_text(encoding="utf-8")
    # The exchange runs as the token's owner (no act-as), labelled for this host.
    bootstrap_api = FakeLoginAPI.instances[0]
    assert exchange_calls(bootstrap_api) == [
        ("list_my_tokens", {"user_id": None}),
        ("issue_my_token", {"label": "nymeria-cli devbox", "user_id": None}),
    ]
    # The live session moves to the new token too (validated before it is
    # trusted), and the bootstrap-token client is closed.
    live = actions[0]["client"]
    assert isinstance(live, APIAgentClient)
    assert live.api.api_key == "nym_durable"
    assert live.api.calls == [("health", {}), ("get_me", {"act_as": "alice"})]
    assert bootstrap_api.closed is True
    # One line tells the user what happened, naming when the token they typed
    # runs out rather than calling it "the short-lived one you entered" (which
    # would misdescribe a long-lived token renewed in its final week). The raw
    # tokens are never echoed.
    texts = [message.content for message in result.messages]
    assert texts[0] == "Connected to http://api as alice."
    assert len(texts) == 2
    entered = _entered_expiry(FakeLoginAPI.tokens_result[1])
    assert texts[1] == (
        f"The token you entered expires {entered}, so a long-lived CLI token "
        "(label nymeria-cli devbox) was issued and saved instead."
    )
    assert "short-lived one you entered" not in texts[1]
    assert "nym_durable" not in texts[1] and "nym_secret" not in texts[1]


def test_login_keeps_a_long_lived_token_without_exchanging(tmp_path) -> None:
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = [token_record("nym_secret", expires_in=timedelta(days=60))]
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert exchange_calls(FakeLoginAPI.instances[0]) == [("list_my_tokens", {"user_id": None})]
    assert len(FakeLoginAPI.instances) == 1
    assert actions[0]["client"].api.api_key == "nym_secret"
    assert [message.content for message in result.messages] == [
        "Connected to http://api as alice."
    ]


@pytest.mark.parametrize(
    "tokens_result, issue_result",
    [
        # POST /me/tokens 5xx.
        (
            [token_record("nym_secret", expires_in=timedelta(hours=20))],
            http_status_error(500, "boom"),
        ),
        # POST /me/tokens 4xx (token cap reached).
        (
            [token_record("nym_secret", expires_in=timedelta(hours=20))],
            http_status_error(409, "Token limit exceeded"),
        ),
        # GET /me/tokens unreachable: the token's lifetime cannot be checked.
        (httpx.ConnectError("connection refused"), {}),
        # The token that just authenticated is missing from its own list.
        ([token_record("nym_other", expires_in=timedelta(days=60))], {}),
        # A malformed exchange response (no raw token).
        ([token_record("nym_secret", expires_in=timedelta(hours=20))], {"metadata": {}}),
    ],
    ids=["issue-5xx", "issue-4xx", "list-network", "record-missing", "issue-empty"],
)
def test_login_exchange_failure_keeps_pasted_token_and_warns(
    tmp_path, tokens_result, issue_result
) -> None:
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = tokens_result
    FakeLoginAPI.issue_result = issue_result
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    # Login itself still succeeds on the pasted token...
    assert result.ok is True
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert actions[0]["type"] == "replace_client"
    assert actions[0]["client"].api.api_key == "nym_secret"
    assert FakeLoginAPI.instances[0].closed is False
    # ...but never silently: the warning names the manual fix.
    assert result.messages[0].content == "Connected to http://api as alice."
    warning = result.messages[1]
    assert warning.level == "warning"
    assert "/account tokens issue" in warning.content
    assert "nym_secret" not in warning.content


def test_login_exchange_keeps_pasted_token_when_new_token_fails_validation(tmp_path) -> None:
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = [token_record("nym_secret", expires_in=timedelta(hours=20))]
    FakeLoginAPI.issue_result = {"raw_token": "nym_durable", "metadata": {}}
    FakeLoginAPI.me_result_by_key = {"nym_durable": http_status_error(401, "Invalid API key")}
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    # An issued token that does not authenticate is never persisted.
    assert result.ok is True
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert actions[0]["client"].api.api_key == "nym_secret"
    assert result.messages[1].level == "warning"
    assert "/account tokens issue" in result.messages[1].content
    # The rejected token's client was closed, the working one kept open.
    by_key = {api.api_key: api for api in FakeLoginAPI.instances}
    assert by_key["nym_durable"].closed is True
    assert by_key["nym_secret"].closed is False


# --- default URL (spec A5) -----------------------------------------------------


def test_login_prompt_defaults_to_the_instance_the_cli_was_launched_against(tmp_path) -> None:
    FakeLoginAPI.reset()
    actions: list[Any] = []
    prompt_calls: list[str] = []
    registry = make_registry()
    # A disconnected placeholder retaining the launch-time connection (backend
    # not up yet at start): /login must offer THAT url, not localhost:8000.
    placeholder = DisconnectedAgentClient(
        default_user_id="alice",
        reconnect_api_url="http://127.0.0.1:8010",
        reconnect_api_key="stale",
        reconnect_user_id="alice",
    )
    context = make_context(
        config_path=tmp_path / ".nymeria" / "cli.json",
        actions=actions,
        prompts=[""],  # accept the default
        prompt_calls=prompt_calls,
        client=placeholder,
    )

    result = run(registry.dispatch_async(context, "/login"))

    assert result.ok is True
    assert prompt_calls == ["API URL [http://127.0.0.1:8010]: "]
    assert result.payload["api_url"] == "http://127.0.0.1:8010"
    assert FakeLoginAPI.instances[0].base_url == "http://127.0.0.1:8010"


def test_login_prompt_default_follows_the_configured_api_port(tmp_path, monkeypatch) -> None:
    FakeLoginAPI.reset()
    monkeypatch.delenv("NYMERIA_API_URL", raising=False)
    monkeypatch.setenv("API_PORT", "8010")
    actions: list[Any] = []
    prompt_calls: list[str] = []
    registry = make_registry()
    context = make_context(
        config_path=tmp_path / ".nymeria" / "cli.json",
        actions=actions,
        prompts=[""],
        prompt_calls=prompt_calls,
    )

    result = run(registry.dispatch_async(context, "/login"))

    assert result.ok is True
    assert prompt_calls == ["API URL [http://localhost:8010]: "]
    assert result.payload["api_url"] == "http://localhost:8010"


def test_login_prompt_default_prefers_the_live_connection(tmp_path, monkeypatch) -> None:
    FakeLoginAPI.reset()
    monkeypatch.setenv("API_PORT", "8010")
    actions: list[Any] = []
    prompt_calls: list[str] = []
    registry = make_registry()
    live = APIAgentClient(
        FakeLoginAPI(base_url="https://remote.example.com:8000", api_key="nym_live"),
        default_user_id="alice",
    )
    FakeLoginAPI.instances.clear()
    context = make_context(
        config_path=tmp_path / ".nymeria" / "cli.json",
        actions=actions,
        prompts=[""],
        prompt_calls=prompt_calls,
        client=live,
    )

    result = run(registry.dispatch_async(context, "/login"))

    assert result.ok is True
    assert prompt_calls == ["API URL [https://remote.example.com:8000]: "]
    assert result.payload["api_url"] == "https://remote.example.com:8000"


# --- a token issued but not adopted is never left orphaned -------------------


def test_login_revokes_the_token_it_issued_when_it_cannot_be_adopted(
    tmp_path, monkeypatch
) -> None:
    """POST /me/tokens has already created the token by the time validation
    fails, and an account holds only ten active tokens: leaving one behind per
    failed login walks the user into the cap with tokens nobody holds."""
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = [token_record("nym_secret", expires_in=timedelta(hours=20))]
    issued = token_record("nym_durable", expires_in=timedelta(days=90), label="cli")
    FakeLoginAPI.issue_result = {"raw_token": "nym_durable", "metadata": issued}
    FakeLoginAPI.me_result_by_key = {"nym_durable": http_status_error(401, "Invalid API key")}
    monkeypatch.setattr(connection.socket, "gethostname", lambda: "devbox")
    actions: list[Any] = []
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=actions)

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    # The orphan is revoked by the hash prefix the issue response reported,
    # over the still-working pasted-token client.
    assert exchange_calls(FakeLoginAPI.instances[0]) == [
        ("list_my_tokens", {"user_id": None}),
        ("issue_my_token", {"label": "nymeria-cli devbox", "user_id": None}),
        (
            "revoke_my_token",
            {"token_hash_prefix": issued["token_hash_prefix"], "user_id": None},
        ),
    ]
    # ...and the pasted token is still what was saved, with the warning intact.
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["profiles"]["default"]["api_key"] == "nym_secret"
    assert result.messages[1].level == "warning"
    assert "/account tokens issue" in result.messages[1].content


def test_login_revokes_an_orphan_addressed_by_its_own_hash_when_metadata_is_thin(
    tmp_path,
) -> None:
    """A response without usable metadata still carries the raw token, and the
    API addresses a token by a prefix of its sha256 hash: the whole hash is
    one, so the orphan is still reachable."""
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = [token_record("nym_secret", expires_in=timedelta(hours=20))]
    FakeLoginAPI.issue_result = {"raw_token": "nym_durable", "metadata": {}}
    FakeLoginAPI.me_result_by_key = {"nym_durable": http_status_error(401, "Invalid API key")}
    registry = make_registry()
    context = make_context(config_path=tmp_path / ".nymeria" / "cli.json", actions=[])

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    revokes = [
        call for call in FakeLoginAPI.instances[0].calls if call[0] == "revoke_my_token"
    ]
    assert revokes == [
        (
            "revoke_my_token",
            {
                "token_hash_prefix": hashlib.sha256(b"nym_durable").hexdigest(),
                "user_id": None,
            },
        )
    ]


def test_login_revoke_failure_never_masks_the_exchange_warning(tmp_path) -> None:
    """The cleanup is best effort: a revoke that itself fails must not replace
    the warning about the token the user is actually left holding."""
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = [token_record("nym_secret", expires_in=timedelta(hours=20))]
    FakeLoginAPI.issue_result = {
        "raw_token": "nym_durable",
        "metadata": token_record("nym_durable", expires_in=timedelta(days=90), label="cli"),
    }
    FakeLoginAPI.me_result_by_key = {"nym_durable": http_status_error(401, "Invalid API key")}
    FakeLoginAPI.revoke_result = http_status_error(500, "boom")
    config_path = tmp_path / ".nymeria" / "cli.json"
    registry = make_registry()
    context = make_context(config_path=config_path, actions=[])

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["profiles"]["default"][
        "api_key"
    ] == "nym_secret"
    assert [message.level for message in result.messages] == ["success", "warning"]
    assert "/account tokens issue" in result.messages[1].content


def test_login_does_not_revoke_when_no_token_was_issued(tmp_path) -> None:
    """A failed POST /me/tokens left nothing behind: do not fire a revoke at a
    token that does not exist."""
    FakeLoginAPI.reset()
    FakeLoginAPI.tokens_result = [token_record("nym_secret", expires_in=timedelta(hours=20))]
    FakeLoginAPI.issue_result = http_status_error(409, "Token limit exceeded")
    registry = make_registry()
    context = make_context(config_path=tmp_path / ".nymeria" / "cli.json", actions=[])

    result = run(registry.dispatch_async(context, "/login --user-id alice"))

    assert result.ok is True
    assert [call[0] for call in exchange_calls(FakeLoginAPI.instances[0])] == [
        "list_my_tokens",
        "issue_my_token",
    ]
