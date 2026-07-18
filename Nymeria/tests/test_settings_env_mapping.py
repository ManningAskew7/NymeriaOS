"""Drift guards for the schema-derived settings env mapping.

The settings-field -> dotenv-var map (`server_settings_env_mapping`) is derived from
`ServerSettingsUpdate` plus a tiny `_ENV_VAR_OVERRIDES` table, and the `Settings` model
carries matching `validation_alias`es for the override fields. These tests pin the three
together so a future divergence (a new field whose write env name differs from the name
the model reads it back from, i.e. the S3 bug class) fails loudly here instead of
silently dropping or orphaning a setting at runtime.

Pure and fast: only class-level `model_fields` are read, so nothing loads the env.
"""

from __future__ import annotations

from pydantic import AliasChoices

from nymeria._env_overrides import FIELD_ENV_OVERRIDES
from nymeria.api.schemas.settings import (
    VIRTUAL_UPDATE_FIELDS,
    ServerSettingsUpdate,
    _ENV_VAR_OVERRIDES,
    server_settings_env_mapping,
)
from nymeria.config.settings import Settings


def _model_read_env_name(field_name: str) -> str:
    """The dotenv var pydantic-settings actually loads ``field_name`` from."""
    alias = Settings.model_fields[field_name].validation_alias
    if isinstance(alias, AliasChoices):
        # The first choice is the canonical name PATCH /settings writes to.
        return alias.choices[0]
    if isinstance(alias, str):
        return alias
    # No alias: pydantic-settings matches the (case-insensitive) field name.
    return field_name.upper()


def test_mapping_covers_every_update_field():
    # Virtual fields (llm_api_key) are routed dynamically by the applier, never
    # written to one fixed var, so they are the one sanctioned mapping gap.
    mapping = server_settings_env_mapping()
    missing = set(ServerSettingsUpdate.model_fields) - set(mapping) - VIRTUAL_UPDATE_FIELDS
    assert not missing, f"update fields with no env mapping: {sorted(missing)}"


def test_virtual_fields_are_real_update_fields_and_stay_out_of_the_mapping():
    # The virtual set must name real patchable fields (else it is dead config)
    # and the mapping must skip them (else field.upper() would write a bogus
    # LLM_API_KEY line nothing reads).
    mapping = server_settings_env_mapping()
    for name in VIRTUAL_UPDATE_FIELDS:
        assert name in ServerSettingsUpdate.model_fields, name
        assert name not in mapping, name


def test_every_update_field_exists_on_settings_model():
    # A patchable field with no Settings field would write a dotenv line nothing
    # reads. Virtual fields are exempt: they never write their own var at all.
    missing = (
        set(ServerSettingsUpdate.model_fields)
        - set(Settings.model_fields)
        - VIRTUAL_UPDATE_FIELDS
    )
    assert not missing, f"patchable fields absent from Settings model: {sorted(missing)}"


def test_non_override_fields_map_to_upper():
    mapping = server_settings_env_mapping()
    for name, env_var in mapping.items():
        if name not in _ENV_VAR_OVERRIDES:
            assert env_var == name.upper(), name


def test_overrides_are_real_update_fields():
    for name in _ENV_VAR_OVERRIDES:
        assert name in ServerSettingsUpdate.model_fields, name


def test_s3_overrides_are_the_only_divergence():
    mapping = server_settings_env_mapping()
    diverging = {name for name, env_var in mapping.items() if env_var != name.upper()}
    assert diverging == set(_ENV_VAR_OVERRIDES)


def test_write_env_name_matches_model_read_name():
    # The crown-jewel guard: the var the mapping WRITES for a field must equal the var
    # the Settings model READS that field from. This is exactly what the S3 fields
    # violated (wrote AWS_*, read S3_*) before their validation_aliases were added.
    mapping = server_settings_env_mapping()
    mismatches = {
        name: (env_var, _model_read_env_name(name))
        for name, env_var in mapping.items()
        if name in Settings.model_fields and env_var != _model_read_env_name(name)
    }
    assert not mismatches, f"write/read env name divergence: {mismatches}"


def test_schema_overrides_are_the_shared_constant():
    # The schema's override table is the one shared `FIELD_ENV_OVERRIDES` source, not a
    # private copy, so the write surface cannot drift from the model's read aliases.
    assert _ENV_VAR_OVERRIDES is FIELD_ENV_OVERRIDES


def test_settings_aliases_built_from_shared_constant():
    # The `Settings` model builds each diverging field's primary AliasChoices entry from
    # the same shared table, so a single edit to `FIELD_ENV_OVERRIDES` moves both the
    # model read name and the API write name together.
    for field_name, env_var in FIELD_ENV_OVERRIDES.items():
        alias = Settings.model_fields[field_name].validation_alias
        assert isinstance(alias, AliasChoices), field_name
        assert alias.choices[0] == env_var, (field_name, alias.choices)
