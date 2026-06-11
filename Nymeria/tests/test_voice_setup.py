"""Wizard voice (TTS/STT) wiring: catalog, env mapping, keys, hydrate, flags.

Mirrors the external-access/hosting round-trip tests: the picks must write the
right env lines per hosting shape, retire stale provider-scoped lines on a
provider switch (and only then), re-ask provider-scoped keys after a switch,
and hydrate back from disk so an untouched reconfigure is a no-op.
"""

from __future__ import annotations

from pathlib import Path

from nymeria.onboarding import DockerStack, HostingOption
from nymeria.setup import voice_catalog
from nymeria.setup.runner import _build_state, build_parser
from nymeria.setup.state import WizardState
from nymeria.setup.tool_keys import required_backend_credentials
from nymeria.setup.voice_catalog import (
    voice_drop_env,
    voice_env_for_state,
    voice_provider_changed,
)


def test_choice_values_match_settings_literals():
    """The wizard values ARE the settings Literal values (finalize writes them raw)."""
    from typing import get_args

    from nymeria.config.settings import Settings

    tts_literal = set(get_args(Settings.model_fields["tts_provider"].annotation))
    stt_literal = set(get_args(Settings.model_fields["stt_provider"].annotation))
    assert voice_catalog.TTS_VALUES == tts_literal
    assert voice_catalog.STT_VALUES == stt_literal


def test_skipped_steps_write_nothing():
    state = WizardState()
    assert voice_env_for_state(state) == {}
    assert voice_drop_env(state) == ()


def test_local_hosting_writes_provider_without_base_url():
    state = WizardState(
        hosting=HostingOption.LOCAL,
        extras={"tts": "kokoro", "stt": "faster-whisper"},
    )
    assert voice_env_for_state(state) == {
        "TTS_PROVIDER": "kokoro",
        "STT_PROVIDER": "faster-whisper",
    }


def test_docker_full_stack_writes_sidecar_urls():
    state = WizardState(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.FULL,
        extras={"tts": "kokoro", "stt": "faster-whisper"},
    )
    env = voice_env_for_state(state)
    assert env["TTS_BASE_URL"] == "http://speaches:8000/v1"
    assert env["STT_BASE_URL"] == "http://speaches:8000/v1"
    assert voice_catalog.uses_voice_sidecar(state)


def test_docker_slim_writes_no_sidecar_url():
    state = WizardState(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.SLIM,
        extras={"tts": "kokoro"},
    )
    assert voice_env_for_state(state) == {"TTS_PROVIDER": "kokoro"}
    assert not voice_catalog.uses_voice_sidecar(state)


def test_hosted_providers_never_get_sidecar_urls():
    state = WizardState(
        hosting=HostingOption.DOCKER,
        docker_stack=DockerStack.FULL,
        extras={"tts": "elevenlabs", "stt": "groq"},
    )
    env = voice_env_for_state(state)
    assert "TTS_BASE_URL" not in env and "STT_BASE_URL" not in env


def test_untouched_reconfigure_keeps_everything():
    state = WizardState(extras={
        "tts": "cartesia", "tts_on_disk": "cartesia",
        "stt": "groq", "stt_on_disk": "groq",
    })
    assert not voice_provider_changed(state, "tts")
    assert voice_drop_env(state) == ()


def test_provider_switch_retires_stale_lines():
    state = WizardState(extras={"tts": "elevenlabs", "tts_on_disk": "cartesia"})
    drops = voice_drop_env(state)
    assert "TTS_VOICE" in drops and "TTS_API_KEY" in drops and "TTS_MODEL" in drops
    assert not any(var.startswith("STT") for var in drops)


def test_marker_less_none_pick_drops_nothing():
    """Entering the wizard on a config with no recorded provider and keeping
    voice off must not retire hand-staged voice lines."""
    state = WizardState(extras={"tts": "none", "stt": "none"})
    assert voice_drop_env(state) == ()


def test_stack_switch_retires_wizard_sidecar_url():
    """Full stack -> slim/bare metal with an unchanged local pick: the
    wizard-written speaches URL is retired so the engines run in-process."""
    state = WizardState(
        hosting=HostingOption.LOCAL,
        extras={
            "tts": "kokoro", "tts_on_disk": "kokoro",
            "tts_base_url_on_disk": "http://speaches:8000/v1",
        },
    )
    assert voice_drop_env(state) == ("TTS_BASE_URL",)


def test_hand_set_base_url_survives_reconfigure():
    state = WizardState(
        hosting=HostingOption.LOCAL,
        extras={
            "tts": "kokoro", "tts_on_disk": "kokoro",
            "tts_base_url_on_disk": "http://my-own-speaches:9000/v1",
        },
    )
    assert voice_drop_env(state) == ()


def test_needs_local_voice_extra_per_shape():
    local = WizardState(hosting=HostingOption.LOCAL, extras={"tts": "kokoro"})
    docker = WizardState(
        hosting=HostingOption.DOCKER, docker_stack=DockerStack.FULL,
        extras={"tts": "kokoro"},
    )
    hosted = WizardState(hosting=HostingOption.LOCAL, extras={"tts": "edge"})
    assert voice_catalog.needs_local_voice_extra(local)
    assert not voice_catalog.needs_local_voice_extra(docker)
    assert not voice_catalog.needs_local_voice_extra(hosted)


# ── backend-keys integration ────────────────────────────────────────────────


def test_voice_picks_collect_their_keys():
    state = WizardState(extras={"tts": "cartesia", "stt": "groq"})
    env_vars = [spec.env_var for spec in required_backend_credentials(state)]
    assert env_vars == ["TTS_API_KEY", "TTS_VOICE", "GROQ_API_KEY"]


def test_keyless_voice_picks_collect_nothing():
    state = WizardState(extras={"tts": "kokoro", "stt": "faster-whisper"})
    assert required_backend_credentials(state) == []
    state_edge = WizardState(extras={"tts": "edge", "stt": "none"})
    assert required_backend_credentials(state_edge) == []


def test_openai_voice_pick_satisfied_by_primary_provider_key():
    state = WizardState(provider="openai", api_key="sk-x", extras={"tts": "openai"})
    assert required_backend_credentials(state) == []


def test_same_provider_reconfigure_does_not_reask():
    state = WizardState(extras={"tts": "cartesia", "tts_on_disk": "cartesia"})
    state.present_env_keys.update({"TTS_API_KEY", "TTS_VOICE"})
    assert required_backend_credentials(state) == []


def test_provider_switch_reasks_scoped_keys():
    state = WizardState(extras={"tts": "elevenlabs", "tts_on_disk": "cartesia"})
    state.present_env_keys.update({"TTS_API_KEY", "TTS_VOICE"})
    env_vars = [spec.env_var for spec in required_backend_credentials(state)]
    assert env_vars == ["TTS_API_KEY"]
    # A key typed this run satisfies (and will win over the drop list).
    state.optional_env["TTS_API_KEY"] = "xi-new"
    assert required_backend_credentials(state) == []


# ── finalize env mapping ────────────────────────────────────────────────────


def test_resolve_extra_env_includes_voice():
    from nymeria.setup.finalize import _resolve_extra_env

    state = WizardState(hosting=HostingOption.LOCAL, extras={"tts": "edge", "stt": "groq"})
    extra = _resolve_extra_env(state)
    assert extra["TTS_PROVIDER"] == "edge"
    assert extra["STT_PROVIDER"] == "groq"


# ── hydrate round-trip ──────────────────────────────────────────────────────


def _write_config(tmp_path: Path, lines: list[str]) -> Path:
    config = tmp_path / "config.env"
    config.write_text("\n".join(lines) + "\n")
    return config


def test_hydrate_round_trips_voice_providers(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "LLM_MODEL=gpt-5.5",
        "TTS_PROVIDER=cartesia",
        "TTS_API_KEY=c-key",
        "TTS_VOICE=some-uuid",
        "STT_PROVIDER=groq",
        "GROQ_API_KEY=gsk-x",
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    assert state.extras["tts"] == "cartesia"
    assert state.extras["tts_on_disk"] == "cartesia"
    assert state.extras["stt"] == "groq"
    assert state.extras["stt_on_disk"] == "groq"
    assert {"TTS_API_KEY", "TTS_VOICE", "GROQ_API_KEY"} <= state.present_env_keys
    # Untouched reconfigure: nothing re-asked, nothing dropped.
    assert required_backend_credentials(state) == []
    assert voice_drop_env(state) == ()


def test_hydrate_ignores_hand_edited_garbage(tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, [
        "LLM_PROVIDER=openai",
        "TTS_PROVIDER=sounddblaster16",
    ])
    state = WizardState(root=tmp_path)
    assert hydrate_state_from_disk(state)
    assert "tts" not in state.extras
    assert "tts_on_disk" not in state.extras


def test_hydrate_respects_explicit_flag(tmp_path):
    """A --tts flag wins over the hydrated value (fill-only-if-unset)."""
    from nymeria.setup.hydrate import hydrate_state_from_disk

    _write_config(tmp_path, ["LLM_PROVIDER=openai", "TTS_PROVIDER=cartesia"])
    state = WizardState(root=tmp_path, extras={"tts": "edge"})
    assert hydrate_state_from_disk(state)
    assert state.extras["tts"] == "edge"
    assert state.extras["tts_on_disk"] == "cartesia"
    assert voice_provider_changed(state, "tts")


# ── CLI flags ────────────────────────────────────────────────────────────────


def test_cli_voice_flags_land_in_state():
    parser = build_parser()
    args = parser.parse_args([
        "--tts", "kokoro", "--stt", "groq",
        "--groq-api-key", "gsk-flag",
        "--tts-api-key", "t-key",
        "--tts-voice", "uuid-1",
    ])
    state = _build_state(args)
    assert state.extras["tts"] == "kokoro"
    assert state.extras["stt"] == "groq"
    assert state.optional_env["GROQ_API_KEY"] == "gsk-flag"
    assert state.optional_env["TTS_API_KEY"] == "t-key"
    assert state.optional_env["TTS_VOICE"] == "uuid-1"


def test_cli_rejects_unknown_voice_provider(capsys):
    import pytest

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--tts", "winamp"])


def test_labels_for_review_screen():
    assert voice_catalog.tts_label("kokoro") == "Local: Kokoro"
    assert voice_catalog.stt_label("groq") == "Groq"
    assert voice_catalog.tts_label("unknown-x") == "unknown-x"
