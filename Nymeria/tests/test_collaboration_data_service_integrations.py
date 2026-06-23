import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_slack_list_channels_uses_env_token_and_query_params(monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-token")
    monkeypatch.setenv("SLACK_BASE_URL", "https://slack.example/api")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, slack_ok=False):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
                "slack_ok": slack_ok,
            }
        )
        return {"ok": True, "channels": [{"id": "C1", "name": "general"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.slack_list_channels.func(
            types="public_channel, private_channel",
            exclude_archived=True,
            limit=10,
        )
    )

    assert result == [{"id": "C1", "name": "general"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://slack.example/api/conversations.list"
    assert captured["params"]["types"] == "public_channel,private_channel"
    assert captured["params"]["exclude_archived"] == "true"
    assert captured["params"]["limit"] == 10
    assert captured["headers"]["Authorization"] == "Bearer slack-token"
    assert captured["slack_ok"] is True


def test_slack_post_message_uses_vault_token_and_blocks(tmp_path, monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Slack",
        provider="slack",
        kind="oauth_token",
        allowed_targets=["native_tool:slack_post_message"],
        secret_fields={
            "bot_token": "slack-token",
            "base_url": "https://slack.example/api",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, slack_ok=False):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
                "slack_ok": slack_ok,
            }
        )
        return {"ok": True, "channel": json_body["channel"], "ts": "123.456"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.slack_post_message.func(
            channel_id="C1",
            text="Ship it",
            thread_ts="111.222",
            blocks_json='[{"type":"section","text":{"type":"mrkdwn","text":"Ship it"}}]',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://slack.example/api/chat.postMessage"
    assert captured["headers"]["Authorization"] == "Bearer slack-token"
    assert captured["json_body"]["channel"] == "C1"
    assert captured["json_body"]["thread_ts"] == "111.222"
    assert captured["json_body"]["blocks"][0]["type"] == "section"
    assert captured["slack_ok"] is True


def test_notion_query_data_source_uses_env_token_version_and_body(monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("NOTION_API_KEY", "notion-token")
    monkeypatch.setenv("NOTION_VERSION", "2026-03-11")
    monkeypatch.setenv("NOTION_BASE_URL", "https://notion.example/v1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, slack_ok=False):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"results": [{"id": "page-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.notion_query_data_source.func(
            data_source_id="ds-1",
            filter_json='{"property":"Status","status":{"equals":"Ready"}}',
            sorts_json='[{"timestamp":"last_edited_time","direction":"descending"}]',
            page_size=5,
            start_cursor="cursor-1",
        )
    )

    assert result == [{"id": "page-1"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://notion.example/v1/data_sources/ds-1/query"
    assert captured["headers"]["Authorization"] == "Bearer notion-token"
    assert captured["headers"]["Notion-Version"] == "2026-03-11"
    assert captured["json_body"]["filter"]["property"] == "Status"
    assert captured["json_body"]["sorts"][0]["direction"] == "descending"
    assert captured["json_body"]["page_size"] == 5
    assert captured["json_body"]["start_cursor"] == "cursor-1"


def test_notion_create_page_uses_vault_token_and_simple_title(tmp_path, monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Notion",
        provider="notion",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "notion-token",
            "base_url": "https://notion.example/v1",
            "version": "2026-03-11",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, slack_ok=False):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "page-1", "parent": json_body["parent"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.notion_create_page.func(
            parent_id="parent-1",
            parent_type="page",
            title="Project notes",
            children_json='[{"object":"block","type":"paragraph","paragraph":{"rich_text":[]}}]',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "page-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://notion.example/v1/pages"
    assert captured["headers"]["Authorization"] == "Bearer notion-token"
    assert captured["json_body"]["parent"] == {"page_id": "parent-1"}
    title = captured["json_body"]["properties"]["title"]["title"][0]["text"]["content"]
    assert title == "Project notes"
    assert captured["json_body"]["children"][0]["type"] == "paragraph"


def test_airtable_list_records_uses_env_token_and_filters(monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "airtable-token")
    monkeypatch.setenv("AIRTABLE_BASE_URL", "https://airtable.example/v0")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, slack_ok=False):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "headers": headers,
            }
        )
        return {"records": [{"id": "rec1", "fields": {"Name": "Ship"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.airtable_list_records.func(
            base_id="app1",
            table_name_or_id="Tasks",
            max_records=10,
            view="Grid view",
            filter_by_formula="{Status} = 'Ready'",
            fields="Name, Status",
            sort_json='[{"field":"Name","direction":"asc"}]',
            page_size=10,
            offset="itr1",
        )
    )

    assert result["records"][0]["id"] == "rec1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://airtable.example/v0/app1/Tasks"
    assert captured["headers"]["Authorization"] == "Bearer airtable-token"
    assert captured["params"]["maxRecords"] == 10
    assert captured["params"]["view"] == "Grid view"
    assert captured["params"]["filterByFormula"] == "{Status} = 'Ready'"
    assert captured["params"]["fields[]"] == ["Name", "Status"]
    assert captured["params"]["sort"] == '[{"field":"Name","direction":"asc"}]'
    assert captured["params"]["offset"] == "itr1"


def test_airtable_update_records_uses_vault_token_and_patch_body(tmp_path, monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Airtable",
        provider="airtable",
        kind="api_key",
        allowed_targets=["native_tool:airtable_update_records"],
        secret_fields={
            "access_token": "airtable-token",
            "base_url": "https://airtable.example/v0",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None, slack_ok=False):
        captured.update(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        return {"records": json_body["records"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.airtable_update_records.func(
            base_id="app1",
            table_name_or_id="Tasks",
            records_json='{"id":"rec1","fields":{"Status":"Done"}}',
            typecast=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["records"][0]["id"] == "rec1"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://airtable.example/v0/app1/Tasks"
    assert captured["headers"]["Authorization"] == "Bearer airtable-token"
    assert captured["json_body"]["typecast"] is True
    assert captured["json_body"]["records"][0]["fields"]["Status"] == "Done"


def test_collaboration_data_missing_tokens_return_setup_hints(monkeypatch):
    from nymeria.tools import collaboration_data_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    slack = tools.slack_get_user.func("U1")
    notion = tools.notion_get_page.func("page-1")
    airtable = tools.airtable_get_record.func("app1", "Tasks", "rec1")

    assert "No Slack credential found" in slack
    assert "SLACK_BOT_TOKEN" in slack
    assert "native_tool:slack_get_user" in slack
    assert "No Notion credential found" in notion
    assert "NOTION_API_KEY" in notion
    assert "native_tool:notion_get_page" in notion
    assert "No Airtable credential found" in airtable
    assert "AIRTABLE_ACCESS_TOKEN" in airtable
    assert "native_tool:airtable_get_record" in airtable


def test_collaboration_data_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "slack_list_channels",
        "slack_get_channel_history",
        "slack_search_messages",
        "slack_list_users",
        "slack_get_user",
        "notion_search",
        "notion_get_page",
        "notion_get_block_children",
        "notion_query_data_source",
        "airtable_list_bases",
        "airtable_get_base_schema",
        "airtable_list_records",
        "airtable_get_record",
    }
    moderate_names = {
        "slack_post_message",
        "slack_update_message",
        "slack_add_reaction",
        "notion_create_page",
        "notion_update_page",
        "notion_append_block_children",
        "airtable_create_records",
        "airtable_update_records",
        "airtable_delete_record",
    }

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


def test_collaboration_data_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.collaboration_data_service_integrations import (
        airtable_list_records,
        notion_search,
        slack_post_message,
    )

    assert "config" not in slack_post_message.args_schema.model_json_schema()["properties"]
    assert "config" not in notion_search.args_schema.model_json_schema()["properties"]
    assert "config" not in airtable_list_records.args_schema.model_json_schema()["properties"]
