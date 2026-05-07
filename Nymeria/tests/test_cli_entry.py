from pathlib import Path

from nymeria import _runtime_paths
from nymeria.config import settings as settings_module
from nymeria.setup_wizard import main as setup_main


def test_runtime_bootstrap_uses_source_checkout_when_markers_exist(
    monkeypatch,
    tmp_path: Path,
):
    root = tmp_path / "Nymeria"
    config_dir = root / "nymeria" / "config"
    config_dir.mkdir(parents=True)
    (root / "run.py").write_text("", encoding="utf-8")
    (config_dir / "soul.md").write_text("", encoding="utf-8")

    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)

    resolved = _runtime_paths.configure_project_root(root / "nymeria" / "cli_entry.py")

    assert resolved == root.resolve()


def test_runtime_bootstrap_uses_user_root_for_installed_package(
    monkeypatch,
    tmp_path: Path,
):
    fake_entry = tmp_path / "site-packages" / "nymeria" / "cli_entry.py"
    fake_entry.parent.mkdir(parents=True)
    fake_entry.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)

    resolved = _runtime_paths.configure_project_root(fake_entry)

    assert resolved == (home / ".nymeria").resolve()


def test_settings_package_paths_do_not_follow_runtime_project_root(
    monkeypatch,
    tmp_path: Path,
):
    runtime_root = tmp_path / "runtime"
    package_root = tmp_path / "site-packages" / "nymeria"
    monkeypatch.delenv("NYMERIA_DATA_DIR", raising=False)
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", runtime_root)
    monkeypatch.setattr(settings_module, "PACKAGE_ROOT", package_root)

    settings = settings_module.Settings(_env_file=None)

    assert settings.project_root == runtime_root
    assert settings.data_dir == runtime_root / "data"
    assert settings.soul_path == package_root / "config" / "soul.md"
    assert settings.bundled_skills_dir == package_root / "skills_bundled"


def test_init_noninteractive_writes_config_and_bootstrap_token(tmp_path: Path):
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert "LLM_PROVIDER=anthropic" in config
    assert "LLM_MODEL=claude-test-model" in config
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in config
    assert f"NYMERIA_DATA_DIR={root / 'data'}" in config
    assert (root / "data" / "accounts.db").exists()
    assert (root / "data" / "BOOTSTRAP_TOKEN.txt").exists()


def test_init_noninteractive_writes_optional_capability_keys(tmp_path: Path):
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "anthropic",
            "--model",
            "claude-test-model",
            "--api-key",
            "sk-ant-test-key",
            "--embedding-api-key",
            "sk-embedding-test",
            "--openai-api-key",
            "sk-openai-test",
            "--gemini-api-key",
            "gemini-test",
            "--perplexity-api-key",
            "pplx-test",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert "EMBEDDING_API_KEY=sk-embedding-test" in config
    assert "OPENAI_API_KEY=sk-openai-test" in config
    assert "GEMINI_API_KEY=gemini-test" in config
    assert "PERPLEXITY_API_KEY=pplx-test" in config


def test_init_noninteractive_does_not_duplicate_primary_openai_key(tmp_path: Path):
    root = tmp_path / "runtime"

    result = setup_main(
        [
            "--provider",
            "openai",
            "--model",
            "openai-test-model",
            "--api-key",
            "sk-openai-primary",
            "--openai-api-key",
            "sk-openai-primary",
            "--root",
            str(root),
            "--non-interactive",
        ]
    )

    config = (root / "config.env").read_text(encoding="utf-8")
    assert result == 0
    assert config.count("OPENAI_API_KEY=") == 1
