"""Locks the slice-15 foundation-helper migration onto ``service_integration_base``.

Each ``*_service_integrations.py`` module in the slice-15 corpus replaced its local
copies of the byte-identical foundation helpers with aliased imports from
``service_integration_base`` (preserving the per-module ``monkeypatch.setattr``
test seam), while keeping a thin local ``_dump_json`` wrapper for its own
``_MAX_JSON_CHARS`` budget and keeping any agent-visibly divergent helper local.

These tests assert that invariant directly:

* every alias resolves to the canonical base function object (identity),
* every ``_dump_json`` is a LOCAL wrapper (not the base object) that truncates at
  the module's own budget, not the base 60k default,
* every deliberately-kept-local divergent helper is NOT the base object.

A regression that re-localises an aliased helper, or that swaps a divergent
helper for the base one (a behavior change), fails here.
"""

import importlib

import pytest

from nymeria.tools import service_integration_base as base

# module name -> {local alias name: canonical base attribute it must resolve to}
ALIASED = {
    "relationship_crm_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_json_object": "json_object",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "enterprise_business_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "microsoft_graph_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "time_hr_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "developer_platform_integrations": {
        "_credential_value": "credential_value",
        "_settings_value": "settings_value",
    },
    "lead_enrichment_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_json_object": "json_object",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "build_ci_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_json_object": "json_object",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "productivity_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "personal_device_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "notification_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "media_discovery_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "public_info_integrations": {
        "_credential_value": "credential_value",
        "_settings_value": "settings_value",
    },
    "bookmark_link_service_integrations": {
        "_base_url": "base_url",
        "_credential_value": "credential_value",
        "_filtered": "filtered",
        "_json_object": "json_object",
        "_settings_value": "settings_value",
        "_setup_hint": "setup_hint",
    },
    "google_business_profile_service_integrations": {},
    "google_analytics_service_integrations": {},
}

# module name -> its _MAX_JSON_CHARS budget (the thin _dump_json wrapper must honour it)
BUDGETS = {
    "relationship_crm_service_integrations": 60_000,
    "enterprise_business_service_integrations": 60_000,
    "microsoft_graph_service_integrations": 80_000,
    "time_hr_service_integrations": 80_000,
    "developer_platform_integrations": 60_000,
    "lead_enrichment_service_integrations": 70_000,
    "build_ci_service_integrations": 70_000,
    "productivity_service_integrations": 60_000,
    "personal_device_service_integrations": 80_000,
    "notification_service_integrations": 60_000,
    "media_discovery_service_integrations": 80_000,
    "public_info_integrations": 60_000,
    "bookmark_link_service_integrations": 60_000,
    "google_business_profile_service_integrations": 80_000,
    "google_analytics_service_integrations": 80_000,
}

# module name -> {local helper that must NOT be the base object: base attribute it diverges from}
# These are the deliberately-kept-local helpers whose behavior differs from the
# canonical base helper (different prefix, error string, drop-set, or defaults).
LOCAL_DIVERGENT = {
    "relationship_crm_service_integrations": {"_basic_auth": "basic_auth"},
    "time_hr_service_integrations": {"_json_object": "json_object"},
    "personal_device_service_integrations": {"_json_object": "json_object"},
    "enterprise_business_service_integrations": {"_json_object": "json_object"},
    "developer_platform_integrations": {
        "_filtered_params": "filtered",
        "_json_object": "json_object",
        "_require_absolute_base_url": "base_url",
    },
    "productivity_service_integrations": {"_filtered_params": "filtered"},
    "public_info_integrations": {
        "_filtered_params": "filtered",
        "_setup_hint": "setup_hint",
    },
}


# Modules whose byte-identical generalized ``_parse_json(value, *, expected, label)``
# helper was consolidated onto ``service_integration_base.parse_json`` (slice 15 F9).
# This corpus spans slices 13/14/15 (the helper was copied across all three), unlike
# the slice-15-only ALIASED map above; the 3 Google modules are intentionally excluded
# (left to their owning slice). A regression that re-localises ``_parse_json`` fails here.
PARSE_JSON_ALIASED = [
    "chat_platform_service_integrations",
    "collaboration_data_service_integrations",
    "commerce_billing_service_integrations",
    "community_publishing_service_integrations",
    "content_management_service_integrations",
    "customer_engagement_service_integrations",
    "data_table_service_integrations",
    "enrichment_security_service_integrations",
    "messaging_delivery_service_integrations",
    "microsoft_graph_service_integrations",
    "notification_service_integrations",
    "operations_monitoring_service_integrations",
    "sales_crm_service_integrations",
    "support_service_integrations",
]


# module name -> (default, max_value) its thin _limit wrapper must bind (slice 15 F3).
# Like dump_json/_dump_json, clamp_limit carries a per-module parameter (the clamp
# bounds), so each module keeps a LOCAL _limit wrapper that binds its own
# default/max_value and delegates to base.clamp_limit, rather than aliasing it. This
# corpus spans slices 13/14/15 (every module had a byte-identical clamp body); the 3
# Google modules are intentionally excluded (left to their owning slice, matching the
# F9 _parse_json scope). A regression that changes a module's default/max_value, or
# that aliases _limit directly to clamp_limit (leaking the base 50/200 bounds), fails
# here.
LIMITS = {
    "bookmark_link_service_integrations": (50, 100),
    "build_ci_service_integrations": (25, 100),
    "business_service_integrations": (20, 100),
    "chat_platform_service_integrations": (50, 200),
    "collaboration_data_service_integrations": (20, 100),
    "commerce_billing_service_integrations": (25, 250),
    "community_publishing_service_integrations": (25, 100),
    "content_management_service_integrations": (25, 500),
    "customer_engagement_service_integrations": (25, 100),
    "data_table_service_integrations": (50, 500),
    "developer_platform_integrations": (20, 100),
    "enrichment_security_service_integrations": (25, 500),
    "enterprise_business_service_integrations": (50, 250),
    "event_meeting_service_integrations": (50, 250),
    "file_storage_service_integrations": (50, 1000),
    "marketing_contact_service_integrations": (20, 100),
    "media_discovery_service_integrations": (10, 50),
    "messaging_delivery_service_integrations": (25, 1000),
    "microsoft_graph_service_integrations": (50, 200),
    "notification_service_integrations": (25, 500),
    "operations_monitoring_service_integrations": (25, 500),
    "personal_device_service_integrations": (25, 100),
    "productivity_service_integrations": (20, 100),
    "project_management_service_integrations": (20, 100),
    "relationship_crm_service_integrations": (50, 200),
    "sales_crm_service_integrations": (50, 500),
    "support_service_integrations": (25, 100),
    "time_hr_service_integrations": (25, 100),
    "work_tracking_service_integrations": (20, 100),
}


def _load(module_name: str):
    return importlib.import_module("nymeria.tools." + module_name)


@pytest.mark.parametrize("module_name", sorted(ALIASED))
def test_aliases_resolve_to_canonical_base_objects(module_name):
    mod = _load(module_name)
    for alias, canonical in ALIASED[module_name].items():
        assert getattr(mod, alias) is getattr(base, canonical), (
            f"{module_name}.{alias} should be service_integration_base.{canonical}"
        )


@pytest.mark.parametrize("module_name", sorted(BUDGETS))
def test_dump_json_is_local_wrapper_honouring_module_budget(module_name):
    mod = _load(module_name)
    # Must be a local wrapper, never the raw base object, so the per-module budget
    # is applied and any future monkeypatch seam on _dump_json keeps working.
    assert mod._dump_json is not base.dump_json
    budget = BUDGETS[module_name]
    out = mod._dump_json({"x": "y" * (budget + 50_000)})
    assert "...[truncated " in out and " chars]" in out
    # Truncated length reflects THIS module's budget, not the base 60k default.
    assert budget < len(out) < budget + 200


@pytest.mark.parametrize("module_name", sorted(LOCAL_DIVERGENT))
def test_divergent_helpers_stay_local(module_name):
    mod = _load(module_name)
    for local_name, base_attr in LOCAL_DIVERGENT[module_name].items():
        assert getattr(mod, local_name) is not getattr(base, base_attr), (
            f"{module_name}.{local_name} diverges from base.{base_attr} and must stay local"
        )


@pytest.mark.parametrize("module_name", sorted(PARSE_JSON_ALIASED))
def test_parse_json_alias_resolves_to_base(module_name):
    mod = _load(module_name)
    assert mod._parse_json is base.parse_json, (
        f"{module_name}._parse_json should be service_integration_base.parse_json"
    )


@pytest.mark.parametrize("module_name", sorted(LIMITS))
def test_limit_is_local_wrapper_honouring_module_bounds(module_name):
    mod = _load(module_name)
    # Must be a LOCAL wrapper, never the raw base object, so each module's own
    # default/max_value bind and any future monkeypatch seam on _limit keeps working.
    assert mod._limit is not base.clamp_limit
    default, max_value = LIMITS[module_name]
    assert mod._limit(3) == 3  # passthrough within range (all bounds admit 3)
    assert mod._limit(10**9) == max_value  # clamped to THIS module's ceiling
    assert mod._limit("bad") == default  # THIS module's fallback on non-int input
