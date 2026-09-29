"""Full Docker stack (Postgres + Redis) and clone-free Docker (published image).

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
import pytest
from nymeria.onboarding import HostingOption
from nymeria.setup import environment as environment_mod
from nymeria.setup import finalize as finalize_mod
from nymeria.setup import runner as runner_mod
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _capture_console,
    _env_line,
    _env_report,
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
    assert "the environment already defines POSTGRES_PASSWORD" in out
    assert "unset POSTGRES_PASSWORD" in out


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
    assert "the environment already defines" not in out


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


@pytest.mark.parametrize("stack,service", [("full", "api"), ("slim", "nymeria-single")])
def test_start_now_names_env_keys_the_app_saved_settings_override(
    monkeypatch, tmp_path, capsys, stack, service
):
    # A value saved in the app lives in /data/settings.env and loads last, so
    # the .env.docker value this run wrote for that key is not in effect.
    _stub_llm(monkeypatch)
    calls = _record_compose(
        monkeypatch,
        overrides_stdout="noise\nNYMERIA_SETTINGS_OVERRIDES=LLM_MODEL,USER_TIMEZONE,bad key;rm\n",
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    root = tmp_path / "checkout"
    root.mkdir()
    args = [a if a != "full" else stack for a in _FULL_ARGS]

    assert setup_main([*args, "--root", str(root), "--start"]) == 0

    [probe] = [cmd for cmd, _env in calls if "python3" in cmd]
    assert probe[probe.index("exec") : probe.index("exec") + 3] == ["exec", "-T", service]
    out = " ".join(capsys.readouterr().out.split())
    assert "LLM_MODEL, USER_TIMEZONE" in out
    # The base is the container environment (compose defaults included), so
    # the copy must not claim every key came from .env.docker.
    assert "set elsewhere (for example in .env.docker)" in out
    assert "bad key" not in out and "rm" not in out.split("USER_TIMEZONE")[1][:20]
    assert "/data/settings.env" in out
    assert "Settings" in out


@pytest.mark.parametrize(
    "stdout,rc",
    [
        ("NYMERIA_SETTINGS_OVERRIDES=\n", 0),  # nothing overridden
        ("Traceback: no such function\n", 1),  # an older image
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


def test_the_in_container_override_snippet_runs_the_servers_own_computation(tmp_path):
    # Runs the real program the wizard execs in the container: a renamed or
    # broken function would otherwise fail silently (non-zero reads as "none").
    marker = finalize_mod._SETTINGS_OVERRIDES_MARKER
    settings_file = tmp_path / "settings.env"
    settings_file.write_text(
        "NYMERIA_TEST_OVERRIDE_PROBE=from-file\nNYMERIA_TEST_ONLY_IN_FILE=x\n",
        encoding="utf-8",
    )
    root = tmp_path / "root"
    root.mkdir()
    env = dict(os.environ)
    env.update(
        NYMERIA_PROJECT_ROOT=str(root),
        NYMERIA_SETTINGS_FILE=str(settings_file),
        NYMERIA_TEST_OVERRIDE_PROBE="from-env",
    )
    checkout = Path(finalize_mod.__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "-c", finalize_mod._SETTINGS_OVERRIDES_SNIPPET],
        cwd=str(checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    [line] = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    # Only a key that held a DIFFERENT value first is an override.
    assert line == marker + "NYMERIA_TEST_OVERRIDE_PROBE"
