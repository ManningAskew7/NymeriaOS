from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Suite hermeticity, layer 1 (backlog #101 entry 20): pin the project root to
# this checkout BEFORE the first `nymeria` import below. Importing any
# `nymeria.*` module runs `configure_project_root()` and freezes both
# `config.settings.PROJECT_ROOT` and the Settings dotenv paths at import time,
# so an ambient NYMERIA_PROJECT_ROOT (e.g. a multi-instance operator export)
# would silently point the whole suite at another instance's config.env.
os.environ["NYMERIA_PROJECT_ROOT"] = str(Path(__file__).resolve().parents[1])

from nymeria.config.settings import (  # noqa: E402
    reset_env_loading_state_for_tests as _reset_env_loading_state,
    suppress_env_file_loading as _suppress_env_file_loading,
)
from nymeria.triggers import api as api_module  # noqa: E402

# Suite hermeticity, layer 2: with the root pinned, the checkout's own .env /
# config.env / .env.docker are still sitting there to be read (a stray
# .env.docker is exactly why `_offline_tool_search_singleton` below exists), and
# reading them is how the suite ends up running against live operator config
# (#294).
#
# Since #302 those files reach Settings through ONE materializing call
# (`load_env_files_into_environ`, made at boot by run.py and idempotently by
# `get_settings()`), so suppressing that call is the whole defense: no
# `Settings()` and no `cache_clear()` anywhere in the suite can pull the
# checkout's files into `os.environ`. Real env vars keep their normal
# precedence; a test that WANTS a file names its own project_root (tmp_path),
# which stays served. Composes with the suite-wide `clear_settings_cache`
# fixture below.
_suppress_env_file_loading()

# Suite hermeticity, layer 3: pin the process environment itself.
#
# Layers 1 and 2 make every Settings() resolve from real env vars only. That
# is a hole, not a floor: anything writing to `os.environ` between here and a
# test defeats both, because real env vars are exactly what is left. The
# measured case (backlog #294) was `run.py`, whose import-time
# `load_dotenv(..., override=True)` merged the operator's real `.env.docker`
# into the process during COLLECTION, since two test modules import `run` at
# module scope. 65 of that file's 72 keys map to Settings fields, so most of
# the suite silently ran against live deployment config on any machine with a
# populated checkout, and the only visible symptom was two Twitch tests that
# read as an xdist flake. Production code under test writes env too (backlog
# #299).
#
# So snapshot the environment here, while it is still clean (conftest is
# imported before any test module), and restore it before every test. Real
# shell exports are part of the snapshot and keep their normal precedence:
# this guarantees only that no test inherits another's writes, whenever those
# happened.
#
# A HOOK rather than an autouse fixture, deliberately. Autouse fixtures of one
# scope are ordered by registration, which pytest builds from `dir()`, i.e.
# ALPHABETICALLY: as a fixture named `pristine_process_env` this ran LAST of
# the four here (measured with `--setup-show`), after `clear_settings_cache`
# and both `_offline_*` fixtures. Nothing breaks today because none of them
# touch `os.environ`, but the guarantee would have been the reverse of the one
# written above, and the next env-touching fixture whose name sorts earlier
# would have inherited the previous test's leak in silence. `tryfirst` on
# `pytest_runtest_setup` runs before `item.setup()`, so it precedes every
# fixture regardless of name and scope, including module- and class-scoped
# ones set up during a test's setup phase.
# Rich honours FORCE_COLOR even on a captured, non-tty stdout, and the wizard
# tests assert on plain captured text: with it set, "port 8010 is already in
# use" arrives as "port \x1b[1;36m8010\x1b[0m is already in use" and nine
# setup tests fail. Claude Code's shell exports FORCE_COLOR=3 (2026-09-07), so
# drop it before the snapshot pins the environment for every test.
os.environ.pop("FORCE_COLOR", None)
_PRISTINE_ENV = dict(os.environ)

# pytest rewrites PYTEST_CURRENT_TEST per phase and pops it UNGUARDED at
# teardown, so deleting it here would be a KeyError waiting on a hook-ordering
# change. It is absent from the snapshot only because the snapshot predates
# the first test.
_PYTEST_OWNED_ENV = frozenset({"PYTEST_CURRENT_TEST"})


def _restore_pristine_env() -> None:
    """Reset ``os.environ`` to the snapshot taken at conftest import.

    Applies the key-level difference rather than `clear()` + `update()`: the
    common case is no difference at all, and a blanket rewrite would reissue
    `unsetenv`/`putenv` for every key of every test while briefly unsetting
    PYTEST_CURRENT_TEST.
    """
    for key in [
        key
        for key in os.environ
        if key not in _PRISTINE_ENV and key not in _PYTEST_OWNED_ENV
    ]:
        del os.environ[key]
    for key, value in _PRISTINE_ENV.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Layer 3: start every test from the pre-collection environment.

    Layer 3b, same idea one level up: drop the env-load flag and any runtime
    shape pins a previous test registered. Those live in module state that
    outlives a test, and a pin re-applied into a later test's environment is
    exactly the cross-test leak layer 3 exists to prevent (measured: a slim
    launcher test's API_HOST/PORT pins reappearing inside a fat-CLI test).
    """
    _restore_pristine_env()
    _reset_env_loading_state()


@pytest.fixture(autouse=True, scope="session")
def _isolated_user_alias_store(tmp_path_factory: pytest.TempPathFactory):
    """Suite hermeticity for the #133 user-alias store.

    The dispatch seam reads the singleton repo on every unknown leading
    token, so without this an unfixtured `CommandService().execute()` test
    opens the checkout's REAL `data/accounts.db` and an operator's own alias
    row (user ids like "alice" are common to both) could change unrelated
    test outcomes. Session-scoped: one empty throwaway table; tests that
    write aliases use their own function-scoped fixture on top.
    """
    from nymeria.core import user_aliases as _ua

    _ua._repo = _ua.UserAliasesRepo(
        tmp_path_factory.mktemp("user-aliases") / "accounts.db"
    )
    yield
    _ua.reset_user_aliases_repo_for_tests()


@dataclass
class ApiTestSettings:
    data_dir: Path
    nymeria_api_key: str | None = "legacy-secret"
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    context_management: str = "none"
    sliding_window_cycles: int = 20
    memory_char_limit: int = 8000
    todo_auto_archive_days: int = 7
    twitch_chatlog_retention_days: int = 14
    scheduler_missed_work_policy: str = "run"
    scheduler_active_execution_stale_minutes: int = 1440
    default_executor_max_workers: int = 32
    max_concurrent_interactive: int = 0
    interactive_admission_wait_seconds: float = 0.0
    hooks_run_command_enabled: bool = False
    nymeria_debug: bool = False
    api_docs_enabled: bool = False
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_base_url: str = "https://graph.facebook.com/v19.0"
    whatsapp_webhook_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_show_tool_events: bool = False
    teams_bot_app_id: str | None = None
    teams_bot_app_password: str | None = None
    teams_bot_tenant_id: str | None = None
    teams_bot_respond_mode: str = "mention"
    teams_bot_validate_auth: bool = False
    teams_bot_show_tool_events: bool = False
    teams_bot_token_url: str = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    teams_bot_openid_config_url: str = "https://login.botframework.com/v1/.well-known/openidconfiguration"
    cors_origins_list: list[str] | None = None

    def __post_init__(self) -> None:
        if self.cors_origins_list is None:
            self.cors_origins_list = [
                "http://localhost:1420",
                "tauri://localhost",
                "http://tauri.localhost",
                "https://tauri.localhost",
                "http://localhost:8000",
            ]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nymeria.db"


class ApiTestClientBuilder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self._monkeypatch = monkeypatch

    def settings(self, data_dir: Path, **overrides: Any) -> ApiTestSettings:
        return ApiTestSettings(data_dir=data_dir, **overrides)

    def create_checkpoint_table(self, settings: ApiTestSettings) -> None:
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
            conn.commit()

    def client(self, agent: Any, settings: ApiTestSettings) -> TestClient:
        self._monkeypatch.setattr(api_module, "get_settings", lambda: settings)
        return TestClient(api_module.create_api_app(agent))

    def authenticated_client(
        self,
        agent: Any,
        settings: ApiTestSettings,
        *,
        user_id: str = "owner",
        email: str | None = None,
        display_name: str | None = None,
        role: str = "user",
    ) -> tuple[TestClient, str]:
        client = self.client(agent, settings)
        agent.accounts_repo.create_user(
            user_id,
            email or f"{user_id}@example.com",
            display_name or user_id.title(),
            role=role,
        )
        return client, agent.accounts_repo.issue_token(user_id)

    @staticmethod
    def auth(token: str, **headers: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", **headers}


@pytest.fixture(autouse=True)
def _offline_tool_search_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the shared tool-search catalog offline during the suite.

    ``get_tool_search_index()`` builds a process singleton from
    ``settings.embedding_api_key``. Historically a developer shell or a stray
    ``.env.docker`` on the path populated that key, so the first semantic
    tool search made a live OpenAI embeddings call (the dotenv half of that
    leak is now closed by the module-level env-file neutralization above; a
    shell-exported key still applies). Without a client timeout that hung for
    the SDK default and tripped pytest-timeout; even with the new bounded
    timeout, unit tests should never reach the network.

    Pre-seed the singleton with a keyless index so the real
    ``is_semantic_available`` logic returns False and search degrades to the
    in-process lexical ranking instantly. Tests that construct their own
    ``ToolSearchIndex`` are unaffected; a test that wants semantic mode can
    re-patch the singleton with a fake embedder.
    """
    from nymeria.core import tool_search_index as tsi

    monkeypatch.setattr(
        tsi, "_DEFAULT_INDEX", tsi.ToolSearchIndex(embedding_api_key=None)
    )


@pytest.fixture(autouse=True)
def _offline_environment_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `nymeria init` runs from probing the real host.

    Every init run (`setup_main`/`run_init`) caches a detection report on the
    wizard state: loopback socket probes, /proc/meminfo, docker/systemd
    markers, and in interactive mode real `docker info` subprocesses. Stubbed
    suite-wide so no test depends on this machine's docker/systemd/port state
    (a `--hosting docker` test must not fail on a host without docker).
    Gating tests re-patch `runner.detect_environment` with crafted reports;
    detection unit tests call `nymeria.setup.environment` directly, which this
    does not touch.
    """
    from nymeria.onboarding import HostingOption
    from nymeria.setup import runner as runner_module
    from nymeria.setup.environment import EnvironmentReport

    monkeypatch.setattr(
        runner_module,
        "detect_environment",
        lambda **_kw: EnvironmentReport(
            os_label="Linux test",
            is_windows=False,
            docker_available=True,
            recommended_hosting=HostingOption.LOCAL,
        ),
    )


@pytest.fixture(autouse=True)
def _offline_server_browser_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the server-browser launcher off the network and off this host's rig.

    `nymeria init` provisions a headless Chrome by default, and the wizard hook
    runs for every finalize: unstubbed, a generic wizard test read Google's
    Chrome for Testing feed and downloaded a real ~150 MB browser into its
    tmp_path (the suite once did exactly that, silently, for minutes). Both HTTP
    primitives raise here, so `install`/`provision` take their documented
    failure path in milliseconds and the wizard finishes as it would on an
    offline host. Launcher tests pass their own `fetch=`/`download=` fakes and
    are unaffected; a test that forgets one now fails with this message instead
    of reaching the internet.

    The env key goes too: a dogfood box exports `SERVER_BROWSER_HOME`, and the
    CLI-shaped resolver reads the process env first, so without this a test
    would resolve (and could re-bake) the developer's own live rig.
    """
    import urllib.error

    from nymeria import server_browser as sb

    def _offline(url, *_args, **_kwargs):
        raise urllib.error.URLError(
            f"the test suite is offline; the launcher tried to fetch {url}. Pass "
            "fetch=/download= fakes, or stub sb.install / sb.provision."
        )

    monkeypatch.setattr(sb, "_fetch_bytes", _offline)
    monkeypatch.setattr(sb, "_download_file", _offline)
    monkeypatch.delenv(sb.HOME_ENV_KEY, raising=False)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Reset the cached ``get_settings()`` between tests, suite-wide.

    Previously duplicated as a byte-identical per-file autouse fixture in ~34
    test modules (optimization slice 34 F1). ``get_settings`` is a side-effect
    free ``lru_cache``, so clearing it at every test boundary only increases
    isolation. Modules that need to clear additional caches (e.g. a tool's token
    cache) define their own same-named autouse fixture, which overrides this one
    for that module.
    """
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def api_client_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ApiTestClientBuilder]:
    api_module._reset_auth_failure_rate_limiter_for_tests()
    yield ApiTestClientBuilder(monkeypatch)
    api_module._reset_auth_failure_rate_limiter_for_tests()
