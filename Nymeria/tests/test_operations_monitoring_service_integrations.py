import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_netlify_list_sites_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Netlify",
        provider="netlify",
        kind="api_key",
        allowed_targets=["native_tool:netlify_list_sites"],
        secret_fields={"access_token": "netlify-token", "base_url": "https://netlify.example/api/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return [{"id": "site-1"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.netlify_list_sites.func(
            limit=3,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"id": "site-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://netlify.example/api/v1/sites"
    assert captured["params"] == {"filter": "all", "per_page": 3}
    assert captured["headers"]["Authorization"] == "Bearer netlify-token"


def test_uptimerobot_list_monitors_uses_env_key(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("UPTIMEROBOT_API_KEY", "ur-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"stat": "ok", "monitors": [{"id": 123}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.uptimerobot_list_monitors.func(
            limit=5,
            monitor_ids="123,456",
            statuses="2,9",
            types="1,3",
            include_logs=True,
            include_response_times=True,
        )
    )

    assert result == [{"id": 123}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.uptimerobot.com/v2/getMonitors"
    assert captured["form_data"]["api_key"] == "ur-key"
    assert captured["form_data"]["format"] == "json"
    assert captured["form_data"]["limit"] == 5
    assert captured["form_data"]["monitors"] == "123-456"
    assert captured["form_data"]["statuses"] == "2-9"
    assert captured["form_data"]["types"] == "1-3"
    assert captured["form_data"]["logs"] == 1
    assert captured["form_data"]["response_times"] == 1


def test_pagerduty_create_incident_uses_env_token_and_from_email(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PAGERDUTY_API_TOKEN", "pd-token")
    monkeypatch.setenv("PAGERDUTY_FROM_EMAIL", "alice@example.com")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"incident": {"id": "inc-1", "title": "API down"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.pagerduty_create_incident.func(
            title="API down",
            service_id="svc-1",
            urgency="high",
            details="500s from edge",
            incident_key="api-down",
        )
    )

    assert result["id"] == "inc-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.pagerduty.com/incidents"
    assert captured["headers"]["Authorization"] == "Token token=pd-token"
    assert captured["headers"]["From"] == "alice@example.com"
    incident = captured["json_body"]["incident"]
    assert incident["title"] == "API down"
    assert incident["service"] == {"id": "svc-1", "type": "service_reference"}
    assert incident["urgency"] == "high"
    assert incident["incident_key"] == "api-down"
    assert incident["body"]["details"] == "500s from edge"


def test_sentry_update_issue_uses_env_token(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sentry-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "issue-1", "status": "resolved"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.sentry_update_issue.func(
            organization_slug="acme",
            issue_id="issue-1",
            status="resolved",
            assigned_to="me",
            fields_json='{"isBookmarked": true}',
        )
    )

    assert result["status"] == "resolved"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://sentry.io/api/0/organizations/acme/issues/issue-1/"
    assert captured["headers"]["Authorization"] == "Bearer sentry-token"
    assert captured["json_body"] == {"isBookmarked": True, "status": "resolved", "assignedTo": "me"}


def test_cloudflare_create_dns_record_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Cloudflare",
        provider="cloudflare",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_token": "cf-token",
            "base_url": "https://cloudflare.example/client/v4",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"success": True, "result": {"id": "record-1"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.cloudflare_create_dns_record.func(
            zone_id="zone-1",
            record_type="a",
            name="api.example.com",
            content="203.0.113.10",
            ttl=120,
            proxied=True,
            fields_json='{"comment":"created by test"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "record-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://cloudflare.example/client/v4/zones/zone-1/dns_records"
    assert captured["headers"]["Authorization"] == "Bearer cf-token"
    assert captured["json_body"] == {
        "type": "A",
        "name": "api.example.com",
        "content": "203.0.113.10",
        "ttl": 120,
        "proxied": True,
        "comment": "created by test",
    }


def test_grafana_search_dashboards_uses_vault_token_and_api_root(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Grafana",
        provider="grafana",
        kind="api_key",
        allowed_targets=["native_tool:grafana_search_dashboards"],
        secret_fields={"api_key": "grafana-token", "base_url": "https://grafana.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return [{"uid": "dash-1"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.grafana_search_dashboards.func(
            query="ops",
            limit=4,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"uid": "dash-1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://grafana.example/api/search"
    assert captured["params"]["query"] == "ops"
    assert captured["params"]["limit"] == 4
    assert captured["headers"]["Authorization"] == "Bearer grafana-token"


def test_metabase_query_question_uses_env_session_token(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("METABASE_BASE_URL", "https://metabase.example")
    monkeypatch.setenv("METABASE_SESSION_TOKEN", "metabase-session")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return [{"count": 2}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.metabase_query_question.func(
            question_id="42",
            parameters_json='[{"type":"category","value":"api"}]',
        )
    )

    assert result == [{"count": 2}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://metabase.example/api/card/42/query/json"
    assert captured["headers"]["X-Metabase-Session"] == "metabase-session"
    assert captured["json_body"] == {"parameters": [{"type": "category", "value": "api"}]}


def test_elasticsearch_search_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ELASTICSEARCH_BASE_URL", "https://elastic.example")
    monkeypatch.setenv("ELASTICSEARCH_USERNAME", "elastic")
    monkeypatch.setenv("ELASTICSEARCH_PASSWORD", "secret")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers, "auth": auth})
        return {"hits": {"hits": [{"_id": "doc-1"}]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.elasticsearch_search.func(
            index="logs-*",
            limit=7,
        )
    )

    assert result["hits"]["hits"][0]["_id"] == "doc-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://elastic.example/logs-*/_search"
    assert captured["auth"] == ("elastic", "secret")
    assert captured["json_body"]["size"] == 7
    assert "Authorization" not in captured["headers"]


def test_splunk_create_search_job_uses_vault_token_and_ssl_flag(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Splunk",
        provider="splunk",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "auth_token": "splunk-token",
            "base_url": "https://splunk.example:8089",
            "allow_unauthorized_certs": "true",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers, "verify": verify})
        return {"sid": "search-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.splunk_create_search_job.func(
            search_query="search index=main error",
            earliest_time="-15m",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"sid": "search-1"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://splunk.example:8089/services/search/jobs"
    assert captured["headers"]["Authorization"] == "Bearer splunk-token"
    assert captured["form_data"]["search"] == "search index=main error"
    assert captured["form_data"]["earliest_time"] == "-15m"
    assert captured["verify"] is False


def test_rundeck_get_job_metadata_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Rundeck",
        provider="rundeck",
        kind="api_key",
        allowed_targets=["native_tool:rundeck_get_job_metadata"],
        secret_fields={"token": "rundeck-token", "base_url": "https://rundeck.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "headers": headers})
        return {"id": "job-1", "name": "Deploy"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.rundeck_get_job_metadata.func(
            job_id="job-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "job-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://rundeck.example/api/18/job/job-1/info"
    assert captured["headers"]["X-Rundeck-Auth-Token"] == "rundeck-token"


def test_rundeck_execute_job_uses_env_token(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("RUNDECK_BASE_URL", "https://rundeck.example")
    monkeypatch.setenv("RUNDECK_TOKEN", "rundeck-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None, auth=None, verify=True):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"id": "exec-1", "status": "running"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.rundeck_execute_job.func(
            job_id="job-1",
            arguments_json='{"env":"prod","message":"hello world"}',
            node_filter="name:node1",
        )
    )

    assert result["id"] == "exec-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://rundeck.example/api/14/job/job-1/run"
    assert captured["headers"]["X-Rundeck-Auth-Token"] == "rundeck-token"
    assert captured["params"] == {"filter": "name:node1"}
    assert captured["json_body"]["argString"] == "-env prod -message 'hello world'"


def test_operations_monitoring_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import operations_monitoring_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.delenv("RUNDECK_BASE_URL", raising=False)
    monkeypatch.delenv("RUNDECK_TOKEN", raising=False)

    netlify_result = tools.netlify_list_sites.func()
    uptimerobot_result = tools.uptimerobot_get_account.func()
    pagerduty_result = tools.pagerduty_list_incidents.func()
    sentry_result = tools.sentry_list_organizations.func()
    cloudflare_result = tools.cloudflare_list_zones.func()
    grafana_result = tools.grafana_search_dashboards.func()
    metabase_result = tools.metabase_list_questions.func()
    elasticsearch_result = tools.elasticsearch_list_indices.func()
    splunk_result = tools.splunk_list_saved_searches.func()
    rundeck_result = tools.rundeck_get_job_metadata.func(job_id="job-1")

    assert 'provider "netlify"' in netlify_result
    assert "NETLIFY_ACCESS_TOKEN" in netlify_result
    assert 'provider "uptimerobot"' in uptimerobot_result
    assert "UPTIMEROBOT_API_KEY" in uptimerobot_result
    assert 'provider "pagerduty"' in pagerduty_result
    assert "PAGERDUTY_API_TOKEN" in pagerduty_result
    assert 'provider "sentry"' in sentry_result
    assert "SENTRY_AUTH_TOKEN" in sentry_result
    assert 'provider "cloudflare"' in cloudflare_result
    assert "CLOUDFLARE_API_TOKEN" in cloudflare_result
    assert 'provider "grafana"' in grafana_result
    assert "GRAFANA_BASE_URL" in grafana_result
    assert 'provider "metabase"' in metabase_result
    assert "METABASE_BASE_URL" in metabase_result
    assert 'provider "elasticsearch"' in elasticsearch_result
    assert "ELASTICSEARCH_BASE_URL" in elasticsearch_result
    assert 'provider "splunk"' in splunk_result
    assert "SPLUNK_BASE_URL" in splunk_result
    assert "Rundeck base URL" in rundeck_result
    assert "RUNDECK_BASE_URL" in rundeck_result


def test_operations_monitoring_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "netlify_list_sites",
        "netlify_get_site",
        "netlify_list_deploys",
        "netlify_get_deploy",
        "uptimerobot_get_account",
        "uptimerobot_list_monitors",
        "uptimerobot_get_monitor",
        "pagerduty_list_incidents",
        "pagerduty_get_incident",
        "pagerduty_list_services",
        "pagerduty_get_user",
        "sentry_list_organizations",
        "sentry_list_projects",
        "sentry_list_project_issues",
        "sentry_get_issue",
        "sentry_list_project_events",
        "sentry_get_event",
        "cloudflare_list_zones",
        "cloudflare_list_dns_records",
        "cloudflare_list_origin_certificates",
        "cloudflare_get_origin_certificate",
        "grafana_search_dashboards",
        "grafana_get_dashboard",
        "grafana_list_teams",
        "metabase_list_questions",
        "metabase_get_question",
        "metabase_query_question",
        "metabase_list_dashboards",
        "metabase_get_dashboard",
        "elasticsearch_list_indices",
        "elasticsearch_search",
        "elasticsearch_get_document",
        "splunk_list_saved_searches",
        "splunk_get_search_job",
        "splunk_get_search_results",
        "rundeck_get_job_metadata",
    ]
    moderate_names = [
        "netlify_cancel_deploy",
        "netlify_delete_site",
        "uptimerobot_create_monitor",
        "uptimerobot_update_monitor",
        "uptimerobot_delete_monitor",
        "uptimerobot_reset_monitor",
        "pagerduty_create_incident",
        "pagerduty_update_incident",
        "pagerduty_add_incident_note",
        "sentry_update_issue",
        "cloudflare_create_dns_record",
        "cloudflare_update_dns_record",
        "cloudflare_delete_dns_record",
        "cloudflare_upload_origin_certificate",
        "cloudflare_delete_origin_certificate",
        "grafana_create_dashboard",
        "grafana_delete_dashboard",
        "elasticsearch_index_document",
        "elasticsearch_delete_document",
        "splunk_create_search_job",
        "rundeck_execute_job",
    ]

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_operations_monitoring_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        cloudflare_create_dns_record,
        elasticsearch_search,
        grafana_search_dashboards,
        metabase_query_question,
        netlify_list_sites,
        pagerduty_create_incident,
        rundeck_execute_job,
        sentry_update_issue,
        splunk_create_search_job,
        uptimerobot_list_monitors,
    )

    assert "config" not in netlify_list_sites.args_schema.model_json_schema()["properties"]
    assert "config" not in uptimerobot_list_monitors.args_schema.model_json_schema()["properties"]
    assert "config" not in pagerduty_create_incident.args_schema.model_json_schema()["properties"]
    assert "config" not in sentry_update_issue.args_schema.model_json_schema()["properties"]
    assert "config" not in cloudflare_create_dns_record.args_schema.model_json_schema()["properties"]
    assert "config" not in grafana_search_dashboards.args_schema.model_json_schema()["properties"]
    assert "config" not in metabase_query_question.args_schema.model_json_schema()["properties"]
    assert "config" not in elasticsearch_search.args_schema.model_json_schema()["properties"]
    assert "config" not in splunk_create_search_job.args_schema.model_json_schema()["properties"]
    assert "config" not in rundeck_execute_job.args_schema.model_json_schema()["properties"]
