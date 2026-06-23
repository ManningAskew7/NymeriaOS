import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_microsoft_todo_list_tasks_uses_env_token_and_status_filter(monkeypatch):
    from nymeria.tools import microsoft_graph_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MICROSOFT_GRAPH_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MICROSOFT_GRAPH_BASE_URL", "https://graph.example/v1.0")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, content=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "content": content,
                "headers": headers,
            }
        )
        return {"value": [{"id": "task-1", "title": "Call dentist"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.microsoft_todo_list_tasks.func(
            list_id="list-1",
            status="notStarted",
            top=10,
        )
    )

    assert result == [{"id": "task-1", "title": "Call dentist"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://graph.example/v1.0/me/todo/lists/list-1/tasks"
    assert captured["params"] == {"$top": 10, "$filter": "status eq 'notStarted'"}
    assert captured["headers"]["Authorization"] == "Bearer graph-token"


def test_microsoft_onedrive_upload_text_file_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import microsoft_graph_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Microsoft Graph",
        provider="microsoft_graph",
        kind="oauth",
        allowed_targets=["native_tool:microsoft_onedrive_upload_text_file"],
        secret_fields={
            "accessToken": "vault-token",
            "baseUrl": "https://graph.example/v1.0",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, content=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "content": content,
                "headers": headers,
            }
        )
        return {"id": "item-1", "name": "today.txt"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.microsoft_onedrive_upload_text_file.func(
            path="/Notes/today.txt",
            content="Meeting notes",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["id"] == "item-1"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://graph.example/v1.0/me/drive/root:/Notes/today.txt:/content"
    assert captured["params"] == {"@microsoft.graph.conflictBehavior": "replace"}
    assert captured["content"] == "Meeting notes"
    assert captured["headers"]["Authorization"] == "Bearer vault-token"
    assert captured["headers"]["Content-Type"] == "text/plain; charset=utf-8"


def test_microsoft_teams_send_channel_message_uses_env_token(monkeypatch):
    from nymeria.tools import microsoft_graph_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MICROSOFT_GRAPH_ACCESS_TOKEN", "graph-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, content=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "message-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.microsoft_teams_send_channel_message.func(
            team_id="team-1",
            channel_id="channel-1",
            content="hello",
            content_type="text",
        )
    )

    assert result["id"] == "message-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://graph.microsoft.com/v1.0/teams/team-1/channels/channel-1/messages"
    assert captured["headers"]["Authorization"] == "Bearer graph-token"
    assert captured["json_body"] == {"body": {"contentType": "text", "content": "hello"}}


def test_microsoft_sharepoint_create_item_uses_env_token(monkeypatch):
    from nymeria.tools import microsoft_graph_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MICROSOFT_GRAPH_ACCESS_TOKEN", "graph-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, content=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "item-1", "fields": json_body["fields"]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.microsoft_sharepoint_create_item.func(
            site_id="site-1",
            list_id="list-1",
            fields_json='{"Title":"Roadmap","Status":"Open"}',
        )
    )

    assert result["id"] == "item-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://graph.microsoft.com/v1.0/sites/site-1/lists/list-1/items"
    assert captured["headers"]["Authorization"] == "Bearer graph-token"
    assert captured["json_body"] == {"fields": {"Title": "Roadmap", "Status": "Open"}}


def test_microsoft_excel_read_range_uses_workbook_path(monkeypatch):
    from nymeria.tools import microsoft_graph_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MICROSOFT_GRAPH_ACCESS_TOKEN", "graph-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, content=None, headers=None):
        captured.update({"method": method, "url": url, "headers": headers})
        return {"address": "Sheet1!A1:B2", "values": [["Name", "Status"], ["Ada", "Open"]]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.microsoft_excel_read_range.func(
            workbook_path="/Reports/status.xlsx",
            worksheet_id_or_name="Sheet1",
            address="A1:B2",
        )
    )

    assert result["values"][1] == ["Ada", "Open"]
    assert captured["method"] == "GET"
    assert captured["url"] == (
        "https://graph.microsoft.com/v1.0/"
        "me/drive/root:/Reports/status.xlsx:/workbook/worksheets/Sheet1/range(address='A1%3AB2')"
    )
    assert captured["headers"]["Authorization"] == "Bearer graph-token"


def test_microsoft_graph_missing_credentials_return_setup_hint(monkeypatch):
    from nymeria.tools import microsoft_graph_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.delenv("MICROSOFT_GRAPH_ACCESS_TOKEN", raising=False)

    result = tools.microsoft_todo_list_task_lists.func()

    assert 'provider "microsoft_graph"' in result
    assert "MICROSOFT_GRAPH_ACCESS_TOKEN" in result
    assert 'allowed target "native_tool:microsoft_todo_list_task_lists"' in result


def test_microsoft_graph_tools_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "microsoft_todo_list_task_lists",
        "microsoft_todo_list_tasks",
        "microsoft_onedrive_list_children",
        "microsoft_onedrive_get_item",
        "microsoft_onedrive_search",
        "microsoft_teams_list_joined_teams",
        "microsoft_teams_list_channels",
        "microsoft_teams_list_channel_messages",
        "microsoft_sharepoint_search_sites",
        "microsoft_sharepoint_get_site",
        "microsoft_sharepoint_list_lists",
        "microsoft_sharepoint_list_items",
        "microsoft_sharepoint_get_item",
        "microsoft_excel_list_worksheets",
        "microsoft_excel_get_used_range",
        "microsoft_excel_read_range",
        "microsoft_excel_list_tables",
    ]
    moderate_names = [
        "microsoft_todo_create_task",
        "microsoft_todo_update_task",
        "microsoft_onedrive_upload_text_file",
        "microsoft_teams_send_channel_message",
        "microsoft_sharepoint_create_item",
        "microsoft_sharepoint_update_item_fields",
        "microsoft_sharepoint_delete_item",
        "microsoft_excel_update_range",
        "microsoft_excel_add_table_row",
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


def test_microsoft_graph_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        microsoft_onedrive_upload_text_file,
        microsoft_sharepoint_create_item,
        microsoft_excel_update_range,
        microsoft_teams_send_channel_message,
        microsoft_todo_create_task,
    )

    assert "config" not in microsoft_todo_create_task.args_schema.model_json_schema()["properties"]
    assert "config" not in microsoft_onedrive_upload_text_file.args_schema.model_json_schema()["properties"]
    assert "config" not in microsoft_teams_send_channel_message.args_schema.model_json_schema()["properties"]
    assert "config" not in microsoft_sharepoint_create_item.args_schema.model_json_schema()["properties"]
    assert "config" not in microsoft_excel_update_range.args_schema.model_json_schema()["properties"]
