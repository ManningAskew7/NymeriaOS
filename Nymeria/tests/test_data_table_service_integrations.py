import json

import pytest

from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_baserow_list_rows_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Baserow",
        provider="baserow",
        kind="api_key",
        allowed_targets=["native_tool:baserow_list_rows"],
        secret_fields={"token": "baserow-token", "host": "https://baserow.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"results": [{"id": 1, "Name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.baserow_list_rows.func(
            table_id="123",
            limit=3,
            page=2,
            search="Ada",
            filters_json='{"filter__field_Name__contains": "Ada"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["results"][0]["Name"] == "Ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://baserow.example/api/database/rows/table/123/"
    assert captured["headers"]["Authorization"] == "Token baserow-token"
    assert captured["params"]["size"] == 3
    assert captured["params"]["page"] == 2
    assert captured["params"]["search"] == "Ada"
    assert captured["params"]["filter__field_Name__contains"] == "Ada"
    assert captured["params"]["user_field_names"] == "true"


def test_nocodb_update_record_uses_env_token_and_auth_header(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("NOCODB_API_TOKEN", "nocodb-token")
    monkeypatch.setenv("NOCODB_BASE_URL", "https://nocodb.example")
    monkeypatch.setenv("NOCODB_AUTH_HEADER", "xc-auth")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"records": [{"id": "row-1", "fields": {"Name": "Ada"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.nocodb_update_record.func(
            base_id="base-1",
            table_id="table-1",
            record_id="row-1",
            fields_json='{"Name": "Ada"}',
        )
    )

    assert result["records"][0]["id"] == "row-1"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://nocodb.example/api/v3/data/base-1/table-1/records"
    assert captured["headers"]["xc-auth"] == "nocodb-token"
    assert captured["json_body"] == [{"id": "row-1", "fields": {"Name": "Ada"}}]


def test_coda_create_table_row_uses_env_token(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("CODA_API_TOKEN", "coda-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"requestId": "req-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.coda_create_table_row.func(
            doc_id="doc-1",
            table_id="table-1",
            cells_json='{"Name": "Ada", "Score": 42}',
            disable_parsing=True,
        )
    )

    assert result["requestId"] == "req-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://coda.io/apis/v1/docs/doc-1/tables/table-1/rows"
    assert captured["headers"]["Authorization"] == "Bearer coda-token"
    assert captured["json_body"] == {
        "rows": [
            {
                "cells": [
                    {"column": "Name", "value": "Ada"},
                    {"column": "Score", "value": 42},
                ]
            }
        ],
        "disableParsing": True,
    }


def test_grist_delete_records_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Grist",
        provider="grist",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"api_key": "grist-key", "base_url": "https://grist.example/api"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.grist_delete_records.func(
            doc_id="doc-1",
            table_id="People",
            row_ids="1, 2",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["deleted"] == [1, 2]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://grist.example/api/docs/doc-1/tables/People/data/delete"
    assert captured["headers"]["Authorization"] == "Bearer grist-key"
    assert captured["json_body"] == [1, 2]


def test_supabase_rows_use_env_key_and_postgrest_headers(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return [{"id": 1, "Name": "Ada"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    list_result = json.loads(
        tools.supabase_list_rows.func(
            table="contacts",
            select="id,Name",
            filters_query="status=eq.active&owner=is.null",
            limit=2,
            offset=5,
            order="created_at.desc",
            db_schema="crm",
        )
    )
    insert_result = json.loads(
        tools.supabase_insert_rows.func(
            table="contacts",
            rows_json='{"Name": "Grace"}',
            db_schema="crm",
        )
    )

    assert list_result[0]["Name"] == "Ada"
    assert insert_result[0]["id"] == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://project.supabase.co/rest/v1/contacts"
    assert calls[0]["params"] == {
        "status": "eq.active",
        "owner": "is.null",
        "limit": 2,
        "offset": 5,
        "order": "created_at.desc",
        "select": "id,Name",
    }
    assert calls[0]["headers"]["Authorization"] == "Bearer service-key"
    assert calls[0]["headers"]["apikey"] == "service-key"
    assert calls[0]["headers"]["Accept-Profile"] == "crm"
    assert calls[1]["method"] == "POST"
    assert calls[1]["json_body"] == {"Name": "Grace"}
    assert calls[1]["headers"]["Content-Profile"] == "crm"


def test_supabase_update_rows_uses_required_filter(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SUPABASE_BASE_URL", "https://project.supabase.co/rest/v1")
    monkeypatch.setenv("SUPABASE_API_KEY", "anon-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return [{"id": 7, "status": "done"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.supabase_update_rows.func(
            table="tasks",
            fields_json='{"status": "done"}',
            filters_query="id=eq.7",
        )
    )

    assert result[0]["status"] == "done"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://project.supabase.co/rest/v1/tasks"
    assert captured["params"] == {"id": "eq.7"}
    assert captured["json_body"] == {"status": "done"}
    assert captured["headers"]["apikey"] == "anon-key"


def test_quickbase_records_use_realm_header_and_record_shapes(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("QUICKBASE_HOSTNAME", "example.quickbase.com")
    monkeypatch.setenv("QUICKBASE_USER_TOKEN", "qb-token")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"data": [{"3": {"value": "Ada"}}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    query_result = json.loads(
        tools.quickbase_query_records.func(
            table_id="bt123",
            where="{3.EX.'Ada'}",
            select_fields="3, 6",
            sort_by_json='[{"fieldId": 3, "order": "ASC"}]',
            limit=10,
            skip=2,
        )
    )
    upsert_result = json.loads(
        tools.quickbase_upsert_records.func(
            table_id="bt123",
            records_json='[{"3": "Ada", "6": {"value": 42}}]',
            merge_field_id=3,
            fields_to_return="3,6",
        )
    )

    assert query_result["data"][0]["3"]["value"] == "Ada"
    assert upsert_result["data"][0]["3"]["value"] == "Ada"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://api.quickbase.com/v1/records/query"
    assert calls[0]["headers"]["QB-Realm-Hostname"] == "example.quickbase.com"
    assert calls[0]["headers"]["Authorization"] == "QB-USER-TOKEN qb-token"
    assert calls[0]["json_body"] == {
        "from": "bt123",
        "options": {"top": 10, "skip": 2},
        "where": "{3.EX.'Ada'}",
        "select": [3, 6],
        "sortBy": [{"fieldId": 3, "order": "ASC"}],
    }
    assert calls[1]["url"] == "https://api.quickbase.com/v1/records"
    assert calls[1]["json_body"] == {
        "to": "bt123",
        "data": [{"3": {"value": "Ada"}, "6": {"value": 42}}],
        "mergeFieldId": 3,
        "fieldsToReturn": [3, 6],
    }


def test_seatable_rows_use_app_access_token(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SEATABLE_API_TOKEN", "base-token")
    monkeypatch.setenv("SEATABLE_BASE_URL", "https://seatable.example")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        if url.endswith("/api/v2.1/dtable/app-access-token/"):
            return {"access_token": "app-token", "dtable_uuid": "dtable-uuid"}
        if method == "GET":
            return {"rows": [{"_id": "row-1", "Name": "Ada"}]}
        return {"first_row": {"_id": "row-2", "Name": "Grace"}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    list_result = json.loads(
        tools.seatable_list_rows.func(
            table_name="People",
            view_name="Grid",
            limit=20,
            start=4,
        )
    )
    create_result = json.loads(
        tools.seatable_create_row.func(
            table_name="People",
            fields_json='{"Name": "Grace"}',
        )
    )

    assert list_result[0]["Name"] == "Ada"
    assert create_result["Name"] == "Grace"
    assert calls[0]["url"] == "https://seatable.example/api/v2.1/dtable/app-access-token/"
    assert calls[0]["headers"]["Authorization"] == "Token base-token"
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == "https://seatable.example/api-gateway/api/v2/dtables/dtable-uuid/rows/"
    assert calls[1]["params"] == {"table_name": "People", "view_name": "Grid", "limit": 20, "start": 4}
    assert calls[1]["headers"]["Authorization"] == "Token app-token"
    assert calls[3]["method"] == "POST"
    assert calls[3]["json_body"] == {"table_name": "People", "rows": [{"Name": "Grace"}]}


def test_seatable_sql_identifier_rejects_unsafe_table_names():
    from nymeria.tools import data_table_service_integrations as tools

    assert tools._seatable_identifier("People 2026") == "People 2026"
    with pytest.raises(ValueError, match="SeaTable table_name"):
        tools._seatable_identifier("People` WHERE 1=1 --")
    with pytest.raises(ValueError, match="SeaTable table_name"):
        tools._seatable_identifier("People; DROP TABLE People")


def test_stackby_rows_use_api_key_header(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("STACKBY_API_KEY", "stackby-key")
    calls = []

    def fake_request(method, url, params=None, json_body=None, headers=None):
        calls.append({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"records": [{"id": "row-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    list_result = json.loads(
        tools.stackby_list_rows.func(
            stack_id="stack-1",
            table="People",
            view="Grid",
            limit=25,
            offset=3,
        )
    )
    create_result = json.loads(
        tools.stackby_create_rows.func(
            stack_id="stack-1",
            table="People",
            records_json='{"Name": "Ada"}',
        )
    )

    assert list_result["records"][0]["id"] == "row-1"
    assert create_result["records"][0]["id"] == "row-1"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://stackby.com/api/betav1/rowlist/stack-1/People"
    assert calls[0]["params"] == {"view": "Grid", "maxrecord": 25, "offset": 3}
    assert calls[0]["headers"]["api-key"] == "stackby-key"
    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "https://stackby.com/api/betav1/rowcreate/stack-1/People"
    assert calls[1]["json_body"] == {"records": [{"field": {"Name": "Ada"}}]}


def test_adalo_create_record_uses_app_credentials(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("ADALO_API_KEY", "adalo-key")
    monkeypatch.setenv("ADALO_APP_ID", "app-1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"id": "row-1", "Name": "Ada"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.adalo_create_record.func(
            collection_id="collection-1",
            fields_json='{"Name": "Ada"}',
        )
    )

    assert result["id"] == "row-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.adalo.com/v0/apps/app-1/collections/collection-1"
    assert captured["headers"]["Authorization"] == "Bearer adalo-key"
    assert captured["json_body"] == {"Name": "Ada"}


def test_bubble_list_objects_uses_vault_token_and_dev_base(tmp_path, monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Bubble",
        provider="bubble",
        kind="api_key",
        allowed_targets=["native_tool:bubble_list_objects"],
        secret_fields={"apiToken": "bubble-token", "appName": "myapp", "environment": "development"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"response": {"results": [{"_id": "obj-1", "Name": "Ada"}], "remaining": 0}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.bubble_list_objects.func(
            type_name="Contact",
            constraints_json='[{"key":"Name","constraint_type":"equals","value":"Ada"}]',
            sort_field="Created Date",
            descending=True,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result[0]["_id"] == "obj-1"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://myapp.bubbleapps.io/version-test/api/1.1/obj/contact"
    assert captured["headers"]["Authorization"] == "Bearer bubble-token"
    assert json.loads(captured["params"]["constraints"]) == [
        {"key": "Name", "constraint_type": "equals", "value": "Ada"}
    ]
    assert captured["params"]["sort_field"] == "Created Date"
    assert captured["params"]["descending"] == "true"


def test_cockpit_save_collection_entry_uses_query_token(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("COCKPIT_BASE_URL", "https://cms.example")
    monkeypatch.setenv("COCKPIT_ACCESS_TOKEN", "cockpit-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "json_body": json_body, "headers": headers})
        return {"_id": "entry-1", "title": "Ada"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.cockpit_save_collection_entry.func(
            collection="people",
            entry_id="entry-1",
            data_json='{"title": "Ada"}',
        )
    )

    assert result["_id"] == "entry-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://cms.example/api/collections/save/people"
    assert captured["params"] == {"token": "cockpit-token"}
    assert captured["json_body"] == {"data": {"_id": "entry-1", "title": "Ada"}}


def test_kobotoolbox_list_submissions_uses_env_token(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("KOBOTOOLBOX_API_TOKEN", "kobo-token")
    monkeypatch.setenv("KOBOTOOLBOX_BASE_URL", "https://kobo.example")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"results": [{"_id": 1, "name": "Ada"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.kobotoolbox_list_submissions.func(
            form_id="form1",
            filter_json='{"_id": {"$gt": 0}}',
            fields="name,_id",
            limit=10,
        )
    )

    assert result[0]["name"] == "Ada"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://kobo.example/api/v2/assets/form1/data/"
    assert captured["headers"]["Authorization"] == "Token kobo-token"
    assert captured["params"]["limit"] == 10
    assert captured["params"]["query"] == '{"_id": {"$gt": 0}}'
    assert json.loads(captured["params"]["fields"]) == ["name", "_id"]


def test_kobotoolbox_create_file_from_url_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="KoBoToolbox",
        provider="kobotoolbox",
        kind="api_key",
        allowed_targets=["native_tool:kobotoolbox_create_file_from_url"],
        secret_fields={"apiToken": "kobo-token", "base_url": "https://kobo.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"uid": "file-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.kobotoolbox_create_file_from_url.func(
            form_id="form1",
            file_url="https://example.com/image.png",
            description="Reference image",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["uid"] == "file-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://kobo.example/api/v2/assets/form1/files/"
    assert captured["headers"]["Authorization"] == "Token kobo-token"
    assert captured["json_body"] == {
        "description": "Reference image",
        "file_type": "form_media",
        "metadata": {"redirect_url": "https://example.com/image.png"},
    }


def test_data_table_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import data_table_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("QUICKBASE_HOSTNAME", "example.quickbase.com")
    monkeypatch.delenv("KOBOTOOLBOX_API_TOKEN", raising=False)

    baserow_result = tools.baserow_list_tables.func()
    nocodb_result = tools.nocodb_list_bases.func()
    coda_result = tools.coda_list_docs.func()
    grist_result = tools.grist_list_orgs.func()
    supabase_result = tools.supabase_list_rows.func(table="contacts")
    quickbase_result = tools.quickbase_list_fields.func(table_id="bt123")
    seatable_result = tools.seatable_get_metadata.func()
    stackby_result = tools.stackby_list_rows.func(stack_id="stack-1", table="People")
    adalo_result = tools.adalo_list_records.func(collection_id="collection-1")
    bubble_result = tools.bubble_list_objects.func(type_name="Contact")
    cockpit_result = tools.cockpit_list_collections.func()
    kobo_result = tools.kobotoolbox_list_forms.func()

    assert 'provider "baserow"' in baserow_result
    assert "BASEROW_API_TOKEN" in baserow_result
    assert 'provider "nocodb"' in nocodb_result
    assert "NOCODB_API_TOKEN" in nocodb_result
    assert 'provider "coda"' in coda_result
    assert "CODA_API_TOKEN" in coda_result
    assert 'provider "grist"' in grist_result
    assert "GRIST_API_KEY" in grist_result
    assert 'provider "supabase"' in supabase_result
    assert "SUPABASE_SERVICE_ROLE_KEY" in supabase_result
    assert 'provider "quickbase"' in quickbase_result
    assert "QUICKBASE_USER_TOKEN" in quickbase_result
    assert 'provider "seatable"' in seatable_result
    assert "SEATABLE_API_TOKEN" in seatable_result
    assert 'provider "stackby"' in stackby_result
    assert "STACKBY_API_KEY" in stackby_result
    assert 'provider "adalo"' in adalo_result
    assert "ADALO_API_KEY + ADALO_APP_ID" in adalo_result
    assert 'provider "bubble"' in bubble_result
    assert "BUBBLE_API_TOKEN + BUBBLE_APP_NAME" in bubble_result
    assert 'provider "cockpit"' in cockpit_result
    assert "COCKPIT_BASE_URL + COCKPIT_ACCESS_TOKEN" in cockpit_result
    assert 'provider "kobotoolbox"' in kobo_result
    assert "KOBOTOOLBOX_API_TOKEN" in kobo_result


def test_data_table_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "supabase_list_rows",
        "quickbase_list_fields",
        "quickbase_query_records",
        "seatable_get_metadata",
        "seatable_list_rows",
        "seatable_get_row",
        "stackby_list_rows",
        "stackby_get_row",
        "baserow_list_tables",
        "baserow_list_fields",
        "baserow_list_rows",
        "baserow_get_row",
        "nocodb_list_bases",
        "nocodb_get_base",
        "nocodb_list_records",
        "nocodb_get_record",
        "nocodb_count_records",
        "coda_list_docs",
        "coda_list_tables",
        "coda_list_table_rows",
        "coda_get_table_row",
        "coda_list_formulas",
        "coda_list_controls",
        "grist_list_orgs",
        "grist_list_workspaces",
        "grist_list_docs",
        "grist_list_tables",
        "grist_list_columns",
        "grist_list_records",
        "adalo_list_records",
        "adalo_get_record",
        "bubble_list_objects",
        "bubble_get_object",
        "cockpit_list_collections",
        "cockpit_list_collection_entries",
        "cockpit_list_singletons",
        "cockpit_get_singleton",
        "kobotoolbox_list_forms",
        "kobotoolbox_get_form",
        "kobotoolbox_list_submissions",
        "kobotoolbox_get_submission",
        "kobotoolbox_get_submission_validation",
        "kobotoolbox_list_hooks",
        "kobotoolbox_get_hook",
        "kobotoolbox_get_hook_logs",
        "kobotoolbox_list_files",
        "kobotoolbox_get_file",
    ]
    moderate_names = [
        "supabase_insert_rows",
        "supabase_update_rows",
        "supabase_delete_rows",
        "quickbase_upsert_records",
        "quickbase_delete_records",
        "seatable_create_row",
        "seatable_update_row",
        "seatable_delete_row",
        "stackby_create_rows",
        "stackby_delete_rows",
        "baserow_create_row",
        "baserow_update_row",
        "baserow_delete_row",
        "nocodb_create_record",
        "nocodb_update_record",
        "nocodb_delete_record",
        "coda_create_table_row",
        "coda_update_table_row",
        "coda_delete_table_row",
        "grist_create_record",
        "grist_update_record",
        "grist_delete_records",
        "adalo_create_record",
        "adalo_update_record",
        "adalo_delete_record",
        "bubble_create_object",
        "bubble_update_object",
        "bubble_delete_object",
        "cockpit_save_collection_entry",
        "cockpit_submit_form",
        "kobotoolbox_redeploy_form",
        "kobotoolbox_delete_submission",
        "kobotoolbox_set_submission_validation",
        "kobotoolbox_retry_hook",
        "kobotoolbox_delete_file",
        "kobotoolbox_create_file_from_url",
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


def test_data_table_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        baserow_list_rows,
        adalo_create_record,
        bubble_create_object,
        coda_create_table_row,
        cockpit_save_collection_entry,
        grist_list_records,
        kobotoolbox_create_file_from_url,
        nocodb_update_record,
        quickbase_query_records,
        seatable_create_row,
        stackby_list_rows,
        supabase_insert_rows,
    )

    assert "config" not in baserow_list_rows.args_schema.model_json_schema()["properties"]
    assert "config" not in adalo_create_record.args_schema.model_json_schema()["properties"]
    assert "config" not in bubble_create_object.args_schema.model_json_schema()["properties"]
    assert "config" not in cockpit_save_collection_entry.args_schema.model_json_schema()["properties"]
    assert "config" not in nocodb_update_record.args_schema.model_json_schema()["properties"]
    assert "config" not in coda_create_table_row.args_schema.model_json_schema()["properties"]
    assert "config" not in grist_list_records.args_schema.model_json_schema()["properties"]
    assert "config" not in kobotoolbox_create_file_from_url.args_schema.model_json_schema()["properties"]
    assert "config" not in supabase_insert_rows.args_schema.model_json_schema()["properties"]
    assert "config" not in quickbase_query_records.args_schema.model_json_schema()["properties"]
    assert "config" not in seatable_create_row.args_schema.model_json_schema()["properties"]
    assert "config" not in stackby_list_rows.args_schema.model_json_schema()["properties"]
