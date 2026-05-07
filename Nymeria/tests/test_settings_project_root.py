from pathlib import Path

import nymeria.config.settings as settings_module


def _create_backend_root(path: Path) -> Path:
    root = path / "Nymeria"
    config_dir = root / "nymeria" / "config"
    config_dir.mkdir(parents=True)
    (root / "run.py").write_text("", encoding="utf-8")
    (config_dir / "settings.py").write_text("", encoding="utf-8")
    (config_dir / "soul.md").write_text("", encoding="utf-8")
    return root


def test_project_root_env_override_is_normalized(monkeypatch, tmp_path):
    root = _create_backend_root(tmp_path)
    monkeypatch.setenv("NYMERIA_PROJECT_ROOT", str(root))
    monkeypatch.delattr(settings_module.sys, "frozen", raising=False)

    assert settings_module._get_project_root() == root.resolve()


def test_project_root_uses_user_root_for_frozen_build_without_override(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(settings_module.sys, "frozen", True, raising=False)

    assert settings_module._get_project_root() == (home / ".nymeria").resolve()


def test_project_root_discovers_markers_when_settings_file_moves(monkeypatch, tmp_path):
    root = _create_backend_root(tmp_path)
    moved_settings = root / "nymeria" / "config" / "packaged" / "settings.py"
    moved_settings.parent.mkdir()
    moved_settings.write_text("", encoding="utf-8")
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.delattr(settings_module.sys, "frozen", raising=False)
    monkeypatch.setattr(settings_module, "__file__", str(moved_settings))

    assert settings_module._get_project_root() == root.resolve()


def test_project_root_can_fall_back_to_cwd_marker_discovery(monkeypatch, tmp_path):
    root = _create_backend_root(tmp_path)
    installed_settings = tmp_path / "site-packages" / "nymeria" / "config" / "settings.py"
    installed_settings.parent.mkdir(parents=True)
    installed_settings.write_text("", encoding="utf-8")
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.delattr(settings_module.sys, "frozen", raising=False)
    monkeypatch.setattr(settings_module, "__file__", str(installed_settings))
    monkeypatch.chdir(root)

    assert settings_module._get_project_root() == root.resolve()


def test_project_root_keeps_fixed_depth_fallback(monkeypatch, tmp_path):
    fallback_root = tmp_path / "fallback-root"
    fake_settings = fallback_root / "nymeria" / "config" / "settings.py"
    fake_settings.parent.mkdir(parents=True)
    fake_settings.write_text("", encoding="utf-8")
    no_marker_cwd = tmp_path / "no-marker-cwd"
    no_marker_cwd.mkdir()
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.delattr(settings_module.sys, "frozen", raising=False)
    monkeypatch.setattr(settings_module, "__file__", str(fake_settings))
    monkeypatch.chdir(no_marker_cwd)

    assert settings_module._get_project_root() == fallback_root.resolve()


def test_env_file_paths_include_package_config_file(tmp_path):
    root = tmp_path / "runtime"

    assert settings_module.get_env_file_paths(root) == (
        root / ".env",
        root / "config.env",
        root / ".env.docker",
    )
