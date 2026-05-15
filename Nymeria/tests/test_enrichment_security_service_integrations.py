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


def test_urlscan_submit_scan_uses_vault_api_key(tmp_path, monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="urlscan",
        provider="urlscan",
        kind="api_key",
        allowed_targets=["native_tool:urlscan_submit_scan"],
        secret_fields={"apiKey": "urlscan-key", "base_url": "https://urlscan.example/api/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"uuid": "scan-1", "result": "https://urlscan.example/result/scan-1/"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.urlscan_submit_scan.func(
            url="https://example.com",
            visibility="private",
            tags="phishing, test",
            custom_agent="Nymeria test",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["uuid"] == "scan-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://urlscan.example/api/v1/scan/"
    assert captured["headers"]["API-Key"] == "urlscan-key"
    assert captured["json_body"]["url"] == "https://example.com"
    assert captured["json_body"]["visibility"] == "private"
    assert captured["json_body"]["tags"] == ["phishing", "test"]
    assert captured["json_body"]["customAgent"] == "Nymeria test"


def test_hunter_domain_search_uses_env_api_key(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("HUNTER_API_KEY", "hunter-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"data": {"emails": [{"value": "alice@example.com"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.hunter_domain_search.func(
            domain="example.com",
            limit=5,
            email_type="personal",
            seniority="senior,executive",
            department="it,sales",
        )
    )

    assert result == [{"value": "alice@example.com"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.hunter.io/v2/domain-search"
    assert captured["params"]["api_key"] == "hunter-key"
    assert captured["params"]["domain"] == "example.com"
    assert captured["params"]["limit"] == 5
    assert captured["params"]["type"] == "personal"
    assert captured["params"]["seniority"] == "senior,executive"
    assert captured["params"]["department"] == "it,sales"


def test_mailcheck_check_email_uses_env_bearer_token(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAILCHECK_API_KEY", "mailcheck-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"valid": True, "email": "alice@example.com"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.mailcheck_check_email.func(email="alice@example.com"))

    assert result["valid"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mailcheck.co/v1/singleEmail:check"
    assert captured["headers"]["Authorization"] == "Bearer mailcheck-key"
    assert captured["json_body"] == {"email": "alice@example.com"}


def test_peekalink_preview_url_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Peekalink",
        provider="peekalink",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"api_key": "peekalink-key", "base_url": "https://peekalink.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"title": "Example", "url": "https://example.com"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.peekalink_preview_url.func(
            url="https://example.com",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["title"] == "Example"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://peekalink.example/"
    assert captured["headers"]["Authorization"] == "Bearer peekalink-key"
    assert captured["json_body"] == {"link": "https://example.com"}


def test_jina_deep_research_uses_env_key_and_simplifies_response(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("JINA_API_KEY", "jina-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {
            "choices": [
                {
                    "message": {
                        "content": "Research report",
                        "annotations": [{"url": "https://example.com"}],
                    }
                }
            ],
            "usage": {"total_tokens": 100},
        }

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.jina_deep_research.func(
            query="What is Nymeria?",
            max_returned_sources=3,
            prioritize_sources="docs.example.com",
            exclude_sources="bad.example.com",
            site_filter="example.com",
        )
    )

    assert result["content"] == "Research report"
    assert result["annotations"] == [{"url": "https://example.com"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://deepsearch.jina.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer jina-key"
    assert captured["json_body"]["messages"] == [{"role": "user", "content": "What is Nymeria?"}]
    assert captured["json_body"]["max_returned_urls"] == 3
    assert captured["json_body"]["boost_hostnames"] == ["docs.example.com"]
    assert captured["json_body"]["bad_hostnames"] == ["bad.example.com"]
    assert captured["json_body"]["only_hostnames"] == ["example.com"]


def test_jina_reader_can_run_without_key(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "headers": headers})
        return {"text": "# Example"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.jina_reader_fetch_url.func(
            url="https://example.com",
            output_format="markdown",
            target_selector="main",
            with_generated_alt=True,
        )
    )

    assert result["text"] == "# Example"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://r.jina.ai/https://example.com"
    assert "Authorization" not in captured["headers"]
    assert captured["headers"]["X-Return-Format"] == "markdown"
    assert captured["headers"]["X-Target-Selector"] == "main"
    assert captured["headers"]["X-With-Generated-Alt"] == "true"


def test_misp_search_attributes_uses_vault_key_and_ssl_flag(tmp_path, monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="MISP",
        provider="misp",
        kind="api_key",
        allowed_targets=["native_tool:misp_search_attributes"],
        secret_fields={
            "apiKey": "misp-key",
            "base_url": "https://misp.example",
            "allow_unauthorized_certs": "true",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers, "verify": verify})
        return {"response": {"Attribute": [{"id": "attr-1"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.misp_search_attributes.func(
            value="1.2.3.4",
            tags="tlp:amber, osint",
            limit=2,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"id": "attr-1"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://misp.example/attributes/restSearch"
    assert captured["headers"]["Authorization"] == "misp-key"
    assert captured["json_body"] == {"value": "1.2.3.4", "tags": ["tlp:amber", "osint"]}
    assert captured["verify"] is False


def test_thehive_list_cases_uses_env_key_and_api_root(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("THEHIVE_BASE_URL", "https://thehive.example")
    monkeypatch.setenv("THEHIVE_API_KEY", "thehive-key")
    monkeypatch.setenv("THEHIVE_API_VERSION", "v1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return [{"_id": "case-1"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.thehive_list_cases.func(limit=3))

    assert result == [{"_id": "case-1"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://thehive.example/api/v1/query"
    assert captured["params"] == {"name": "cases"}
    assert captured["headers"]["Authorization"] == "Bearer thehive-key"
    assert captured["json_body"]["query"][0] == {"_name": "listCase"}
    assert captured["json_body"]["query"][1] == {"_name": "page", "from": 0, "to": 3}


def test_securityscorecard_get_company_uses_env_token(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SECURITYSCORECARD_API_KEY", "ssc-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "headers": headers})
        return {"domain": "example.com", "score": 91}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.securityscorecard_get_company_scorecard.func(scorecard_identifier="example.com"))

    assert result["score"] == 91
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.securityscorecard.io/companies/example.com"
    assert captured["headers"]["Authorization"] == "Token ssc-key"


def test_elastic_security_list_cases_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ELASTIC_SECURITY_BASE_URL", "https://kibana.example:9243")
    monkeypatch.setenv("ELASTIC_SECURITY_USERNAME", "elastic")
    monkeypatch.setenv("ELASTIC_SECURITY_PASSWORD", "secret")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "params": params, "headers": headers, "auth": auth})
        return {"cases": [{"id": "case-1"}], "total": 1}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.elastic_security_list_cases.func(
            status="open",
            tags="incident, sev1",
            limit=4,
        )
    )

    assert result == [{"id": "case-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://kibana.example:9243/api/cases/_find"
    assert captured["auth"] == ("elastic", "secret")
    assert captured["headers"]["kbn-xsrf"] == "true"
    assert captured["params"]["status"] == "open"
    assert captured["params"]["tags"] == "incident,sev1"
    assert captured["params"]["perPage"] == 4


def test_enrichment_security_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import enrichment_security_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    urlscan_result = tools.urlscan_search_scans.func(query="domain:example.com")
    hunter_result = tools.hunter_email_verifier.func(email="alice@example.com")
    mailcheck_result = tools.mailcheck_check_email.func(email="alice@example.com")
    peekalink_result = tools.peekalink_preview_url.func(url="https://example.com")
    jina_result = tools.jina_deep_research.func(query="test")
    misp_result = tools.misp_search_events.func(value="example.com")
    thehive_result = tools.thehive_list_cases.func()
    securityscorecard_result = tools.securityscorecard_get_company_scorecard.func(scorecard_identifier="example.com")
    elastic_result = tools.elastic_security_list_cases.func()

    assert 'provider "urlscan"' in urlscan_result
    assert "URLSCAN_API_KEY" in urlscan_result
    assert 'provider "hunter"' in hunter_result
    assert "HUNTER_API_KEY" in hunter_result
    assert 'provider "mailcheck"' in mailcheck_result
    assert "MAILCHECK_API_KEY" in mailcheck_result
    assert 'provider "peekalink"' in peekalink_result
    assert "PEEKALINK_API_KEY" in peekalink_result
    assert 'provider "jina"' in jina_result
    assert "JINA_API_KEY" in jina_result
    assert 'provider "misp"' in misp_result
    assert "MISP_BASE_URL" in misp_result
    assert 'provider "thehive"' in thehive_result
    assert "THEHIVE_BASE_URL" in thehive_result
    assert 'provider "securityscorecard"' in securityscorecard_result
    assert "SECURITYSCORECARD_API_KEY" in securityscorecard_result
    assert 'provider "elastic_security"' in elastic_result
    assert "ELASTIC_SECURITY_BASE_URL" in elastic_result


def test_enrichment_security_tools_are_registered_with_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "urlscan_search_scans",
        "urlscan_get_result",
        "hunter_domain_search",
        "hunter_email_finder",
        "hunter_email_verifier",
        "mailcheck_check_email",
        "peekalink_preview_url",
        "peekalink_check_availability",
        "jina_reader_fetch_url",
        "jina_search_web",
        "misp_search_attributes",
        "misp_search_events",
        "misp_get_event",
        "misp_list_tags",
        "thehive_list_cases",
        "thehive_get_case",
        "thehive_list_alerts",
        "thehive_get_alert",
        "securityscorecard_get_company_scorecard",
        "securityscorecard_list_company_factors",
        "securityscorecard_get_company_history",
        "securityscorecard_list_portfolios",
        "elastic_security_list_cases",
        "elastic_security_get_case",
        "elastic_security_list_case_tags",
    ]
    moderate_names = [
        "urlscan_submit_scan",
        "jina_deep_research",
        "misp_create_event",
        "misp_add_event_tag",
        "misp_remove_event_tag",
        "thehive_create_case",
        "thehive_create_alert",
        "securityscorecard_add_portfolio_company",
        "securityscorecard_remove_portfolio_company",
        "elastic_security_create_case",
        "elastic_security_add_case_comment",
    ]

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


def test_enrichment_security_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        hunter_domain_search,
        jina_deep_research,
        mailcheck_check_email,
        misp_search_attributes,
        peekalink_preview_url,
        securityscorecard_get_company_scorecard,
        thehive_list_cases,
        urlscan_search_scans,
        elastic_security_list_cases,
    )

    assert "config" not in urlscan_search_scans.args_schema.model_json_schema()["properties"]
    assert "config" not in hunter_domain_search.args_schema.model_json_schema()["properties"]
    assert "config" not in mailcheck_check_email.args_schema.model_json_schema()["properties"]
    assert "config" not in peekalink_preview_url.args_schema.model_json_schema()["properties"]
    assert "config" not in jina_deep_research.args_schema.model_json_schema()["properties"]
    assert "config" not in misp_search_attributes.args_schema.model_json_schema()["properties"]
    assert "config" not in thehive_list_cases.args_schema.model_json_schema()["properties"]
    assert "config" not in securityscorecard_get_company_scorecard.args_schema.model_json_schema()["properties"]
    assert "config" not in elastic_security_list_cases.args_schema.model_json_schema()["properties"]
