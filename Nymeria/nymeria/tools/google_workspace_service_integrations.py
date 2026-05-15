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


def _slides_request(
    user_id: str,
    operation: Callable[[Any], Any],
    *,
    account_id: Optional[str],
) -> tuple[bool, Any]:
    return _workspace_request(
        user_id,
        "slides",
        "v1",
        operation,
        account_id=account_id,
        api_label="Google Slides",
    )


def _chat_request(
    user_id: str,
    operation: Callable[[Any], Any],
    *,
    account_id: Optional[str],
) -> tuple[bool, Any]:
    return _workspace_request(
        user_id,
        "chat",
        "v1",
        operation,
        account_id=account_id,
        api_label="Google Chat",
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


def _slides_id(value: str, *, label: str = "presentation_id") -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} is required.")
    return cleaned


def _page_ids(value: str) -> list[str]:
    if not value.strip():
        return []
    parsed = _parse_json(value, expected=list, label="page_object_ids_json")
    out: list[str] = []
    for item in parsed:
        item_id = str(item).strip()
        if item_id:
            out.append(item_id)
    return out


def _slide_text(slide: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in slide.get("pageElements", []) or []:
        shape = element.get("shape") if isinstance(element, dict) else None
        text = shape.get("text") if isinstance(shape, dict) else None
        for text_element in (text or {}).get("textElements", []) or []:
            run = text_element.get("textRun") if isinstance(text_element, dict) else None
            content = run.get("content") if isinstance(run, dict) else None
            if content:
                parts.append(str(content))
    return "".join(parts).strip()


def _summarize_slides(slides: list[dict[str, Any]], *, include_text: bool) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for idx, slide in enumerate(slides, start=1):
        item: dict[str, Any] = {
            "index": idx,
            "objectId": slide.get("objectId"),
            "pageType": slide.get("pageType"),
        }
        if "slideProperties" in slide:
            item["slideProperties"] = slide.get("slideProperties")
        if include_text:
            item["text"] = _slide_text(slide)
        summarized.append(item)
    return summarized


def _chat_resource_name(value: str, *, label: str, prefix: str) -> str:
    cleaned = value.strip().strip("/")
    if not cleaned:
        raise ValueError(f"{label} is required.")
    if cleaned.startswith(prefix):
        return cleaned
    return f"{prefix}{cleaned}"


def _chat_message_body(*, text: str, message_json: str, label: str = "message_json") -> dict[str, Any]:
    if message_json.strip():
        return _parse_json(message_json, expected=dict, label=label)
    if text.strip():
        return {"text": text}
    raise ValueError("text or message_json is required.")


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


@tool
def google_slides_create_presentation(
    title: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Google Slides presentation."""
    user_id = get_user_id(config)
    if not title.strip():
        return "[Error]: title is required."
    try:
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().create(body={"title": title.strip()}).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_slides_create_presentation failed", exc_info=True)
        return f"[Error]: Google Slides presentation creation failed: {e}"


@tool
def google_slides_get_presentation(
    presentation_id: str,
    fields: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Google Slides presentation metadata and content."""
    user_id = get_user_id(config)
    try:
        presentation_id = _slides_id(presentation_id)
        kwargs: dict[str, Any] = {"presentationId": presentation_id}
        if fields.strip():
            kwargs["fields"] = fields.strip()
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().get(**kwargs).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_slides_get_presentation failed", exc_info=True)
        return f"[Error]: Google Slides presentation lookup failed: {e}"


@tool
def google_slides_list_slides(
    presentation_id: str,
    include_text: bool = True,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List slides in a Google Slides presentation with optional text summaries."""
    user_id = get_user_id(config)
    try:
        presentation_id = _slides_id(presentation_id)
        fields = "presentationId,title,slides(objectId,pageType,slideProperties,pageElements(objectId,shape(text(textElements(textRun(content)))))"
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().get(presentationId=presentation_id, fields=fields).execute(),
            account_id=account_id,
        )
        if not success:
            return f"[Error]: {result}"
        slides = result.get("slides", []) if isinstance(result, dict) else []
        return _dump_json(
            {
                "presentationId": result.get("presentationId") if isinstance(result, dict) else presentation_id,
                "title": result.get("title") if isinstance(result, dict) else None,
                "slides": _summarize_slides(slides, include_text=include_text),
            }
        )
    except Exception as e:
        logger.error("google_slides_list_slides failed", exc_info=True)
        return f"[Error]: Google Slides slide list failed: {e}"


@tool
def google_slides_get_page_thumbnail(
    presentation_id: str,
    page_object_id: str,
    thumbnail_size: str = "LARGE",
    mime_type: str = "PNG",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a temporary thumbnail URL for a Google Slides page."""
    user_id = get_user_id(config)
    try:
        presentation_id = _slides_id(presentation_id)
        page_object_id = _slides_id(page_object_id, label="page_object_id")
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().pages().getThumbnail(
                presentationId=presentation_id,
                pageObjectId=page_object_id,
                **{
                    "thumbnailProperties.thumbnailSize": thumbnail_size.strip().upper() or "LARGE",
                    "thumbnailProperties.mimeType": mime_type.strip().upper() or "PNG",
                },
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_slides_get_page_thumbnail failed", exc_info=True)
        return f"[Error]: Google Slides thumbnail lookup failed: {e}"


@tool
def google_slides_create_slide(
    presentation_id: str,
    insertion_index: int = -1,
    predefined_layout: str = "BLANK",
    object_id: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a slide in a Google Slides presentation."""
    user_id = get_user_id(config)
    try:
        presentation_id = _slides_id(presentation_id)
        create_slide: dict[str, Any] = {
            "slideLayoutReference": {
                "predefinedLayout": (predefined_layout.strip().upper() or "BLANK"),
            }
        }
        if insertion_index >= 0:
            create_slide["insertionIndex"] = int(insertion_index)
        if object_id.strip():
            create_slide["objectId"] = object_id.strip()
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().batchUpdate(
                presentationId=presentation_id,
                body={"requests": [{"createSlide": create_slide}]},
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_slides_create_slide failed", exc_info=True)
        return f"[Error]: Google Slides slide creation failed: {e}"


@tool
def google_slides_replace_text(
    presentation_id: str,
    contains_text: str,
    replace_text: str,
    match_case: bool = False,
    page_object_ids_json: str = "",
    required_revision_id: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Replace matching text across a Google Slides presentation or selected pages."""
    user_id = get_user_id(config)
    if not contains_text:
        return "[Error]: contains_text is required."
    try:
        presentation_id = _slides_id(presentation_id)
        request: dict[str, Any] = {
            "replaceAllText": {
                "containsText": {"text": contains_text, "matchCase": bool(match_case)},
                "replaceText": replace_text,
            }
        }
        page_ids = _page_ids(page_object_ids_json)
        if page_ids:
            request["replaceAllText"]["pageObjectIds"] = page_ids
        body: dict[str, Any] = {"requests": [request]}
        if required_revision_id.strip():
            body["writeControl"] = {"requiredRevisionId": required_revision_id.strip()}
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().batchUpdate(presentationId=presentation_id, body=body).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_slides_replace_text failed", exc_info=True)
        return f"[Error]: Google Slides text replacement failed: {e}"


@tool
def google_slides_batch_update(
    presentation_id: str,
    requests_json: str,
    required_revision_id: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a Google Slides batchUpdate request for advanced presentation edits."""
    user_id = get_user_id(config)
    try:
        presentation_id = _slides_id(presentation_id)
        requests = _parse_json(requests_json, expected=list, label="requests_json")
        body: dict[str, Any] = {"requests": requests}
        if required_revision_id.strip():
            body["writeControl"] = {"requiredRevisionId": required_revision_id.strip()}
        success, result = _slides_request(
            user_id,
            lambda s: s.presentations().batchUpdate(presentationId=presentation_id, body=body).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_slides_batch_update failed", exc_info=True)
        return f"[Error]: Google Slides batch update failed: {e}"


@tool
def google_chat_list_spaces(
    filter_query: str = "",
    page_size: int = 50,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Google Chat spaces visible to the authenticated account."""
    user_id = get_user_id(config)
    try:
        params: dict[str, Any] = {"pageSize": _limit(page_size, default=50, max_value=1000)}
        if filter_query.strip():
            params["filter"] = filter_query.strip()
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().list(**params).execute(),
            account_id=account_id,
        )
        return _dump_json(result.get("spaces", result) if success and isinstance(result, dict) else result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_list_spaces failed", exc_info=True)
        return f"[Error]: Google Chat space list failed: {e}"


@tool
def google_chat_get_space(
    space_name: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Google Chat space by resource name."""
    user_id = get_user_id(config)
    try:
        name = _chat_resource_name(space_name, label="space_name", prefix="spaces/")
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().get(name=name).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_get_space failed", exc_info=True)
        return f"[Error]: Google Chat space lookup failed: {e}"


@tool
def google_chat_list_members(
    space_name: str,
    page_size: int = 50,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List memberships in a Google Chat space."""
    user_id = get_user_id(config)
    try:
        parent = _chat_resource_name(space_name, label="space_name", prefix="spaces/")
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().members().list(
                parent=parent,
                pageSize=_limit(page_size, default=50, max_value=1000),
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result.get("memberships", result) if success and isinstance(result, dict) else result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_list_members failed", exc_info=True)
        return f"[Error]: Google Chat member list failed: {e}"


@tool
def google_chat_get_member(
    member_name: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Google Chat membership by resource name."""
    user_id = get_user_id(config)
    if not member_name.strip():
        return "[Error]: member_name is required, e.g. spaces/AAA/members/BBB."
    try:
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().members().get(name=member_name.strip()).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_get_member failed", exc_info=True)
        return f"[Error]: Google Chat member lookup failed: {e}"


@tool
def google_chat_list_messages(
    space_name: str,
    page_size: int = 50,
    filter_query: str = "",
    order_by: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List recent Google Chat messages in a space."""
    user_id = get_user_id(config)
    try:
        parent = _chat_resource_name(space_name, label="space_name", prefix="spaces/")
        params: dict[str, Any] = {
            "parent": parent,
            "pageSize": _limit(page_size, default=50, max_value=1000),
        }
        if filter_query.strip():
            params["filter"] = filter_query.strip()
        if order_by.strip():
            params["orderBy"] = order_by.strip()
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().messages().list(**params).execute(),
            account_id=account_id,
        )
        return _dump_json(result.get("messages", result) if success and isinstance(result, dict) else result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_list_messages failed", exc_info=True)
        return f"[Error]: Google Chat message list failed: {e}"


@tool
def google_chat_get_message(
    message_name: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Google Chat message by resource name."""
    user_id = get_user_id(config)
    if not message_name.strip():
        return "[Error]: message_name is required, e.g. spaces/AAA/messages/BBB."
    try:
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().messages().get(name=message_name.strip()).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_get_message failed", exc_info=True)
        return f"[Error]: Google Chat message lookup failed: {e}"


@tool
def google_chat_send_message(
    space_name: str,
    text: str = "",
    message_json: str = "",
    thread_key: str = "",
    request_id: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send a Google Chat message to a space."""
    user_id = get_user_id(config)
    try:
        parent = _chat_resource_name(space_name, label="space_name", prefix="spaces/")
        body = _chat_message_body(text=text, message_json=message_json)
        kwargs: dict[str, Any] = {"parent": parent, "body": body}
        if thread_key.strip():
            kwargs["threadKey"] = thread_key.strip()
        if request_id.strip():
            kwargs["requestId"] = request_id.strip()
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().messages().create(**kwargs).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_send_message failed", exc_info=True)
        return f"[Error]: Google Chat message send failed: {e}"


@tool
def google_chat_update_message(
    message_name: str,
    text: str = "",
    message_json: str = "",
    update_mask: str = "",
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Google Chat message."""
    user_id = get_user_id(config)
    if not message_name.strip():
        return "[Error]: message_name is required, e.g. spaces/AAA/messages/BBB."
    try:
        body = _chat_message_body(text=text, message_json=message_json, label="message_json")
        mask = update_mask.strip()
        if not mask:
            mask = ",".join(key for key in ("text", "cardsV2", "cards") if key in body) or "text"
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().messages().patch(
                name=message_name.strip(),
                updateMask=mask,
                body=body,
            ).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_update_message failed", exc_info=True)
        return f"[Error]: Google Chat message update failed: {e}"


@tool
def google_chat_delete_message(
    message_name: str,
    account_id: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete a Google Chat message."""
    user_id = get_user_id(config)
    if not message_name.strip():
        return "[Error]: message_name is required, e.g. spaces/AAA/messages/BBB."
    try:
        success, result = _chat_request(
            user_id,
            lambda s: s.spaces().messages().delete(name=message_name.strip()).execute(),
            account_id=account_id,
        )
        return _dump_json(result) if success else f"[Error]: {result}"
    except Exception as e:
        logger.error("google_chat_delete_message failed", exc_info=True)
        return f"[Error]: Google Chat message deletion failed: {e}"


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
    google_slides_create_presentation,
    google_slides_get_presentation,
    google_slides_list_slides,
    google_slides_get_page_thumbnail,
    google_slides_create_slide,
    google_slides_replace_text,
    google_slides_batch_update,
    google_chat_list_spaces,
    google_chat_get_space,
    google_chat_list_members,
    google_chat_get_member,
    google_chat_list_messages,
    google_chat_get_message,
    google_chat_send_message,
    google_chat_update_message,
    google_chat_delete_message,
]
