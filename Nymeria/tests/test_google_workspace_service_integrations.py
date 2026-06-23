import json


class _Executable:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def _patch_google_request(monkeypatch, service):
    from nymeria.tools import google_workspace_service_integrations as tools

    calls = []

    def fake_request(user_id, provider, scopes, operation, **kwargs):
        calls.append(
            {
                "user_id": user_id,
                "provider": provider,
                "scopes": scopes,
                "kwargs": kwargs,
            }
        )
        return True, operation(service)

    monkeypatch.setattr(tools.auth_utils, "google_api_request", fake_request)
    return calls


def test_google_tasks_create_task_calls_tasks_api(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class TasksResource:
        def insert(self, **kwargs):
            captured.update(kwargs)
            return _Executable({"id": "task-1", "title": kwargs["body"]["title"]})

    class TasksService:
        def tasks(self):
            return TasksResource()

    calls = _patch_google_request(monkeypatch, TasksService())

    result = json.loads(
        tools.google_tasks_create_task.func(
            title="Call dentist",
            notes="Ask about Tuesday",
            due="2026-05-16T12:00:00.000Z",
            tasklist_id="list-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"id": "task-1", "title": "Call dentist"}
    assert captured["tasklist"] == "list-1"
    assert captured["body"]["title"] == "Call dentist"
    assert captured["body"]["notes"] == "Ask about Tuesday"
    assert captured["body"]["due"] == "2026-05-16T12:00:00.000Z"
    assert calls[0]["provider"] == "google_docs"
    assert calls[0]["kwargs"]["service_name"] == "tasks"
    assert "https://www.googleapis.com/auth/tasks" in calls[0]["scopes"]


def test_google_contacts_create_contact_calls_people_api(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class PeopleResource:
        def createContact(self, **kwargs):
            captured.update(kwargs)
            return _Executable({"resourceName": "people/c1", **kwargs["body"]})

    class PeopleService:
        def people(self):
            return PeopleResource()

    calls = _patch_google_request(monkeypatch, PeopleService())

    result = json.loads(
        tools.google_contacts_create_contact.func(
            given_name="Ada",
            family_name="Lovelace",
            email="ada@example.com",
            phone="+15551234567",
            organization="Analytical Engines",
            job_title="Researcher",
            notes="Met at conference",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["resourceName"] == "people/c1"
    assert captured["body"]["names"] == [{"givenName": "Ada", "familyName": "Lovelace"}]
    assert captured["body"]["emailAddresses"] == [{"value": "ada@example.com"}]
    assert captured["body"]["phoneNumbers"] == [{"value": "+15551234567"}]
    assert captured["body"]["organizations"] == [{"name": "Analytical Engines", "title": "Researcher"}]
    assert captured["body"]["biographies"] == [{"value": "Met at conference", "contentType": "TEXT_PLAIN"}]
    assert calls[0]["kwargs"]["service_name"] == "people"
    assert "https://www.googleapis.com/auth/contacts" in calls[0]["scopes"]


def test_google_drive_upload_text_file_calls_drive_api(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class FilesResource:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Executable({"id": "file-1", "name": kwargs["body"]["name"]})

    class DriveService:
        def files(self):
            return FilesResource()

    calls = _patch_google_request(monkeypatch, DriveService())

    result = json.loads(
        tools.google_drive_upload_text_file.func(
            name="notes.txt",
            content="hello",
            parent_folder_id="folder-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"id": "file-1", "name": "notes.txt"}
    assert captured["body"] == {"name": "notes.txt", "parents": ["folder-1"]}
    assert captured["fields"] == "id,name,mimeType,webViewLink,parents,size"
    assert captured["media_body"].mimetype() == "text/plain"
    assert calls[0]["kwargs"]["service_name"] == "drive"


def test_google_slides_replace_text_calls_batch_update(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class PresentationsResource:
        def batchUpdate(self, **kwargs):
            captured.update(kwargs)
            return _Executable({"replies": [{"replaceAllText": {"occurrencesChanged": 2}}]})

    class SlidesService:
        def presentations(self):
            return PresentationsResource()

    calls = _patch_google_request(monkeypatch, SlidesService())

    result = json.loads(
        tools.google_slides_replace_text.func(
            presentation_id="deck-1",
            contains_text="{{name}}",
            replace_text="Ada",
            match_case=True,
            page_object_ids_json='["slide-1"]',
            required_revision_id="rev-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["replies"][0]["replaceAllText"]["occurrencesChanged"] == 2
    assert captured["presentationId"] == "deck-1"
    assert captured["body"]["writeControl"] == {"requiredRevisionId": "rev-1"}
    replace_request = captured["body"]["requests"][0]["replaceAllText"]
    assert replace_request["containsText"] == {"text": "{{name}}", "matchCase": True}
    assert replace_request["replaceText"] == "Ada"
    assert replace_request["pageObjectIds"] == ["slide-1"]
    assert calls[0]["kwargs"]["service_name"] == "slides"
    assert "https://www.googleapis.com/auth/presentations" in calls[0]["scopes"]


def test_google_slides_list_fields_mask_is_balanced():
    # An unbalanced field mask makes the Slides API reject the whole request
    # with a 400, which the broad try/except in the tool masks as a generic
    # error. Guard the parentheses balance directly.
    from nymeria.tools.google_workspace_service_integrations import _SLIDES_LIST_FIELDS

    assert _SLIDES_LIST_FIELDS.count("(") == _SLIDES_LIST_FIELDS.count(")")
    # The mask must project the full text path that _slide_text walks.
    for segment in ("slides(", "pageElements(", "shape(", "text(", "textElements(", "textRun(content"):
        assert segment in _SLIDES_LIST_FIELDS


def test_google_slides_list_slides_summarizes_text(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class PresentationsResource:
        def get(self, **kwargs):
            captured.update(kwargs)
            return _Executable(
                {
                    "presentationId": kwargs["presentationId"],
                    "title": "Deck",
                    "slides": [
                        {
                            "objectId": "slide-1",
                            "pageType": "SLIDE",
                            "pageElements": [
                                {
                                    "shape": {
                                        "text": {
                                            "textElements": [
                                                {"textRun": {"content": "Hello "}},
                                                {"textRun": {"content": "world"}},
                                            ]
                                        }
                                    }
                                }
                            ],
                        }
                    ],
                }
            )

    class SlidesService:
        def presentations(self):
            return PresentationsResource()

    _patch_google_request(monkeypatch, SlidesService())

    result = json.loads(
        tools.google_slides_list_slides.func(
            presentation_id="deck-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {
        "presentationId": "deck-1",
        "title": "Deck",
        "slides": [{"index": 1, "objectId": "slide-1", "pageType": "SLIDE", "text": "Hello world"}],
    }
    # The field mask passed to the Slides API must be syntactically valid.
    fields = captured["fields"]
    assert fields.count("(") == fields.count(")")


def test_google_chat_send_message_calls_chat_api(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class MessagesResource:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Executable({"name": "spaces/AAA/messages/BBB", "text": kwargs["body"]["text"]})

    class SpacesResource:
        def messages(self):
            return MessagesResource()

    class ChatService:
        def spaces(self):
            return SpacesResource()

    calls = _patch_google_request(monkeypatch, ChatService())

    result = json.loads(
        tools.google_chat_send_message.func(
            space_name="AAA",
            text="hello",
            thread_key="thread-1",
            request_id="request-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == {"name": "spaces/AAA/messages/BBB", "text": "hello"}
    assert captured["parent"] == "spaces/AAA"
    assert captured["body"] == {"text": "hello"}
    assert captured["threadKey"] == "thread-1"
    assert captured["requestId"] == "request-1"
    assert calls[0]["kwargs"]["service_name"] == "chat"
    assert "https://www.googleapis.com/auth/chat.messages" in calls[0]["scopes"]


def test_google_chat_list_spaces_calls_chat_api(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    captured = {}

    class SpacesResource:
        def list(self, **kwargs):
            captured.update(kwargs)
            return _Executable({"spaces": [{"name": "spaces/AAA", "displayName": "Team"}]})

    class ChatService:
        def spaces(self):
            return SpacesResource()

    _patch_google_request(monkeypatch, ChatService())

    result = json.loads(
        tools.google_chat_list_spaces.func(
            filter_query='spaceType = "SPACE"',
            page_size=3,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result == [{"name": "spaces/AAA", "displayName": "Team"}]
    assert captured["pageSize"] == 3
    assert captured["filter"] == 'spaceType = "SPACE"'


def test_google_workspace_missing_auth_returns_error(monkeypatch):
    from nymeria.tools import google_workspace_service_integrations as tools

    def fake_request(*args, **kwargs):
        return False, (
            "No authenticated Google account. Call "
            "request_credential(provider=\"google_docs\", kind=\"oauth\") to connect."
        )

    monkeypatch.setattr(tools.auth_utils, "google_api_request", fake_request)

    result = tools.google_tasks_list_tasklists.func()

    assert result == (
        "[Error]: No authenticated Google account. Call "
        "request_credential(provider=\"google_docs\", kind=\"oauth\") to connect."
    )


def test_google_workspace_tools_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "google_tasks_list_tasklists",
        "google_tasks_list_tasks",
        "google_tasks_get_task",
        "google_contacts_list_contacts",
        "google_contacts_get_contact",
        "google_drive_search_files",
        "google_drive_get_file",
        "google_drive_download_text",
        "google_slides_get_presentation",
        "google_slides_list_slides",
        "google_slides_get_page_thumbnail",
        "google_chat_list_spaces",
        "google_chat_get_space",
        "google_chat_list_members",
        "google_chat_get_member",
        "google_chat_list_messages",
        "google_chat_get_message",
    ]
    moderate_names = [
        "google_tasks_create_task",
        "google_tasks_update_task",
        "google_tasks_complete_task",
        "google_tasks_delete_task",
        "google_contacts_create_contact",
        "google_contacts_update_contact",
        "google_contacts_delete_contact",
        "google_drive_create_folder",
        "google_drive_upload_text_file",
        "google_drive_trash_file",
        "google_slides_create_presentation",
        "google_slides_create_slide",
        "google_slides_replace_text",
        "google_slides_batch_update",
        "google_chat_send_message",
        "google_chat_update_message",
        "google_chat_delete_message",
    ]

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.GOOGLE_DOCS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.GOOGLE_DOCS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_google_workspace_tool_schemas_hide_runtime_config():
    from nymeria.tools import (
        google_contacts_create_contact,
        google_chat_send_message,
        google_drive_upload_text_file,
        google_slides_create_presentation,
        google_tasks_create_task,
    )

    assert "config" not in google_tasks_create_task.args_schema.model_json_schema()["properties"]
    assert "config" not in google_contacts_create_contact.args_schema.model_json_schema()["properties"]
    assert "config" not in google_drive_upload_text_file.args_schema.model_json_schema()["properties"]
    assert "config" not in google_slides_create_presentation.args_schema.model_json_schema()["properties"]
    assert "config" not in google_chat_send_message.args_schema.model_json_schema()["properties"]
