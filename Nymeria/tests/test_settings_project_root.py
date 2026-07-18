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
    monkeypatch.setenv("HOME", str(home))  # POSIX
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
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
    # A detached (non-install) module dir with no markers of its own: marker
    # discovery from the module fails, so resolution falls back to the cwd.
    # Must NOT be a site-packages path, or the installed-location guard wins.
    detached_settings = tmp_path / "detached" / "nymeria" / "config" / "settings.py"
    detached_settings.parent.mkdir(parents=True)
    detached_settings.write_text("", encoding="utf-8")
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.delattr(settings_module.sys, "frozen", raising=False)
    monkeypatch.setattr(settings_module, "__file__", str(detached_settings))
    monkeypatch.chdir(root)

    assert settings_module._get_project_root() == root.resolve()


def test_project_root_uses_user_root_for_installed_wheel(monkeypatch, tmp_path):
    # A wheel install: settings.py lives under site-packages and run.py ships
    # beside nymeria/, so the source markers match here. The installed-location
    # guard must win over both marker discovery and any cwd checkout, so config
    # and data resolve to ~/.nymeria (survive upgrades) not site-packages.
    site_packages = tmp_path / "site-packages"
    installed_settings = site_packages / "nymeria" / "config" / "settings.py"
    installed_settings.parent.mkdir(parents=True)
    installed_settings.write_text("", encoding="utf-8")
    (installed_settings.parent / "soul.md").write_text("", encoding="utf-8")
    (site_packages / "run.py").write_text("", encoding="utf-8")
    checkout = _create_backend_root(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))  # POSIX
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    monkeypatch.delenv("NYMERIA_PROJECT_ROOT", raising=False)
    monkeypatch.delattr(settings_module.sys, "frozen", raising=False)
    monkeypatch.setattr(settings_module, "__file__", str(installed_settings))
    monkeypatch.chdir(checkout)

    assert settings_module._get_project_root() == (home / ".nymeria").resolve()


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


def test_env_write_path_dotenv_for_checkout_config_for_packaged(tmp_path):
    """The shape convention `nymeria init` now defers to: a source checkout
    writes `.env` (gitignored), a packaged runtime root writes `config.env`.

    This is what keeps an editable/source install's written config gitignored and
    loadable: run.py and settings both read `.env, config.env, .env.docker`.
    """
    checkout = _create_backend_root(tmp_path)
    assert settings_module.get_env_write_path(checkout) == checkout / ".env"

    packaged = tmp_path / "user-runtime"
    packaged.mkdir()
    assert settings_module.get_env_write_path(packaged) == packaged / "config.env"

    # An existing higher-precedence file is updated in place rather than shadowed.
    (checkout / "config.env").write_text("API_PORT=8000\n", encoding="utf-8")
    assert settings_module.get_env_write_path(checkout) == checkout / "config.env"
