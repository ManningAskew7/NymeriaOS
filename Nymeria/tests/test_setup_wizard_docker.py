"""Full Docker stack (Postgres + Redis) and clone-free Docker (published image).

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from nymeria.onboarding import HostingOption
from nymeria.setup import app_settings_shadow as app_shadow
from nymeria.setup import environment as environment_mod
from nymeria.setup import finalize as finalize_mod
from nymeria.setup import runner as runner_mod
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _capture_console,
    _env_line,
    _env_report,
    _loaded_from,
    _no_checkout,
    _stub_llm,
)


def _images_published(monkeypatch):
    """Flip the beta gate: pretend release images exist on the registry, which
    re-opens the clone-free published-image compose path."""
    monkeypatch.setattr(environment_mod, "PUBLISHED_DOCKER_IMAGES_AVAILABLE", True)


# --- full Docker stack (Postgres + Redis) -----------------------------------


def test_docker_stack_spec_selects_per_stack():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.state import WizardState

    slim = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.SLIM))
    assert slim.compose_args == ("-f", "docker-compose.single.yml")
    assert slim.service == "nymeria-single"
    assert slim.health_url.endswith("/health")

    full = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.FULL))
    assert full.compose_args == ("--env-file", ".env.docker")
    assert full.service == "api"
    assert full.health_url.endswith("/ready")
    # No command-env sentinel: DISCORD_BOT_TOKEN is fully defaulted in the compose,
    # so the api self-mints with an empty (cleanly "not configured") token.
    assert full.command_env == ()
    # The full stack's first boot (build + deep Postgres/Redis check) gets a longer
    # readiness budget than the single container.
    assert full.health_timeout > slim.health_timeout

    # No explicit stack defaults to slim (back-compat with the single-container path).
    assert finalize_mod._docker_stack_spec(WizardState()).service == "nymeria-single"


def test_compose_command_str_per_stack():
    from nymeria.onboarding import DockerStack
    from nymeria.setup.state import WizardState

    slim = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.SLIM))
    full = finalize_mod._docker_stack_spec(WizardState(docker_stack=DockerStack.FULL))

    assert (
        finalize_mod._compose_command_str(slim, "up", "-d")
        == "docker compose -f docker-compose.single.yml up -d"
    )
    # The full stack uses --env-file with no DISCORD sentinel prefix.
    assert (
        finalize_mod._compose_command_str(full, "up", "-d")
        == "docker compose --env-file .env.docker up -d"
    )


def test_finalize_full_stack_writes_minted_db_secrets(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()

    args = [
        "--provider", "anthropic", "--model", "claude-test-model",
        "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
        "--root", str(root), "--non-interactive",
    ]
    assert setup_main(args) == 0

    content = (root / ".env.docker").read_text(encoding="utf-8")
    # The compose requires POSTGRES_PASSWORD / REDIS_PASSWORD; both are minted.
    pg = _env_line(content, "POSTGRES_PASSWORD")
    rd = _env_line(content, "REDIS_PASSWORD")
    assert pg and rd and pg != rd
    assert '"' not in pg and len(pg) >= 16  # unquoted, non-trivial
    assert "POSTGRES_USER=nymeria" in content
    assert "POSTGRES_DB=nymeria" in content
    # The api self-mints the internal service token onto the shared volume, so the
    # installer writes no NYMERIA_SERVICE_TOKEN line of its own on a fresh install.
    assert "NYMERIA_SERVICE_TOKEN" not in content

    # A reconfigure (--force) must PRESERVE the same DB password: rotating it would
    # break auth against the already-initialized postgres volume.
    assert setup_main(args + ["--force"]) == 0
    content2 = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content2, "POSTGRES_PASSWORD") == pg
    assert _env_line(content2, "REDIS_PASSWORD") == rd


def test_finalize_slim_docker_writes_no_db_secrets(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # Default (no --docker-stack) is slim; it must not write Postgres/Redis secrets.
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--non-interactive"]
    ) == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD" not in content
    assert "REDIS_PASSWORD" not in content


def test_full_stack_and_slim_both_carry_init_picks(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    picks = ["--web-search", "web_search_tavily"]

    # Full stack: the shared api/worker `environment:` anchor passes NYMERIA_INIT_*
    # through `--env-file` interpolation, so the picks ARE written and there is no
    # "set them in Settings" note anymore.
    full_root = tmp_path / "full"
    full_root.mkdir()
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(full_root),
         "--non-interactive", *picks]
    ) == 0
    full_content = (full_root / ".env.docker").read_text(encoding="utf-8")
    full_out = capsys.readouterr().out
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in full_content
    assert "not yet carried into the full stack" not in full_out

    # Slim stack with the same picks carries them too (env_file injects the whole file).
    slim_root = tmp_path / "slim"
    slim_root.mkdir()
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "slim", "--root", str(slim_root),
         "--non-interactive", *picks]
    ) == 0
    slim_content = (slim_root / ".env.docker").read_text(encoding="utf-8")
    assert "NYMERIA_INIT_DEFAULT_THREAD_TOOLS" in slim_content

    # Both shapes write the same carrier value: one writer, two delivery paths.
    assert (_env_line(full_content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")
            == _env_line(slim_content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS"))


def test_finalize_full_stack_default_prints_single_up_and_token_read(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()

    def boom(*_a, **_k):
        pytest.fail("the default handoff must not launch a process")

    monkeypatch.setattr(finalize_mod.subprocess, "run", boom)
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full",
         "--root", str(root), "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out

    # The full-stack handoff is now a single `up -d` plus the in-container bootstrap
    # token read: no api-only first phase, no DISCORD sentinel, no host-side mint.
    assert "docker compose --env-file .env.docker up -d" in out
    assert "DISCORD_BOT_TOKEN=disabled" not in out
    assert "up -d api" not in out
    assert "exec api cat /data/BOOTSTRAP_TOKEN.txt" in out
    assert "users add bot-service" not in out


def test_finalize_starts_full_stack_single_up_and_surfaces_token(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    boot_token = "nym_bootstrap_aaa111"
    calls: list[tuple[list[str], object, dict]] = []

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd"), kwargs.get("env") or {}))
        if "cat" in cmd:
            return _Result(stdout=f"Token: {boot_token}\n")
        return _Result()

    health: list[dict] = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: bool(health.append(kw)) or True
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0

    cmds = [cmd for cmd, _cwd, _env in calls]
    # A single `up -d` brings up the whole stack (no api-only phase, no DISCORD
    # sentinel); the api self-mints the service token onto the shared volume and the
    # worker/mcp read it from disk, so there is no host-side mint.
    assert cmds[0] == ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
    assert "DISCORD_BOT_TOKEN=disabled" not in out
    # Health polled on the deep /ready endpoint.
    assert health and str(health[0].get("url", "")).endswith("/ready")
    # Bootstrap token surfaced from the api container; NO `users add` mint exec.
    assert any("cat" in c for c in cmds)
    assert boot_token in out
    assert not any("users" in c for c in cmds)
    # The installer writes no NYMERIA_SERVICE_TOKEN line (the api self-mints it).
    assert _env_line((root / ".env.docker").read_text(encoding="utf-8"),
                     "NYMERIA_SERVICE_TOKEN") is None
    # Exactly one `up -d` total (no second recreate phase).
    up = ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
    assert cmds.count(up) == 1


def test_finalize_full_stack_start_carries_init_picks_without_note(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()

    class _Result:
        returncode = 0
        stdout = "Token: nym_bootstrap_aaa111\n"
        stderr = ""

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--start", "--non-interactive", "--web-search", "web_search_tavily"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # The picks now reach the full stack via the compose env passthrough, so the
    # old "set them in Settings" note must be gone from both the config output and
    # the start handoff, and the carriers must be in the written env file.
    assert "not yet carried into the full stack" not in out
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_INIT_DEFAULT_THREAD_TOOLS")


def test_finalize_full_stack_service_token_preserved_on_reconfigure(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".env.docker").write_text(
        "NYMERIA_SERVICE_TOKEN=nym_existing_token\nPOSTGRES_PASSWORD=keepme\n",
        encoding="utf-8",
    )

    def fake_run(cmd, *args, **kwargs):
        if "users" in cmd and "add" in cmd:
            pytest.fail("must not re-mint a service token that already exists")
        class _R:
            returncode = 0
            stdout = "Token: nym_bootstrap_xyz\n"
            stderr = ""
        return _R()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--start", "--force", "--non-interactive"]
    )
    assert rc == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_SERVICE_TOKEN") == "nym_existing_token"
    # The preserved POSTGRES_PASSWORD is also kept (not rotated).
    assert _env_line(content, "POSTGRES_PASSWORD") == "keepme"


# --- clone-free Docker (published image) -------------------------------------


def test_published_compose_asset_matches_canonical():
    # The wheel-bundled compose that finalize materializes for clone-free
    # installs must stay byte-identical to the canonical file install.sh
    # serves (Nymeria/docker-compose.single.published.yml). Edit both together.
    from importlib import resources

    asset = resources.files("nymeria.setup").joinpath(
        "assets", finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE
    )
    canonical = (
        Path(__file__).parent.parent / finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE
    )
    assert asset.read_bytes() == canonical.read_bytes()


def test_searxng_settings_asset_matches_canonical():
    # Same drift gate for the SearXNG sidecar config the published compose
    # bind-mounts: the wheel asset must stay byte-identical to the canonical
    # Nymeria/searxng/settings.yml the source stacks mount. Edit both together.
    from importlib import resources

    asset = resources.files("nymeria.setup").joinpath(
        "assets", finalize_mod.SEARXNG_SETTINGS_ASSET
    )
    canonical = Path(__file__).parent.parent / "searxng" / "settings.yml"
    assert asset.read_bytes() == canonical.read_bytes()


def test_single_composes_pin_the_same_searxng_image():
    # The three compose files ship the same digest-pinned SearXNG sidecar;
    # bumping the digest in one without the others would fork sidecar behavior
    # between deployment shapes.
    base = Path(__file__).parent.parent
    digests = {}
    for name in (
        "docker-compose.yml",
        "docker-compose.single.yml",
        finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE,
    ):
        text = (base / name).read_text()
        images = re.findall(r"image:\s*(searxng/searxng@sha256:[0-9a-f]+)", text)
        assert images, f"{name} has no digest-pinned searxng image"
        digests[name] = set(images)
    assert len(set().union(*digests.values())) == 1, digests


def test_docker_stack_spec_clone_free_uses_published_compose(monkeypatch):
    from nymeria import __version__
    from nymeria.setup.state import WizardState

    _no_checkout(monkeypatch)
    spec = finalize_mod._docker_stack_spec(WizardState())
    # --env-file is mandatory here even on the default port: the NYMERIA_VERSION
    # image-tag pin in .env.docker only reaches compose interpolation that way
    # (the service-level `env_file:` feeds the container, not the image line).
    assert spec.compose_args == (
        "-f", finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE,
        "--env-file", ".env.docker",
    )
    assert spec.service == "nymeria-single"
    assert spec.image_version == __version__

    # The wizard's own compose invocation pins the fresh version over a stale
    # process env (run.py's import-time dotenv load after a wheel upgrade).
    monkeypatch.setenv("NYMERIA_VERSION", "0.0.0-stale")
    assert finalize_mod._compose_env(spec)["NYMERIA_VERSION"] == __version__

    # A checkout install is untouched: local-build compose, no version pin.
    monkeypatch.setattr(environment_mod, "source_checkout_root", lambda: Path("/x"))
    spec = finalize_mod._docker_stack_spec(WizardState())
    assert spec.compose_args == ("-f", finalize_mod.DOCKER_SINGLE_COMPOSE)
    assert spec.image_version is None


def test_docker_stack_step_gates_full_without_checkout(monkeypatch):
    from nymeria.onboarding import DockerStack
    from nymeria.setup.steps.deployment import _docker_stack_choices

    _no_checkout(monkeypatch)
    by_value = {choice.value: choice for choice in _docker_stack_choices()}
    assert by_value[DockerStack.FULL].disabled
    assert "needs a source checkout" in by_value[DockerStack.FULL].label
    assert not by_value[DockerStack.SLIM].disabled

    monkeypatch.setattr(environment_mod, "source_checkout_root", lambda: Path("/x"))
    by_value = {choice.value: choice for choice in _docker_stack_choices()}
    assert not by_value[DockerStack.FULL].disabled


def test_finalize_clone_free_slim_materializes_compose_and_pins_version(
    monkeypatch, tmp_path, capsys
):
    # The published-image path, kept working behind the beta gate for when
    # release images ship (the flag is the only thing standing in its way).
    from nymeria import __version__

    _stub_llm(monkeypatch)
    _no_checkout(monkeypatch)
    _images_published(monkeypatch)
    root = tmp_path / "clone-free"
    root.mkdir()

    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out

    # The wheel-bundled compose lands next to .env.docker, byte-identical to
    # the canonical file, and the env file pins the image tag to this wheel.
    compose_path = root / finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE
    canonical = (
        Path(__file__).parent.parent / finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE
    )
    assert compose_path.read_bytes() == canonical.read_bytes()
    # The SearXNG sidecar config materializes alongside it (the compose
    # bind-mounts ./searxng when the search profile is up).
    searxng_settings = root / "searxng" / "settings.yml"
    canonical_searxng = Path(__file__).parent.parent / "searxng" / "settings.yml"
    assert searxng_settings.read_bytes() == canonical_searxng.read_bytes()
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_VERSION") == __version__
    # The old deferred-case guidance is gone; the printed start command drives
    # the materialized published compose instead.
    assert "move it next to the compose file" not in out
    assert (
        "docker compose -f docker-compose.single.published.yml "
        "--env-file .env.docker up -d"
    ) in out

    # A --force re-run regenerates the compose (it is generated output, not
    # user state) and re-produces the version pin.
    compose_path.unlink()
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--non-interactive",
         "--force"]
    ) == 0
    assert compose_path.read_bytes() == canonical.read_bytes()
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_VERSION") == __version__


def test_write_config_merge_retires_stale_version_pin(tmp_path):
    # A clone-free root later reconfigured from a source checkout (no pin
    # produced) must retire the stale NYMERIA_VERSION line on merge; a run
    # that produces a pin wins over its own drop entry.
    config = tmp_path / ".env.docker"
    config.write_text("NYMERIA_VERSION=9.9.9\nLLM_TIMEOUT=120\n", encoding="utf-8")
    finalize_mod.write_config(
        config, data_dir=tmp_path / "data", for_docker=True, merge=True,
    )
    content = config.read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_VERSION") is None
    assert _env_line(content, "LLM_TIMEOUT") == "120"

    finalize_mod.write_config(
        config, data_dir=tmp_path / "data", for_docker=True, merge=True,
        image_version="1.2.3",
    )
    content = config.read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_VERSION") == "1.2.3"


def test_finalize_clone_free_start_drives_published_compose(monkeypatch, tmp_path):
    from nymeria import __version__

    _stub_llm(monkeypatch)
    _no_checkout(monkeypatch)
    _images_published(monkeypatch)
    root = tmp_path / "clone-free"
    root.mkdir()
    calls: list[tuple[list[str], object, dict]] = []

    class _Result:
        returncode = 0
        stdout = "Token: nym_bootstrap_zzz\n"
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd"), kwargs.get("env") or {}))
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--start",
         "--skip-llm-test", "--non-interactive"]
    )
    assert rc == 0
    up_cmd, up_cwd, up_env = calls[0]
    assert up_cmd == [
        "docker", "compose", "-f", finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE,
        "--env-file", ".env.docker", "up", "-d",
    ]
    # Runs from the root where finalize materialized the compose + .env.docker,
    # with the image tag pinned against any stale process env.
    assert up_cwd == str(root)
    assert up_env.get("NYMERIA_VERSION") == __version__
    assert (root / finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE).exists()


def test_finalize_clone_free_full_stack_rejected(monkeypatch, tmp_path, capsys):
    # Even with images published, the full stack builds from the repo.
    _stub_llm(monkeypatch)
    _no_checkout(monkeypatch)
    _images_published(monkeypatch)
    root = tmp_path / "clone-free"
    root.mkdir()

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--non-interactive"]
    )
    assert rc == 2
    out = " ".join(capsys.readouterr().out.split())
    assert "compose file builds the images from the repo" in out
    # And the remedy names a setup run that actually sees the checkout:
    # __file__-based detection ignores the cwd, so a packaged `nymeria init`
    # inside the clone would land right back here.
    assert "python run.py init" in out
    assert "re-run `nymeria init`" not in out
    # Rejected before any file writes.
    assert not (root / ".env.docker").exists()


def test_headless_clone_free_docker_is_rejected_before_any_write(monkeypatch, tmp_path):
    """No images are published for the beta: a clone-free `--hosting docker`
    is refused by the hosting gate (same copy as the picker) and nothing is
    written, instead of a compose that fails at `up` with a pull error."""
    _stub_llm(monkeypatch)
    _no_checkout(monkeypatch)
    # Headless runs get a crafted detection report (conftest stubs the real
    # probe suite-wide), so the clone-free signal is set on the report itself.
    monkeypatch.setattr(
        runner_mod,
        "detect_environment",
        lambda **_kw: _env_report(source_checkout=False),
    )
    root = tmp_path / "clone-free"
    root.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        setup_main(
            ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
             "--hosting", "docker", "--root", str(root), "--non-interactive",
             "--skip-llm-test"]
        )
    message = str(excinfo.value)
    assert "no published images yet" in message
    assert "install.sh --source" in message
    assert list(root.iterdir()) == []


def test_finalize_clone_free_docker_gate_writes_nothing(monkeypatch, tmp_path):
    """Finalize's own gate (hydrated or scoped runs reach it without the
    headless hosting check): the Docker shape without a checkout writes no
    config and no compose, and says where the source install is."""
    _stub_llm(monkeypatch)
    _no_checkout(monkeypatch)
    root = tmp_path / "clone-free"
    root.mkdir()
    args = runner_mod.build_parser().parse_args(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--skip-llm-test"]
    )
    state = runner_mod._build_state(args)
    console, buf = _capture_console()

    rc = finalize_mod.finalize(state, console=console, non_interactive=True)

    assert rc == 2
    out = " ".join(buf.getvalue().split())  # undo Rich's line wrapping
    assert "no published Nymeria images exist yet" in out
    # Detection is __file__-based, so the remedy named must be an install that
    # runs from the checkout, never "git clone it and run `nymeria init`".
    assert "install.sh --source" in out
    assert "python run.py init" in out
    assert "git clone" not in out
    assert list(root.iterdir()) == []

    # The gate is the beta flag, nothing else: with images published the same
    # state materializes the published compose as before.
    _images_published(monkeypatch)
    console, _buf = _capture_console()
    assert finalize_mod.finalize(state, console=console, non_interactive=True) == 0
    assert (root / finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE).exists()


def test_finalize_checkout_docker_writes_no_version_pin(monkeypatch, tmp_path):
    # Tests run from a source checkout, so source_checkout_root() is real here:
    # the checkout shape must keep the local-build compose untouched (no
    # published compose materialized, no image-tag pin in .env.docker).
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--root", str(root), "--non-interactive"]
    ) == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "NYMERIA_VERSION") is None
    assert not (root / finalize_mod.DOCKER_SINGLE_PUBLISHED_COMPOSE).exists()


def test_finalize_full_stack_health_timeout_prints_token_read(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    calls: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *_a, **_k):
        calls.append(cmd)
        return _R()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    # The API never becomes healthy within the timeout.
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **_kw: False)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0  # a slow boot is non-fatal
    # The single `up -d` ran; on timeout we do NOT exec anything else (no token
    # read, no `users` mint, no second `up`).
    assert calls == [
        ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
    ]
    assert not any("users" in c for c in calls)
    # The bootstrap-token read is printed so the user can finish by hand; the api
    # still self-mints the service token, so no host-side mint command appears.
    assert "exec api cat /data/BOOTSTRAP_TOKEN.txt" in out
    assert "users add bot-service" not in out
    assert _env_line(
        (root / ".env.docker").read_text(encoding="utf-8"), "NYMERIA_SERVICE_TOKEN"
    ) is None


def test_finalize_full_stack_warns_on_shadowing_process_env(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # An exported POSTGRES_PASSWORD that differs from the generated one would win
    # over .env.docker in docker compose (shell env precedes --env-file); finalize
    # must flag it so the operator does not silently boot with stale credentials.
    monkeypatch.setenv("POSTGRES_PASSWORD", "shell-exported-value")
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out
    assert "your shell exports POSTGRES_PASSWORD" in " ".join(out.split())
    assert "unset POSTGRES_PASSWORD" in out


def test_a_value_only_an_env_file_loaded_is_never_called_a_shell_export(
    monkeypatch, tmp_path, capsys
):
    # #435 review PA-4: os.environ also holds the launch root's FILE values
    # after run.py's boot load. Those never reach the user's shell (where the
    # printed commands run) and the wizard's own compose calls drop them, so
    # telling the user to unset them named something their shell never had.
    _stub_llm(monkeypatch)
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "NYMERIA_SECRETS_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REDIS_PASSWORD", "shell-exported-value")
    _loaded_from(
        tmp_path / "launch",
        "POSTGRES_PASSWORD=launch-file-value\nNYMERIA_SECRETS_KEY=launch-file-key\n",
    )
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out

    assert "your shell exports REDIS_PASSWORD" in " ".join(out.split())
    assert "unset REDIS_PASSWORD" in out
    assert "POSTGRES_PASSWORD" not in out
    assert "NYMERIA_SECRETS_KEY with" not in out and "unset NYMERIA_SECRETS_KEY" not in out


def _hash12(value: str | None) -> str | None:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None


def _full_stack_start(monkeypatch, root, *, token: str = "nym_bootstrap_aaa111"):
    """Run a full-stack `--start` into ``root``; return (rc, subprocess calls)."""
    calls: list[tuple[list[str], dict]] = []

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, dict(kwargs.get("env") or {})))
        if "cat" in cmd:
            return _Result(stdout=f"Token: {token}\n")
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
         "--root", str(root), "--start", "--non-interactive", "--skip-llm-test"]
    )
    return rc, calls


def test_the_wizards_compose_calls_run_on_the_written_key_over_a_shell_export(
    monkeypatch, tmp_path, capsys
):
    # It32 K2: docker-compose.yml interpolates ${NYMERIA_SECRETS_KEY}, and
    # compose reads the process environment before --env-file, so a shell
    # export of ANOTHER key would start the stack on it while .env.docker holds
    # the install's own (a split vault). The wizard's own compose calls pin the
    # written key; keys are compared by hash and never printed.
    from cryptography.fernet import Fernet

    _stub_llm(monkeypatch)
    shell_key = Fernet.generate_key().decode()
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", shell_key)
    _loaded_from(tmp_path / "launch", "IT32_UNRELATED=1\n")
    root = tmp_path / "checkout"
    root.mkdir()

    rc, calls = _full_stack_start(monkeypatch, root)
    out = capsys.readouterr().out

    assert rc == 0
    written = _env_line((root / ".env.docker").read_text(encoding="utf-8"), "NYMERIA_SECRETS_KEY")
    assert written and _hash12(written) != _hash12(shell_key)  # a new root mints its own
    compose = [env for cmd, env in calls if cmd[:2] == ["docker", "compose"]]
    assert compose  # the up, the token readback
    assert {_hash12(env.get("NYMERIA_SECRETS_KEY")) for env in compose} == {_hash12(written)}
    assert written not in out and shell_key not in out  # never a key value
    # Nor in any argv (a process listing shows argv to every local user).
    assert not [cmd for cmd, _env in calls if any(written in a or shell_key in a for a in cmd)]


def test_the_docker_start_now_handoff_names_the_root_to_rerun_setup_in(
    monkeypatch, tmp_path, capsys
):
    # P1: after `init --root B --start`, "Re-run setup anytime with `nymeria
    # init`" reached the default install; it names B.
    import shlex

    _stub_llm(monkeypatch)
    root = tmp_path / "my checkout"
    root.mkdir()

    rc, _calls = _full_stack_start(monkeypatch, root)
    flat = " ".join(capsys.readouterr().out.split())

    assert rc == 0
    assert "nym_bootstrap_aaa111" in flat  # the readback path, not the fallback
    assert f"Re-run setup anytime with `nymeria --root {shlex.quote(str(root))} init`" in flat


def test_finalize_full_stack_no_shadow_warning_when_env_clean(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "checkout"
    root.mkdir()
    # No conflicting values in the environment: the minted/written credentials are
    # the ones compose will use, so the shadow note must NOT fire (guards the
    # warning against false positives on a clean install).
    for key in ("POSTGRES_PASSWORD", "REDIS_PASSWORD", "NYMERIA_SECRETS_KEY",
                "POSTGRES_USER", "POSTGRES_DB"):
        monkeypatch.delenv(key, raising=False)
    assert setup_main(
        ["--provider", "anthropic", "--model", "m", "--api-key", "sk-ant-x",
         "--hosting", "docker", "--docker-stack", "full", "--root", str(root),
         "--non-interactive"]
    ) == 0
    out = capsys.readouterr().out
    assert "your shell exports" not in " ".join(out.split())


def test_wizard_pilot_start_now_docker_defaults_to_start_and_can_switch():
    from nymeria.onboarding import NextAction
    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.start_now import make_start_now_step

    async def drive(switch: bool) -> WizardState:
        state = WizardState(hosting=HostingOption.DOCKER)
        app = SetupWizardApp(state, steps=[make_start_now_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            if switch:
                await pilot.press("down")   # to "Just print the command"
                await pilot.press("space")  # select it
                await pilot.pause()
            await pilot.press("enter")      # lock the highlighted option + finish
            await pilot.pause()
        return state

    # Docker default: Enter accepts the highlighted first option (start now).
    assert asyncio.run(drive(False)).next_action is NextAction.START_API_OPEN_FRONTEND
    # Arrow + space switches to print before Enter records it.
    assert asyncio.run(drive(True)).next_action is NextAction.PRINT_COMMANDS


def test_full_stack_local_rag_pick_writes_image_opt_in(monkeypatch, tmp_path, capsys):
    """A local embedder/reranker on Docker needs the local-rag extra IN THE IMAGE.
    finalize writes `NYMERIA_LOCAL_RAG=1` to `.env.docker` (the compose build arg
    that bakes it in) and the Docker hint names that flag plus the rebuild command
    instead of a generic "build an image" note. `--quick` equips the local granite
    + Ettin stack, which is the silent default most Docker installs land on."""
    _stub_llm(monkeypatch)
    from nymeria.setup import local_rag_install as lri

    monkeypatch.setattr(lri, "local_rag_importable", lambda: False)
    root = tmp_path / "checkout"
    root.mkdir()
    args = [
        "--provider", "anthropic", "--model", "claude-test-model",
        "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
        "--quick", "--root", str(root), "--non-interactive",
    ]
    assert setup_main(args) == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "EMBEDDING_PROVIDER") == "local"
    assert _env_line(content, "NYMERIA_LOCAL_RAG") == "1"
    out = capsys.readouterr().out
    assert "NYMERIA_LOCAL_RAG=1" in out
    assert "--build" in out


def test_full_stack_hosted_rag_writes_no_image_opt_in(monkeypatch, tmp_path):
    """No local pick, no build flag: the lean image stays the default, and a
    reconfigure away from the local stack retires a stale flag. A scripted run
    with no RAG pick lands the local quickstart default, so stand in a hosted
    embedder at the one seam that decides it."""
    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        finalize_mod,
        "apply_quickstart_rag",
        lambda state: setattr(state, "embedder", "value-openai-small"),
    )
    root = tmp_path / "checkout"
    root.mkdir()
    base = [
        "--provider", "anthropic", "--model", "claude-test-model",
        "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
        "--root", str(root), "--non-interactive",
    ]
    assert setup_main(base) == 0
    content = (root / ".env.docker").read_text(encoding="utf-8")
    assert _env_line(content, "EMBEDDING_PROVIDER") == "openai"
    assert "NYMERIA_LOCAL_RAG" not in content
    # Plant a stale flag as if an earlier run had picked the local stack, then
    # reconfigure without it: the merge must drop the line.
    env_path = root / ".env.docker"
    env_path.write_text(content + "NYMERIA_LOCAL_RAG=1\n", encoding="utf-8")
    assert setup_main(base) == 0
    assert "NYMERIA_LOCAL_RAG" not in env_path.read_text(encoding="utf-8")


# --- #314: start-now must not reuse an image the config has outgrown ---------
# --- #254 follow-up: say when app-saved settings override .env.docker -------

# Captured at import, before the suite-wide conftest stub replaces it.
_REAL_IMAGE_PROBE = finalize_mod._full_image_has_local_rag

_FULL_UP = ["docker", "compose", "--env-file", ".env.docker", "up", "-d"]
_FULL_ARGS = [
    "--provider", "anthropic", "--model", "claude-test-model",
    "--api-key", "sk-ant-x", "--hosting", "docker", "--docker-stack", "full",
    "--non-interactive",
]


def _record_compose(monkeypatch, *, overrides_stdout: str = "", overrides_rc: int = 0):
    """Fake every finalize subprocess: record argv + env, answer the probes."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append((list(cmd), dict(kwargs.get("env") or {})))
        if "python3" in cmd:
            return subprocess.CompletedProcess(cmd, overrides_rc, overrides_stdout, "")
        if "cat" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "Token: nym_bootstrap_aaa111\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    return calls


def _hosted_rag(monkeypatch):
    monkeypatch.setattr(
        finalize_mod,
        "apply_quickstart_rag",
        lambda state: setattr(state, "embedder", "value-openai-small"),
    )


def _image_has_extra(monkeypatch, answer):
    seen: list[bool] = []

    def probe():
        seen.append(True)
        return answer

    monkeypatch.setattr(finalize_mod, "_full_image_has_local_rag", probe)
    return seen


def test_start_now_rebuilds_a_full_stack_image_that_lacks_the_local_rag_extra(
    monkeypatch, tmp_path, capsys
):
    # The config needs sentence-transformers baked in; `up -d` alone reuses
    # the lean image that is already there.
    _stub_llm(monkeypatch)
    _image_has_extra(monkeypatch, False)
    calls = _record_compose(monkeypatch)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--quick", "--root", str(root), "--start"]) == 0

    up_calls = [(cmd, env) for cmd, env in calls if "up" in cmd]
    assert [cmd for cmd, _env in up_calls] == [[*_FULL_UP, "--build"]]
    # The build arg the rebuild bakes in is this run's value, whatever the
    # process inherited from the old config.
    assert up_calls[0][1]["NYMERIA_LOCAL_RAG"] == "1"
    out = capsys.readouterr().out
    assert re.search(r"^\s*docker compose --env-file \.env\.docker up -d --build\s*$", out, re.M)


@pytest.mark.parametrize(
    "answer",
    [
        True,  # the image already carries the extra
        None,  # no image yet (compose builds it), or the probe could not tell
    ],
)
def test_start_now_keeps_plain_up_when_no_rebuild_is_needed(monkeypatch, tmp_path, answer):
    _stub_llm(monkeypatch)
    _image_has_extra(monkeypatch, answer)
    calls = _record_compose(monkeypatch)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--quick", "--root", str(root), "--start"]) == 0

    assert [cmd for cmd, _env in calls if "up" in cmd] == [_FULL_UP]


def test_a_hosted_config_neither_rebuilds_nor_bakes_a_stale_flag(monkeypatch, tmp_path):
    # Switching the extra OFF: the heavier image still works, so no forced
    # rebuild; but the build arg is pinned to this run's "0", so the stale "1"
    # the process inherited from the old config cannot reach a later build.
    _stub_llm(monkeypatch)
    _hosted_rag(monkeypatch)
    seen = _image_has_extra(monkeypatch, False)
    monkeypatch.setenv("NYMERIA_LOCAL_RAG", "1")
    calls = _record_compose(monkeypatch)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--root", str(root), "--start"]) == 0

    [(cmd, env)] = [(cmd, env) for cmd, env in calls if "up" in cmd]
    assert cmd == _FULL_UP
    assert env["NYMERIA_LOCAL_RAG"] == "0"
    assert seen == []  # a config that needs no extra never probes the image


def test_single_container_start_never_probes_or_rebuilds(monkeypatch, tmp_path):
    # The single-container image does not consume the flag at all.
    _stub_llm(monkeypatch)
    seen = _image_has_extra(monkeypatch, False)
    calls = _record_compose(monkeypatch)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()

    args = [a if a != "full" else "slim" for a in _FULL_ARGS]
    assert setup_main([*args, "--quick", "--root", str(root), "--start"]) == 0

    ups = [cmd for cmd, _env in calls if "up" in cmd]
    assert ups and all("--build" not in cmd for cmd in ups)
    assert seen == []


@pytest.mark.parametrize("answer,rebuild", [(False, True), (True, False)])
def test_the_printed_start_command_carries_build_under_the_same_rule(
    monkeypatch, tmp_path, capsys, answer, rebuild
):
    _stub_llm(monkeypatch)
    _image_has_extra(monkeypatch, answer)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--quick", "--root", str(root)]) == 0

    out = capsys.readouterr().out
    with_build = re.search(
        r"^\s*docker compose --env-file \.env\.docker up -d --build\s*$", out, re.M
    )
    plain = re.search(r"^\s*docker compose --env-file \.env\.docker up -d\s*$", out, re.M)
    assert bool(with_build) is rebuild
    assert bool(plain) is not rebuild
    # The printed command says why it rebuilds, as start-now does.
    assert ("lacks the local RAG extra" in out) is rebuild


def test_docker_local_rag_hint_ignores_the_host_interpreter(monkeypatch, tmp_path, capsys):
    # sentence-transformers in the WIZARD's Python says nothing about the image.
    _stub_llm(monkeypatch)
    from nymeria.setup import local_rag_install as lri

    monkeypatch.setattr(lri, "local_rag_importable", lambda: True)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--quick", "--root", str(root)]) == 0

    assert "NYMERIA_LOCAL_RAG=1 was written" in capsys.readouterr().out


def _report_stdout(*entries: dict) -> str:
    """The in-container report marker line, as the real program prints it."""
    return "noise\n" + app_shadow.REPORT_MARKER + json.dumps(
        {"file": True, "keys": list(entries)}
    ) + "\n"


def _entry(key: str, cls: str = "setting", kind: str = "override", **extra) -> dict:
    return {"key": key, "class": cls, "kind": kind, "empty": False, **extra}


@pytest.mark.parametrize("stack,service", [("full", "api"), ("slim", "nymeria-single")])
def test_start_now_names_env_keys_the_app_saved_settings_override(
    monkeypatch, tmp_path, capsys, stack, service
):
    # A value saved in the app lives in /data/settings.env and loads last, so
    # the .env.docker value this run wrote for that key is not in effect.
    _stub_llm(monkeypatch)
    calls = _record_compose(
        monkeypatch,
        overrides_stdout=_report_stdout(
            _entry("LLM_MODEL"),
            _entry("USER_TIMEZONE"),
            _entry("bad key;rm"),
            _entry("TWITCH_CHANNEL", kind="app_only"),  # saved only in the app
            _entry("LLM_PROVIDER", "route", kind="same"),  # equal: no shadow
        ),
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()
    args = [a if a != "full" else stack for a in _FULL_ARGS]

    assert setup_main([*args, "--root", str(root), "--start"]) == 0

    [probe] = [cmd for cmd, _env in calls if "python3" in cmd]
    assert probe[probe.index("exec") : probe.index("exec") + 3] == ["exec", "-T", service]
    out = " ".join(capsys.readouterr().out.split())
    assert "override 2 value(s)" in out
    assert "LLM_MODEL, USER_TIMEZONE." in out
    assert "TWITCH_CHANNEL" not in out and "LLM_PROVIDER" not in out
    # The base is the container environment (compose defaults included), so
    # the copy must not claim every key came from .env.docker.
    assert "set elsewhere (for example in .env.docker)" in out
    assert "bad key" not in out and "rm" not in out.split("USER_TIMEZONE")[1][:20]
    assert "/data/settings.env" in out
    # The remedy removes the app's copy (#434). Setting the key in the app
    # would re-save it to the same file and re-arm the shadow for the next run.
    assert "an admin runs /settings clear <KEY> in the app" in out
    assert "/settings set" not in out
    assert "discards the app's copy" not in out  # no credential key here


def test_the_override_note_names_the_direct_slot_for_a_shared_credential():
    from rich.console import Console

    console = Console(record=True, width=400)
    finalize_mod._print_docker_settings_overrides(console, ("LLM_MODEL", "OPENAI_API_KEY"))

    out = " ".join(console.export_text().split())
    assert "/settings clear <KEY>" in out
    assert (
        "Clearing OPENAI_API_KEY discards the app's copy: if it is a real vendor key "
        "you still need, save it as OPENAI_DIRECT_API_KEY first." in out
    )
    assert "Clearing LLM_MODEL" not in out


def test_an_image_older_than_the_report_still_names_its_overrides(
    monkeypatch, tmp_path, capsys
):
    # A published image from before #435 prints the #254 override names.
    _stub_llm(monkeypatch)
    _record_compose(
        monkeypatch,
        overrides_stdout=(
            app_shadow.REPORT_MARKER + "unsupported\n"
            + app_shadow.LEGACY_OVERRIDES_MARKER + "LLM_MODEL,bad key\n"
        ),
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--root", str(root), "--start"]) == 0

    out = " ".join(capsys.readouterr().out.split())
    assert "override 1 value(s) set elsewhere (for example in .env.docker): LLM_MODEL." in out
    assert "bad key" not in out


@pytest.mark.parametrize(
    "stdout,rc",
    [
        (_report_stdout(), 0),  # nothing saved in the app
        ("Traceback: no such function\n", 1),  # no marker: no answer
        (app_shadow.REPORT_MARKER + "unsupported\n", 0),  # older than #254 too
        ("", 0),
    ],
)
def test_start_now_prints_no_override_note_without_overrides(
    monkeypatch, tmp_path, capsys, stdout, rc
):
    _stub_llm(monkeypatch)
    _record_compose(monkeypatch, overrides_stdout=stdout, overrides_rc=rc)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--root", str(root), "--start"]) == 0

    assert "settings.env" not in capsys.readouterr().out


def test_no_override_probe_when_the_stack_is_not_healthy(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    calls = _record_compose(
        monkeypatch, overrides_stdout="NYMERIA_SETTINGS_OVERRIDES=LLM_MODEL\n"
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: False)
    root = tmp_path / "checkout"
    root.mkdir()

    assert setup_main([*_FULL_ARGS, "--root", str(root), "--start"]) == 0

    assert not any("python3" in cmd for cmd, _env in calls)


def test_the_image_probe_names_the_compose_files_full_image(monkeypatch):
    # The probe must inspect the image compose actually runs.
    checkout = Path(finalize_mod.__file__).resolve().parents[2]
    compose = (checkout / "docker-compose.yml").read_text(encoding="utf-8")
    anchor = compose.index("x-nymeria-full-image:")
    image = re.search(r"^\s*image:\s*(\S+)", compose[anchor:], re.M)
    assert image and image.group(1) == finalize_mod.DOCKER_FULL_IMAGE

    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        environment_mod,
        "docker_image_has_module",
        lambda image, module, **_kw: asked.append((image, module)) or True,
    )
    assert _REAL_IMAGE_PROBE() is True
    assert asked == [(finalize_mod.DOCKER_FULL_IMAGE, "sentence_transformers")]


def test_the_in_container_report_runs_the_servers_own_computation(tmp_path):
    # Runs the real program the wizard execs in the container: a renamed or
    # broken function would otherwise fail silently (no marker reads as "no
    # answer").
    settings_file = tmp_path / "settings.env"
    settings_file.write_text(
        "NYMERIA_TEST_OVERRIDE_PROBE=from-file\nNYMERIA_TEST_ONLY_IN_FILE=x\n"
        "NYMERIA_TEST_SAME=same\n",
        encoding="utf-8",
    )
    root = tmp_path / "root"
    root.mkdir()
    env = dict(os.environ)
    env.update(
        NYMERIA_PROJECT_ROOT=str(root),
        NYMERIA_SETTINGS_FILE=str(settings_file),
        NYMERIA_TEST_OVERRIDE_PROBE="from-env",
        NYMERIA_TEST_SAME="same",
    )
    checkout = Path(finalize_mod.__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "-c", app_shadow.REPORT_PROGRAM],
        cwd=str(checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    report = app_shadow.parse_report(result.stdout)
    assert report.status == "report"
    # Only a key that held a DIFFERENT value first is an override; a copy
    # equal to it is `same` (still listed: the wizard needs it).
    assert {e.key: e.kind for e in report.keys} == {
        "NYMERIA_TEST_ONLY_IN_FILE": "app_only",
        "NYMERIA_TEST_OVERRIDE_PROBE": "override",
        "NYMERIA_TEST_SAME": "same",
    }


# --- #435: a reconfigure asks about the app's saved copies before the start ---
#
# The container is emulated by running the REAL in-container programs in a
# fresh interpreter whose environment is only the "container's" (the
# .env.docker the stack was created from, plus the shape's settings-file
# variable) against a tmp stand-in for /data/settings.env. Every value below
# carries DUMMY so the names-only sweep can look for it.

_REAL_RUN = subprocess.run  # captured at import, before any test fakes it
_CHECKOUT = Path(finalize_mod.__file__).resolve().parents[2]
DUMMY = "dummy-it31"
_GATEWAY = [
    "--provider", "openai", "--base-url", "http://127.0.0.1:9/v1",
    "--api-key", f"sk-{DUMMY}-gw", "--model", "gpt-dummy",
]
_ANTHROPIC = ["--provider", "anthropic", "--model", "claude-dummy", "--api-key", f"sk-ant-{DUMMY}"]
_OPENAI = ["--provider", "openai", "--model", "gpt-dummy", "--api-key", f"sk-{DUMMY}-one"]
_OPENAI_NEW_KEY = ["--provider", "openai", "--model", "gpt-dummy", "--api-key", f"sk-{DUMMY}-two"]
# What a GUI-saved gateway route and its key look like in the app's file.
_APP_GATEWAY_FILE = (
    "# keep-me\n"
    "LLM_BASE_URL=http://127.0.0.1:9/v1\n"
    f"OPENAI_API_KEY=sk-{DUMMY}-appgw\n"
    "USER_TIMEZONE=UTC\n"
)


def _docker(stack: str = "slim") -> list[str]:
    return [
        "--hosting", "docker", "--docker-stack", stack, "--non-interactive",
        "--skip-llm-test", "--no-server-browser",
    ]


class _FakeStack:
    """The running stack as the wizard's subprocess calls meet it (see above).

    ``up`` recreates (re-reads .env.docker and starts a stopped stack);
    ``down`` answers every exec like a stopped service; ``report_stdout`` and
    ``apply_stdout`` stand in for an older image's answer; ``before_apply``
    runs between the probe and the removal (a concurrent in-app change);
    ``apply_times_out`` lets the real removal run, then times the exec out
    (the docker client gave up after the write).
    """

    def __init__(self, tmp_path: Path, root: Path):
        self.root = root
        self.app = tmp_path / "container-app"
        self.app.mkdir()
        self.settings = tmp_path / "container-data" / "settings.env"
        self.settings.parent.mkdir()
        self.down = False
        self.env: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.outputs: list[str] = []
        self.report_stdout: str | None = None
        self.apply_stdout: str | None = None
        self.before_apply = None
        self.apply_times_out = False

    def boot(self, app_file: str | None = None) -> "_FakeStack":
        """Create the stack from the current .env.docker, with an app file."""
        if app_file is not None:
            self.settings.write_text(app_file, encoding="utf-8")
            self.settings.chmod(0o600)
        self.recreate()
        self.calls.clear()
        self.envs.clear()
        return self

    def recreate(self) -> None:
        from dotenv import dotenv_values

        values = dotenv_values(self.root / ".env.docker", interpolate=False)
        self.env = {key: value for key, value in values.items() if value is not None}

    def install(self, monkeypatch) -> "_FakeStack":
        def fake_run(cmd, *args, **kwargs):
            cmd = list(cmd)
            self.calls.append(cmd)
            self.envs.append(dict(kwargs.get("env") or {}))
            if "exec" in cmd and "python3" in cmd:
                return self._exec(cmd)
            if "cat" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "Token: nym_bootstrap_aaa111\n", "")
            if "up" in cmd:
                self.down = False
                self.recreate()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return self

    def _exec(self, cmd: list[str]):
        if self.down:
            return subprocess.CompletedProcess(cmd, 1, "", "service is not running")
        program_args = cmd[cmd.index("python3") + 1:]
        program = program_args[1]
        if program == app_shadow.REPORT_PROGRAM and self.report_stdout is not None:
            return subprocess.CompletedProcess(cmd, 0, self.report_stdout, "")
        if program == app_shadow.APPLY_PROGRAM and self.apply_stdout is not None:
            return subprocess.CompletedProcess(cmd, 0, self.apply_stdout, "")
        if program == app_shadow.APPLY_PROGRAM and self.before_apply is not None:
            self.before_apply(self)
        env = {
            "PATH": os.environ.get("PATH", ""),
            # The image's packages: this interpreter's import path, checkout
            # first (a user-site install needs no HOME this way).
            "PYTHONPATH": os.pathsep.join([str(_CHECKOUT), *filter(None, sys.path)]),
            "NYMERIA_PROJECT_ROOT": str(self.app),
            "NYMERIA_SETTINGS_FILE": str(self.settings),
            **self.env,
        }
        result = _REAL_RUN(
            [sys.executable, *program_args],
            cwd=str(_CHECKOUT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.outputs.append(result.stdout + result.stderr)
        if program == app_shadow.APPLY_PROGRAM and self.apply_times_out:
            raise subprocess.TimeoutExpired(cmd, app_shadow.EXEC_TIMEOUT_SECONDS)
        return result

    def execs(self, program: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if "python3" in cmd and program in cmd]

    def program_args(self, program: str) -> list[list[str]]:
        """The argv after `-c <program>` of each exec of ``program``."""
        return [cmd[cmd.index(program) + 1:] for cmd in self.execs(program)]

    def text(self) -> str:
        return self.settings.read_text(encoding="utf-8")


class _ScriptedConsole(Console):
    """A console at a terminal that answers prompts from a script."""

    def __init__(self, *answers):
        self.buffer = io.StringIO()
        super().__init__(file=self.buffer, width=400, force_terminal=False)
        self.answers = list(answers)
        self.prompts: list[str] = []

    def input(self, prompt="", **_kwargs):  # type: ignore[override]
        self.prompts.append(" ".join(str(prompt).replace("\\[", "[").split()))
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def text(self) -> str:
        return " ".join(self.buffer.getvalue().split())


def _interactive(monkeypatch, *answers) -> _ScriptedConsole:
    """Finalize as the interactive wizard runs it after the TUI (at a terminal,
    non_interactive=False), reached through the flag path for brevity."""
    console = _ScriptedConsole(*answers)
    monkeypatch.setattr(runner_mod, "Console", lambda: console)
    real = runner_mod.finalize
    monkeypatch.setattr(
        runner_mod,
        "finalize",
        lambda state, **kw: real(state, **{**kw, "non_interactive": False}),
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return console


def _installed(monkeypatch, tmp_path, *install, stack: str = "slim", app_file=None):
    """A first Docker install, then the stack created from it with an app file."""
    _stub_llm(monkeypatch)
    _hosted_rag(monkeypatch)
    monkeypatch.delenv("NYMERIA_SECRETS_KEY", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    fake = _FakeStack(tmp_path, root).install(monkeypatch)
    assert setup_main([*_docker(stack), "--root", str(root), *install]) == 0
    # A first install has no stack whose app file could hold anything.
    assert fake.execs(app_shadow.REPORT_PROGRAM) == []
    return root, fake.boot(app_file)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_in_container_programs_survive_windows_argv_quoting():
    # W26: list2cmdline plus docker.exe's Go argv parser round-trip a program
    # with no double quote and no backslash unchanged.
    for program in (app_shadow.REPORT_PROGRAM, app_shadow.APPLY_PROGRAM):
        assert '"' not in program
        assert "\\" not in program
        assert subprocess.list2cmdline(["python3", "-c", program]).count('"') == 2


@pytest.mark.parametrize("extra", [[], ["--port", "8097"]])
def test_a_reconfigure_that_changes_no_route_or_credential_never_execs(
    monkeypatch, tmp_path, capsys, extra
):
    # W1: same provider and key (nothing changes), or a plain setting
    # (API_PORT): no exec before the start, no question, no new output.
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI, app_file=f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
    )
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_OPENAI, *extra]) == 0

    assert [cmd for cmd in fake.calls if "exec" in cmd] == []
    out = _flat(capsys.readouterr().out)
    assert "settings.env" not in out and "saved in the app" not in out.lower()


def test_one_probe_and_no_output_when_the_app_saved_none_of_the_changed_keys(
    monkeypatch, tmp_path, capsys
):
    # W2: a candidate exists (the key changed), the app file holds other keys.
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI, app_file="USER_TIMEZONE=UTC\nLLM_MODEL=gpt-app\n"
    )
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_OPENAI_NEW_KEY]) == 0

    assert len(fake.execs(app_shadow.REPORT_PROGRAM)) == 1
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    out = _flat(capsys.readouterr().out)
    assert "settings.env" not in out and "saved in the app" not in out.lower()


def test_interactive_yes_removes_the_app_copy_of_a_changed_key(monkeypatch, tmp_path):
    # W3: the run changes OPENAI_API_KEY and the app saved its own copy. A
    # discard takes an explicit yes (review S3: Enter keeps it).
    app_file = (
        "# saved in the app\n"
        f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
        "USER_TIMEZONE=UTC\n"
        f"OPENAI_API_KEY=sk-{DUMMY}-dup\n"
    )
    root, fake = _installed(monkeypatch, tmp_path, *_OPENAI, app_file=app_file)
    console = _interactive(monkeypatch, "y")

    assert setup_main([*_docker(), "--root", str(root), *_OPENAI_NEW_KEY]) == 0

    assert console.prompts == [
        "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]"
    ]
    # One removal exec, and its argv after the program is the key name only.
    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [["OPENAI_API_KEY"]]
    assert fake.text() == "# saved in the app\nUSER_TIMEZONE=UTC\n"
    assert fake.settings.stat().st_mode & 0o777 == 0o600
    out = console.text
    assert "Removed the app's copy of OPENAI_API_KEY." in out
    # Review S1: this run only prints the start command, so the removal is
    # already in the file while the recreate is the user's: say so plainly.
    assert (
        "The app's settings already changed, so recreate the stack now with "
        "`docker compose -f docker-compose.single.yml up -d`. Until then it keeps "
        "its old configuration, and a restart before the recreate (a reboot, say) "
        "would start the old configuration without the removed copies." in out
    )
    assert "does that" not in out


def test_the_interactive_wizard_without_a_terminal_warns_instead_of_asking(
    monkeypatch, tmp_path
):
    # A pipe or a harness cannot answer: warn only (the local-rag rule).
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI, app_file=f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
    )
    console = _interactive(monkeypatch)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))

    assert setup_main([*_docker(), "--root", str(root), *_OPENAI_NEW_KEY]) == 0

    assert console.prompts == []
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert "pass --clear-app-overrides on the run that makes the change" in console.text


@pytest.mark.parametrize("answer", ["n", "no", EOFError(), KeyboardInterrupt()])
def test_declining_keeps_the_app_copy_and_says_how_to_clear_it(
    monkeypatch, tmp_path, answer
):
    # W4: "n", EOF (Ctrl+D) and Ctrl+C all decline; nothing is removed.
    app_file = f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
    root, fake = _installed(monkeypatch, tmp_path, *_OPENAI, app_file=app_file)
    console = _interactive(monkeypatch, answer)

    assert setup_main([*_docker(), "--root", str(root), *_OPENAI_NEW_KEY]) == 0

    assert len(console.prompts) == 1
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert fake.text() == app_file
    out = console.text
    assert "Kept the app's copy of OPENAI_API_KEY" in out
    assert "an admin runs /settings clear OPENAI_API_KEY in the app" in out
    assert "save it as OPENAI_DIRECT_API_KEY first" in out


def test_a_dropped_route_key_is_asked_about_as_coming_back(monkeypatch, tmp_path):
    # W5 + W8: gateway route to anthropic drops LLM_BASE_URL (and retires
    # the gateway key, #433); the app's copies would bring both back. The
    # app's key is the app gateway's own: cleared, never relocated.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    console = _interactive(monkeypatch, "y", "y")

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC]) == 0

    assert console.prompts == [
        "Remove the app-saved route settings (LLM_BASE_URL) so this choice takes effect? [Y/n]",
        "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]",
    ]
    out = console.text
    assert (
        "Route settings the app saved: LLM_BASE_URL (route; this setup removed it, "
        "the app's copy would bring it back)." in out
    )
    assert "This setup removed OPENAI_API_KEY; the app's copy would bring it back." in out
    assert "is a gateway's key: it only works with the app's own gateway route" in out
    assert "moves to" not in out
    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [["LLM_BASE_URL", "OPENAI_API_KEY"]]
    assert fake.text() == "# keep-me\nUSER_TIMEZONE=UTC\n"


def test_a_route_change_asks_about_every_route_key_and_model_the_app_saved(
    monkeypatch, tmp_path
):
    # W6 + DP4: a GUI-saved route (base URL, model, its gateway's key) under
    # an install whose .env.docker never had a base URL. The run changes the
    # provider: the app's LLM_BASE_URL (neither written nor dropped) and
    # models ride the route group, and its gateway key is asked about alone.
    app_file = (
        "LLM_BASE_URL=http://litellm.example:4000/v1\n"
        "LLM_MODEL=gpt-app\n"
        f"OPENAI_API_KEY=sk-{DUMMY}-litellm\n"
        "LLM_FAST_MODEL=gpt-fast\n"
    )
    root, fake = _installed(monkeypatch, tmp_path, *_OPENAI, app_file=app_file)
    console = _interactive(monkeypatch, "y", "n")

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC]) == 0

    assert console.prompts == [
        "Remove the app-saved route settings (LLM_BASE_URL, LLM_FAST_MODEL, "
        "LLM_MODEL) so this choice takes effect? [Y/n]",
        "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]",
    ]
    out = console.text
    assert "LLM_BASE_URL (route), LLM_FAST_MODEL (model), LLM_MODEL (model)." in out
    assert "it only works with the app's own gateway route" in out
    assert fake.text() == f"OPENAI_API_KEY=sk-{DUMMY}-litellm\n"


def test_no_route_change_leaves_an_app_saved_model_alone(monkeypatch, tmp_path, capsys):
    # DP4's other half: only a route change pulls the model in.
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI,
        app_file=f"LLM_MODEL=gpt-app\nOPENAI_API_KEY=sk-{DUMMY}-app\n",
    )

    assert setup_main([
        *_docker(), "--root", str(root), "--provider", "openai", "--model", "gpt-other",
        "--api-key", f"sk-{DUMMY}-two", "--clear-app-overrides",
    ]) == 0

    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [["OPENAI_API_KEY"]]
    assert fake.text() == "LLM_MODEL=gpt-app\n"


def test_a_real_vendor_key_moves_to_its_direct_slot_when_a_gateway_takes_the_slot(
    monkeypatch, tmp_path
):
    # W7: the app's copy is a real OpenAI key (judged under the app's own
    # route), this run hands OPENAI_API_KEY to a gateway, and the direct slot
    # is set nowhere: it moves inside the container, the value never leaves.
    vendor = f"sk-{DUMMY}-vendor"
    root, fake = _installed(
        monkeypatch, tmp_path, *_ANTHROPIC, app_file=f"# keep\nOPENAI_API_KEY={vendor}\n"
    )
    console = _interactive(monkeypatch, "")

    assert setup_main([*_docker(), "--root", str(root), *_GATEWAY]) == 0

    assert console.prompts == [
        "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [Y/n]"
    ]
    out = console.text
    assert (
        "looks like a real OpenAI key, and this setup's route sends OPENAI_API_KEY "
        "to a gateway, so it moves to OPENAI_DIRECT_API_KEY in the app's settings"
    ) in out
    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [
        ["OPENAI_API_KEY:OPENAI_DIRECT_API_KEY"]
    ]
    from dotenv import dotenv_values

    saved = dotenv_values(fake.settings)
    assert "OPENAI_API_KEY" not in saved
    assert saved["OPENAI_DIRECT_API_KEY"] == vendor
    assert fake.text().startswith("# keep\n")
    assert "Moved the app's copy of OPENAI_API_KEY to OPENAI_DIRECT_API_KEY" in out


@pytest.mark.parametrize("where", ["app file", "container env", "new .env.docker"])
def test_no_move_when_the_direct_slot_is_already_set_anywhere(monkeypatch, tmp_path, where):
    # W9: clearing then discards the app's copy, and the question says so.
    app_file = f"OPENAI_API_KEY=sk-{DUMMY}-vendor\n"
    if where == "app file":
        app_file += f"OPENAI_DIRECT_API_KEY=sk-{DUMMY}-direct\n"
    root, fake = _installed(monkeypatch, tmp_path, *_ANTHROPIC, app_file=app_file)
    if where == "container env":
        fake.env["OPENAI_DIRECT_API_KEY"] = f"sk-{DUMMY}-direct"
    extra = ["--openai-api-key", f"sk-{DUMMY}-media"] if where == "new .env.docker" else []
    console = _interactive(monkeypatch, "y")

    assert setup_main([*_docker(), "--root", str(root), *_GATEWAY, *extra]) == 0

    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [["OPENAI_API_KEY"]]
    out = console.text
    assert "moves to" not in out
    if where == "new .env.docker":
        # The host knows the new file sets it; the container cannot.
        assert "save it as OPENAI_DIRECT_API_KEY first" in out
    else:
        assert "OPENAI_DIRECT_API_KEY is already set, so it is not moved there" in out
    from dotenv import dotenv_values

    assert "OPENAI_API_KEY" not in dotenv_values(fake.settings)


def test_a_move_whose_preconditions_changed_since_the_probe_keeps_the_key(
    monkeypatch, tmp_path, capsys
):
    # W10: the direct slot was saved in the app between the probe and the
    # removal: the container refuses the move and KEEPS the slot.
    root, fake = _installed(
        monkeypatch, tmp_path, *_ANTHROPIC, app_file=f"OPENAI_API_KEY=sk-{DUMMY}-vendor\n"
    )

    def late_save(stack):
        with stack.settings.open("a", encoding="utf-8") as fh:
            fh.write(f"OPENAI_DIRECT_API_KEY=sk-{DUMMY}-late\n")

    fake.before_apply = late_save
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_GATEWAY, "--clear-app-overrides"]) == 0

    assert fake.text() == (
        f"OPENAI_API_KEY=sk-{DUMMY}-vendor\nOPENAI_DIRECT_API_KEY=sk-{DUMMY}-late\n"
    )
    out = _flat(capsys.readouterr().out)
    assert "Kept OPENAI_API_KEY: the app's copy no longer qualifies for the move" in out
    assert "Moved the app's copy" not in out and "Removed the app's copy" not in out


def test_headless_default_warns_and_changes_nothing(monkeypatch, tmp_path, capsys):
    # W11: no prompt, no removal; one warning with classes, the commands and
    # the flag; the app file is byte-identical; exit 0.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    before = fake.settings.read_bytes()
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC]) == 0

    assert len(fake.execs(app_shadow.REPORT_PROGRAM)) == 1
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert fake.settings.read_bytes() == before
    out = _flat(capsys.readouterr().out)
    assert "Settings saved in the app override this setup once the stack is recreated" in out
    assert (
        "LLM_BASE_URL (route; this setup removed it, the app's copy would bring it "
        "back), OPENAI_API_KEY (credential; this setup removed it" in out
    )
    assert (
        "an admin runs /settings clear LLM_BASE_URL and /settings clear "
        "OPENAI_API_KEY in the app" in out
    )
    # A re-run with the same values checks nothing, so the advice names the
    # run that makes the change, never a bare re-run.
    assert "pass --clear-app-overrides on the run that makes the change" in out
    assert "re-run setup with --clear-app-overrides" not in out


@pytest.mark.parametrize("stack", ["slim", "full"])
def test_headless_clear_flag_removes_every_candidate(monkeypatch, tmp_path, capsys, stack):
    # W12 + W17: one probe and one removal, both execs into the stack's api
    # service (never the worker) with the spec's compose args and env.
    root, fake = _installed(
        monkeypatch, tmp_path, *_GATEWAY, stack=stack, app_file=_APP_GATEWAY_FILE
    )
    capsys.readouterr()

    assert setup_main([
        *_docker(stack), "--root", str(root), *_ANTHROPIC, "--clear-app-overrides",
    ]) == 0

    assert fake.text() == "# keep-me\nUSER_TIMEZONE=UTC\n"
    assert fake.settings.stat().st_mode & 0o777 == 0o600
    out = _flat(capsys.readouterr().out)
    assert "Removed the app's copy of LLM_BASE_URL, OPENAI_API_KEY." in out
    prefix = (
        ["docker", "compose", "-f", "docker-compose.single.yml", "exec", "-T", "nymeria-single"]
        if stack == "slim"
        else ["docker", "compose", "--env-file", ".env.docker", "exec", "-T", "api"]
    )
    execs = [(cmd, env) for cmd, env in zip(fake.calls, fake.envs) if "exec" in cmd]
    assert len(execs) == 2
    for cmd, env in execs:
        assert cmd[: len(prefix)] == prefix
        assert "worker" not in cmd
        # finalize's _compose_env: the API port pinned for compose.
        assert env["API_PORT"] == "8000"


def test_the_no_clear_flag_never_asks(monkeypatch, tmp_path):
    # W13: the interactive wizard with --no-clear-app-overrides warns only.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    console = _interactive(monkeypatch)

    assert setup_main([
        *_docker(), "--root", str(root), *_ANTHROPIC, "--no-clear-app-overrides",
    ]) == 0

    assert console.prompts == []
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert fake.text() == _APP_GATEWAY_FILE
    assert "pass --clear-app-overrides on the run that makes the change" in console.text


@pytest.mark.parametrize(
    "state,why",
    [
        ("down", "the stack is not running (or did not answer)"),
        ("old image", "the running image predates this check"),
    ],
)
def test_an_unchecked_stack_names_this_runs_keys_conditionally(
    monkeypatch, tmp_path, state, why
):
    # W14: no prompt, no removal; the warning names the run's route and
    # credential keys as "if the app saved its own copy".
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    if state == "down":
        fake.down = True
    else:
        fake.report_stdout = app_shadow.REPORT_MARKER + "unsupported\n"
    console = _interactive(monkeypatch)

    assert setup_main([
        *_docker(), "--root", str(root), *_ANTHROPIC, "--clear-app-overrides",
    ]) == 0

    assert console.prompts == []
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert fake.text() == _APP_GATEWAY_FILE
    out = console.text
    assert f"Setup could not check the settings saved in the app: {why}." in out
    assert "If the app saved its own copy (/data/settings.env on the data volume)" in out
    for key, cls in (
        ("LLM_BASE_URL", "route"), ("LLM_PROVIDER", "route"), ("OPENAI_API_KEY", "credential"),
    ):
        assert f"{key} ({cls})" in out
        # The real commands, not a placeholder (review NIT, W14).
        assert f"/settings clear {key}" in out
    assert "<KEY>" not in out


@pytest.mark.parametrize(
    "stack,command",
    [
        ("slim", "docker compose -f docker-compose.single.yml up -d"),
        ("full", "docker compose --env-file .env.docker up -d"),
    ],
)
def test_a_scoped_provider_run_asks_first_and_names_the_recreate(
    monkeypatch, tmp_path, stack, command
):
    # W16: the question flow runs before "Updated", which now names the
    # command that applies the change.
    root, fake = _installed(
        monkeypatch, tmp_path, *_GATEWAY, stack=stack, app_file=_APP_GATEWAY_FILE
    )
    console = _interactive(monkeypatch, "y", "y")

    assert setup_main(["provider", *_docker(stack), "--root", str(root), *_ANTHROPIC]) == 0

    assert len(console.prompts) == 2
    out = console.text
    updated = out.index("Updated the provider settings.")
    assert out.index("Removed the app's copy of LLM_BASE_URL, OPENAI_API_KEY.") < updated
    assert f"Recreate the stack from {root} to apply them: {command}" in out[updated:]


def test_nothing_crosses_the_exec_boundary_but_names(monkeypatch, tmp_path):
    # W18: dummy values in .env.docker and the app file never reach console
    # output, an exec argv, or (running the real programs) their output.
    root, fake = _installed(
        monkeypatch, tmp_path, *_ANTHROPIC,
        app_file=(
            f"OPENAI_API_KEY=sk-{DUMMY}-vendor\nLLM_BASE_URL=http://{DUMMY}.example/v1\n"
            f"GEMINI_API_KEY=AIza-{DUMMY}\n"
        ),
    )
    console = _interactive(monkeypatch, "y", "y")

    assert setup_main([*_docker(), "--root", str(root), *_GATEWAY]) == 0

    assert len(fake.execs(app_shadow.APPLY_PROGRAM)) == 1
    assert fake.outputs and all(DUMMY not in output for output in fake.outputs)
    assert all(DUMMY not in arg for cmd in fake.calls for arg in cmd)
    assert DUMMY not in console.buffer.getvalue()
    assert all(DUMMY not in prompt for prompt in console.prompts)


def test_an_unparsable_line_is_reported_never_claimed_removed(monkeypatch, tmp_path, capsys):
    # W19: the other key still clears; the export line stays, with the recipe.
    app_file = f"LLM_BASE_URL=http://127.0.0.1:9/v1\nexport OPENAI_API_KEY=sk-{DUMMY}-appgw\n"
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=app_file)
    capsys.readouterr()

    assert setup_main([
        *_docker(), "--root", str(root), *_ANTHROPIC, "--clear-app-overrides",
    ]) == 0

    assert fake.text() == f"export OPENAI_API_KEY=sk-{DUMMY}-appgw\n"
    out = _flat(capsys.readouterr().out)
    assert "Removed the app's copy of LLM_BASE_URL." in out
    assert (
        "Could not remove OPENAI_API_KEY: its line in /data/settings.env is not in "
        "KEY=value form" in out
    )
    assert (
        "docker compose -f docker-compose.single.yml exec nymeria-single sed -i -E "
        "'/^[[:space:]]*(export[[:space:]]+)?OPENAI_API_KEY[[:space:]]*=/d' "
        "/data/settings.env" in out
    )


def test_a_key_gone_before_the_removal_reads_as_already_gone(monkeypatch, tmp_path, capsys):
    # W20: removed in the app between the probe and the removal.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)

    def removed_in_app(stack):
        stack.settings.write_text(
            stack.text().replace("LLM_BASE_URL=http://127.0.0.1:9/v1\n", ""),
            encoding="utf-8",
        )

    fake.before_apply = removed_in_app
    capsys.readouterr()

    assert setup_main([
        *_docker(), "--root", str(root), *_ANTHROPIC, "--clear-app-overrides",
    ]) == 0

    out = _flat(capsys.readouterr().out)
    assert "LLM_BASE_URL: already gone from the app's settings." in out
    assert "Removed the app's copy of OPENAI_API_KEY." in out
    assert "Could not remove" not in out
    assert fake.text() == "# keep-me\nUSER_TIMEZONE=UTC\n"


@pytest.mark.parametrize("clear", [False, True])
def test_after_the_start_a_kept_dropped_key_is_named_and_a_cleared_one_never(
    monkeypatch, tmp_path, capsys, clear
):
    # W21 (DP6): judged by the recreated container. Kept: the app's copies
    # of keys this run dropped are named as brought back. Cleared: neither
    # is named, and the plain setting the app saved stays unmentioned.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    capsys.readouterr()
    flags = ["--clear-app-overrides"] if clear else []

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC, "--start", *flags]) == 0

    # The post-start probe ran against the recreated container.
    assert len(fake.execs(app_shadow.REPORT_PROGRAM)) == 2
    assert "LLM_BASE_URL" not in fake.env
    out = _flat(capsys.readouterr().out)
    after_start = out[out.index("Nymeria is up."):]
    if clear:
        # This run runs the start itself, so the recreate it names is its own.
        assert (
            "This takes effect when the stack is recreated: the start command "
            "(`docker compose -f docker-compose.single.yml up -d`) does that." in out
        )
        assert "LLM_BASE_URL" not in after_start and "OPENAI_API_KEY" not in after_start
    else:
        assert (
            "This setup removed LLM_BASE_URL, OPENAI_API_KEY from .env.docker, but the "
            "app's saved copies (/data/settings.env) bring them back, so they are "
            "still in effect. To drop them" in after_start
        )
        assert "/settings clear LLM_BASE_URL and /settings clear OPENAI_API_KEY" in after_start
    assert "USER_TIMEZONE" not in after_start
    # The pre-start check reached the stack, so the start asks nothing again.
    assert "in the stack that just started" not in after_start
    assert [cmd for cmd in fake.calls if "restart" in cmd] == []


def _started_from_down(
    monkeypatch, tmp_path, *, stack="slim", app_file=_APP_GATEWAY_FILE, install=_GATEWAY
):
    """W15: an installed stack, stopped before this reconfigure; health waits
    are recorded (and pass)."""
    root, fake = _installed(monkeypatch, tmp_path, *install, stack=stack, app_file=app_file)
    fake.down = True
    health: list[str] = []
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: health.append(kw["url"]) or True
    )
    return root, fake, health


def _restarts(fake: _FakeStack) -> list[list[str]]:
    return [cmd for cmd in fake.calls if "restart" in cmd]


@pytest.mark.parametrize(
    "stack,restart",
    [
        ("slim", ["docker", "compose", "-f", "docker-compose.single.yml", "restart", "nymeria-single"]),
        ("full", ["docker", "compose", "--env-file", ".env.docker", "restart", "api", "worker"]),
    ],
)
def test_a_stack_the_wizard_started_is_checked_after_the_start(
    monkeypatch, tmp_path, stack, restart
):
    # W15 (DP2): the pre-start check found the stack down; the wizard starts
    # it, then the check runs against THIS run's config: it asks, removes,
    # restarts what loads the file (api, plus the worker on the full stack)
    # and waits for health again. The app's LLM_PROVIDER already equals the
    # new value, so it is neither asked about nor removed.
    app_file = _APP_GATEWAY_FILE + "LLM_PROVIDER=anthropic\n"
    root, fake, health = _started_from_down(monkeypatch, tmp_path, stack=stack, app_file=app_file)
    console = _interactive(monkeypatch, "y", "y")

    assert setup_main([*_docker(stack), "--root", str(root), *_ANTHROPIC, "--start"]) == 0

    assert console.prompts == [
        "Remove the app-saved route settings (LLM_BASE_URL) so this choice takes effect? [Y/n]",
        "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]",
    ]
    assert fake.text() == "# keep-me\nUSER_TIMEZONE=UTC\nLLM_PROVIDER=anthropic\n"
    # Ordered: start, the one removal, then the restart; health waited twice.
    applies = fake.execs(app_shadow.APPLY_PROGRAM)
    assert len(applies) == 1
    up = next(i for i, cmd in enumerate(fake.calls) if "up" in cmd)
    assert up < fake.calls.index(applies[0]) < fake.calls.index(restart)
    assert _restarts(fake) == [restart]
    assert len(health) == 2
    out = console.text
    assert "Setup could not check the settings saved in the app" in out
    assert "That copy loads last, so it overrides this setup in the stack that just started." in out
    assert "Removed the app's copy of LLM_BASE_URL, OPENAI_API_KEY." in out
    command = " ".join(restart)
    assert f"The running stack loaded them at boot, so setup restarts it: {command}" in out
    # The closing note does not name what the catch-up already handled.
    tail = out[out.index("so setup restarts it:"):]
    assert "LLM_BASE_URL" not in tail and "OPENAI_API_KEY" not in tail


@pytest.mark.parametrize("how", ["headless", "declined"])
def test_a_started_stack_that_keeps_the_app_copies_is_never_restarted(
    monkeypatch, tmp_path, capsys, how
):
    # W15, warn-only side: the headless default warns about the stack that
    # just started; declining keeps both copies. Either way nothing is
    # removed, no restart, one health wait, and the keys are named once.
    root, fake, health = _started_from_down(monkeypatch, tmp_path)
    console = _interactive(monkeypatch, "n", "n") if how == "declined" else None
    # Headless from a real terminal still never asks.
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC, "--start"]) == 0

    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert fake.text() == _APP_GATEWAY_FILE
    assert _restarts(fake) == []
    assert len(health) == 1
    out = console.text if console is not None else _flat(capsys.readouterr().out)
    if how == "headless":
        assert "Settings saved in the app override this setup in the stack that just started" in out
        assert "in the app. To have setup remove them instead, pass --clear-app-overrides" in out
        assert "in the app after the start" not in out
    else:
        assert len(console.prompts) == 2
        assert (
            "Kept the app's copy of LLM_BASE_URL: it overrides this setup in the "
            "stack that just started." in out
        )
    tail = out[out.index("Nymeria is up."):]
    assert "brings it back" not in tail and "bring them back" not in tail


@pytest.mark.parametrize("how", ["flag", "enter"])
def test_after_the_start_a_real_vendor_key_still_moves_to_its_direct_slot(
    monkeypatch, tmp_path, capsys, how
):
    # The catch-up twin of W7 (review HIGH): the recreated container runs
    # THIS run's gateway route, under which the app's real OpenAI key reads
    # as the gateway's. Judged under the route it was saved under (the old
    # .env.docker, passed in as booleans), it moves exactly as before the
    # start: never discarded.
    vendor = f"sk-{DUMMY}-vendor"
    root, fake, health = _started_from_down(
        monkeypatch, tmp_path, install=_ANTHROPIC, app_file=f"# keep\nOPENAI_API_KEY={vendor}\n"
    )
    console = _interactive(monkeypatch, "") if how == "enter" else None
    flags = ["--clear-app-overrides"] if how == "flag" else []
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_GATEWAY, "--start", *flags]) == 0

    from dotenv import dotenv_values

    saved = dotenv_values(fake.settings)
    assert "OPENAI_API_KEY" not in saved
    assert saved["OPENAI_DIRECT_API_KEY"] == vendor
    assert fake.text().startswith("# keep\n")
    out = console.text if console is not None else _flat(capsys.readouterr().out)
    assert "Moved the app's copy of OPENAI_API_KEY to OPENAI_DIRECT_API_KEY" in out
    assert "discards nothing" not in out
    if console is not None:
        # A relocation keeps Enter as yes.
        assert console.prompts == [
            "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [Y/n]"
        ]
    assert _restarts(fake) == [
        ["docker", "compose", "-f", "docker-compose.single.yml", "restart", "nymeria-single"]
    ]


def test_a_failed_restart_names_the_command_to_run(monkeypatch, tmp_path, capsys):
    # W15 with --clear-app-overrides: the removal ran, the restart did not.
    root, fake, health = _started_from_down(monkeypatch, tmp_path)
    stack_run = subprocess.run

    def failing_restart(cmd, *args, **kwargs):
        if "restart" in cmd:
            fake.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return stack_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_restart)
    capsys.readouterr()

    assert setup_main([
        *_docker(), "--root", str(root), *_ANTHROPIC, "--start", "--clear-app-overrides",
    ]) == 0

    assert fake.text() == "# keep-me\nUSER_TIMEZONE=UTC\n"
    assert len(_restarts(fake)) == 1
    assert len(health) == 1
    out = _flat(capsys.readouterr().out)
    assert (
        "Could not restart automatically. Run `docker compose -f "
        "docker-compose.single.yml restart nymeria-single` yourself so the removal "
        "takes effect." in out
    )


def test_the_flag_on_a_native_install_is_ignored_with_one_note(monkeypatch, tmp_path, capsys):
    # W22 (DP8): no exec, one note, the run succeeds.
    _stub_llm(monkeypatch)
    _hosted_rag(monkeypatch)
    root = tmp_path / "native"
    root.mkdir()
    fake = _FakeStack(tmp_path, root).install(monkeypatch)
    base = [
        "--hosting", "local", "--root", str(root), "--non-interactive",
        "--skip-llm-test", "--no-server-browser",
    ]
    assert setup_main([*base, *_OPENAI]) == 0
    capsys.readouterr()

    assert setup_main([*base, *_ANTHROPIC, "--clear-app-overrides"]) == 0

    assert [cmd for cmd in fake.calls if "exec" in cmd] == []
    out = _flat(capsys.readouterr().out)
    assert out.count("--clear-app-overrides applies only to Docker installs") == 1
    assert "Ignored." in out


def test_force_counts_every_written_route_and_credential_key(monkeypatch, tmp_path, capsys):
    # W23: a fresh write over a running stack has no "before": the app's copy
    # of a key re-written unchanged is still a candidate (the user asked for
    # this file's values wholesale). The same run without --force is not.
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI, app_file=f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
    )
    assert setup_main([*_docker(), "--root", str(root), *_OPENAI]) == 0
    assert fake.execs(app_shadow.REPORT_PROGRAM) == []
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_OPENAI, "--force"]) == 0

    assert len(fake.execs(app_shadow.REPORT_PROGRAM)) == 1
    out = _flat(capsys.readouterr().out)
    assert "loads last and sets OPENAI_API_KEY (credential)." in out


def test_the_clear_flag_parses_three_ways():
    # Section 5: a mode-dependent default (None) with explicit overrides.
    parser = runner_mod.build_parser()
    for argv, expected in (
        ([], None),
        (["--clear-app-overrides"], True),
        (["--no-clear-app-overrides"], False),
    ):
        state = runner_mod._build_state(parser.parse_args(argv))
        assert state.clear_app_overrides is expected


# --- #435 review fixes ---------------------------------------------------------


def test_enter_takes_the_route_group_but_never_discards_a_credential(monkeypatch, tmp_path):
    # Review S3: Enter is yes for the route group (and for a move), but a
    # credential whose copy would be discarded takes an explicit yes.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    console = _interactive(monkeypatch, "", "")

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC]) == 0

    assert console.prompts == [
        "Remove the app-saved route settings (LLM_BASE_URL) so this choice takes effect? [Y/n]",
        "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]",
    ]
    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [["LLM_BASE_URL"]]
    assert fake.text() == f"# keep-me\nOPENAI_API_KEY=sk-{DUMMY}-appgw\nUSER_TIMEZONE=UTC\n"
    assert "Kept the app's copy of OPENAI_API_KEY" in console.text


@pytest.mark.parametrize("app_route", [True, False])
def test_a_key_rotation_asks_about_the_route_the_app_saved_for_its_gateway(
    monkeypatch, tmp_path, app_route
):
    # Review MED: the run only rotates OPENAI_API_KEY (.env.docker keeps its
    # direct openai route), but the app saved its own LiteLLM route, which
    # would send the new key to LiteLLM after the recreate: the app's route
    # group is asked about, with the reason. When the gateway is
    # .env.docker's own (the app saved no route), only the key is asked and
    # the app's model stays.
    if app_route:
        install = _OPENAI
        app_file = (
            "LLM_BASE_URL=http://litellm.example:4000/v1\n"
            "LLM_MODEL=gpt-app\n"
            f"OPENAI_API_KEY=sk-{DUMMY}-litellm\n"
        )
        rotate = _OPENAI_NEW_KEY
    else:
        install = _GATEWAY
        app_file = f"LLM_MODEL=gpt-app\nOPENAI_API_KEY=sk-{DUMMY}-litellm\n"
        rotate = [*_GATEWAY[:-4], "--api-key", f"sk-{DUMMY}-gw2", "--model", "gpt-dummy"]
    root, fake = _installed(monkeypatch, tmp_path, *install, app_file=app_file)
    console = _interactive(monkeypatch, *(["y", "y"] if app_route else ["y"]))

    assert setup_main([*_docker(), "--root", str(root), *rotate]) == 0

    key_prompt = "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]"
    if app_route:
        assert console.prompts == [
            "Remove the app-saved route settings (LLM_BASE_URL, LLM_MODEL) so this "
            "choice takes effect? [Y/n]",
            key_prompt,
        ]
        assert (
            "The app's own saved route sends OPENAI_API_KEY to its gateway, so while "
            "it stays, this setup's new OPENAI_API_KEY goes there too." in console.text
        )
        assert fake.text().strip() == ""
    else:
        assert console.prompts == [key_prompt]
        assert "saved route sends" not in console.text
        assert fake.text() == "LLM_MODEL=gpt-app\n"


def test_an_embedder_change_never_touches_the_apps_llm_route(monkeypatch, tmp_path, capsys):
    # Review F2: EMBEDDING_BASE_URL is route-class but not the LLM route, so
    # `nymeria init embedder` on a GUI-onboarded install (its LLM route and
    # the gateway's key saved in the app) offers only the embedder's own
    # base URL, alone.
    app_file = (
        "LLM_BASE_URL=http://litellm.example:4000/v1\n"
        "LLM_MODEL=gpt-app\n"
        f"OPENAI_API_KEY=sk-{DUMMY}-litellm\n"
        "EMBEDDING_BASE_URL=http://embedder.example/v1\n"
    )
    root, fake = _installed(monkeypatch, tmp_path, *_OPENAI, app_file=app_file)
    real = runner_mod.finalize

    def choosing_an_embedder(state, **kw):
        state.embedder = "value-voyage-lite"  # what the embedder step records
        return real(state, **kw)

    monkeypatch.setattr(runner_mod, "finalize", choosing_an_embedder)
    capsys.readouterr()

    assert setup_main([
        "embedder", *_docker(), "--root", str(root), *_OPENAI,
        "--embedding-api-key", f"pa-{DUMMY}", "--clear-app-overrides",
    ]) == 0

    assert fake.program_args(app_shadow.APPLY_PROGRAM) == [["EMBEDDING_BASE_URL"]]
    assert fake.text() == (
        "LLM_BASE_URL=http://litellm.example:4000/v1\nLLM_MODEL=gpt-app\n"
        f"OPENAI_API_KEY=sk-{DUMMY}-litellm\n"
    )
    assert "Removed the app's copy of EMBEDDING_BASE_URL." in _flat(capsys.readouterr().out)


@pytest.mark.parametrize("how", ["flag", "enter"])
def test_after_the_start_a_key_saved_under_an_unknown_route_is_never_removed_unasked(
    monkeypatch, tmp_path, capsys, how
):
    # Review HIGH (b): the old .env.docker's base URL is a ${...} reference
    # the host cannot resolve, so the route the app saved its sk- key under
    # is unknown: the key may be the vendor's or a gateway's. It never moves,
    # --clear-app-overrides keeps it with a warning, and Enter keeps it.
    saved = f"OPENAI_API_KEY=sk-{DUMMY}-saved\n"
    root, fake, health = _started_from_down(
        monkeypatch, tmp_path, install=_OPENAI, app_file=saved
    )
    env_docker = root / ".env.docker"
    env_docker.write_text(
        env_docker.read_text(encoding="utf-8") + "LLM_BASE_URL=${IT31_OLD_GATEWAY}\n",
        encoding="utf-8",
    )
    console = _interactive(monkeypatch, "") if how == "enter" else None
    flags = ["--clear-app-overrides"] if how == "flag" else []
    capsys.readouterr()

    assert setup_main([*_docker(), "--root", str(root), *_GATEWAY, "--start", *flags]) == 0

    assert fake.text() == saved
    assert fake.execs(app_shadow.APPLY_PROGRAM) == []
    assert _restarts(fake) == []
    out = console.text if console is not None else _flat(capsys.readouterr().out)
    assert (
        "Setup cannot tell whether the app's copy of OPENAI_API_KEY is a real OpenAI "
        "key or a gateway's" in out
    )
    assert "save it as OPENAI_DIRECT_API_KEY first" in out
    assert "moves to" not in out
    if console is not None:
        assert console.prompts == [
            "Remove the app-saved OPENAI_API_KEY so this choice takes effect? [y/N]"
        ]
    else:
        assert "--clear-app-overrides never removes a key setup cannot judge" in out


@pytest.mark.parametrize("answer", ["timeout", "old image"])
def test_a_removal_without_an_answer_says_what_is_known(monkeypatch, tmp_path, capsys, answer):
    # Review S2: the removal is one atomic write. A client that timed out
    # after it may or may not have made the change (say so, and how to see
    # which keys remain, names only); an image without the step changed
    # nothing.
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    if answer == "timeout":
        fake.apply_times_out = True
    else:
        fake.apply_stdout = app_shadow.APPLY_MARKER + "unsupported\n"
    capsys.readouterr()

    assert setup_main([
        *_docker(), "--root", str(root), *_ANTHROPIC, "--clear-app-overrides",
    ]) == 0

    out = _flat(capsys.readouterr().out)
    assert "Removed the app's copy" not in out
    if answer == "timeout":
        assert fake.text() == "# keep-me\nUSER_TIMEZONE=UTC\n"
        assert (
            "Setup could not confirm the removal of LLM_BASE_URL, OPENAI_API_KEY: the "
            "container did not answer, so the change may or may not have been made." in out
        )
        assert (
            "docker compose -f docker-compose.single.yml exec nymeria-single cut -d= "
            "-f1 /data/settings.env" in out
        )
        assert "nothing changed" not in out
    else:
        assert fake.text() == _APP_GATEWAY_FILE
        assert (
            "Could not remove the app's copies: setup could not run the removal in "
            "the container, so nothing changed." in out
        )
        assert "may or may not" not in out


@pytest.mark.parametrize("start", [False, True])
def test_force_over_an_unchanged_config_restarts_instead_of_claiming_a_recreate(
    monkeypatch, tmp_path, capsys, start
):
    # Review F7: --force rewrote the same values, so compose recreates
    # nothing and the running api keeps the removed copy until a restart.
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI, app_file=f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    capsys.readouterr()
    flags = ["--start"] if start else []

    assert setup_main([
        *_docker(), "--root", str(root), *_OPENAI, "--force", "--clear-app-overrides", *flags,
    ]) == 0

    assert fake.text().strip() == ""
    out = _flat(capsys.readouterr().out)
    assert "does that" not in out and "recreate the stack now" not in out
    restart = ["docker", "compose", "-f", "docker-compose.single.yml", "restart", "nymeria-single"]
    if start:
        assert "setup restarts it after the start so this takes effect" in out
        assert _restarts(fake) == [restart]
        up = next(i for i, cmd in enumerate(fake.calls) if "up" in cmd)
        assert up < fake.calls.index(restart)
    else:
        assert (
            f"The stack's configuration did not change, so `up -d` does not recreate "
            f"it: run `{' '.join(restart)}` now so this takes effect." in out
        )
        assert _restarts(fake) == []


@pytest.mark.parametrize("start", [False, True])
def test_force_with_a_rotated_key_rides_the_recreate_without_a_restart(
    monkeypatch, tmp_path, capsys, start
):
    # Delta review finding 3: the F7 twin where --force rewrites the SAME
    # keys with one VALUE changed. That is a different configuration, so the
    # recreate applies the removal: the recreate copy, never the
    # unchanged-config restart.
    root, fake = _installed(
        monkeypatch, tmp_path, *_OPENAI, app_file=f"OPENAI_API_KEY=sk-{DUMMY}-app\n"
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    capsys.readouterr()
    flags = ["--start"] if start else []

    assert setup_main([
        *_docker(), "--root", str(root), *_OPENAI_NEW_KEY, "--force",
        "--clear-app-overrides", *flags,
    ]) == 0

    assert fake.text().strip() == ""
    out = _flat(capsys.readouterr().out)
    assert "did not change" not in out
    assert _restarts(fake) == []
    up = "docker compose -f docker-compose.single.yml up -d"
    if start:
        assert (
            "This takes effect when the stack is recreated: the start command "
            f"(`{up}`) does that." in out
        )
        assert sum("up" in cmd for cmd in fake.calls) == 1
    else:
        assert f"The app's settings already changed, so recreate the stack now with `{up}`." in out
        assert not any("up" in cmd for cmd in fake.calls)


def test_after_the_start_a_copy_declined_before_it_is_not_named_again(monkeypatch, tmp_path):
    # Review NIT: the user said no before the start; the closing note does
    # not repeat it (a headless warning still is, see the W21 test).
    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, app_file=_APP_GATEWAY_FILE)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    console = _interactive(monkeypatch, "n", "n")

    assert setup_main([*_docker(), "--root", str(root), *_ANTHROPIC, "--start"]) == 0

    assert fake.text() == _APP_GATEWAY_FILE
    out = console.text
    assert "Kept the app's copy of LLM_BASE_URL" in out
    after_start = out[out.index("Nymeria is up."):]
    assert "LLM_BASE_URL" not in after_start and "OPENAI_API_KEY" not in after_start


def test_no_start_command_is_built_when_nothing_is_removed(tmp_path):
    # Review NIT: deciding the start command may probe the image, so it is
    # built only for a removal's closing line. The channel answers like a
    # container whose app file holds no key this run changed.
    import json

    report = app_shadow.REPORT_MARKER + json.dumps(
        {"file": True, "keys": [{"key": "USER_TIMEZONE", "class": "setting", "kind": "override"}]}
    )
    built: list[int] = []
    channel = app_shadow.ComposeChannel(
        exec_argv=(sys.executable, "-c", f"print({report!r})"),
        cwd=tmp_path,
        env=dict(os.environ),
        start_command=lambda: built.append(1) or "docker compose up -d",
        restart_command="docker compose restart api",
        exec_hint="docker compose exec api",
    )
    console = _ScriptedConsole()
    diff = app_shadow.config_diff({"OPENAI_API_KEY": "a"}, {"OPENAI_API_KEY": "b"})

    run = app_shadow.check_before_start(
        console, channel, diff, mode=True, interactive=False, gateway_slot=lambda _s: None
    )

    assert run.checked is True
    assert built == []
    assert console.text == ""


def _launched_from(monkeypatch, root: Path) -> dict[str, str]:
    """os.environ as run.py's boot load (`_load_environment`) leaves it for an
    `init` started from ``root``: every env file there merged in with override,
    so the OLD config on a reconfigure."""
    from dotenv import dotenv_values

    from nymeria.config.settings import get_env_file_paths

    loaded: dict[str, str] = {}
    for path in get_env_file_paths(root):
        if path.is_file():
            loaded.update(
                (key, value) for key, value in dotenv_values(path).items() if value is not None
            )
    for key, value in loaded.items():
        monkeypatch.setenv(key, value)
    return loaded


@pytest.mark.parametrize("stack,port", [("full", []), ("slim", ["--port", "8097"])])
def test_the_start_interpolates_this_runs_config_not_the_launch_env(
    monkeypatch, tmp_path, stack, port
):
    # Delta review DF1: `init` runs with the OLD .env.docker in os.environ,
    # and compose resolves `${VAR}` from the process env BEFORE --env-file,
    # so the full stack's api/worker `environment:` block kept the old route
    # and key, and compose saw nothing to recreate. Wherever --env-file is
    # passed (the full stack always; the single container on a non-default
    # port, say), compose must interpolate exactly the file this run wrote,
    # while the shell's own environment (PATH, a DOCKER_HOST export) still
    # reaches docker.
    from dotenv import dotenv_values

    from nymeria.setup.local_rag_install import DOCKER_LOCAL_RAG_ENV

    root, fake = _installed(monkeypatch, tmp_path, *_GATEWAY, *port, stack=stack)
    launched = _launched_from(monkeypatch, root)
    assert launched["LLM_BASE_URL"] == "http://127.0.0.1:9/v1"
    monkeypatch.setenv("NYMERIA_IT31_SHELL_ONLY", "kept")
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)

    assert setup_main([*_docker(stack), "--root", str(root), *_ANTHROPIC, *port, "--start"]) == 0

    new = {k: v for k, v in dotenv_values(root / ".env.docker").items() if v is not None}
    [(up, up_env)] = [(cmd, env) for cmd, env in zip(fake.calls, fake.envs) if "up" in cmd]
    assert up[up.index("--env-file") + 1] == ".env.docker"

    def interpolated(key: str) -> str | None:
        # Compose's precedence: the process env, then --env-file.
        return up_env.get(key, new.get(key))

    # The four old values the unfixed start handed compose: the route, the
    # model, and the retired gateway key.
    assert interpolated("LLM_PROVIDER") == "anthropic"
    assert interpolated("LLM_MODEL") == "claude-dummy"
    assert interpolated("LLM_BASE_URL") is None
    assert interpolated("OPENAI_API_KEY") is None
    # Every key the launch loaded, bar the three finalize pins on purpose.
    pinned = {"API_PORT", "NYMERIA_VERSION", DOCKER_LOCAL_RAG_ENV}
    assert {k for k in launched.keys() - pinned if interpolated(k) != new.get(k)} == set()
    assert up_env["API_PORT"] == (port[1] if port else "8000")
    assert up_env["NYMERIA_IT31_SHELL_ONLY"] == "kept"
    assert up_env["PATH"] == os.environ["PATH"]
