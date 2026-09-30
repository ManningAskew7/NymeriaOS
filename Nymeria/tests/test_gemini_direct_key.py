"""The Gemini direct key (#152): direct Google callers never send a gateway's key.

GEMINI_API_KEY is the google LLM route's key slot, so a CLIProxy antigravity
route stores the proxy's ``cpx-`` gatekeeper there. Gemini TTS, image
generation and Outlook attachment extraction call Google DIRECTLY, so they
resolve through ``Settings.gemini_media_api_key``: GEMINI_DIRECT_API_KEY
first, else GEMINI_API_KEY only while no gateway owns it. The wizard asks
for the direct slot on such a route and retires a stale gatekeeper when the
route is abandoned.
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
    _cliproxy_first_run,
    _env_line,
    _fake_cliproxy_client,
    _stub_llm,
)

PROXY_ROOT = "http://localhost:8318"
GATEKEEPER = "cpx-nymeria-gate"
REAL_KEY = "AIza-real-google-key"
DIRECT_KEY = "AIza-direct-google-key"


def _settings(**kwargs: Any) -> Settings:
    """Settings isolated from env files and every ambient key/route var."""
    for field in (
        "gemini_api_key",
        "gemini_direct_api_key",
        "llm_base_url",
        "openai_api_key",
    ):
        kwargs.setdefault(field, None)
    kwargs.setdefault("llm_provider", "google")
    return Settings(_env_file=None, **kwargs)


def _antigravity(**kwargs: Any) -> Settings:
    """The settings an antigravity apply-route leaves behind."""
    kwargs.setdefault("llm_base_url", PROXY_ROOT)
    kwargs.setdefault("gemini_api_key", GATEKEEPER)
    return _settings(llm_provider="google", **kwargs)


# --- the resolver ------------------------------------------------------------


def test_antigravity_route_uses_the_direct_key_for_media():
    settings = _antigravity(gemini_direct_api_key=DIRECT_KEY)
    assert settings.gemini_media_api_key == DIRECT_KEY


def test_antigravity_route_without_a_direct_key_has_no_media_key():
    settings = _antigravity()
    assert settings.gemini_key_is_gateway_owned() is True
    assert settings.gemini_media_api_key is None
    assert "GEMINI_DIRECT_API_KEY" in settings.gemini_media_key_hint()


def test_a_non_cpx_gateway_key_on_a_google_proxy_route_is_still_withheld():
    """A hand-configured proxy key (not cpx-shaped) behind a google route
    belongs to that gateway: the base URL says so, as it does for anthropic."""
    settings = _settings(llm_base_url=PROXY_ROOT, gemini_api_key="sk-my-proxy-key")
    assert settings.gemini_media_api_key is None


def test_a_cpx_key_is_withheld_even_off_a_google_route():
    settings = _settings(llm_provider="anthropic", gemini_api_key=GATEKEEPER)
    assert settings.gemini_media_api_key is None
    assert "GEMINI_DIRECT_API_KEY" in settings.gemini_media_key_hint()


@pytest.mark.parametrize(
    "base_url",
    [
        None,
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "generativelanguage.googleapis.com",
    ],
)
def test_a_plain_google_key_powers_media_as_before(base_url):
    settings = _settings(llm_base_url=base_url, gemini_api_key=REAL_KEY)
    assert settings.gemini_key_is_gateway_owned() is False
    assert settings.gemini_media_api_key == REAL_KEY
    assert settings.gemini_media_key_hint() == "GEMINI_API_KEY"


def test_a_real_gemini_key_beside_a_claude_proxy_route_still_works():
    """Only the google route puts a gateway key in GEMINI_API_KEY: a Claude or
    Codex subscription route leaves a real media key there untouched."""
    settings = _settings(
        llm_provider="anthropic", llm_base_url=PROXY_ROOT, gemini_api_key=REAL_KEY
    )
    assert settings.gemini_media_api_key == REAL_KEY


def test_a_gatekeeper_pasted_into_the_direct_slot_is_not_sent():
    """The hint says "set GEMINI_DIRECT_API_KEY" beside a cpx- line; copying
    that line over must not open the leak through the new slot."""
    settings = _antigravity(gemini_direct_api_key=GATEKEEPER)
    assert settings.gemini_media_api_key is None
    hint = settings.gemini_media_key_hint()
    assert "GEMINI_DIRECT_API_KEY" in hint and "real Google key" in hint
    # A real GEMINI_API_KEY still serves when the direct slot is unusable.
    fallback = _settings(gemini_api_key=REAL_KEY, gemini_direct_api_key=GATEKEEPER)
    assert fallback.gemini_media_api_key == REAL_KEY


def test_the_direct_slot_wins_over_a_real_gemini_key():
    settings = _settings(gemini_api_key=REAL_KEY, gemini_direct_api_key=DIRECT_KEY)
    assert settings.gemini_media_api_key == DIRECT_KEY


def test_a_look_alike_host_is_not_google():
    settings = _settings(
        llm_base_url="https://googleapis.com.evil.example/v1", gemini_api_key="sk-x"
    )
    assert settings.gemini_media_api_key is None


def test_the_llm_route_keeps_the_gatekeeper():
    """The LLM route talks to the proxy, which wants its own key: unchanged."""
    settings = _antigravity(gemini_direct_api_key=DIRECT_KEY)
    assert settings.get_api_key_for_provider() == GATEKEEPER


# --- the direct callers ------------------------------------------------------


def test_gemini_tts_uses_the_direct_key_and_refuses_the_gatekeeper():
    from nymeria.core.voice import GeminiTTSService, VoiceServiceError, get_tts_service

    tts = get_tts_service(_antigravity(tts_provider="gemini", gemini_direct_api_key=DIRECT_KEY))
    assert isinstance(tts, GeminiTTSService)
    assert tts.api_key == DIRECT_KEY

    with pytest.raises(VoiceServiceError, match="GEMINI_DIRECT_API_KEY"):
        get_tts_service(_antigravity(tts_provider="gemini"))


def _use_settings(monkeypatch, settings: Settings) -> None:
    import nymeria.config as config_pkg
    from nymeria.tools import image_generation

    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)
    monkeypatch.setattr(image_generation, "get_settings", lambda: settings)


class _RecordingGenaiClient:
    """Stands in for google.genai.Client: records the key, then stops the call."""

    keys: list[str] = []

    def __init__(self, *, api_key: str, **_kwargs: Any) -> None:
        type(self).keys.append(api_key)
        raise RuntimeError("stop after construction")


@pytest.fixture
def genai_client(monkeypatch):
    from google import genai

    _RecordingGenaiClient.keys = []
    monkeypatch.setattr(genai, "Client", _RecordingGenaiClient)
    return _RecordingGenaiClient


def test_image_generation_sends_the_direct_key_to_google(monkeypatch, genai_client):
    from nymeria.tools import image_generation

    _use_settings(monkeypatch, _antigravity(gemini_direct_api_key=DIRECT_KEY))
    with pytest.raises(RuntimeError, match="stop after construction"):
        image_generation._generate_gemini("a banana", {})
    assert genai_client.keys == [DIRECT_KEY]


def test_image_generation_never_sends_the_gatekeeper(monkeypatch, genai_client):
    from nymeria.tools import image_generation

    _use_settings(monkeypatch, _antigravity())
    with pytest.raises(RuntimeError, match="GEMINI_DIRECT_API_KEY"):
        image_generation._generate_gemini("a banana", {})
    assert genai_client.keys == []


def test_image_gen_tool_resolves_the_direct_key(monkeypatch):
    from nymeria.tools import image_gen_integrations as igi

    _use_settings(monkeypatch, _antigravity(gemini_direct_api_key=DIRECT_KEY))
    assert igi._get_gemini_image_api_key(None) == DIRECT_KEY


def test_image_gen_tool_refuses_the_gatekeeper_with_the_remedy(monkeypatch):
    from nymeria.tools import image_gen_integrations as igi

    _use_settings(monkeypatch, _antigravity())
    # A raw GEMINI_API_KEY in the environment must not re-open the leak.
    monkeypatch.setenv("GEMINI_API_KEY", GATEKEEPER)
    assert igi._get_gemini_image_api_key(None) is None
    monkeypatch.setattr(
        igi, "_generate_gemini", lambda *a, **k: pytest.fail("no key, no call")
    )
    content, artifact = igi.image_gen_gemini.func(prompt="a banana", config=None)
    assert content.startswith("[Error]")
    assert "GEMINI_DIRECT_API_KEY" in content
    assert artifact == {}


def test_outlook_extraction_uses_the_direct_key(monkeypatch, genai_client):
    from nymeria.tools import outlook_attachments as oa

    _use_settings(monkeypatch, _antigravity(gemini_direct_api_key=DIRECT_KEY))
    result = oa._extract_with_gemini("aGVsbG8=", "application/pdf", "a.pdf")
    assert genai_client.keys == [DIRECT_KEY]
    assert "stop after construction" in result


def test_outlook_extraction_never_sends_the_gatekeeper(monkeypatch, genai_client):
    from nymeria.tools import outlook_attachments as oa

    _use_settings(monkeypatch, _antigravity())
    result = oa._extract_with_gemini("aGVsbG8=", "application/pdf", "a.pdf")
    assert genai_client.keys == []
    assert result.startswith("[Error]")
    assert "GEMINI_DIRECT_API_KEY" in result


# --- the wizard ---------------------------------------------------------------


def _media_state(**kwargs: Any) -> WizardState:
    return WizardState(extras={"image_gen": ["image_gen_gemini"], "tts": "gemini"}, **kwargs)


def test_wizard_asks_for_the_direct_slot_on_an_antigravity_route():
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="antigravity"
    )
    specs = required_backend_credentials(state)
    # One question, deduped across image generation and TTS.
    assert [spec.env_var for spec in specs] == ["GEMINI_DIRECT_API_KEY"]
    assert "Google" in specs[0].note and "directly" in specs[0].note


def test_wizard_on_a_claude_route_still_asks_for_gemini_api_key():
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="claude"
    )
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "GEMINI_API_KEY"
    ]


def test_wizard_reconfigure_does_not_count_the_gatekeeper_as_a_media_key():
    """A hydrated antigravity install has the gatekeeper on disk in
    GEMINI_API_KEY; that must not satisfy the media tools' key question."""
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH,
        cliproxy_provider="antigravity",
        provider="google",
        api_key=GATEKEEPER,
    )
    state.present_env_keys.add("GEMINI_API_KEY")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "GEMINI_DIRECT_API_KEY"
    ]
    state.present_env_keys.add("GEMINI_DIRECT_API_KEY")
    assert required_backend_credentials(state) == []


def test_wizard_asks_again_when_a_gatekeeper_sits_in_gemini_api_key():
    """Switching from the antigravity route to Claude leaves the gatekeeper in
    GEMINI_API_KEY until finalize retires it: present, but it serves nothing."""
    state = _media_state(
        auth_method=ProviderAuthMethod.CLIPROXY_OAUTH, cliproxy_provider="claude"
    )
    state.present_env_keys.add("GEMINI_API_KEY")
    state.gatekeeper_env_keys.add("GEMINI_API_KEY")
    assert [spec.env_var for spec in required_backend_credentials(state)] == [
        "GEMINI_API_KEY"
    ]
    # A real key on disk (no gatekeeper bit) still satisfies.
    state.gatekeeper_env_keys.clear()
    assert required_backend_credentials(state) == []


def test_wizard_plain_google_primary_satisfies_media_keys():
    state = _media_state(provider="google", api_key=REAL_KEY)
    assert required_backend_credentials(state) == []


def _antigravity_install(monkeypatch, root, *extra: str) -> int:
    _stub_llm(monkeypatch)
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("antigravity")])
    return setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "antigravity",
         "--cliproxy-management-url", PROXY_ROOT,
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", GATEKEEPER,
         "--hosting", "local", "--root", str(root),
         "--non-interactive", "--skip-llm-test", *extra]
    )


def test_headless_antigravity_keeps_the_media_key_in_the_direct_slot(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    rc = _antigravity_install(
        monkeypatch, root,
        "--image-gen", "image_gen_gemini", "--tts", "gemini",
        "--gemini-api-key", REAL_KEY,
    )
    assert rc == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(content, "LLM_PROVIDER") == "google"
    assert _env_line(content, "GEMINI_API_KEY") == GATEKEEPER
    assert _env_line(content, "GEMINI_DIRECT_API_KEY") == REAL_KEY
    out = capsys.readouterr().out
    assert "ok Gemini media tools" in " ".join(out.split())


def test_headless_antigravity_without_a_media_key_says_which_slot(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root) == 0
    content = (root / "config.env").read_text(encoding="utf-8")
    assert "GEMINI_DIRECT_API_KEY" not in content
    out = " ".join(capsys.readouterr().out.split())
    assert "-- Gemini media tools (set GEMINI_DIRECT_API_KEY" in out


def test_summary_counts_a_plain_google_primary_as_media_ready(capsys):
    from nymeria.config.llm_providers import get_llm_provider_spec
    from nymeria.setup.finalize import print_capability_summary
    from rich.console import Console

    console = Console(force_terminal=False, width=200)
    print_capability_summary(get_llm_provider_spec("google"), {}, console)
    out = " ".join(capsys.readouterr().out.split())
    assert "ok Gemini media tools" in out
    assert "ok Image generation" in out


def test_reconfigure_hydrates_the_antigravity_pick(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root) == 0
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert state.auth_method is ProviderAuthMethod.CLIPROXY_OAUTH
    assert state.cliproxy_provider == "antigravity"


def test_hydrate_records_which_present_keys_are_gatekeepers(monkeypatch, tmp_path):
    from nymeria.setup.hydrate import hydrate_state_from_disk

    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root, "--gemini-api-key", REAL_KEY) == 0
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    assert {"GEMINI_API_KEY", "GEMINI_DIRECT_API_KEY"} <= state.present_env_keys
    assert "GEMINI_API_KEY" in state.gatekeeper_env_keys
    assert "GEMINI_DIRECT_API_KEY" not in state.gatekeeper_env_keys


def test_an_untouched_antigravity_reconfigure_keeps_its_gatekeeper(
    monkeypatch, tmp_path
):
    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root) == 0
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "GEMINI_API_KEY") == GATEKEEPER
    assert _env_line(after, "LLM_BASE_URL") == PROXY_ROOT


def test_switching_antigravity_to_claude_retires_the_gemini_gatekeeper(
    monkeypatch, tmp_path, capsys
):
    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root) == 0
    capsys.readouterr()
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    assert setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", PROXY_ROOT,
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", GATEKEEPER,
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_PROVIDER") == "anthropic"
    assert _env_line(after, "ANTHROPIC_API_KEY") == GATEKEEPER
    assert "GEMINI_API_KEY" not in after
    out = " ".join(capsys.readouterr().out.split())
    assert "Removed the old CLIProxy route's local key from GEMINI_API_KEY" in out


def test_a_plain_reconfigure_of_an_unnamed_cli_route_keeps_its_gatekeeper(
    monkeypatch, tmp_path
):
    """kimi, grok and gemini-cli share the openai+/v1 chat shape, so hydrate
    cannot name the CLI and the run has no new slot: the route is unchanged
    and its working gatekeeper must stay."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root, provider="kimi")
    config = root / "config.env"
    assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == "cpx-gate"
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == "cpx-gate"


def test_a_key_typed_over_the_gatekeeper_is_not_reported_removed(
    monkeypatch, tmp_path, capsys
):
    """On the switch to Claude a real Gemini key given this run replaces the
    retired gatekeeper line; the notice must not claim the slot was emptied."""
    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root) == 0
    capsys.readouterr()
    _fake_cliproxy_client(monkeypatch, auth_files=[_active_auth("claude")])
    assert setup_main(
        ["--auth-method", "cliproxy_oauth", "--cliproxy-provider", "claude",
         "--cliproxy-management-url", PROXY_ROOT,
         "--cliproxy-management-key", "cpm-secret",
         "--cliproxy-gatekeeper-key", GATEKEEPER,
         "--gemini-api-key", REAL_KEY,
         "--root", str(root), "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "GEMINI_API_KEY") == REAL_KEY
    assert "Removed the old CLIProxy route's local key" not in capsys.readouterr().out


def _hydrated(root) -> WizardState:
    from nymeria.setup.hydrate import hydrate_state_from_disk

    state = WizardState(root=root)
    assert hydrate_state_from_disk(state) is True
    return state


def test_the_provider_step_never_offers_to_keep_a_gatekeeper(monkeypatch, tmp_path):
    """Leaving a Codex route for direct OpenAI: the on-disk OPENAI_API_KEY is
    the proxy's key, which finalize retires, so "leave blank to keep the
    existing key" would end with no key at all."""
    from nymeria.setup.steps.provider import _provider_key_present

    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root, provider="codex")
    state = _hydrated(root)
    assert "OPENAI_API_KEY" in state.present_env_keys
    assert _provider_key_present(state, "openai") is False

    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8").replace("OPENAI_API_KEY=cpx-gate", "OPENAI_API_KEY=sk-real"),
        encoding="utf-8",
    )
    assert _provider_key_present(_hydrated(root), "openai") is True


def test_finalize_does_not_keep_a_gatekeeper_as_a_direct_key(monkeypatch, tmp_path):
    """The finalize backstop: a blank key off the branch is not "kept" when
    the slot holds the retired gatekeeper; the run says no provider is set
    rather than writing a provider it has no key for."""
    from nymeria.onboarding import ProviderAuthMethod
    from nymeria.setup import finalize as finalize_mod
    from rich.console import Console

    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root, provider="codex")
    state = _hydrated(root)
    state.auth_method = ProviderAuthMethod.API_KEY
    state.auth_method_explicit = True
    state.cliproxy_provider = None
    state.provider = "openai"
    state.base_url = ""
    state.api_mode = ""
    state.api_key = ""
    state.skip_llm_test = True
    console = Console(record=True, width=200)
    assert finalize_mod.finalize(state, console=console, non_interactive=True, merge=True) == 0
    assert "No LLM provider configured" in console.export_text()
    assert "OPENAI_API_KEY" not in (root / "config.env").read_text(encoding="utf-8")


def test_an_unrelated_reconfigure_keeps_a_hand_kept_proxy_key(monkeypatch, tmp_path):
    """A direct-key install whose operator keeps a cpx- key by hand (per-thread
    proxy routes read the global slot) is not leaving any proxy route: a plain
    reconfigure must leave that line alone."""
    root = tmp_path / "init"
    _stub_llm(monkeypatch)
    assert setup_main(
        ["--provider", "anthropic", "--model", "claude-direct",
         "--api-key", "sk-ant-direct", "--hosting", "local", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    ) == 0
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + "OPENAI_API_KEY=cpx-hand-kept\n",
        encoding="utf-8",
    )
    assert setup_main(["--root", str(root), "--non-interactive", "--skip-llm-test"]) == 0
    assert _env_line(config.read_text(encoding="utf-8"), "OPENAI_API_KEY") == "cpx-hand-kept"


def test_leaving_antigravity_retires_the_gatekeeper_from_gemini_api_key(
    monkeypatch, tmp_path
):
    root = tmp_path / "init"
    assert _antigravity_install(monkeypatch, root) == 0
    assert setup_main(
        ["--provider", "anthropic", "--model", "claude-direct",
         "--api-key", "sk-ant-direct", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = (root / "config.env").read_text(encoding="utf-8")
    assert _env_line(after, "LLM_PROVIDER") == "anthropic"
    assert "GEMINI_API_KEY" not in after
    assert "LLM_BASE_URL" not in after


def test_leaving_the_proxy_keeps_a_real_key_in_a_gatekeeper_slot(
    monkeypatch, tmp_path
):
    """Only gatekeeper-shaped values retire: a real Gemini key beside a Claude
    subscription route is the user's, and survives the branch exit."""
    root = tmp_path / "init"
    _cliproxy_first_run(monkeypatch, root)
    config = root / "config.env"
    config.write_text(
        config.read_text(encoding="utf-8") + f"GEMINI_API_KEY={REAL_KEY}\n",
        encoding="utf-8",
    )
    assert setup_main(
        ["--provider", "anthropic", "--model", "claude-direct",
         "--api-key", "sk-ant-direct", "--root", str(root),
         "--non-interactive", "--skip-llm-test"]
    ) == 0
    after = config.read_text(encoding="utf-8")
    assert _env_line(after, "GEMINI_API_KEY") == REAL_KEY
    assert "ANTHROPIC_API_KEY" not in after
