"""Direct coverage for `config/llm_providers` env-var -> Settings-attribute resolution.

`_setting_value_for_env_var` previously carried a per-name `attr_map` whose every
entry mapped `KEY -> key.lower()` (i.e. it was identical to the `env_var.lower()`
fallback for every input). These tests lock the resolver's behavior so the removal
of that dead table stays a provable no-op: each former-mapped key still resolves to
its `key.lower()` Settings attribute, and a diverging field (the S3/AWS credentials,
whose Settings name is NOT `env_var.lower()`) is intentionally never resolved here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nymeria.config.llm_providers import _setting_value_for_env_var, resolve_env_value

# The seven env vars the old `attr_map` listed. Every one maps to `key.lower()`, which
# is exactly the fallback, so the table was dead code.
_FORMER_ATTR_MAP_KEYS = [
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_DIRECT_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
    "GITHUB_TOKEN",
]


@pytest.mark.parametrize("env_var", _FORMER_ATTR_MAP_KEYS)
def test_resolves_former_attr_map_keys_via_lower(env_var):
    attr = env_var.lower()
    settings = SimpleNamespace(**{attr: f"value-for-{attr}"})
    assert _setting_value_for_env_var(settings, env_var) == f"value-for-{attr}"


def test_unknown_attr_returns_none():
    settings = SimpleNamespace()  # no matching attribute
    assert _setting_value_for_env_var(settings, "OPENAI_API_KEY") is None


def test_s3_aws_var_is_not_resolved_here():
    # The S3 credentials diverge (env var AWS_*, Settings field s3_*), so this resolver
    # returns None for them even when the s3_* attribute IS set. This is the documented
    # boundary and proves dropping the attr_map (which never listed S3 vars) is inert.
    settings = SimpleNamespace(
        s3_access_key_id="set-but-not-reachable-via-aws-name",
        aws_access_key_id=None,  # the name env_var.lower() would look up
    )
    assert _setting_value_for_env_var(settings, "AWS_ACCESS_KEY_ID") is None


def test_value_is_stripped_and_blank_becomes_none():
    settings = SimpleNamespace(openai_api_key="  spaced-key  ")
    assert _setting_value_for_env_var(settings, "OPENAI_API_KEY") == "spaced-key"
    blank = SimpleNamespace(openai_api_key="   ")
    assert _setting_value_for_env_var(blank, "OPENAI_API_KEY") is None


def test_resolve_env_value_prefers_settings(monkeypatch):
    # `resolve_env_value` consults Settings before process env / dotenv; a truthy
    # Settings value short-circuits, exercising `_setting_value_for_env_var` on the
    # real call path.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = SimpleNamespace(openai_api_key="from-settings", project_root=None)
    assert resolve_env_value(["OPENAI_API_KEY"], settings=settings) == "from-settings"
