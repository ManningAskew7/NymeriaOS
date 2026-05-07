from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from nymeria import doctor


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


def test_run_doctor_returns_nonzero_for_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda _args: [doctor.CheckResult("Example", "fail", "broken")],
    )

    assert doctor.run_doctor(argparse.Namespace(skip_llm_test=True)) == 1
