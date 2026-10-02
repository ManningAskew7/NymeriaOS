from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from nymeria import doctor
from nymeria.config import settings as settings_module


@dataclass
class FakeSettings:
    data_dir: Path
    llm_provider: str = "anthropic"
    llm_model: str = "claude-test"
    llm_base_url: str | None = None
    anthropic_api_key: str | None = "sk-ant-test"
    anthropic_direct_api_key: str | None = None
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    openai_api_mode: str = "responses"
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    tts_provider: str = "none"
    stt_provider: str = "none"
    api_host: str = "127.0.0.1"
    api_port: int = 9876

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nymeria.db"

    def get_api_key_for_provider(self) -> str | None:
        if self.llm_provider == "anthropic":
            return self.anthropic_api_key
        if self.llm_provider == "openai":
            return self.openai_api_key
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key
        return None


def _result(results: list[doctor.CheckResult], name: str) -> doctor.CheckResult:
    return next(result for result in results if result.name == name)


def _write_sqlite_state(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data_dir / "nymeria.db") as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        conn.executemany(
            "INSERT INTO checkpoints (thread_id) VALUES (?)",
            [("thread-a",), ("thread-a",), ("thread-b",)],
        )
        conn.commit()
    with sqlite3.connect(data_dir / "accounts.db") as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users (id) VALUES ('default')")
        conn.commit()


def _stub_static_checks(monkeypatch, tmp_path: Path, settings: FakeSettings) -> None:
    config_path = tmp_path / "config.env"
    config_path.write_text("LLM_PROVIDER=anthropic\n", encoding="utf-8")
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(doctor, "get_env_file_paths", lambda _root: (config_path,))
    monkeypatch.setattr(doctor, "_frontend_candidates", lambda: (frontend_dir,))
    monkeypatch.setattr(doctor, "get_settings", lambda: settings)
    monkeypatch.setattr(doctor, "_probe_llm_connection", lambda _config: None)
    monkeypatch.setattr(doctor, "_port_in_use", lambda _host, _port: False)


def test_collect_checks_reports_core_installation_status(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_sqlite_state(data_dir)
    settings = FakeSettings(data_dir=data_dir)
    _stub_static_checks(monkeypatch, tmp_path, settings)

    results = doctor.collect_checks(argparse.Namespace(skip_llm_test=False))

    assert _result(results, "Config").status == "pass"
    assert _result(results, "Data dir").status == "pass"
    assert _result(results, "LLM").status == "pass"
    assert _result(results, "Database").status == "pass"
    assert "2 thread(s)" in _result(results, "Database").detail
    assert "1 user(s)" in _result(results, "Database").detail
    assert _result(results, "Redis").status == "warn"
    assert _result(results, "Voice").status == "warn"
    assert _result(results, "Frontend").status == "pass"
    assert _result(results, "Port").status == "pass"


def test_collect_checks_allows_fresh_sqlite_checkpoint_to_be_created_later(
    monkeypatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    with sqlite3.connect(data_dir / "accounts.db") as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users (id) VALUES ('default')")
        conn.commit()
    settings = FakeSettings(data_dir=data_dir)
    _stub_static_checks(monkeypatch, tmp_path, settings)

    results = doctor.collect_checks(argparse.Namespace(skip_llm_test=True))

    database = _result(results, "Database")
    assert database.status == "pass"
    assert "checkpoint DB not created yet" in database.detail
    assert "1 user(s)" in database.detail


def test_collect_checks_fails_when_llm_key_is_missing(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_sqlite_state(data_dir)
    settings = FakeSettings(data_dir=data_dir, anthropic_api_key=None)
    _stub_static_checks(monkeypatch, tmp_path, settings)

    def fail_if_called(_config):
        raise AssertionError("LLM probe should not run without an API key")

    monkeypatch.setattr(doctor, "_probe_llm_connection", fail_if_called)

    results = doctor.collect_checks(argparse.Namespace(skip_llm_test=False))

    llm = _result(results, "LLM")
    assert llm.status == "fail"
    assert "no configured API key" in llm.detail


def test_collect_checks_can_target_specific_project_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_DIRECT_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "NYMERIA_DATA_DIR",
        "SQLITE_PATH",
        "DATABASE_BACKEND",
        "REDIS_ENABLED",
        "REDIS_URL",
        "API_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime_root = tmp_path / "runtime"
    data_dir = runtime_root / "data"
    runtime_root.mkdir()
    _write_sqlite_state(data_dir)
    (runtime_root / "config.env").write_text(
        "\n".join(
            [
                "LLM_PROVIDER=anthropic",
                "LLM_MODEL=claude-from-runtime",
                "ANTHROPIC_API_KEY=sk-ant-runtime",
                f"NYMERIA_DATA_DIR={data_dir}",
                "DATABASE_BACKEND=sqlite",
                "API_PORT=9877",
                "",
            ]
        ),
        encoding="utf-8",
    )
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    original_project_root = settings_module.PROJECT_ROOT

    monkeypatch.setattr(doctor, "_frontend_candidates", lambda: (frontend_dir,))
    monkeypatch.setattr(doctor, "_port_in_use", lambda _host, _port: False)

    def fail_if_called(_config):
        raise AssertionError("project-root override test skips the LLM probe")

    monkeypatch.setattr(doctor, "_probe_llm_connection", fail_if_called)

    results = doctor.collect_checks(
        argparse.Namespace(skip_llm_test=True, project_root=runtime_root)
    )

    assert settings_module.PROJECT_ROOT == original_project_root
    assert _result(results, "Config").status == "pass"
    assert str(runtime_root / "config.env") in _result(results, "Config").detail
    assert _result(results, "Data dir").status == "pass"
    assert _result(results, "Database").status == "pass"
    llm = _result(results, "LLM")
    assert llm.status == "warn"
    assert "anthropic/claude-from-runtime connection test skipped" in llm.detail


def test_run_doctor_returns_nonzero_for_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda _args: [doctor.CheckResult("Example", "fail", "broken")],
    )

    assert doctor.run_doctor(argparse.Namespace(skip_llm_test=True)) == 1


# --- web search default-toolset check (2026-08-30) ---------------------------


def _write_profile(data_dir: Path, default_thread_tools) -> None:
    profile_dir = data_dir / "users" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    import json

    (profile_dir / "profile.json").write_text(
        json.dumps(
            {
                "user_id": "default",
                "tool_preferences": {"default_thread_tools": default_thread_tools},
            }
        ),
        encoding="utf-8",
    )


def test_web_search_check_passes_on_fresh_install_defaults(tmp_path: Path) -> None:
    # No profile on disk: the fresh-install defaults apply (keyless ddgs +
    # nymeria fetch), which pass with upgrade guidance rather than a warning.
    settings = FakeSettings(data_dir=tmp_path / "data")
    result = doctor._check_web_search(settings)
    assert result.status == "pass"
    assert "web_search_ddgs" in result.detail


def test_web_search_check_warns_when_defaults_have_no_search(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_profile(data_dir, ["bash_execute", "fetch_url_nymeria"])
    result = doctor._check_web_search(FakeSettings(data_dir=data_dir))
    assert result.status == "warn"
    assert "no web_search_" in result.detail


def test_web_search_check_warns_on_link_only_search_without_fetch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_profile(data_dir, ["bash_execute", "web_search_tavily"])
    result = doctor._check_web_search(FakeSettings(data_dir=data_dir))
    assert result.status == "warn"
    assert "fetch_url" in result.detail
    # Perplexity is self-sufficient: no fetch needed, no warning.
    _write_profile(data_dir, ["bash_execute", "web_search_perplexity"])
    ok = doctor._check_web_search(FakeSettings(data_dir=data_dir))
    assert ok.status == "pass"
    assert ok.detail == "web_search_perplexity"


def test_web_search_check_is_wired_into_settings_checks(tmp_path: Path) -> None:
    results: list[doctor.CheckResult] = []
    doctor._append_settings_checks(
        results,
        FakeSettings(data_dir=tmp_path / "data"),
        argparse.Namespace(skip_llm_test=True),
    )
    assert any(result.name == "Web search" for result in results)


# --- #101 entries 17 and 21 (2026-08-24) ---------------------------------------


MINT_COMMAND = (
    'python3 -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _real_secrets_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def test_secrets_key_check_warns_when_unset_or_malformed_and_passes_when_valid(
    monkeypatch,
) -> None:
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    missing = doctor._check_secrets_key()
    assert missing.status == "warn"
    assert "is not set" in missing.detail
    assert MINT_COMMAND in missing.detail

    # The bytes repr a bare print(Fernet.generate_key()) writes: set, unusable.
    malformed = "b'" + "A" * 43 + "='"
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", malformed)
    invalid = doctor._check_secrets_key()
    assert invalid.status == "warn"
    assert "not a valid key" in invalid.detail
    assert MINT_COMMAND in invalid.detail
    assert malformed not in invalid.detail

    monkeypatch.setenv("NYMERIA_SECRETS_KEY", _real_secrets_key())
    assert doctor._check_secrets_key().status == "pass"


def test_secrets_key_check_reads_another_roots_own_env_files(monkeypatch, tmp_path: Path) -> None:
    # The wizard's final doctor inspects the root it just configured, whose key
    # may live only in that root's files.
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "config.env").write_text(
        f"NYMERIA_SECRETS_KEY={_real_secrets_key()}\n", encoding="utf-8"
    )

    assert doctor._check_secrets_key(project_root=other_root).status == "pass"
    # This process's own root (no override) does not read another root's files.
    assert doctor._check_secrets_key().status == "warn"

    (other_root / "config.env").write_text("NYMERIA_SECRETS_KEY=nope\n", encoding="utf-8")
    assert "not a valid key" in doctor._check_secrets_key(project_root=other_root).detail


def _fake_sentence_transformers(monkeypatch) -> dict[str, bool]:
    import importlib.util

    real_find_spec = importlib.util.find_spec
    present = {"value": False}

    def fake_find_spec(name, *args, **kwargs):
        if name == "sentence_transformers":
            return object() if present["value"] else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    return present


@dataclass
class RagSettings(FakeSettings):
    embedding_provider: str = "local"
    rag_rerank_provider: str = "llm"
    rag_rerank_enabled: bool = False


def test_local_rag_check_warns_only_when_the_runtime_would_load_a_missing_model(
    monkeypatch, tmp_path: Path
) -> None:
    present = _fake_sentence_transformers(monkeypatch)

    local = doctor._check_local_rag(RagSettings(data_dir=tmp_path))
    assert local is not None and local.status == "warn"
    assert "EMBEDDING_PROVIDER=local" in local.detail
    assert "without embeddings" in local.detail
    assert "nymeriaos[local-rag]" in local.detail

    reranker = doctor._check_local_rag(
        RagSettings(
            data_dir=tmp_path,
            embedding_provider="openai",
            rag_rerank_provider="local",
            rag_rerank_enabled=True,
        )
    )
    assert reranker is not None and reranker.status == "warn"
    assert "RAG_RERANK_PROVIDER=local" in reranker.detail
    assert "not reranked" in reranker.detail
    assert "EMBEDDING_PROVIDER" not in reranker.detail

    # Reranking off: the local reranker is never reached, so nothing to check.
    rerank_off = RagSettings(
        data_dir=tmp_path, embedding_provider="openai", rag_rerank_provider="local"
    )
    assert doctor._check_local_rag(rerank_off) is None

    present["value"] = True
    assert doctor._check_local_rag(RagSettings(data_dir=tmp_path)).status == "pass"

    hosted = RagSettings(data_dir=tmp_path, embedding_provider="openai")
    assert doctor._check_local_rag(hosted) is None  # nothing local to check


def test_new_checks_are_wired_into_settings_checks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    _fake_sentence_transformers(monkeypatch)

    results: list[doctor.CheckResult] = []
    doctor._append_settings_checks(
        results, RagSettings(data_dir=tmp_path / "data"), argparse.Namespace(skip_llm_test=True)
    )
    assert _result(results, "Secrets key").status == "warn"
    assert _result(results, "Local RAG").status == "warn"


def test_another_roots_doctor_skips_the_local_rag_row(monkeypatch, tmp_path: Path) -> None:
    # The extra is a property of THIS interpreter; the root the wizard just
    # configured may run in a Docker image with its own Python.
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    _fake_sentence_transformers(monkeypatch)
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "config.env").write_text(
        f"NYMERIA_SECRETS_KEY={_real_secrets_key()}\n", encoding="utf-8"
    )

    results: list[doctor.CheckResult] = []
    doctor._append_settings_checks(
        results,
        RagSettings(data_dir=tmp_path / "data"),
        argparse.Namespace(skip_llm_test=True, project_root=other_root),
    )
    names = {result.name for result in results}
    assert "Local RAG" not in names
    # ...while the key check follows the override to that root's files.
    assert _result(results, "Secrets key").status == "pass"


# --- #434: the Settings file row ---------------------------------------------


@pytest.fixture
def booted_container(tmp_path: Path, monkeypatch):
    """Doctor run INSIDE a container: the process loads the runtime file.
    ``boot(text, **env)`` sets the compose env (None = unset), writes the
    file, and runs the real boot load."""
    settings_module.reset_env_loading_state_for_tests()
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", app)
    monkeypatch.setattr(settings_module, "_PROCESS_ROOT", app)
    monkeypatch.setattr(settings_module, "_env_file_loading_suppressed", False)
    runtime = tmp_path / "data" / "settings.env"
    runtime.parent.mkdir()
    monkeypatch.setenv("NYMERIA_SETTINGS_FILE", str(runtime))

    def boot(text: str, **env: str | None) -> None:
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        runtime.write_text(text, encoding="utf-8")
        settings_module.reset_env_loading_state_for_tests()
        settings_module.load_env_files_into_environ(force=True)

    yield app, runtime, boot
    settings_module.reset_env_loading_state_for_tests()


def test_settings_file_row_warns_when_a_credential_or_route_is_shadowed(booted_container):
    app, runtime, boot = booted_container
    boot(
        "OPENAI_API_KEY=sk-dummy-434\nUSER_TIMEZONE=Australia/Sydney\n"
        "TWITCH_CHANNEL=only-in-app\n",
        OPENAI_API_KEY="cpx-dummy-434", USER_TIMEZONE="UTC", TWITCH_CHANNEL=None,
    )

    row = doctor._check_settings_file(app, own_root=True)

    assert row is not None and row.name == "Settings file"
    assert row.status == "warn"
    assert str(runtime) in row.detail
    assert "overrides values set elsewhere: OPENAI_API_KEY (credential), USER_TIMEZONE" in row.detail
    assert "/settings clear <KEY>" in row.detail
    assert "save it as OPENAI_DIRECT_API_KEY first" in row.detail
    # Saved only in the app: listed here (and only here), never a warning.
    assert "saved only in the app: TWITCH_CHANNEL" in row.detail
    assert "dummy-434" not in row.detail


def test_settings_file_row_passes_for_plain_setting_overrides(booted_container):
    app, _runtime, boot = booted_container
    boot(
        "USER_TIMEZONE=Australia/Sydney\nLLM_BASE_URL=http://proxy:8317/v1\n",
        USER_TIMEZONE="UTC", LLM_BASE_URL="",  # compose ${VAR:-}: app-only
    )

    row = doctor._check_settings_file(app, own_root=True)

    assert row is not None and row.status == "pass"
    assert "overrides values set elsewhere: USER_TIMEZONE" in row.detail
    assert "saved only in the app: LLM_BASE_URL (route)" in row.detail


def test_settings_file_row_points_a_docker_host_at_where_the_file_is_visible(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("NYMERIA_SETTINGS_FILE", raising=False)
    docker_root = tmp_path / "docker"
    docker_root.mkdir()
    (docker_root / ".env.docker").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
    native_root = tmp_path / "slim"
    native_root.mkdir()
    (native_root / "config.env").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")

    row = doctor._check_settings_file(docker_root, own_root=True)

    assert row is not None and row.status == "pass"
    assert "/data/settings.env" in row.detail and "not visible from here" in row.detail
    assert "/status" in row.detail and "python run.py doctor" in row.detail
    assert doctor._check_settings_file(native_root, own_root=True) is None


def test_settings_file_row_is_wired_into_settings_checks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    monkeypatch.delenv("NYMERIA_SETTINGS_FILE", raising=False)
    (tmp_path / ".env.docker").write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)

    results: list[doctor.CheckResult] = []
    doctor._append_settings_checks(
        results, RagSettings(data_dir=tmp_path / "data"), argparse.Namespace(skip_llm_test=True)
    )

    assert _result(results, "Settings file").status == "pass"


# --- #296: the SearXNG row ------------------------------------------------------
#
# One canary search against the operator's SEARXNG_BASE_URL, only when some
# default toolset carries web_search_searxng. A real loopback HTTP server stands
# in for the instance (the network boundary), so the probe's own request,
# parsing and redaction all run for real. Skipped on purpose: a live SearXNG
# (the live leg runs one), and wall-clock timing (the timeout CONFIG is pinned
# instead, in test_searxng_probe_uses_the_policy_client_with_short_timeouts).


@dataclass
class SearxSettings(FakeSettings):
    searxng_base_url: str | None = None


# The zero-egress copy of the pinned image, measured 2026-10-02 (the mates
# incident's shape: HTTP 200, no results, every engine failed).
_MEASURED_ALL_FAILED = {
    "query": "wikipedia",
    "results": [],
    "answers": [],
    "unresponsive_engines": [
        ["brave", "HTTP connection error"],
        ["duckduckgo", "HTTP connection error"],
        ["google", "HTTP connection error"],
        ["startpage", "HTTP connection error"],
        ["wikipedia", "HTTP connection error"],
    ],
}


def _hits(n: int) -> list[dict]:
    return [{"title": f"R{i}", "url": f"https://r{i}.example", "content": "x"} for i in range(n)]


def _write_user_profile(data_dir: Path, user_id: str, tools) -> None:
    import json

    profile_dir = data_dir / "users" / user_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.json").write_text(
        json.dumps({"user_id": user_id, "tool_preferences": {"default_thread_tools": tools}}),
        encoding="utf-8",
    )


def _closed_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def searxng_stub():
    """A loopback SearXNG stand-in that records every request it serves."""
    import http.server
    import json
    import threading
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    state = {"status": 200, "body": b"{}", "type": "application/json"}
    seen: list[tuple[str, str, dict]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            seen.append(("GET", parts.path, query))
            self.send_response(state["status"])
            self.send_header("Content-Type", state["type"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def serve(payload=None, *, status=200, html=None):
        if html is not None:
            state.update(status=status, body=html.encode("utf-8"), type="text/html")
        else:
            state.update(
                status=status, body=json.dumps(payload).encode("utf-8"), type="application/json"
            )

    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_address[1]}", seen=seen, serve=serve
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def searxng_data(monkeypatch, tmp_path: Path) -> Path:
    """Hermetic gate inputs: no init-seed carrier in the environment, an empty data dir."""
    monkeypatch.delenv("NYMERIA_INIT_DEFAULT_THREAD_TOOLS", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def _in_use(data_dir: Path) -> None:
    _write_user_profile(
        data_dir, "default", ["bash_execute", "web_search_searxng", "fetch_url_nymeria"]
    )


def _probe_row(data_dir: Path, url: str | None, **kwargs) -> doctor.CheckResult | None:
    return doctor._check_searxng(SearxSettings(data_dir=data_dir, searxng_base_url=url), **kwargs)


def test_searxng_row_is_absent_and_sends_nothing_when_no_default_toolset_uses_it(
    searxng_stub, searxng_data
) -> None:
    searxng_stub.serve({"results": _hits(1)})

    # No profile at all: the fresh defaults (ddgs) apply.
    assert _probe_row(searxng_data, searxng_stub.url) is None
    # Profiles that do not carry it, the bootstrap admin's included.
    _write_user_profile(searxng_data, "default", ["web_search_ddgs", "fetch_url_nymeria"])
    _write_user_profile(searxng_data, "alice", None)
    assert _probe_row(searxng_data, searxng_stub.url) is None
    # The compose files set SEARXNG_BASE_URL on every full stack, so the URL
    # alone must never cost a request.
    assert searxng_stub.seen == []


@pytest.mark.parametrize("url", [None, "", "   "], ids=["none", "empty", "blank"])
def test_searxng_row_warns_without_a_base_url_and_sends_nothing(
    searxng_stub, searxng_data, url
) -> None:
    _in_use(searxng_data)

    row = _probe_row(searxng_data, url)

    assert row is not None and row.name == "SearXNG"
    assert row.status == "warn"
    assert row.detail.startswith(
        "web_search_searxng is in the default tools but SEARXNG_BASE_URL is not set"
    )
    assert "not checked here" in row.detail and "nymeria init" in row.detail
    assert searxng_stub.seen == []


def test_searxng_settings_without_the_field_do_not_raise(searxng_data) -> None:
    _in_use(searxng_data)

    row = doctor._check_searxng(FakeSettings(data_dir=searxng_data))

    assert row is not None and row.status == "warn"
    assert "SEARXNG_BASE_URL is not set" in row.detail


class _RefusingClient:
    """A policy client whose every GET is refused at the network boundary."""

    def __init__(self, calls: list[str], message: str) -> None:
        self.calls = calls
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **_kwargs):
        import httpx

        self.calls.append(url)
        raise httpx.ConnectError(self.message)


def test_searxng_docker_service_host_is_not_checked_from_the_host(
    monkeypatch, searxng_data
) -> None:
    from nymeria.core import http_policy

    _in_use(searxng_data)

    def no_request(**_kwargs):
        raise AssertionError("a Docker service host must not be probed from the host")

    monkeypatch.setattr(settings_module, "_in_container", lambda: False)
    monkeypatch.setattr(http_policy, "policy_http_client", no_request)

    row = _probe_row(searxng_data, "http://searxng:8080")

    assert row is not None
    assert row.status == "warn"  # never a pass for a backend nobody checked
    assert row.detail.startswith("not checked from the host:")
    assert "'searxng'" in row.detail
    assert "docker exec <api container> python run.py doctor" in row.detail

    # Inside a container the same address resolves, so it IS probed.
    calls: list[str] = []
    monkeypatch.setattr(settings_module, "_in_container", lambda: True)
    monkeypatch.setattr(
        http_policy,
        "policy_http_client",
        lambda **_kwargs: _RefusingClient(calls, "[Errno -2] Name or service not known"),
    )

    inside = _probe_row(searxng_data, "http://searxng:8080")

    assert calls == ["http://searxng:8080/search"]
    assert inside is not None and inside.status == "warn"
    assert inside.detail.startswith(
        "cannot reach http://searxng:8080 ([Errno -2] Name or service not known)"
    )


def test_searxng_probe_uses_the_policy_client_with_short_timeouts(
    monkeypatch, searxng_data
) -> None:
    from nymeria.core import http_policy

    _in_use(searxng_data)
    built: list[dict] = []
    calls: list[str] = []

    def capture(**kwargs):
        built.append(kwargs)
        return _RefusingClient(calls, "[Errno 111] Connection refused")

    monkeypatch.setattr(http_policy, "policy_http_client", capture)

    _probe_row(searxng_data, "http://127.0.0.1:9")

    # The env-proxy-neutralising factory, a 3s connect and 10s read bound, and
    # redirects followed the way the tool follows them.
    assert len(built) == 1 and len(calls) == 1
    timeout = built[0]["timeout"]
    assert (timeout.connect, timeout.read) == (3.0, 10.0)
    assert built[0]["follow_redirects"] is True


def test_searxng_slow_instance_warns_as_a_timeout(monkeypatch, searxng_data) -> None:
    import httpx

    from nymeria.core import http_policy

    _in_use(searxng_data)

    class SlowClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **_kwargs):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(http_policy, "policy_http_client", lambda **_kwargs: SlowClient())

    row = _probe_row(searxng_data, "http://127.0.0.1:9")

    assert row == doctor.CheckResult(
        "SearXNG", "warn", "http://127.0.0.1:9 did not answer a test search within 10s (timed out)"
    )


def test_searxng_check_never_raises(monkeypatch, searxng_data) -> None:
    from nymeria.core import searxng_health

    _in_use(searxng_data)

    def boom(_url, **_kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(searxng_health, "probe_searxng", boom)

    row = _probe_row(searxng_data, "http://127.0.0.1:9")

    assert row == doctor.CheckResult("SearXNG", "warn", "check failed: probe exploded")
    # A settings object the gate cannot read means "cannot tell": no row.
    assert doctor._check_searxng(SearxSettings(data_dir=object())) is None  # type: ignore[arg-type]


def test_searxng_unreachable_instance_warns_with_the_remedy(searxng_data) -> None:
    _in_use(searxng_data)
    url = f"http://127.0.0.1:{_closed_port()}"

    row = _probe_row(searxng_data, url)

    assert row is not None and row.status == "warn"
    assert row.detail.startswith(f"cannot reach {url} (")
    assert "Connection refused" in row.detail
    assert "docker compose --profile search up -d searxng" in row.detail
    assert "fix SEARXNG_BASE_URL" in row.detail


def test_searxng_answering_instance_passes_with_one_canary_request(
    searxng_stub, searxng_data
) -> None:
    _in_use(searxng_data)
    searxng_stub.serve({"results": _hits(3), "unresponsive_engines": []})

    row = _probe_row(searxng_data, searxng_stub.url + "/")

    assert row == doctor.CheckResult(
        "SearXNG", "pass", f"{searxng_stub.url}/ answered a test search with 3 result(s)"
    )
    # Exactly one request, the tool's own shape, with a non-identifying query.
    assert searxng_stub.seen == [
        (
            "GET",
            "/search",
            {
                "q": "wikipedia",
                "format": "json",
                "categories": "general",
                "safesearch": "1",
                "pageno": "1",
            },
        )
    ]


def test_searxng_partial_engine_failure_still_passes_and_names_them(
    searxng_stub, searxng_data
) -> None:
    _in_use(searxng_data)
    searxng_stub.serve(
        {"results": _hits(2), "unresponsive_engines": [["brave", "timeout"], ["google", "CAPTCHA"]]}
    )

    row = _probe_row(searxng_data, searxng_stub.url)

    assert row is not None and row.status == "pass"
    assert row.detail == (
        f"{searxng_stub.url} answered a test search with 2 result(s); "
        "2 engine(s) failed: brave (timeout), google (CAPTCHA)"
    )


def test_searxng_every_engine_failed_warns_naming_them(searxng_stub, searxng_data) -> None:
    # The mates incident: the sidecar reported healthy for weeks while this was
    # the answer to every search.
    _in_use(searxng_data)
    searxng_stub.serve(_MEASURED_ALL_FAILED)

    row = _probe_row(searxng_data, searxng_stub.url)

    assert row is not None and row.status == "warn"
    assert row.detail.startswith(
        f"{searxng_stub.url} is up, but its engines failed a test search with no results: "
        "brave (HTTP connection error), duckduckgo (HTTP connection error), "
        "google (HTTP connection error), startpage (HTTP connection error), "
        "wikipedia (HTTP connection error). "
    )
    assert "newer SearXNG image" in row.detail
    assert "make web_search_ddgs the default" in row.detail


def test_searxng_empty_answer_without_engine_errors_warns(searxng_stub, searxng_data) -> None:
    _in_use(searxng_data)
    searxng_stub.serve({"results": [], "unresponsive_engines": []})

    row = _probe_row(searxng_data, searxng_stub.url)

    assert row is not None and row.status == "warn"
    assert row.detail == (
        f"{searxng_stub.url} answered a test search with no results and no engine "
        "errors; check the instance's enabled engines"
    )


@pytest.mark.parametrize(
    "status, expected",
    [
        (
            403,
            "refused a JSON test search (HTTP 403); the instance's search.formats "
            "setting must include json",
        ),
        (
            429,
            "rate-limited the test search (HTTP 429); a private instance should set "
            "server.limiter: false",
        ),
        (500, "answered the test search with HTTP 500"),
    ],
)
def test_searxng_http_errors_warn_with_their_hint(
    searxng_stub, searxng_data, status, expected
) -> None:
    _in_use(searxng_data)
    searxng_stub.serve({"error": "x"}, status=status)

    row = _probe_row(searxng_data, searxng_stub.url)

    assert row == doctor.CheckResult("SearXNG", "warn", f"{searxng_stub.url} {expected}")


@pytest.mark.parametrize(
    "html, payload",
    [("<html>login</html>", None), (None, ["not", "an", "object"])],
    ids=["html", "json-list"],
)
def test_searxng_non_searxng_answer_warns(searxng_stub, searxng_data, html, payload) -> None:
    _in_use(searxng_data)
    searxng_stub.serve(payload, html=html)

    row = _probe_row(searxng_data, searxng_stub.url)

    assert row == doctor.CheckResult(
        "SearXNG",
        "warn",
        f"{searxng_stub.url} answered without JSON; is SEARXNG_BASE_URL a SearXNG instance?",
    )


def test_searxng_row_never_prints_url_credentials(searxng_stub, searxng_data) -> None:
    _in_use(searxng_data)
    searxng_stub.serve({"results": _hits(1)})
    port = searxng_stub.url.rsplit(":", 1)[1]

    answered = _probe_row(searxng_data, f"http://sx-user:s3cret@127.0.0.1:{port}")
    refused = _probe_row(searxng_data, f"http://sx-user:s3cret@127.0.0.1:{_closed_port()}")

    assert answered is not None and answered.status == "pass"
    assert answered.detail.startswith(f"http://sx-user:***@127.0.0.1:{port} answered")
    assert refused is not None and refused.status == "warn"
    assert refused.detail.startswith("cannot reach http://sx-user:***@127.0.0.1:")
    for row in (answered, refused):
        assert "s3cret" not in row.detail


@pytest.mark.parametrize(
    "url",
    ["http://sx-user:s3cret@[bad", "http://sx-user:s3cret@127.0.0.1:99999"],
    ids=["bracket", "port-out-of-range"],
)
def test_searxng_malformed_url_is_reported_unechoed_and_unprobed(
    monkeypatch, searxng_data, url
) -> None:
    from nymeria.core import http_policy

    _in_use(searxng_data)

    def no_request(**_kwargs):
        raise AssertionError("a malformed URL must not be probed")

    monkeypatch.setattr(http_policy, "policy_http_client", no_request)

    row = _probe_row(searxng_data, url)

    assert row == doctor.CheckResult(
        "SearXNG",
        "warn",
        "SEARXNG_BASE_URL is not a valid URL, so web_search_searxng cannot use it; "
        "fix it or rerun `nymeria init`",
    )


def test_searxng_row_never_fails_the_doctor_run(monkeypatch, searxng_stub, searxng_data) -> None:
    # A FAIL would make `nymeria init`'s final doctor skip start-now, and
    # search is optional: WARN is the ceiling, so the run still exits 0.
    _in_use(searxng_data)
    searxng_stub.serve(_MEASURED_ALL_FAILED)
    row = _probe_row(searxng_data, searxng_stub.url)
    assert row is not None and row.status == "warn"
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda _args: [doctor.CheckResult("Python", "pass", "3.12"), row],
    )

    assert doctor.run_doctor(argparse.Namespace(skip_llm_test=True)) == 0


def test_searxng_gate_reads_every_profile_and_the_init_seed_carrier(
    monkeypatch, searxng_stub, searxng_data, tmp_path: Path
) -> None:
    searxng_stub.serve({"results": _hits(1)})

    # A non-admin account carrying SearXNG counts (the mates shape), and a
    # corrupt profile beside it is skipped, not fatal.
    (searxng_data / "users" / "broken").mkdir(parents=True)
    (searxng_data / "users" / "broken" / "profile.json").write_text("{not json", encoding="utf-8")
    assert _probe_row(searxng_data, searxng_stub.url) is None
    _write_user_profile(searxng_data, "mates", ["web_search_searxng"])
    mates = _probe_row(searxng_data, searxng_stub.url)
    assert mates is not None and mates.status == "pass"

    # No bootstrap profile yet (a Docker host: it lives in the volume): the
    # carrier the wizard wrote counts.
    fresh = tmp_path / "fresh-data"
    fresh.mkdir()
    assert _probe_row(fresh, searxng_stub.url) is None
    monkeypatch.setenv("NYMERIA_INIT_DEFAULT_THREAD_TOOLS", "bash_execute:web_search_searxng")
    seeded = _probe_row(fresh, searxng_stub.url)
    assert seeded is not None and seeded.status == "pass"

    # Once the bootstrap profile exists the carrier is spent and ignored.
    _write_user_profile(fresh, "default", ["web_search_ddgs"])
    assert _probe_row(fresh, searxng_stub.url) is None
    assert len(searxng_stub.seen) == 2


def test_searxng_gate_reads_another_roots_carrier(searxng_stub, searxng_data, tmp_path: Path) -> None:
    # The wizard's final doctor inspects the root it just configured, whose
    # carrier may live only in that root's env file.
    searxng_stub.serve({"results": _hits(1)})
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / ".env.docker").write_text(
        "NYMERIA_INIT_DEFAULT_THREAD_TOOLS=web_search_searxng:fetch_url_nymeria\n",
        encoding="utf-8",
    )

    there = _probe_row(searxng_data, searxng_stub.url, project_root=other_root)

    assert there is not None and there.status == "pass"
    # This process's own root does not read another root's files.
    assert _probe_row(searxng_data, searxng_stub.url) is None


def test_searxng_row_is_wired_after_the_web_search_row(
    monkeypatch, searxng_stub, tmp_path: Path
) -> None:
    monkeypatch.delenv("NYMERIA_INIT_DEFAULT_THREAD_TOOLS", raising=False)
    data_dir = tmp_path / "data"
    _write_sqlite_state(data_dir)
    _in_use(data_dir)
    searxng_stub.serve({"results": _hits(4)})
    settings = SearxSettings(data_dir=data_dir, searxng_base_url=searxng_stub.url)
    _stub_static_checks(monkeypatch, tmp_path, settings)

    results = doctor.collect_checks(argparse.Namespace(skip_llm_test=True))

    names = [result.name for result in results]
    assert names[names.index("Web search") + 1] == "SearXNG"
    assert _result(results, "SearXNG").detail.endswith("with 4 result(s)")


@pytest.mark.timeout(300)
def test_doctor_searxng_check_never_loads_the_tools_package(tmp_path: Path) -> None:
    """The row's code path stays out of `nymeria.tools` (+7s, measured 2026-10-02).

    A subprocess, because this test process has long since imported the tools
    package for other tests. The probe line guards against a vacuous pass: a
    run that never reached the probe must not look clean.
    """
    import subprocess
    import sys

    data_dir = tmp_path / "data"
    _in_use(data_dir)
    code = (
        "import sys, types\n"
        "from pathlib import Path\n"
        "from nymeria import doctor\n"
        f"settings = types.SimpleNamespace(data_dir=Path({str(data_dir)!r}), "
        f"searxng_base_url='http://127.0.0.1:{_closed_port()}')\n"
        "row = doctor._check_searxng(settings)\n"
        "print('ROW=' + row.status + ':' + row.detail[:20])\n"
        "loaded = sorted(m for m in sys.modules if m == 'nymeria.tools' or m.startswith('nymeria.tools.'))\n"
        "print('TOOLS=' + ','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert result.returncode == 0, result.stderr
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert lines.get("ROW") == "warn:cannot reach http://"
    assert lines.get("TOOLS") == ""
