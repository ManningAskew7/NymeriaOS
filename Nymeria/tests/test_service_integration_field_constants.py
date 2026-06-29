"""Locks the slice-15 F4 credential field-name constants.

``service_integration_base`` defines a small set of named ``field_names`` tuples
(``API_KEY_FIELDS`` etc.) that the ``*_service_integrations`` modules pass to
``credential_value`` / ``setup_hint``. Those helpers forward the tuple to
``native_credentials``, which tries the fields IN ORDER and takes the first match
(and renders them in order in the setup-hint text), so each constant's value AND
ORDER are behaviorally significant: a constant only legitimately stands in for a
call site whose literal tuple was byte-identical.

These tests guard that contract:

* each constant equals its exact expected tuple (value + order), so an accidental
  reorder/typo that would silently change credential precedence fails here;
* the constants are distinct tuples;
* the deliberately-excluded base-URL order-variant is NOT equal to the promoted
  ``BASE_URL_ALIAS_FIELDS`` (different first-match precedence), documenting why it
  was left inline;
* every migrated module imports the SHARED constant object (identity), so a
  regression that imports the wrong constant, or swaps the shared constant for a
  re-localized copy, fails here.
"""

import importlib

import pytest

from nymeria.tools import service_integration_base as base

# constant name -> its exact expected tuple (elements AND order are load-bearing).
EXPECTED_VALUES = {
    "API_KEY_FIELDS": ("api_key", "value"),
    "API_KEY_ALIAS_FIELDS": ("api_key", "apiKey", "token", "value"),
    "BASE_URL_FIELDS": ("base_url", "url"),
    "BASE_URL_ALIAS_FIELDS": ("base_url", "baseUrl", "api_url", "apiUrl", "url"),
    "USERNAME_FIELDS": ("username", "user", "login"),
}

# module name -> the shared constants it imports and uses at its call sites.
MODULE_WIRING = {
    "relationship_crm_service_integrations": [
        "API_KEY_FIELDS",
        "API_KEY_ALIAS_FIELDS",
        "BASE_URL_ALIAS_FIELDS",
    ],
    "enterprise_business_service_integrations": ["BASE_URL_FIELDS"],
    "time_hr_service_integrations": ["API_KEY_FIELDS", "BASE_URL_ALIAS_FIELDS"],
    "build_ci_service_integrations": ["BASE_URL_ALIAS_FIELDS", "USERNAME_FIELDS"],
    "productivity_service_integrations": ["BASE_URL_FIELDS"],
    "lead_enrichment_service_integrations": [
        "API_KEY_ALIAS_FIELDS",
        "BASE_URL_ALIAS_FIELDS",
    ],
    "personal_device_service_integrations": ["BASE_URL_ALIAS_FIELDS"],
    "notification_service_integrations": ["API_KEY_ALIAS_FIELDS", "BASE_URL_FIELDS"],
    "bookmark_link_service_integrations": ["BASE_URL_ALIAS_FIELDS", "USERNAME_FIELDS"],
    "public_info_integrations": ["API_KEY_FIELDS"],
}

# The base-URL order-variant deliberately kept inline (it puts "url" before
# "api_url"/"apiUrl", a different first-match precedence than BASE_URL_ALIAS_FIELDS).
_BASE_URL_ORDER_VARIANT = ("base_url", "baseUrl", "url", "api_url", "apiUrl")


def _load(module_name: str):
    return importlib.import_module("nymeria.tools." + module_name)


@pytest.mark.parametrize("name, expected", sorted(EXPECTED_VALUES.items()))
def test_constant_has_exact_value_and_order(name, expected):
    value = getattr(base, name)
    assert isinstance(value, tuple), f"{name} must be a tuple"
    # tuple equality is order-sensitive, so this also locks the first-match order.
    assert value == expected, f"{name} value/order drifted"


def test_constants_are_distinct():
    values = [getattr(base, name) for name in EXPECTED_VALUES]
    assert len(set(values)) == len(values), "field-name constants must be distinct"


def test_base_url_order_variant_differs_from_alias_constant():
    # Guards the rationale for leaving the order-variant inline: merging it into
    # BASE_URL_ALIAS_FIELDS would change which field wins first.
    assert _BASE_URL_ORDER_VARIANT != base.BASE_URL_ALIAS_FIELDS


@pytest.mark.parametrize("module_name", sorted(MODULE_WIRING))
def test_modules_reference_shared_constants(module_name):
    mod = _load(module_name)
    for const_name in MODULE_WIRING[module_name]:
        assert hasattr(mod, const_name), (
            f"{module_name} should import {const_name} from service_integration_base"
        )
        assert getattr(mod, const_name) is getattr(base, const_name), (
            f"{module_name}.{const_name} must be the shared "
            f"service_integration_base.{const_name} object"
        )
