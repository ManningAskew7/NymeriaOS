"""The OpenAI direct key (#428): direct OpenAI callers never send a gateway's key.

OPENAI_API_KEY is the openai LLM route's key slot, so a CLIProxy codex,
gemini-cli, kimi or grok route stores the proxy's ``cpx-`` gatekeeper there.
OpenAI image generation, TTS and STT call OpenAI DIRECTLY, so they resolve
through ``Settings.openai_media_api_key``: OPENAI_DIRECT_API_KEY first, else
OPENAI_API_KEY only while no gateway owns it. The wizard asks for the direct
slot on such a route. The Gemini twin is ``test_gemini_direct_key.py`` (#152).
"""

from __future__ import annotations

from typing import Any

import pytest
from nymeria.config.settings import Settings
from nymeria.onboarding import ProviderAuthMethod
from nymeria.setup.runner import main as setup_main
from nymeria.setup.state import WizardState
from nymeria.setup.tool_keys import required_backend_credentials

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _active_auth,
    _env_line,
    _fake_cliproxy_client,
    _stub_llm,
)

PROXY_V1 = "http://localhost:8318/v1"
GATEKEEPER = "cpx-nymeria-gate"
REAL_KEY = "sk-real-openai-key"
DIRECT_KEY = "sk-direct-openai-key"


def _settings(**kwargs: Any) -> Settings:
    """Settings isolated from env files and every ambient key/route var."""
    for field in (
        "openai_api_key",
        "openai_direct_api_key",
        "llm_base_url",
        "tts_api_key",
        "tts_base_url",
        "stt_api_key",
        "stt_base_url",
    ):
        kwargs.setdefault(field, None)
    kwargs.setdefault("llm_provider", "openai")
    return Settings(_env_file=None, **kwargs)


def _codex(**kwargs: Any) -> Settings:
    """The settings a codex apply-route leaves behind."""
    kwargs.setdefault("llm_base_url", PROXY_V1)
    kwargs.setdefault("openai_api_key", GATEKEEPER)
    return _settings(llm_provider="openai", **kwargs)


# --- the resolver ------------------------------------------------------------


def test_codex_route_uses_the_direct_key_for_media():
    settings = _codex(openai_direct_api_key=DIRECT_KEY)
    assert settings.openai_key_is_gateway_owned() is True
    assert settings.openai_media_api_key == DIRECT_KEY


def test_codex_route_without_a_direct_key_has_no_media_key():
    settings = _codex()
    assert settings.openai_media_api_key is None
    hint = settings.openai_media_key_hint()
    assert "OPENAI_DIRECT_API_KEY" in hint and "gateway" in hint


def test_an_empty_slot_on_a_gateway_route_points_at_the_direct_slot():
    """"Set OPENAI_API_KEY" there would hand a real key to the gateway."""
    settings = _settings(llm_base_url="http://litellm:4000/v1")
    assert settings.openai_media_api_key is None
    assert "OPENAI_DIRECT_API_KEY" in settings.openai_media_key_hint()


def test_a_non_cpx_key_on_an_openai_gateway_route_is_withheld():
    """A hand-configured gateway key (LiteLLM, a local server) behind an openai
    route belongs to that gateway: the base URL says so."""
    settings = _settings(llm_base_url="http://litellm:4000/v1", openai_api_key="sk-litellm")
    assert settings.openai_media_api_key is None
    assert "OPENAI_DIRECT_API_KEY" in settings.openai_media_key_hint()


def test_a_cpx_key_is_withheld_even_off_an_openai_route():
    settings = _settings(llm_provider="anthropic", openai_api_key=GATEKEEPER)
    assert settings.openai_media_api_key is None
    assert "OPENAI_DIRECT_API_KEY" in settings.openai_media_key_hint()


@pytest.mark.parametrize(
    "base_url",
    [None, "", "https://api.openai.com/v1", "api.openai.com", "https://eu.api.openai.com/v1"],
)
def test_a_plain_openai_key_powers_media_as_before(base_url):
    settings = _settings(llm_base_url=base_url, openai_api_key=REAL_KEY)
    assert settings.openai_key_is_gateway_owned() is False
    assert settings.openai_media_api_key == REAL_KEY
    assert settings.openai_media_key_hint() == "OPENAI_API_KEY"


def test_a_real_openai_key_beside_a_claude_proxy_route_still_works():
    """Only the openai route feeds OPENAI_API_KEY to its base URL: a Claude
    subscription route leaves a real media key there untouched."""
    settings = _settings(
        llm_provider="anthropic", llm_base_url="http://localhost:8318", openai_api_key=REAL_KEY
    )
    assert settings.openai_media_api_key == REAL_KEY


def test_a_gatekeeper_pasted_into_the_direct_slot_is_not_sent():
    """The hint says "set OPENAI_DIRECT_API_KEY" beside a cpx- line; copying
    that line over must not open the leak through the new slot."""
    settings = _codex(openai_direct_api_key=GATEKEEPER)
    assert settings.openai_media_api_key is None
    hint = settings.openai_media_key_hint()
    assert "OPENAI_DIRECT_API_KEY" in hint and "real OpenAI key" in hint
    # A real OPENAI_API_KEY still serves when the direct slot is unusable.
    fallback = _settings(openai_api_key=REAL_KEY, openai_direct_api_key=GATEKEEPER)
    assert fallback.openai_media_api_key == REAL_KEY


def test_the_direct_slot_wins_over_a_real_openai_key():
    settings = _settings(openai_api_key=REAL_KEY, openai_direct_api_key=DIRECT_KEY)
    assert settings.openai_media_api_key == DIRECT_KEY


def test_a_look_alike_host_is_not_openai():
    settings = _settings(
        llm_base_url="https://api.openai.com.evil.example/v1", openai_api_key="sk-x"
    )
    assert settings.openai_media_api_key is None


def test_the_llm_route_keeps_the_gatekeeper():
    """The LLM route talks to the proxy, which wants its own key: unchanged."""
    settings = _codex(openai_direct_api_key=DIRECT_KEY)
    assert settings.get_api_key_for_provider() == GATEKEEPER


# --- the direct callers ------------------------------------------------------


def test_openai_tts_and_stt_use_the_direct_key():
    from nymeria.core.voice import STTService, TTSService, get_stt_service, get_tts_service

    settings = _codex(
        openai_direct_api_key=DIRECT_KEY, tts_provider="openai", stt_provider="openai"
    )
    tts = get_tts_service(settings)
    stt = get_stt_service(settings)
    assert isinstance(tts, TTSService) and isinstance(stt, STTService)
    assert tts.api_key == DIRECT_KEY
    assert stt.api_key == DIRECT_KEY


def test_openai_voice_refuses_the_gatekeeper_with_the_remedy():
    from nymeria.core.voice import VoiceServiceError, get_stt_service, get_tts_service

    settings = _codex(tts_provider="openai", stt_provider="openai")
    with pytest.raises(VoiceServiceError, match="OPENAI_DIRECT_API_KEY"):
        get_tts_service(settings)
    with pytest.raises(VoiceServiceError, match="OPENAI_DIRECT_API_KEY"):
        get_stt_service(settings)


def test_openai_voice_on_a_plain_install_is_unchanged():
    from nymeria.core.voice import get_tts_service

    tts = get_tts_service(_settings(openai_api_key=REAL_KEY, tts_provider="openai"))
    assert tts.api_key == REAL_KEY


def _use_settings(monkeypatch, settings: Settings) -> None:
    import nymeria.config as config_pkg
    from nymeria.tools import image_generation

    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)
    monkeypatch.setattr(image_generation, "get_settings", lambda: settings)


class _RecordingOpenAI:
    """Stands in for openai.OpenAI: records the key, then stops the call."""

    keys: list[str] = []

    def __init__(self, *, api_key: str, **_kwargs: Any) -> None:
        type(self).keys.append(api_key)
        raise RuntimeError("stop after construction")


@pytest.fixture
def openai_client(monkeypatch):
    import openai

    _RecordingOpenAI.keys = []
    monkeypatch.setattr(openai, "OpenAI", _RecordingOpenAI)
    return _RecordingOpenAI


def test_image_generation_sends_the_direct_key_to_openai(monkeypatch, openai_client):
    from nymeria.tools import image_generation

    _use_settings(monkeypatch, _codex(openai_direct_api_key=DIRECT_KEY))
    with pytest.raises(RuntimeError, match="stop after construction"):
        image_generation._generate_openai("a banana", {})
    assert openai_client.keys == [DIRECT_KEY]


def test_image_generation_never_sends_the_gatekeeper(monkeypatch, openai_client):
    from nymeria.tools import image_generation

    _use_settings(monkeypatch, _codex())
    with pytest.raises(RuntimeError, match="OPENAI_DIRECT_API_KEY"):
        image_generation._generate_openai("a banana", {})
    assert openai_client.keys == []


def test_image_gen_tool_resolves_the_direct_key(monkeypatch):
    from nymeria.tools import image_gen_integrations as igi

    _use_settings(monkeypatch, _codex(openai_direct_api_key=DIRECT_KEY))
    assert igi._get_openai_image_api_key(None) == DIRECT_KEY


def test_image_gen_tool_refuses_the_gatekeeper_with_the_remedy(monkeypatch, openai_client):
    from nymeria.tools import image_gen_integrations as igi

    _use_settings(monkeypatch, _codex())
    # A raw OPENAI_API_KEY in the environment must not re-open the leak.
    monkeypatch.setenv("OPENAI_API_KEY", GATEKEEPER)
    assert igi._get_openai_image_api_key(None) is None
    content, artifact = igi.image_gen_openai.func(prompt="a banana", config=None)
    assert content.startswith("[Error]")
    assert "OPENAI_DIRECT_API_KEY" in content
    assert artifact == {}
    assert openai_client.keys == []


def test_image_gen_tool_never_sends_a_gatekeeper_from_the_direct_env_var(
    monkeypatch, openai_client
):
    """The config file lands in os.environ (slim loads it, Docker's env_file
    sets it), so a cpx- line copied into OPENAI_DIRECT_API_KEY is in the env
    too: a raw env read behind the screened property would send it."""
    from nymeria.tools import image_gen_integrations as igi

    _use_settings(monkeypatch, _codex(openai_direct_api_key=GATEKEEPER))
    monkeypatch.setenv("OPENAI_DIRECT_API_KEY", GATEKEEPER)
    assert igi._get_openai_image_api_key(None) is None
    content, _artifact = igi.image_gen_openai.func(prompt="a banana", config=None)
    assert "real OpenAI key" in content
    assert openai_client.keys == []


# --- the wizard ---------------------------------------------------------------


def _media_state(**kwargs: Any) -> WizardState:
    return WizardState(
        extras={"image_gen": ["image_gen_openai"], "tts": "openai", "stt": "openai"},
        **kwargs,
    )


@pytest.mark.parametrize("cli", ["codex", "gemini-cli", "kimi", "grok"])
def test_wizard_asks_for_the_direct_slot_on_an_openai_slot_route(cli):
    state = _media_state(auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider=cli)
    specs = required_backend_credentials(state)
    # One question, deduped across image generation, TTS and STT.
    assert [spec.env_var for spec in specs] == ["OPENAI_DIRECT_API_KEY"]
    assert "OpenAI" in specs[0].note and "directly" in specs[0].note


def test_wizard_on_a_claude_route_still_asks_for_openai_api_key():
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="claude"
    )
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_API_KEY"
    ]


def test_wizard_on_an_openai_gateway_route_asks_for_the_direct_slot():
    """Not only CLIProxy: an openai route through any non-OpenAI base URL."""
    state = _media_state(provider="openai", base_url="http://litellm:4000/v1", api_key="sk-gw")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_DIRECT_API_KEY"
    ]


def test_wizard_plain_openai_primary_satisfies_media_keys():
    state = _media_state(provider="openai", api_key=REAL_KEY)
    assert required_backend_credentials(state) == []


def test_wizard_reconfigure_does_not_count_the_gatekeeper_as_a_media_key():
    """A hydrated codex install has the gatekeeper on disk in OPENAI_API_KEY;
    that must not satisfy the media tools' key question."""
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH,
        cliproxy_provider="codex",
        provider="openai",
        base_url=PROXY_V1,
        api_key=GATEKEEPER,
    )
    state.present_env_keys.add("OPENAI_API_KEY")
    state.gatekeeper_env_keys.add("OPENAI_API_KEY")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_DIRECT_API_KEY"
    ]
    state.present_env_keys.add("OPENAI_DIRECT_API_KEY")
    assert required_backend_credentials(state) == []


def test_wizard_does_not_ask_for_a_key_finalize_will_move():
    """A real OpenAI key on disk in the slot codex now takes moves to the
    direct slot at finalize (#431): the keys step does not ask for it again."""
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="codex"
    )
    state.present_env_keys.add("OPENAI_API_KEY")
    state.vendor_env_keys.add("OPENAI_API_KEY")
    assert required_backend_credentials(state) == []
    # Without the vendor bit (a gateway's key on disk) the question stays.
    state.vendor_env_keys.clear()
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_DIRECT_API_KEY"
    ]


def test_wizard_asks_again_when_a_gatekeeper_sits_in_openai_api_key():
    """Switching from the codex route to Claude leaves the gatekeeper in
    OPENAI_API_KEY until finalize retires it: present, but it serves nothing."""
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="claude"
    )
    state.present_env_keys.add("OPENAI_API_KEY")
    state.gatekeeper_env_keys.add("OPENAI_API_KEY")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_API_KEY"
    ]
    # A real key on disk (no gatekeeper bit) still satisfies.
    state.gatekeeper_env_keys.clear()
    assert required_backend_credentials(state) == []


def _codex_install(monkeypatch, root, *extra: str) -> int:
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("codex")])
    return setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", GATEKEEPER,
         "--hosting", "local", "--root", str(root),
         "--non-interactive", "--skip-llm-test", *extra]
    )


def test_headless_codex_keeps_the_media_key_in_the_direct_slot(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    rc = _codex_install(
        monkeypatch, root,
        "--image-gen", "image_gen_openai", "--tts", "openai",
        "--openai-api-key", REAL_KEY,
    )
    assert rc == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_PROVIDER") == "openai"
    assert _env_line(content, "OPENAI_API_KEY") == GATEKEEPER
    assert _env_line(content, "OPENAI_DIRECT_API_KEY") == REAL_KEY
    out = " ".join(capsys.readouterr().out.split())
    assert "ok Image generation" in out


def test_headless_codex_without_a_media_key_says_which_slot(
    monkeypatch, tmp_path, capsys
):
    """The gatekeeper no longer counts as "Image generation ready"."""
    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root) == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert "OPENAI_DIRECT_API_KEY" not in content
    out = " ".join(capsys.readouterr().out.split())
    assert "-- Image generation (" in out
    assert "OPENAI_DIRECT_API_KEY" in out


def test_headless_codex_does_not_relocate_the_gatekeeper_itself(monkeypatch, tmp_path):
    """`--openai-api-key` repeating the gatekeeper is not a media key."""
    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root, "--openai-api-key", GATEKEEPER) == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert "OPENAI_DIRECT_API_KEY" not in content


def test_hydrate_records_the_direct_key_as_present(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root, "--openai-api-key", REAL_KEY) == 0
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert {"OPENAI_API_KEY", "OPENAI_DIRECT_API_KEY"} <= state.present_env_keys
    assert "OPENAI_API_KEY" in state.gatekeeper_env_keys
    assert "OPENAI_DIRECT_API_KEY" not in state.gatekeeper_env_keys


def test_an_untouched_codex_reconfigure_keeps_both_keys(monkeypatch, tmp_path):
    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root, "--openai-api-key", REAL_KEY) == 0
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == GATEKEEPER
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == REAL_KEY


def _summary(spec_name: str | None, optional_env: dict[str, str], capsys, **kwargs: Any) -> str:
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.finalize import print_capability_summary
    from rich.console import Console

    spec = get_llm_provider_spec(spec_name) if spec_name else None
    console = Console(force_terminal=False, width=200)
    print_capability_summary(spec, optional_env, console, **kwargs)
    return " ".join(capsys.readouterr().out.split())


def test_summary_counts_a_plain_openai_primary_as_image_ready(capsys):
    assert "ok Image generation" in _summary("openai", {}, capsys)


def test_summary_does_not_count_a_gateway_owned_openai_slot(capsys):
    out = _summary("openai", {}, capsys, openai_gateway_slot=True)
    assert "-- Image generation (" in out and "OPENAI_DIRECT_API_KEY" in out
    out = _summary("openai", {"OPENAI_DIRECT_API_KEY": REAL_KEY}, capsys, openai_gateway_slot=True)
    assert "ok Image generation" in out


def test_summary_does_not_count_a_gatekeeper_in_a_direct_slot(capsys):
    out = _summary(
        "openai",
        {"OPENAI_DIRECT_API_KEY": GATEKEEPER, "GEMINI_DIRECT_API_KEY": GATEKEEPER},
        capsys,
        openai_gateway_slot=True,
        gemini_gateway_slot=True,
    )
    assert "-- Image generation (" in out
    assert "-- Gemini media tools (" in out


# --- #431: a reconfigure onto a gateway route keeps the old real key ----------


def _plain_install(monkeypatch, root, *extra: str) -> None:
    _stub_llm(monkeypatch)
    assert setup_main(
        ["--provider", "anthropic", "--model", "claude-direct",
         "--api-key", "sk-ant-direct", "--hosting", "local", "--root", str(root),
         "--non-interactive", "--skip-llm-test", *extra]
    ) == 0


def _codex_reconfigure(monkeypatch, root, *extra: str) -> int:
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("codex")])
    return setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", GATEKEEPER,
         "--root", str(root), "--non-interactive", "--skip-llm-test", *extra]
    )


def test_entering_codex_moves_a_real_openai_key_to_the_direct_slot(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    capsys.readouterr()
    assert _codex_reconfigure(monkeypatch, root) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == GATEKEEPER
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == REAL_KEY
    out = " ".join(capsys.readouterr().out.split())
    assert "it is now in OPENAI_DIRECT_API_KEY" in out
    assert REAL_KEY not in out
    assert "ok Image generation" in out


def test_a_manual_openai_gateway_route_also_keeps_the_old_key(monkeypatch, tmp_path):
    """Not only CLIProxy: an openai route through LiteLLM takes the slot too."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    assert setup_main(
        ["--auth-method", "api_key", "--provider", "openai", "--model", "gpt-x",
         "--api-key", "sk-litellm", "--base-url", "http://litellm:4000/v1",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == "sk-litellm"
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == REAL_KEY


def test_a_gateway_key_on_disk_is_never_moved_to_the_direct_slot(
    monkeypatch, tmp_path, capsys
):
    """The old route was openai through LiteLLM, so OPENAI_API_KEY held
    LiteLLM's key: the direct slot would send it to OpenAI."""
    root = tmp_path / "init"
    _stub_llm(monkeypatch)
    assert setup_main(
        ["--provider", "openai", "--model", "gpt-x", "--api-key", "sk-litellm",
         "--base-url", "http://litellm:4000/v1", "--hosting", "local",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    capsys.readouterr()
    assert _codex_reconfigure(monkeypatch, root) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == GATEKEEPER
    assert "OPENAI_DIRECT_API_KEY" not in after
    assert "it is now in" not in capsys.readouterr().out


def test_a_direct_key_already_on_disk_is_left_alone(monkeypatch, tmp_path, capsys):
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + f"OPENAI_DIRECT_API_KEY={DIRECT_KEY}\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert _codex_reconfigure(monkeypatch, root) == 0
    after = config.read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == DIRECT_KEY
    assert "it is now in" not in capsys.readouterr().out


def test_a_direct_key_typed_this_run_wins_over_the_old_one(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    capsys.readouterr()
    assert _codex_reconfigure(monkeypatch, root, "--openai-api-key", DIRECT_KEY) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == GATEKEEPER
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == DIRECT_KEY
    assert "it is now in" not in capsys.readouterr().out


def test_re_running_the_codex_route_moves_nothing(monkeypatch, tmp_path):
    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root) == 0
    assert _codex_reconfigure(monkeypatch, root) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == GATEKEEPER
    assert "OPENAI_DIRECT_API_KEY" not in after


def test_a_plain_openai_key_change_moves_nothing(monkeypatch, tmp_path):
    """No gateway: a new direct OpenAI key simply replaces the old one."""
    root = tmp_path / "init"
    _stub_llm(monkeypatch)
    for key in ("sk-old-openai", "sk-new-openai"):
        assert setup_main(
            ["--provider", "openai", "--model", "gpt-x", "--api-key", key,
             "--hosting", "local", "--root", str(root),
             "--non-interactive", "--skip-llm-test"]
        ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == "sk-new-openai"
    assert "OPENAI_DIRECT_API_KEY" not in after


def test_a_hand_kept_proxy_key_is_never_moved(monkeypatch, tmp_path):
    """A cpx- value beside a non-openai route is a proxy's key, not OpenAI's."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "OPENAI_API_KEY=cpx-hand-kept\n",
        encoding="utf-8",
    )
    assert _codex_reconfigure(monkeypatch, root) == 0
    assert "OPENAI_DIRECT_API_KEY" not in config.read_text(encoding="utf-8")


def test_headless_codex_without_a_gatekeeper_flag_mints_one_and_keeps_the_key(
    monkeypatch, tmp_path
):
    """A real OpenAI key in the slot is not the proxy's gatekeeper: the
    headless branch mints one through the management API instead of sending
    the OpenAI key to the proxy as its bearer (#431 review)."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    _fake_cliproxy_client(
        monkeypatch, auth_files=[_active_auth("codex")], knobs={"api-keys": ["cpx-from-proxy"]}
    )
    assert setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == "cpx-from-proxy"
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == REAL_KEY


def test_a_gateway_route_never_keeps_the_vendor_key_as_its_own(
    monkeypatch, tmp_path, capsys
):
    """Blank key on a new openai route through LiteLLM: "keep the existing
    key" would hand the OpenAI key to LiteLLM. The run stops and says what to
    enter, and the config is untouched."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    config = root / "config.env"
    before = config.read_text(encoding="utf-8")
    capsys.readouterr()
    assert setup_main(
        ["--auth-method", "api_key", "--provider", "openai", "--model", "gpt-x",
         "--base-url", "http://litellm:4000/v1",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 2
    out = " ".join(capsys.readouterr().out.split())
    assert "OPENAI_API_KEY holds your OpenAI key" in out
    assert "gateway's own key" in out
    assert REAL_KEY not in out
    assert config.read_text(encoding="utf-8") == before


def test_a_non_vendor_shaped_key_is_never_moved(monkeypatch, tmp_path):
    """A hand-kept proxy key that is not cpx- shaped is not OpenAI's either."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "OPENAI_API_KEY=my-proxy-key\n",
        encoding="utf-8",
    )
    assert _codex_reconfigure(monkeypatch, root) == 0
    assert "OPENAI_DIRECT_API_KEY" not in config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("install", "value", "is_vendor"),
    [
        ("anthropic", REAL_KEY, True),
        ("anthropic", "my-proxy-key", False),
        ("anthropic", GATEKEEPER, False),
        ("litellm", "sk-litellm", False),
        ("openai", REAL_KEY, True),
    ],
)
def test_hydrate_records_which_shared_slot_holds_the_vendor_key(
    monkeypatch, tmp_path, install, value, is_vendor
):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    root = tmp_path / "init"
    _stub_llm(monkeypatch)
    route = {
        "anthropic": ["--provider", "anthropic", "--model", "claude-direct", "--api-key", "sk-ant-x"],
        "litellm": ["--provider", "openai", "--model", "gpt-x", "--api-key", "sk-litellm",
                    "--base-url", "http://litellm:4000/v1"],
        "openai": ["--provider", "openai", "--model", "gpt-x", "--api-key", REAL_KEY],
    }[install]
    assert setup_main(
        [*route, "--hosting", "local", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    ) == 0
    config = root / "config.env"
    if install == "anthropic":
        config.write_text(
            config.read_text(encoding="utf-8") + f"OPENAI_API_KEY={value}\n",
            encoding="utf-8",
        )
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert "OPENAI_API_KEY" in state.present_env_keys
    assert ("OPENAI_API_KEY" in state.vendor_env_keys) is is_vendor


def _connection_step_collect(base_url: str) -> tuple[bool, str]:
    """Mount the interactive connection step on a reconfigure whose
    OPENAI_API_KEY holds the vendor key and whose key field was left blank,
    set the base URL, and collect."""
    import asyncio

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.steps.provider import make_connection_step
    from textual.widgets import Input, Static

    state = WizardState(provider="openai", api_key="")
    state.present_env_keys.add("OPENAI_API_KEY")
    state.vendor_env_keys.add("OPENAI_API_KEY")

    async def drive():
        app = SetupWizardApp(state, steps=[make_connection_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one("#base-url", Input).value = base_url
            ok = screen.collect()
            await pilot.pause()
            return ok, str(screen.query_one("#wizard-error", Static).render())

    return asyncio.run(drive())


def test_the_connection_step_refuses_a_gateway_url_over_a_kept_vendor_key():
    """Interactive twin of the finalize stop: caught on the step where the
    base URL is typed, not after the whole walkthrough."""
    ok, error = _connection_step_collect("http://litellm:4000/v1")
    assert ok is False
    assert "OPENAI_API_KEY holds your OpenAI key" in error
    assert "gateway's own key" in error
    ok, _ = _connection_step_collect("")
    assert ok is True


def test_a_pass_through_gateway_given_the_same_key_still_fills_the_direct_slot(
    monkeypatch, tmp_path, capsys
):
    """Helicone-style gateways forward the OpenAI key itself, so the user
    enters the same key for the route. The keys step counted the direct slot
    as supplied, so finalize must fill it (#431 review)."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    capsys.readouterr()
    assert setup_main(
        ["--auth-method", "api_key", "--provider", "openai", "--model", "gpt-x",
         "--api-key", REAL_KEY, "--base-url", "https://oai.helicone.ai/v1",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == REAL_KEY
    assert _env_line(after, "OPENAI_DIRECT_API_KEY") == REAL_KEY
    assert "ok Image generation" in " ".join(capsys.readouterr().out.split())


def test_headless_codex_with_a_vendor_key_and_no_management_key_says_what_to_pass(
    monkeypatch, tmp_path
):
    """No management key to mint with, and the key on disk is OpenAI's, not
    the proxy's: the run stops naming the flags instead of wiring the OpenAI
    key in as the proxy's bearer."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    config = root / "config.env"
    before = config.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="--cliproxy-gatekeeper-key"):
        setup_main(
            ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
             "--cliproxy-management-url", "http://localhost:8318",
             "--root", str(root), "--non-interactive", "--skip-llm-test"]
        )
    assert config.read_text(encoding="utf-8") == before


def test_a_skipped_cliproxy_login_over_a_vendor_key_names_the_login(
    monkeypatch, tmp_path
):
    """Interactive codex pick with the login skipped (no gatekeeper) and the
    OpenAI key on disk: the stop speaks the CLIProxy branch's language, not
    "enter the gateway's own key" (that branch has no key field)."""
    from nymeria.setup import finalize as finalize_mod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from rich.console import Console

    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.auth_method = ProviderAuthMethod.CLIPROXY_OAUTH
    state.auth_method_explicit = True
    state.cliproxy_provider = "codex"
    state.cliproxy_management_url = "http://localhost:8318"
    state.cliproxy_logged_in = False
    state.api_key = ""
    state.skip_llm_test = True
    console = Console(record=True, width=200)
    assert finalize_mod.finalize(state, console=console, non_interactive=True, merge=True) == 2
    text = " ".join(console.export_text().split())
    assert "No CLIProxy gatekeeper key is available" in text
    assert "Finish the CLIProxy login" in text
    assert "gateway's own key" not in text


# --- #433: leaving a gateway route retires the gateway's key -----------------


def _litellm_install(monkeypatch, root) -> None:
    _stub_llm(monkeypatch)
    assert setup_main(
        ["--provider", "openai", "--model", "gpt-x", "--api-key", "sk-litellm",
         "--base-url", "http://litellm:4000/v1", "--hosting", "local",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0


def _to_claude(root, *extra: str) -> int:
    return setup_main(
        ["--auth-method", "api_key", "--provider", "anthropic", "--model", "claude-direct",
         "--api-key", "sk-ant-direct", "--root", str(root),
         "--non-interactive", "--skip-llm-test", *extra]
    )


def test_leaving_a_gateway_route_retires_its_key_from_the_shared_slot(
    monkeypatch, tmp_path, capsys
):
    """LiteLLM's key left in OPENAI_API_KEY beside a Claude route would be
    OpenAI image generation's key, sent to api.openai.com."""
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    capsys.readouterr()
    assert _to_claude(root) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_PROVIDER") == "anthropic"
    assert "OPENAI_API_KEY" not in after
    assert "LLM_BASE_URL" not in after
    out = " ".join(capsys.readouterr().out.split())
    assert "Removed the old gateway route's key from OPENAI_API_KEY" in out
    assert "sk-litellm" not in out
    assert "-- Image generation (" in out


def test_a_real_key_given_on_the_way_out_replaces_the_gateway_key(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    capsys.readouterr()
    assert _to_claude(root, "--openai-api-key", REAL_KEY) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == REAL_KEY
    out = capsys.readouterr().out
    assert "Removed the old gateway route's key" not in out


def test_a_blank_key_on_the_way_to_openai_direct_does_not_keep_the_gateway_key(
    monkeypatch, tmp_path, capsys
):
    """openai via LiteLLM, then openai direct with the key field left blank:
    "keep the existing key" would make LiteLLM's key the OpenAI key."""
    from nymeria.setup import finalize as finalize_mod
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from rich.console import Console

    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    config = root / "config.env"
    # Headless: the flag check stops before anything is written.
    before = config.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="--api-key is required"):
        setup_main(
            ["--auth-method", "api_key", "--provider", "openai", "--model", "gpt-x",
             "--base-url", "https://api.openai.com/v1", "--root", str(root),
             "--non-interactive", "--skip-llm-test"]
        )
    assert config.read_text(encoding="utf-8") == before
    # The finalize backstop (the connection step refuses first, below): the
    # run stops naming the slot rather than handing LiteLLM's key to OpenAI.
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    state.base_url = ""
    state.api_key = ""
    state.skip_llm_test = True
    console = Console(record=True, width=200)
    assert finalize_mod.finalize(state, console=console, non_interactive=True, merge=True) == 2
    text = " ".join(console.export_text().split())
    assert "OPENAI_API_KEY holds the old gateway route's key" in text
    assert "sk-litellm" not in text
    assert config.read_text(encoding="utf-8") == before


def test_the_headless_refusal_says_the_slot_holds_the_old_gateway_key(
    monkeypatch, tmp_path
):
    """#433 follow-up: the bare "--api-key is required" read as a wizard that had
    lost the key. It names the slot, why its key does not count, and the flag;
    the config stays byte-identical and the key never appears."""
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    config = root / "config.env"
    before = config.read_bytes()

    with pytest.raises(SystemExit) as refused:
        setup_main(
            ["--auth-method", "api_key", "--provider", "openai", "--model", "gpt-x",
             "--base-url", "https://api.openai.com/v1", "--root", str(root),
             "--non-interactive", "--skip-llm-test"]
        )

    message = str(refused.value.code)
    assert "OPENAI_API_KEY holds the old gateway route's key" in message
    assert "not with https://api.openai.com/v1" in message
    assert "--api-key is required with --non-interactive" in message
    assert "sk-litellm" not in message
    assert config.read_bytes() == before


def test_the_headless_refusal_never_prints_credentials_in_the_base_url(
    monkeypatch, tmp_path
):
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)

    with pytest.raises(SystemExit) as refused:
        setup_main(
            ["--provider", "openai", "--model", "gpt-x",
             "--base-url", "https://gw-user:gw-pass-434@gateway.example/v1?key=q-434",
             "--root", str(root), "--non-interactive", "--skip-llm-test"]
        )

    message = str(refused.value.code)
    assert "not with https://gateway.example/v1." in message
    assert "gw-pass-434" not in message and "gw-user" not in message
    assert "q-434" not in message


def test_a_fresh_headless_install_without_a_key_keeps_the_plain_refusal(tmp_path):
    with pytest.raises(SystemExit) as refused:
        setup_main(
            ["--provider", "openai", "--model", "gpt-x", "--hosting", "local",
             "--root", str(tmp_path / "fresh"), "--non-interactive", "--skip-llm-test"]
        )

    assert str(refused.value.code) == "--api-key is required with --non-interactive"


def test_a_new_openai_key_on_the_way_to_openai_direct_is_written(monkeypatch, tmp_path):
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    assert setup_main(
        ["--auth-method", "api_key", "--provider", "openai", "--model", "gpt-x",
         "--api-key", REAL_KEY, "--base-url", "https://api.openai.com/v1",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == REAL_KEY
    assert _env_line(after, "LLM_BASE_URL") == "https://api.openai.com/v1"


def test_an_untouched_gateway_route_keeps_its_key(monkeypatch, tmp_path):
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == "sk-litellm"
    assert _env_line(after, "LLM_BASE_URL") == "http://litellm:4000/v1"


def test_a_real_media_key_beside_claude_survives_a_plain_reconfigure(monkeypatch, tmp_path):
    root = tmp_path / "init"
    _plain_install(monkeypatch, root, "--openai-api-key", REAL_KEY)
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    assert _env_line((root / "config.env").read_text(encoding="utf-8"), "OPENAI_API_KEY") == REAL_KEY


def test_leaving_a_proxy_route_retires_a_hand_set_non_cpx_proxy_key(monkeypatch, tmp_path):
    """A CLIProxy route whose api-key is not cpx- shaped (set by hand in the
    proxy config) is still that proxy's key."""
    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root) == 0
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"OPENAI_API_KEY={GATEKEEPER}", "OPENAI_API_KEY=sk-my-proxy-key"
        ),
        encoding="utf-8",
    )
    assert _to_claude(root) == 0
    assert "OPENAI_API_KEY" not in config.read_text(encoding="utf-8")


def test_the_keys_step_asks_again_after_leaving_the_gateway_route():
    state = _media_state(provider="anthropic", api_key="sk-ant-x")
    state.present_env_keys.add("OPENAI_API_KEY")
    state.gateway_env_keys.add("OPENAI_API_KEY")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_API_KEY"
    ]
    # On a route that still feeds the slot to a gateway, the question is the
    # direct slot's, as before.
    state = _media_state(provider="openai", base_url="http://litellm:4000/v1", api_key="")
    state.present_env_keys.add("OPENAI_API_KEY")
    state.gateway_env_keys.add("OPENAI_API_KEY")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "OPENAI_DIRECT_API_KEY"
    ]


def test_hydrate_records_a_gateway_key_apart_from_a_vendor_key(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert "OPENAI_API_KEY" in state.gateway_env_keys
    assert "OPENAI_API_KEY" not in state.vendor_env_keys
    assert "OPENAI_API_KEY" not in state.gatekeeper_env_keys


def test_a_same_provider_headless_edit_keeps_the_gateway_base_url(monkeypatch, tmp_path):
    """Only a provider SWITCH drops the hydrated base URL; a new key or
    model for the same openai route through LiteLLM keeps pointing there."""
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    assert setup_main(
        ["--provider", "openai", "--model", "gpt-y", "--api-key", "sk-litellm-2",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_BASE_URL") == "http://litellm:4000/v1"
    assert _env_line(after, "OPENAI_API_KEY") == "sk-litellm-2"


def test_a_hand_kept_non_vendor_key_beside_claude_is_not_retired(monkeypatch, tmp_path):
    """Only a key the ON-DISK route fed to a gateway retires. A non-sk value
    beside a Claude route (a per-thread proxy route's key, say) is the
    operator's, and a plain reconfigure leaves it alone."""
    root = tmp_path / "init"
    _plain_install(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "OPENAI_API_KEY=my-proxy-key\n",
        encoding="utf-8",
    )
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == "my-proxy-key"



def test_a_gateway_key_is_not_carried_to_a_different_gateway(monkeypatch, tmp_path):
    """Same provider, new base URL, blank key: LiteLLM's key is not the new
    gateway's (#433 review). A key given for the new gateway is written."""
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    config = root / "config.env"
    before = config.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="--api-key is required"):
        setup_main(
            ["--provider", "openai", "--model", "gpt-x",
             "--base-url", "https://openrouter.example/api/v1",
             "--root", str(root), "--non-interactive", "--skip-llm-test"]
        )
    assert config.read_text(encoding="utf-8") == before
    assert setup_main(
        ["--provider", "openai", "--model", "gpt-x", "--api-key", "sk-other-gw",
         "--base-url", "https://openrouter.example/api/v1",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == "sk-other-gw"


def test_headless_codex_does_not_adopt_a_litellm_key_as_its_gatekeeper(monkeypatch, tmp_path):
    """LiteLLM's key on disk is no CLIProxy bearer: the headless branch reads
    or mints the proxy's own key (#433 review)."""
    root = tmp_path / "init"
    _litellm_install(monkeypatch, root)
    _fake_cliproxy_client(
        monkeypatch, auth_files=[_active_auth("codex")], knobs={"api-keys": ["cpx-from-proxy"]}
    )
    assert setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "OPENAI_API_KEY") == "cpx-from-proxy"
    assert "sk-litellm" not in after


def test_a_hand_set_proxy_key_still_serves_its_own_cliproxy_route(monkeypatch, tmp_path):
    """A non-cpx api-key set by hand on the proxy, beside a route that was
    already that CLIProxy route, is the gatekeeper: nothing is minted."""
    root = tmp_path / "init"
    assert _codex_install(monkeypatch, root) == 0
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"OPENAI_API_KEY={GATEKEEPER}", "OPENAI_API_KEY=sk-my-proxy-key"
        ),
        encoding="utf-8",
    )
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("codex")], knobs={})
    assert setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "codex",
         "--cliproxy-management-url", "http://localhost:8318",
         "--cliproxy-management-key", "cpm-secret",
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == "sk-my-proxy-key"


def _connection_step_collect_gateway(base_url: str) -> tuple[bool, str]:
    import asyncio

    from nymeria.setup.app import SetupWizardApp
    from nymeria.setup.steps.provider import make_connection_step
    from textual.widgets import Input, Static

    state = WizardState(provider="openai", api_key="")
    state.present_env_keys.add("OPENAI_API_KEY")
    state.gateway_env_keys.add("OPENAI_API_KEY")
    state.extras["llm_base_url_on_disk"] = "http://litellm:4000/v1"

    async def drive():
        app = SetupWizardApp(state, steps=[make_connection_step()])
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one("#base-url", Input).value = base_url
            ok = screen.collect()
            await pilot.pause()
            return ok, str(screen.query_one("#wizard-error", Static).render())

    return asyncio.run(drive())


def test_the_connection_step_refuses_to_carry_a_gateway_key_elsewhere():
    ok, error = _connection_step_collect_gateway("")
    assert ok is False
    assert "OPENAI_API_KEY holds the old gateway's key" in error
    ok, _ = _connection_step_collect_gateway("http://litellm:4000/v1/")
    assert ok is True


def test_the_wizard_and_the_container_judge_shared_slots_with_one_rule():
    # #435 moved the value predicates to the config layer so the container's
    # settings-file report can use them without the wizard package; the
    # wizard's names are the same functions, and every direct-slot map agrees.
    from nymeria.config import vendor_keys
    from nymeria.config.secret_keys import DIRECT_KEY_SLOTS
    from nymeria.setup import tool_keys

    assert tool_keys.route_feeds_gateway is vendor_keys.route_feeds_gateway
    assert tool_keys.slot_holds_vendor_key is vendor_keys.slot_holds_vendor_key
    assert tool_keys.slot_holds_gateway_key is vendor_keys.slot_holds_gateway_key
    for slot, entry in vendor_keys.VENDOR_KEY_SLOTS.items():
        assert tool_keys._DIRECT_MEDIA_SLOTS[slot].env_var == entry.direct
        assert DIRECT_KEY_SLOTS[slot] == entry.direct
        assert tool_keys.direct_slot_vendor(slot) == entry.vendor


@pytest.mark.parametrize(
    "provider,base_url,shape",
    [
        # The app's own LiteLLM route: the key only works there.
        ("openai", "http://litellm.example:4000/v1", "gateway"),
        ("openai", "https://api.openai.com/v1", "vendor"),
        ("openai", "", "vendor"),
        # A different provider's route never reads the slot.
        ("anthropic", "http://litellm.example:4000/v1", "vendor"),
    ],
)
def test_a_shared_slot_shape_follows_the_route_beside_it(provider, base_url, shape):
    from nymeria.config.vendor_keys import shared_slot_shape

    got = shared_slot_shape("OPENAI_API_KEY", REAL_KEY, provider=provider, base_url=base_url)
    assert got == shape
    assert shared_slot_shape("OPENAI_API_KEY", GATEKEEPER, provider=provider, base_url=base_url) == "gatekeeper"
    assert shared_slot_shape("OPENAI_API_KEY", "", provider=provider, base_url=base_url) is None
    assert shared_slot_shape("ANTHROPIC_API_KEY", REAL_KEY, provider=provider, base_url=base_url) is None


def test_the_keys_step_counts_a_new_route_key_typed_this_run():
    """Leaving LiteLLM for openai direct WITH a new key: that key serves
    image generation, so the keys step does not ask for it again."""
    state = _media_state(provider="openai", api_key=REAL_KEY)
    state.present_env_keys.add("OPENAI_API_KEY")
    state.gateway_env_keys.add("OPENAI_API_KEY")
    assert required_backend_credentials(state) == []
