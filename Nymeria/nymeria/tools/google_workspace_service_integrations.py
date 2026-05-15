"""Google Workspace productivity service tools."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from . import auth_cache_utils as auth_utils
from .google_docs_auth import GOOGLE_SCOPES, PROVIDER
from .utils import get_user_id

logger = logging.getLogger(__name__)

_MAX_JSON_CHARS = 80_000
_CONTACT_FIELDS = "names,emailAddresses,phoneNumbers,organizations,biographies,metadata,photos"
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_APP_PREFIX = "application/vnd.google-apps."


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _parse_json(value: str, *, expected: type, label: str) -> Any:
    if not value.strip():
        return {} if expected is dict else []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} must be valid JSON: {e}") from e
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} must be a JSON {expected.__name__}.")
    return parsed


def _limit(value: int, *, default: int = 50, max_value: int = 1000) -> int:
    try:
        return max(1, min(max_value, int(value)))
    except Exception:
        return default


def _workspace_request(
    user_id: str,
    service_name: str,
    service_version: str,
    operation: Callable[[Any], Any],
    *,
    account_id: Optional[str],
    api_label: str,
) -> tuple[bool, Any]:
    return auth_utils.google_api_request(
        user_id,
        PROVIDER,
        GOOGLE_SCOPES,
        operation,
        service_name=service_name,
        service_version=service_version,
        account_id=account_id,
        auth_tool_name="google_docs_auth_start",
        api_label=api_label,
        build_kwargs={"cache_discovery": False},
    )


def _tasks_request(
    user_id: str,
    operation: Callable[[Any], Any],
    *,
    account_id: Optional[str],
) -> tuple[bool, Any]:
    return _workspace_request(
        user_id,
        "tasks",
        "v1",
        operation,
        account_id=account_id,
        api_label="Google Tasks",
    )


def _people_request(
    user_id: str,
    operation: Callable[[Any], Any],
    *,
    account_id: Optional[str],
) -> tuple[bool, Any]:
    return _workspace_request(
        user_id,
        "people",
        "v1",
        operation,
        account_id=account_id,
        api_label="Google Contacts",
    )


def _drive_request(
    user_id: str,
    operation: Callable[[Any], Any],
    *,
    account_id: Optional[str],
) -> tuple[bool, Any]:
    return _workspace_request(
        user_id,
        "drive",
        "v3",
        operation,
        account_id=account_id,
        api_label="Google Drive",
    )


def _resource_name(contact_id_or_resource: str) -> str:
    value = contact_id_or_resource.strip()
    if not value:
        raise ValueError("contact_id is required.")
    return value if value.startswith("people/") else f"people/{value}"


def _task_body(title: str = "", notes: str = "", due: str = "", fields_json: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {}
    if title.strip():
        body["title"] = title.strip()
    if notes.strip():
        body["notes"] = notes
    if due.strip():
        body["due"] = due.strip()
    extra = _parse_json(fields_json, expected=dict, label="fields_json") if fields_json.strip() else {}
    body.update(extra)
    return body


def _contact_body(
    *,
    given_name: str = "",
    family_name: str = "",
    email: str = "",
    phone: str = "",
    organization: str = "",
    job_title: str = "",
    notes: str = "",
    fields_json: str = "",
) -> tuple[dict[str, Any], set[str]]:
    body: dict[str, Any] = {}
    changed: set[str] = set()
    if given_name.strip() or family_name.strip():
        body["names"] = [{"givenName": given_name.strip(), "familyName": family_name.strip()}]
        changed.add("names")
    if email.strip():
        body["emailAddresses"] = [{"value": email.strip()}]
        changed.add("emailAddresses")
    if phone.strip():
        body["phoneNumbers"] = [{"value": phone.strip()}]
        changed.add("phoneNumbers")
    if organization.strip() or job_title.strip():
        body["organizations"] = [{"name": organization.strip(), "title": job_title.strip()}]
        changed.add("organizations")
    if notes.strip():
        body["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
        changed.add("biographies")
    extra = _parse_json(fields_json, expected=dict, label="fields_json") if fields_json.strip() else {}
    for key, value in extra.items():
        body[key] = value
        changed.add(key)
    return body, changed


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _drive_query(
    *,
    query: str,
    raw_query: str,
    mime_type: str,
    folder_id: str,
    type_filter: str,
    include_trashed: bool,
) -> str:
    if raw_query.strip():
        return raw_query.strip()
    parts: list[str] = []
    if not include_trashed:
        parts.append("trashed = false")
    if query.strip():
        escaped = _escape_drive_query(query.strip())
        parts.append(f"(name contains '{escaped}' or fullText contains '{escaped}')")
    if mime_type.strip():
        parts.append(f"mimeType = '{_escape_drive_query(mime_type.strip())}'")
    normalized_type = type_filter.strip().lower()
    if normalized_type == "folders":
        parts.append(f"mimeType = '{_DRIVE_FOLDER_MIME}'")
    elif normalized_type == "files":
        parts.append(f"mimeType != '{_DRIVE_FOLDER_MIME}'")
    elif normalized_type and normalized_type != "all":
        raise ValueError("type_filter must be all, files, or folders.")
    if folder_id.strip():
        parts.append(f"'{_escape_drive_query(folder_id.strip())}' in parents")
    return " and ".join(parts) if parts else "trashed = false"


def _rfc3339_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@tool
def google_tasks_list_tasklists(
    max_results: int = 20,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Google Tasks task lists."""
    user_id = get_user_id(config)
    try:
        limit = _limit(max_results, default=20, max_value=100)
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasklists().list(maxResults=limit).execute(),
            account_id=account_id,
        )
        return _dump_json(result.get("items", result) if success and isinstance(result, dict) else result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_list_tasklists failed", exc_info=True)
        return f"[Error]: Google Tasks task-list lookup failed: {e}"


@tool
def google_tasks_list_tasks(
    tasklist_id: str = "@default",
    show_completed: bool = False,
    show_hidden: bool = False,
    max_results: int = 50,
    due_min: str = "",
    due_max: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List tasks from a Google Tasks task list."""
    user_id = get_user_id(config)
    if not tasklist_id.strip():
        return "[Error]: tasklist_id is required."
    try:
        params = {
            "tasklist": tasklist_id.strip(),
            "showCompleted": bool(show_completed),
            "showHidden": bool(show_hidden),
            "maxResults": _limit(max_results, max_value=100),
        }
        if due_min.strip():
            params["dueMin"] = due_min.strip()
        if due_max.strip():
            params["dueMax"] = due_max.strip()
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasks().list(**params).execute(),
            account_id=account_id,
        )
        return _dump_json(result.get("items", result) if success and isinstance(result, dict) else result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_list_tasks failed", exc_info=True)
        return f"[Error]: Google Tasks lookup failed: {e}"


@tool
def google_tasks_get_task(
    task_id: str,
    tasklist_id: str = "@default",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Google Tasks task."""
    user_id = get_user_id(config)
    if not task_id.strip() or not tasklist_id.strip():
        return "[Error]: task_id and tasklist_id are required."
    try:
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasks().get(tasklist=tasklist_id.strip(), task=task_id.strip()).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_get_task failed", exc_info=True)
        return f"[Error]: Google Tasks task lookup failed: {e}"


@tool
def google_tasks_create_task(
    title: str,
    notes: str = "",
    due: str = "",
    tasklist_id: str = "@default",
    parent_task_id: str = "",
    previous_task_id: str = "",
    fields_json: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Google Tasks task."""
    user_id = get_user_id(config)
    if not title.strip() or not tasklist_id.strip():
        return "[Error]: title and tasklist_id are required."
    try:
        body = _task_body(title=title, notes=notes, due=due, fields_json=fields_json)
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasks().insert(
                tasklist=tasklist_id.strip(),
                body=body,
                parent=parent_task_id.strip() or None,
                previous=previous_task_id.strip() or None,
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_create_task failed", exc_info=True)
        return f"[Error]: Google Tasks task creation failed: {e}"


@tool
def google_tasks_update_task(
    task_id: str,
    fields_json: str,
    tasklist_id: str = "@default",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Patch a Google Tasks task with an API-shaped JSON object."""
    user_id = get_user_id(config)
    if not task_id.strip() or not tasklist_id.strip():
        return "[Error]: task_id and tasklist_id are required."
    try:
        body = _task_body(fields_json=fields_json)
        if not body:
            return "[Error]: fields_json must include at least one field."
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasks().patch(tasklist=tasklist_id.strip(), task=task_id.strip(), body=body).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_update_task failed", exc_info=True)
        return f"[Error]: Google Tasks task update failed: {e}"


@tool
def google_tasks_complete_task(
    task_id: str,
    tasklist_id: str = "@default",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Mark a Google Tasks task complete."""
    user_id = get_user_id(config)
    if not task_id.strip() or not tasklist_id.strip():
        return "[Error]: task_id and tasklist_id are required."
    try:
        body = {"status": "completed", "completed": _rfc3339_now()}
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasks().patch(tasklist=tasklist_id.strip(), task=task_id.strip(), body=body).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_complete_task failed", exc_info=True)
        return f"[Error]: Google Tasks task completion failed: {e}"


@tool
def google_tasks_delete_task(
    task_id: str,
    tasklist_id: str = "@default",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Google Tasks task."""
    user_id = get_user_id(config)
    if not task_id.strip() or not tasklist_id.strip():
        return "[Error]: task_id and tasklist_id are required."
    try:
        success, result = _tasks_request(
            user_id,
            lambda s: s.tasks().delete(tasklist=tasklist_id.strip(), task=task_id.strip()).execute(),
            account_id=account_id,
        )
        return _dump_json({"status": "deleted", "task_id": task_id.strip()}) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_tasks_delete_task failed", exc_info=True)
        return f"[Error]: Google Tasks task deletion failed: {e}"


@tool
def google_contacts_list_contacts(
    query: str = "",
    limit: int = 50,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List or search Google Contacts."""
    user_id = get_user_id(config)
    try:
        max_items = _limit(limit, max_value=200)
        if query.strip():
            def operation(service: Any) -> Any:
                return service.people().searchContacts(
                    query=query.strip(),
                    readMask=_CONTACT_FIELDS,
                    pageSize=max_items,
                ).execute()
        else:
            def operation(service: Any) -> Any:
                return service.people().connections().list(
                    resourceName="people/me",
                    personFields=_CONTACT_FIELDS,
                    pageSize=max_items,
                    sortOrder="FIRST_NAME_ASCENDING",
                ).execute()
        success, result = _people_request(user_id, operation, account_id=account_id)
        if not success:
            return f"[Error]: {result}"
        if isinstance(result, dict) and "connections" in result:
            return _dump_json(result["connections"][:max_items])
        if isinstance(result, dict) and "results" in result:
            return _dump_json([row.get("person", row) for row in result["results"][:max_items]])
        return _dump_json(result)
    except Exception as e:
        logger.error("google_contacts_list_contacts failed", exc_info=True)
        return f"[Error]: Google Contacts listing failed: {e}"


@tool
def google_contacts_get_contact(
    contact_id: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Google Contacts person by contact ID or people/* resource name."""
    user_id = get_user_id(config)
    try:
        resource_name = _resource_name(contact_id)
        success, result = _people_request(
            user_id,
            lambda s: s.people().get(resourceName=resource_name, personFields=_CONTACT_FIELDS).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_contacts_get_contact failed", exc_info=True)
        return f"[Error]: Google Contacts lookup failed: {e}"


@tool
def google_contacts_create_contact(
    given_name: str = "",
    family_name: str = "",
    email: str = "",
    phone: str = "",
    organization: str = "",
    job_title: str = "",
    notes: str = "",
    fields_json: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Google Contacts contact."""
    user_id = get_user_id(config)
    try:
        body, _changed = _contact_body(
            given_name=given_name,
            family_name=family_name,
            email=email,
            phone=phone,
            organization=organization,
            job_title=job_title,
            notes=notes,
            fields_json=fields_json,
        )
        if not body:
            return "[Error]: Provide contact fields or fields_json."
        success, result = _people_request(
            user_id,
            lambda s: s.people().createContact(body=body).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_contacts_create_contact failed", exc_info=True)
        return f"[Error]: Google Contacts creation failed: {e}"


@tool
def google_contacts_update_contact(
    contact_id: str,
    given_name: str = "",
    family_name: str = "",
    email: str = "",
    phone: str = "",
    organization: str = "",
    job_title: str = "",
    notes: str = "",
    fields_json: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Google Contacts contact by replacing supplied top-level fields."""
    user_id = get_user_id(config)
    try:
        resource_name = _resource_name(contact_id)
        update_body, changed = _contact_body(
            given_name=given_name,
            family_name=family_name,
            email=email,
            phone=phone,
            organization=organization,
            job_title=job_title,
            notes=notes,
            fields_json=fields_json,
        )
        if not update_body:
            return "[Error]: Provide contact fields or fields_json."

        def operation(service: Any) -> Any:
            existing = service.people().get(resourceName=resource_name, personFields=_CONTACT_FIELDS).execute()
            existing.update(update_body)
            return service.people().updateContact(
                resourceName=resource_name,
                updatePersonFields=",".join(sorted(changed)),
                body=existing,
            ).execute()

        success, result = _people_request(user_id, operation, account_id=account_id)
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_contacts_update_contact failed", exc_info=True)
        return f"[Error]: Google Contacts update failed: {e}"


@tool
def google_contacts_delete_contact(
    contact_id: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Google Contacts contact."""
    user_id = get_user_id(config)
    try:
        resource_name = _resource_name(contact_id)
        success, result = _people_request(
            user_id,
            lambda s: s.people().deleteContact(resourceName=resource_name).execute(),
            account_id=account_id,
        )
        return _dump_json({"status": "deleted", "contact": resource_name}) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_contacts_delete_contact failed", exc_info=True)
        return f"[Error]: Google Contacts deletion failed: {e}"


@tool
def google_drive_search_files(
    query: str = "",
    raw_query: str = "",
    mime_type: str = "",
    folder_id: str = "",
    type_filter: str = "all",
    include_trashed: bool = False,
    limit: int = 20,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Google Drive files and folders."""
    user_id = get_user_id(config)
    try:
        max_items = _limit(limit, default=20, max_value=100)
        q = _drive_query(
            query=query,
            raw_query=raw_query,
            mime_type=mime_type,
            folder_id=folder_id,
            type_filter=type_filter,
            include_trashed=include_trashed,
        )
        fields = "files(id,name,mimeType,modifiedTime,webViewLink,size,parents,trashed),nextPageToken"
        success, result = _drive_request(
            user_id,
            lambda s: s.files().list(
                q=q,
                pageSize=max_items,
                orderBy="modifiedTime desc",
                fields=fields,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result.get("files", result) if success and isinstance(result, dict) else result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_drive_search_files failed", exc_info=True)
        return f"[Error]: Google Drive search failed: {e}"


@tool
def google_drive_get_file(
    file_id: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Google Drive file metadata."""
    user_id = get_user_id(config)
    if not file_id.strip():
        return "[Error]: file_id is required."
    try:
        fields = "id,name,mimeType,modifiedTime,createdTime,webViewLink,webContentLink,size,parents,trashed,owners(emailAddress,displayName),sharingUser(emailAddress,displayName)"
        success, result = _drive_request(
            user_id,
            lambda s: s.files().get(fileId=file_id.strip(), fields=fields, supportsAllDrives=True).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_drive_get_file failed", exc_info=True)
        return f"[Error]: Google Drive metadata lookup failed: {e}"


@tool
def google_drive_download_text(
    file_id: str,
    export_mime_type: str = "text/plain",
    max_chars: int = 50000,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Download or export a Google Drive file as text."""
    user_id = get_user_id(config)
    if not file_id.strip():
        return "[Error]: file_id is required."
    try:
        char_limit = _limit(max_chars, default=50000, max_value=500000)

        def operation(service: Any) -> Any:
            from googleapiclient.http import MediaIoBaseDownload

            metadata = service.files().get(
                fileId=file_id.strip(),
                fields="id,name,mimeType,modifiedTime,webViewLink,size",
                supportsAllDrives=True,
            ).execute()
            mime_type = str(metadata.get("mimeType") or "")
            if mime_type.startswith(_GOOGLE_APP_PREFIX):
                request = service.files().export_media(fileId=file_id.strip(), mimeType=export_mime_type)
            else:
                request = service.files().get_media(fileId=file_id.strip(), supportsAllDrives=True)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            text = buffer.getvalue().decode("utf-8", errors="replace")
            if len(text) > char_limit:
                text = text[:char_limit] + "\n...[truncated]"
            return {"file": metadata, "text": text}

        success, result = _drive_request(user_id, operation, account_id=account_id)
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_drive_download_text failed", exc_info=True)
        return f"[Error]: Google Drive text download failed: {e}"


@tool
def google_drive_create_folder(
    name: str,
    parent_folder_id: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Google Drive folder."""
    user_id = get_user_id(config)
    if not name.strip():
        return "[Error]: name is required."
    try:
        body: dict[str, Any] = {"name": name.strip(), "mimeType": _DRIVE_FOLDER_MIME}
        if parent_folder_id.strip():
            body["parents"] = [parent_folder_id.strip()]
        success, result = _drive_request(
            user_id,
            lambda s: s.files().create(body=body, fields="id,name,mimeType,webViewLink,parents").execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_drive_create_folder failed", exc_info=True)
        return f"[Error]: Google Drive folder creation failed: {e}"


@tool
def google_drive_upload_text_file(
    name: str,
    content: str,
    parent_folder_id: str = "",
    mime_type: str = "text/plain",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload a UTF-8 text file to Google Drive."""
    user_id = get_user_id(config)
    if not name.strip():
        return "[Error]: name is required."
    try:
        def operation(service: Any) -> Any:
            from googleapiclient.http import MediaInMemoryUpload

            metadata: dict[str, Any] = {"name": name.strip()}
            if parent_folder_id.strip():
                metadata["parents"] = [parent_folder_id.strip()]
            media = MediaInMemoryUpload(
                content.encode("utf-8"),
                mimetype=mime_type.strip() or "text/plain",
                resumable=False,
            )
            return service.files().create(
                body=metadata,
                media_body=media,
                fields="id,name,mimeType,webViewLink,parents,size",
            ).execute()

        success, result = _drive_request(user_id, operation, account_id=account_id)
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_drive_upload_text_file failed", exc_info=True)
        return f"[Error]: Google Drive text upload failed: {e}"


@tool
def google_drive_trash_file(
    file_id: str,
    trashed: bool = True,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Move a Google Drive file to or from trash."""
    user_id = get_user_id(config)
    if not file_id.strip():
        return "[Error]: file_id is required."
    try:
        success, result = _drive_request(
            user_id,
            lambda s: s.files().update(
                fileId=file_id.strip(),
                body={"trashed": bool(trashed)},
                fields="id,name,mimeType,trashed,webViewLink",
                supportsAllDrives=True,
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_drive_trash_file failed", exc_info=True)
        return f"[Error]: Google Drive trash update failed: {e}"


GOOGLE_WORKSPACE_SERVICE_TOOLS = [
    google_tasks_list_tasklists,
    google_tasks_list_tasks,
    google_tasks_get_task,
    google_tasks_create_task,
    google_tasks_update_task,
    google_tasks_complete_task,
    google_tasks_delete_task,
    google_contacts_list_contacts,
    google_contacts_get_contact,
    google_contacts_create_contact,
    google_contacts_update_contact,
    google_contacts_delete_contact,
    google_drive_search_files,
    google_drive_get_file,
    google_drive_download_text,
    google_drive_create_folder,
    google_drive_upload_text_file,
    google_drive_trash_file,
]
