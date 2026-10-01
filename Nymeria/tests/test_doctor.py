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
