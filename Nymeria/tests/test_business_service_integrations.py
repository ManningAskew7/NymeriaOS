import base64
import json

import pytest
from cryptography.fernet import Fernet

from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from nymeria.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _repo(tmp_path, monkeypatch) -> CredentialVaultRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")
    return CredentialVaultRepo(db_path)


def _use_repo(monkeypatch, repo: CredentialVaultRepo) -> None:
    import nymeria.core.credential_vault as credential_vault

    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: repo)


def test_bitly_create_uses_vault_token_and_body(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Bitly",
        provider="bitly",
        kind="api_key",
        allowed_targets=["native_tool:bitly_create_bitlink"],
        secret_fields={
            "access_token": "bitly-token",
            "base_url": "https://bitly.example/v4",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"id": "bit.ly/example", "long_url": json_body["long_url"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.bitly_create_bitlink.func(
            long_url="https://example.com",
            title="Example",
            tags="docs, test",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "bit.ly/example"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://bitly.example/v4/bitlinks"
    assert captured["headers"]["Authorization"] == "Bearer bitly-token"
    assert captured["json_body"]["long_url"] == "https://example.com"
    assert captured["json_body"]["tags"] == ["docs", "test"]


def test_bitly_get_returns_setup_hint_without_token(monkeypatch):
    from nymeria.tools.business_service_integrations import bitly_get_bitlink

    monkeypatch.delenv("BITLY_TOKEN", raising=False)

    result = bitly_get_bitlink.func("bit.ly/example")

    assert "No Bitly credential found" in result
    assert "BITLY_TOKEN" in result


def test_brandfetch_colors_uses_api_key(monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"url": url, "headers": headers})
        return {"colors": [{"hex": "#000000", "type": "dark"}]}

    monkeypatch.setenv("BRANDFETCH_API_KEY", "brand-key")
    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.brandfetch_get_brand_colors.func("openai.com"))

    assert result == [{"hex": "#000000", "type": "dark"}]
    assert captured["url"] == "https://api.brandfetch.io/v2/brands/openai.com"
    assert captured["headers"]["Authorization"] == "Bearer brand-key"


def test_marketstack_eod_builds_latest_request(monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"url": url, "params": params})
        return {"data": [{"symbol": "AAPL"}]}

    monkeypatch.setenv("MARKETSTACK_API_KEY", "market-key")
    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.marketstack_get_eod.func(symbols="AAPL, MSFT", latest=True, limit=2))

    assert result == {"data": [{"symbol": "AAPL"}]}
    assert captured["url"] == "https://api.marketstack.com/v1/eod/latest"
    assert captured["params"]["access_key"] == "market-key"
    assert captured["params"]["symbols"] == "AAPL,MSFT"
    assert captured["params"]["limit"] == 2


def test_deepl_translate_uses_vault_free_plan(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="DeepL Free",
        provider="deepl",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "deepl-key",
            "api_plan": "free",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "data": data, "headers": headers})
        return {"translations": [{"text": "Hallo", "detected_source_language": "EN"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.deepl_translate_text.func(
            text="Hello",
            target_lang="DE",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"text": "Hallo", "detected_source_language": "EN"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api-free.deepl.com/v2/translate"
    assert captured["headers"]["Authorization"] == "DeepL-Auth-Key deepl-key"
    assert captured["data"]["target_lang"] == "DE"


def test_lingvanex_translate_uses_env_key(monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"result": "Hola"}

    monkeypatch.setenv("LINGVANEX_API_KEY", "ling-key")
    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.lingvanex_translate_text.func(
            text="Hello",
            target_lang="es",
            source_lang="en",
            translate_mode="html",
        )
    )

    assert result == {"result": "Hola"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api-b2b.backenster.com/b1/api/v3/translate"
    assert captured["headers"]["Authorization"] == "Bearer ling-key"
    assert captured["json_body"]["data"] == "Hello"
    assert captured["json_body"]["to"] == "es"
    assert captured["json_body"]["from"] == "en"
    assert captured["json_body"]["translateMode"] == "html"


def test_apitemplate_create_pdf_uses_vault_key_and_base_url(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="APITemplate",
        provider="apitemplate",
        kind="api_key",
        allowed_targets=["native_tool:apitemplate_create_pdf"],
        secret_fields={
            "api_key": "template-key",
            "base_url": "https://template.example/v1",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"download_url": "https://cdn.example/report.pdf"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.apitemplate_create_pdf.func(
            template_id="tpl_pdf",
            properties_json='{"name": "Alice"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["download_url"] == "https://cdn.example/report.pdf"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://template.example/v1/create"
    assert captured["params"] == {"template_id": "tpl_pdf"}
    assert captured["json_body"] == {"name": "Alice"}
    assert captured["headers"]["X-API-KEY"] == "template-key"


def test_onesimple_create_screenshot_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="One Simple API",
        provider="onesimple",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_token": "one-token",
            "base_url": "https://one.example/api",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"url": "https://cdn.example/screenshot.png"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.onesimple_create_screenshot.func(
            url="https://example.com",
            screen_size="desktop",
            full_page=True,
            force_refresh=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["url"] == "https://cdn.example/screenshot.png"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://one.example/api/screenshot"
    assert captured["params"]["url"] == "https://example.com"
    assert captured["params"]["screen"] == "desktop"
    assert captured["params"]["fullpage"] == "yes"
    assert captured["params"]["force"] == "yes"
    assert captured["params"]["token"] == "one-token"
    assert captured["params"]["output"] == "json"


def test_dhl_track_uses_vault_api_key(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="DHL",
        provider="dhl",
        kind="api_key",
        allowed_targets=["native_tool:dhl_track_shipment"],
        secret_fields={
            "api_key": "dhl-key",
            "base_url": "https://dhl.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"shipments": [{"id": "shipment-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.dhl_track_shipment.func(
            tracking_number="TRACK1",
            recipient_postal_code="90210",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"id": "shipment-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://dhl.example/track/shipments"
    assert captured["params"] == {"trackingNumber": "TRACK1", "recipientPostalCode": "90210"}
    assert captured["headers"]["DHL-API-Key"] == "dhl-key"


def test_onfleet_complete_task_uses_basic_auth_and_body(monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    monkeypatch.setenv("ONFLEET_API_KEY", "onfleet-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.onfleet_complete_task.func(task_id="task-1", success=False, notes="missed"))

    assert result == {"status": "ok"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://onfleet.com/api/v2/tasks/task-1/complete"
    assert captured["json_body"] == {"completionDetails": {"success": False, "notes": "missed"}}
    expected_auth = base64.b64encode(b"onfleet-key:").decode()
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_phantombuster_launch_uses_vault_key_and_resolves_container(tmp_path, monkeypatch):
    from nymeria.tools import business_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Phantombuster",
        provider="phantombuster",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "phantom-key",
            "base_url": "https://phantom.example/api/v2",
        },
        created_by_user_id="alice",
    )
    requests = []

    def fake_request(method, url, params=None, json_body=None, data=None, headers=None):
        requests.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        if url.endswith("/agents/launch"):
            return {"containerId": "container-1"}
        return {"id": "container-1", "status": "running"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.phantombuster_launch_agent.func(
            agent_id="agent-1",
            arguments_json='{"profileUrl":"https://example.com"}',
            fields_json='{"saveArguments": true}',
            resolve_container=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "container-1"
    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://phantom.example/api/v2/agents/launch"
    assert requests[0]["json_body"] == {
        "id": "agent-1",
        "saveArguments": True,
        "arguments": {"profileUrl": "https://example.com"},
    }
    assert requests[0]["headers"]["X-Phantombuster-Key"] == "phantom-key"
    assert requests[1]["url"] == "https://phantom.example/api/v2/containers/fetch"
    assert requests[1]["params"] == {"id": "container-1"}


def test_business_service_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "bitly_get_bitlink",
        "brandfetch_get_brand",
        "brandfetch_get_brand_logos",
        "brandfetch_get_brand_colors",
        "marketstack_get_eod",
        "marketstack_get_ticker",
        "marketstack_get_exchange",
        "deepl_list_languages",
        "lingvanex_list_languages",
        "apitemplate_list_templates",
        "apitemplate_get_account",
        "dhl_track_shipment",
        "onfleet_test_auth",
        "onfleet_list_tasks",
        "onfleet_get_task",
        "onfleet_list_workers",
        "onfleet_get_worker",
        "onfleet_list_teams",
        "onfleet_get_team",
        "phantombuster_list_agents",
        "phantombuster_get_agent",
        "phantombuster_get_agent_output",
    }
    moderate_names = {
        "bitly_create_bitlink",
        "bitly_update_bitlink",
        "deepl_translate_text",
        "lingvanex_translate_text",
        "apitemplate_create_image",
        "apitemplate_create_pdf",
        "onesimple_create_pdf",
        "onesimple_create_screenshot",
        "onesimple_get_page_info",
        "onesimple_get_exchange_rate",
        "onesimple_get_image_metadata",
        "onesimple_validate_email",
        "onesimple_expand_url",
        "onesimple_create_qr_code",
        "onfleet_complete_task",
        "phantombuster_launch_agent",
        "phantombuster_delete_agent",
    }

    for name in safe_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in OPTIONAL_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_business_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.business_service_integrations import (
        apitemplate_create_pdf,
        bitly_get_bitlink,
        dhl_track_shipment,
        deepl_translate_text,
        lingvanex_translate_text,
        onesimple_create_pdf,
        onfleet_complete_task,
        phantombuster_launch_agent,
    )

    assert "config" not in apitemplate_create_pdf.args_schema.model_json_schema()["properties"]
    assert "config" not in bitly_get_bitlink.args_schema.model_json_schema()["properties"]
    assert "config" not in dhl_track_shipment.args_schema.model_json_schema()["properties"]
    assert "config" not in deepl_translate_text.args_schema.model_json_schema()["properties"]
    assert "config" not in lingvanex_translate_text.args_schema.model_json_schema()["properties"]
    assert "config" not in onesimple_create_pdf.args_schema.model_json_schema()["properties"]
    assert "config" not in onfleet_complete_task.args_schema.model_json_schema()["properties"]
    assert "config" not in phantombuster_launch_agent.args_schema.model_json_schema()["properties"]
