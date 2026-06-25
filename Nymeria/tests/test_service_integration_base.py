"""Tests for the shared service-integration foundation helpers."""

import base64
import json

import pytest

from nymeria.tools import service_integration_base as base


# --- dump_json ---------------------------------------------------------------


def test_dump_json_pretty_prints_and_keeps_short_payloads():
    out = base.dump_json({"b": 1, "a": 2})
    assert '"b": 1' in out and '"a": 2' in out
    assert "\n" in out  # indent=2
    assert "truncated" not in out


def test_dump_json_truncates_with_marker_at_max_chars():
    out = base.dump_json({"x": "y" * 5000}, max_chars=100)
    assert len(out) > 100  # body[:100] + marker
    assert out.startswith("{\n")
    assert "...[truncated " in out and " chars]" in out


def test_dump_json_default_budget_is_module_constant():
    assert base.DEFAULT_MAX_JSON_CHARS == 60_000
    big = base.dump_json({"x": "z" * 70_000})
    assert "truncated" in big


def test_dump_json_handles_non_serializable_via_default_str():
    class Weird:
        def __str__(self):
            return "weird-value"

    assert "weird-value" in base.dump_json({"k": Weird()})


# --- filtered ----------------------------------------------------------------


def test_filtered_drops_empty_values_but_keeps_falsy_scalars():
    result = base.filtered(
        {
            "keep_zero": 0,
            "keep_false": False,
            "keep_str": "hi",
            "drop_none": None,
            "drop_empty_str": "",
            "drop_empty_list": [],
            "drop_empty_dict": {},
        }
    )
    assert result == {"keep_zero": 0, "keep_false": False, "keep_str": "hi"}


def test_filtered_none_returns_empty_dict():
    assert base.filtered(None) == {}


# --- base_url ----------------------------------------------------------------


def test_base_url_strips_trailing_slash_for_public_host():
    assert base.base_url("https://api.example.com/v1/") == "https://api.example.com/v1"


@pytest.mark.parametrize("bad", ["ftp://api.example.com", "not-a-url", "", "://nope"])
def test_base_url_rejects_non_http_or_hostless(bad):
    with pytest.raises(ValueError):
        base.base_url(bad)


@pytest.mark.parametrize("blocked", ["http://127.0.0.1:8000", "http://169.254.169.254/latest"])
def test_base_url_enforces_egress_policy(blocked):
    # The cheap scheme/netloc pre-check passes for these, so reaching a rejection
    # proves the second-stage validate_http_egress_url call is wired in.
    with pytest.raises(ValueError, match="egress policy"):
        base.base_url(blocked)


# --- json_object / json_array_or_object --------------------------------------


def test_json_object_parses_and_validates():
    assert base.json_object('{"a": 1}', field_name="f") == {"a": 1}
    assert base.json_object("   ", field_name="f") == {}


def test_json_object_rejects_invalid_and_non_object():
    with pytest.raises(ValueError):
        base.json_object("{not json", field_name="f")
    with pytest.raises(ValueError):
        base.json_object("[1, 2]", field_name="f")


def test_json_array_or_object_normalizes_to_list_of_dicts():
    assert base.json_array_or_object("", field_name="f") == []
    assert base.json_array_or_object('{"a": 1}', field_name="f") == [{"a": 1}]
    assert base.json_array_or_object('[{"a": 1}, {"b": 2}]', field_name="f") == [
        {"a": 1},
        {"b": 2},
    ]


def test_json_array_or_object_rejects_non_dict_records():
    with pytest.raises(ValueError):
        base.json_array_or_object("[1, 2]", field_name="f")
    with pytest.raises(ValueError):
        base.json_array_or_object("{bad", field_name="f")


# --- parse_json --------------------------------------------------------------


def test_parse_json_parses_dict_and_list_payloads():
    assert base.parse_json('{"a": 1}', expected=dict, label="body") == {"a": 1}
    assert base.parse_json('[{"a": 1}, {"b": 2}]', expected=list, label="items") == [
        {"a": 1},
        {"b": 2},
    ]


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_parse_json_empty_returns_empty_instance_of_expected(blank):
    # Whitespace-only input yields the empty instance of the expected type;
    # the two helper copy-groups (``{} if expected is dict`` vs
    # ``[] if expected is list``) are identical for the only two used types.
    assert base.parse_json(blank, expected=dict, label="f") == {}
    assert base.parse_json(blank, expected=list, label="f") == []


def test_parse_json_invalid_json_raises_with_label_and_cause():
    with pytest.raises(ValueError, match="payload must be valid JSON") as exc:
        base.parse_json("{not json", expected=dict, label="payload")
    assert isinstance(exc.value.__cause__, json.JSONDecodeError)


def test_parse_json_wrong_type_raises_with_expected_type_name():
    with pytest.raises(ValueError, match=r"rows must be a JSON list\."):
        base.parse_json('{"a": 1}', expected=list, label="rows")
    with pytest.raises(ValueError, match=r"obj must be a JSON dict\."):
        base.parse_json("[1, 2]", expected=dict, label="obj")


# --- basic_auth --------------------------------------------------------------


def test_basic_auth_encodes_user_and_password():
    assert base.basic_auth("user", "pass") == base64.b64encode(b"user:pass").decode()


def test_basic_auth_defaults_to_empty_password():
    assert base.basic_auth("token") == base64.b64encode(b"token:").decode()


# --- settings_value ----------------------------------------------------------


def test_settings_value_reads_attribute_from_settings(monkeypatch):
    class FakeSettings:
        my_attr = "the-value"

    monkeypatch.setattr("nymeria.config.get_settings", lambda: FakeSettings())
    assert base.settings_value("my_attr") == "the-value"


# --- credential_value / setup_hint -------------------------------------------


def test_credential_value_returns_value_or_none(monkeypatch):
    import nymeria.tools.native_credentials as nc

    class Cred:
        value = "secret"

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: Cred())
    assert (
        base.credential_value(
            provider="p", field_names=("k",), tool_name="t", config=None
        )
        == "secret"
    )

    monkeypatch.setattr(nc, "get_native_credential_value", lambda **kw: None)
    assert (
        base.credential_value(
            provider="p", field_names=("k",), tool_name="t", config=None
        )
        is None
    )


def test_credential_value_passes_through_aliases(monkeypatch):
    import nymeria.tools.native_credentials as nc

    captured = {}

    def fake(**kw):
        captured.update(kw)
        return None

    monkeypatch.setattr(nc, "get_native_credential_value", fake)
    base.credential_value(
        provider="stripe",
        provider_aliases=("stripe_api",),
        field_names=("api_key", "token"),
        tool_name="native_tool:x",
        config=None,
    )
    assert captured["provider"] == "stripe"
    assert captured["provider_aliases"] == ("stripe_api",)
    assert captured["field_names"] == ("api_key", "token")


def test_setup_hint_delegates_to_native_credentials(monkeypatch):
    import nymeria.tools.native_credentials as nc

    captured = {}

    def fake(**kw):
        captured.update(kw)
        return "hint-text"

    monkeypatch.setattr(nc, "native_credential_setup_hint", fake)
    out = base.setup_hint(
        provider="p",
        field_names=("k",),
        tool_name="t",
        env_var="P_API_KEY",
        display_name="Provider",
    )
    assert out == "hint-text"
    assert captured["env_var"] == "P_API_KEY"
    assert captured["display_name"] == "Provider"
