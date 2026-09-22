"""``run.py`` log-file selection and the unwritable-log-dir fallback (#383,
#101 entry 16).

Every ``run.py`` process used to open its own rotating handler on ONE
``service.log`` (api, worker and each bot share the data volume), so each
rotated the others' live file; and a read-only data mount crashed a thin bot
at that open(). ``configure_logging`` is spied rather than run: it replaces
the root logger's handlers, which would take pytest's own capture with it.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import pytest

import run


def _settings(tmp_path: Path, name: str = "service.log") -> SimpleNamespace:
    return SimpleNamespace(
        logs_dir=tmp_path / "logs",
        service_log_file=name,
        service_log_max_bytes=1024,
        service_log_backup_count=2,
        log_level="INFO",
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("api", "service.log"),
        ("slim", "service.log"),
        (None, "service.log"),
        ("worker", "worker.log"),
        ("telegram-bot", "telegram.log"),
        ("discord-bot", "discord.log"),
        ("cli", "cli.log"),
        ("claude-code-runner", "claude-code-runner.log"),
    ],
)
def test_each_process_role_gets_its_own_file(tmp_path, command, expected):
    settings = _settings(tmp_path)
    assert run.log_file_for(settings, command) == tmp_path / "logs" / expected


def test_a_custom_service_log_name_prefixes_the_role(tmp_path):
    settings = _settings(tmp_path, "nym.log")
    assert run.log_file_for(settings, "api") == tmp_path / "logs" / "nym.log"
    assert run.log_file_for(settings, "worker") == tmp_path / "logs" / "nym-worker.log"
    assert run.log_file_for(settings, "slack-bot") == tmp_path / "logs" / "nym-slack.log"


def _spy_configure(monkeypatch):
    seen: dict = {}

    def fake_configure(**kwargs):
        seen.update(kwargs)

    import nymeria.config.logging_config as lc

    monkeypatch.setattr(lc, "configure_logging", fake_configure)
    return seen


def test_setup_logging_opens_the_roles_file(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    import nymeria.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: settings)
    seen = _spy_configure(monkeypatch)
    run.setup_logging("INFO", command="worker")
    try:
        handler = seen["file_handler"]
        assert isinstance(handler, RotatingFileHandler)
        assert Path(handler.baseFilename) == tmp_path / "logs" / "worker.log"
        assert (tmp_path / "logs" / "worker.log").exists()
        assert not (tmp_path / "logs" / "service.log").exists()
    finally:
        handler = seen.get("file_handler")
        if handler is not None:
            handler.close()


def _require_unprivileged_posix():
    # A skipif decorator is evaluated at import: Windows has no geteuid, and
    # neither platform-less modes nor root honour the bits anyway.
    if os.name != "posix":
        pytest.skip("directory-mode semantics are POSIX")
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory modes")


def test_an_unwritable_log_dir_degrades_to_console_only(tmp_path, monkeypatch, caplog):
    _require_unprivileged_posix()
    settings = _settings(tmp_path)
    tmp_path.chmod(0o500)  # logs/ cannot be created underneath
    import nymeria.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: settings)
    seen = _spy_configure(monkeypatch)
    try:
        with caplog.at_level(logging.WARNING, logger="nymeria"):
            run.setup_logging("INFO", command="telegram-bot")
    finally:
        tmp_path.chmod(0o700)
    assert seen["file_handler"] is None
    assert "telegram.log" in caplog.text and "console only" in caplog.text


def test_a_read_only_log_file_degrades_to_console_only(tmp_path, monkeypatch, caplog):
    """The likelier production shape: a root-owned worker.log left by an
    earlier container run. Exercises the handler-open half of the net."""
    _require_unprivileged_posix()
    settings = _settings(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker.log").write_text("old\n")
    (logs / "worker.log").chmod(0o444)
    import nymeria.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: settings)
    seen = _spy_configure(monkeypatch)
    try:
        with caplog.at_level(logging.WARNING, logger="nymeria"):
            assert run.setup_logging("INFO", command="worker") is None
    finally:
        (logs / "worker.log").chmod(0o644)
    assert seen["file_handler"] is None
    assert "worker.log" in caplog.text and "console only" in caplog.text


def test_logs_existing_as_a_file_degrades_to_console_only(tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path)
    (tmp_path / "logs").write_text("not a directory")
    import nymeria.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: settings)
    seen = _spy_configure(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="nymeria"):
        assert run.setup_logging("INFO", command="cli") is None
    assert seen["file_handler"] is None
    assert "cli.log" in caplog.text


def test_a_configured_directory_part_is_kept_for_every_role(tmp_path):
    settings = _settings(tmp_path, "sub/service.log")
    assert run.log_file_for(settings, "api") == tmp_path / "logs" / "sub" / "service.log"
    assert run.log_file_for(settings, "worker") == tmp_path / "logs" / "sub" / "worker.log"
    absolute = _settings(tmp_path, str(tmp_path / "elsewhere" / "nym.log"))
    assert run.log_file_for(absolute, "worker") == tmp_path / "elsewhere" / "nym-worker.log"


def test_a_record_lands_in_the_roles_file_and_not_the_service_file(tmp_path, monkeypatch):
    """Real configure_logging: separation, not just naming. Root handlers are
    restored afterwards so pytest's own capture survives."""
    settings = _settings(tmp_path)
    import nymeria.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: settings)
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_nymeria_level = logging.getLogger("nymeria").level
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        path = run.setup_logging("INFO", command="worker")
        logging.getLogger("nymeria.test_logging").warning("worker record 383")
        for h in root.handlers:
            h.flush()
        assert path == tmp_path / "logs" / "worker.log"
        assert "worker record 383" in path.read_text(encoding="utf-8")
        assert not (tmp_path / "logs" / "service.log").exists()
    finally:
        for h in root.handlers[:]:
            h.close()
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
        logging.getLogger("nymeria").setLevel(saved_nymeria_level)
