"""Cloud file storage service integration tools."""

from __future__ import annotations

import base64
import json
import logging
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Optional
from urllib.parse import quote

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .service_integration_base import (
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 70_000
_MAX_TEXT_DOWNLOAD_BYTES = 1_000_000
_DROPBOX_API_BASE_URL = "https://api.dropboxapi.com/2"
_DROPBOX_CONTENT_BASE_URL = "https://content.dropboxapi.com/2"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 50, max_value: int = 1000) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _path(value: str, *, allow_root: bool = False) -> str:
    clean = value.strip()
    if allow_root and clean in {"", "/"}:
        return ""
    if not clean.startswith("/"):
        clean = f"/{clean}"
    return clean


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "path", "force_path_style"}


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    data: Any = None,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[httpx.Auth] = None,
) -> Any:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params),
                json=json_body,
                data=data,
                headers=headers,
                auth=auth,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except ValueError:
                return {"status": "ok", "status_code": response.status_code, "text": response.text[:2000]}
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            if isinstance(body, dict):
                detail = (
                    body.get("error_summary")
                    or body.get("message")
                    or body.get("error")
                    or body.get("error_description")
                    or body.get("detail")
                    or ""
                )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _request_text(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[httpx.Auth] = None,
    content: str | bytes | None = None,
    params: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, str]]:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params),
                headers=headers,
                auth=auth,
                content=content,
            )
            response.raise_for_status()
            return response.text, dict(response.headers)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"HTTP {e.response.status_code}: {e.response.text[:300]}".strip()) from e


def _request_bytes(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[httpx.Auth] = None,
) -> tuple[bytes, dict[str, str]]:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(method, url, headers=headers, auth=auth)
            response.raise_for_status()
            return response.content, dict(response.headers)
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"HTTP {e.response.status_code}: {e.response.text[:300]}".strip()) from e


def _dropbox_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, str, dict[str, str] | str]:
    api_base = (
        _credential_value(
            provider="dropbox",
            provider_aliases=("dropbox_api", "dropbox_oauth2", "dropbox_oauth2_api"),
            field_names=("api_base_url", "apiBaseUrl", "base_url", "baseUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("dropbox_api_base_url")
        or _DROPBOX_API_BASE_URL
    )
    content_base = (
        _credential_value(
            provider="dropbox",
            provider_aliases=("dropbox_api", "dropbox_oauth2", "dropbox_oauth2_api"),
            field_names=("content_base_url", "contentBaseUrl", "content_url", "contentUrl"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("dropbox_content_base_url")
        or _DROPBOX_CONTENT_BASE_URL
    )
    token = _credential_value(
        provider="dropbox",
        provider_aliases=("dropbox_api", "dropbox_oauth2", "dropbox_oauth2_api"),
        field_names=("access_token", "accessToken", "token", "api_key", "apiKey", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("dropbox_access_token")
    if not token:
        return _base_url(api_base), _base_url(content_base), _setup_hint(
            provider="dropbox",
            field_names=("access_token", "token", "value"),
            tool_name=tool_name,
            env_var="DROPBOX_ACCESS_TOKEN",
            display_name="Dropbox",
        )
    return _base_url(api_base), _base_url(content_base), {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _nextcloud_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, httpx.Auth | None, dict[str, str], str | None]:
    webdav_url = (
        _credential_value(
            provider="nextcloud",
            provider_aliases=("next_cloud", "nextcloud_api", "nextCloudApi", "nextcloud_oauth2"),
            field_names=("webdav_url", "webDavUrl", "web_dav_url", "base_url", "baseUrl", "url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("nextcloud_webdav_url")
    )
    username = _credential_value(
        provider="nextcloud",
        provider_aliases=("next_cloud", "nextcloud_api", "nextCloudApi", "nextcloud_oauth2"),
        field_names=("username", "user", "login"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("nextcloud_username")
    password = _credential_value(
        provider="nextcloud",
        provider_aliases=("next_cloud", "nextcloud_api", "nextCloudApi", "nextcloud_oauth2"),
        field_names=("password", "app_password", "appPassword"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("nextcloud_password")
    access_token = _credential_value(
        provider="nextcloud",
        provider_aliases=("next_cloud", "nextcloud_api", "nextCloudApi", "nextcloud_oauth2"),
        field_names=("access_token", "accessToken", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("nextcloud_access_token")
    if not webdav_url:
        return "", None, {}, "[Error]: No Nextcloud WebDAV URL found. Save a Nextcloud credential with webdav_url, or set NEXTCLOUD_WEBDAV_URL."
    headers = {"Accept": "application/json", "User-Agent": "Nymeria"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        return _base_url(webdav_url), None, headers, None
    if username and password:
        return _base_url(webdav_url), httpx.BasicAuth(username, password), headers, None
    return _base_url(webdav_url), None, headers, _setup_hint(
        provider="nextcloud",
        field_names=("username", "password", "access_token"),
        tool_name=tool_name,
        env_var="NEXTCLOUD_USERNAME + NEXTCLOUD_PASSWORD or NEXTCLOUD_ACCESS_TOKEN",
        display_name="Nextcloud",
    )


def _nextcloud_base_from_webdav(webdav_url: str) -> str:
    for marker in ("/remote.php/webdav", "/remote.php/dav/files/"):
        if marker in webdav_url:
            return webdav_url.split(marker, 1)[0].rstrip("/")
    return webdav_url.rstrip("/")


def _nextcloud_webdav_url(webdav_url: str, path: str) -> str:
    clean = _path(path, allow_root=True)
    if not clean:
        return f"{webdav_url.rstrip('/')}/"
    return f"{webdav_url.rstrip('/')}/{quote(clean.strip('/'), safe='/')}"


def _nextcloud_ocs_url(webdav_url: str, endpoint: str) -> str:
    return f"{_nextcloud_base_from_webdav(webdav_url)}/{endpoint.lstrip('/')}"


def _parse_webdav_propfind(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    ns = {"d": "DAV:"}
    items: list[dict[str, Any]] = []
    for response in root.findall("d:response", ns):
        href = response.findtext("d:href", default="", namespaces=ns)
        prop = response.find("d:propstat/d:prop", ns)
        if prop is None:
            continue
        resource_type = prop.find("d:resourcetype", ns)
        is_folder = resource_type is not None and resource_type.find("d:collection", ns) is not None
        items.append(
            {
                "href": href,
                "name": prop.findtext("d:displayname", default="", namespaces=ns),
                "type": "folder" if is_folder else "file",
                "content_length": prop.findtext("d:getcontentlength", default="", namespaces=ns),
                "content_type": prop.findtext("d:getcontenttype", default="", namespaces=ns),
                "etag": prop.findtext("d:getetag", default="", namespaces=ns),
                "last_modified": prop.findtext("d:getlastmodified", default="", namespaces=ns),
            }
        )
    return items


def _preview_bytes(content: bytes, headers: dict[str, str], *, max_bytes: int) -> dict[str, Any]:
    limited = content[:max_bytes]
    result: dict[str, Any] = {
        "content_type": headers.get("content-type"),
        "content_length": len(content),
        "truncated": len(content) > len(limited),
    }
    try:
        result["text"] = limited.decode("utf-8")
    except UnicodeDecodeError:
        result["base64"] = base64.b64encode(limited).decode("ascii")
        result["encoding"] = "base64"
    return result


def _s3_client(tool_name: str, config: Optional[RunnableConfig]) -> tuple[Any, str | None]:
    access_key = _credential_value(
        provider="s3",
        provider_aliases=("aws", "aws_s3", "s3_api"),
        field_names=("access_key_id", "accessKeyId", "aws_access_key_id"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_access_key_id")
    secret_key = _credential_value(
        provider="s3",
        provider_aliases=("aws", "aws_s3", "s3_api"),
        field_names=("secret_access_key", "secretAccessKey", "aws_secret_access_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_secret_access_key")
    session_token = _credential_value(
        provider="s3",
        provider_aliases=("aws", "aws_s3", "s3_api"),
        field_names=("session_token", "sessionToken", "aws_session_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_session_token")
    region = _credential_value(
        provider="s3",
        provider_aliases=("aws", "aws_s3", "s3_api"),
        field_names=("region", "aws_region"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_region")
    endpoint_url = _credential_value(
        provider="s3",
        provider_aliases=("aws", "aws_s3", "s3_api"),
        field_names=("endpoint_url", "endpointUrl", "endpoint", "base_url", "baseUrl"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_endpoint_url")
    force_path_style = _credential_value(
        provider="s3",
        provider_aliases=("aws", "aws_s3", "s3_api"),
        field_names=("force_path_style", "forcePathStyle", "addressing_style"),
        tool_name=tool_name,
        config=config,
    )
    if force_path_style is None:
        force_path_style = _settings_value("s3_force_path_style")
    if not access_key or not secret_key:
        return None, _setup_hint(
            provider="s3",
            field_names=("access_key_id", "secret_access_key"),
            tool_name=tool_name,
            env_var="AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY",
            display_name="S3",
        )

    import boto3
    from botocore.config import Config

    client_kwargs: dict[str, Any] = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region or "us-east-1",
    }
    if session_token:
        client_kwargs["aws_session_token"] = session_token
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url
    if _bool_value(force_path_style):
        client_kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **client_kwargs), None


def _s3_error(prefix: str, exc: Exception) -> str:
    return f"[Error]: {prefix}: {exc}"


@tool
def dropbox_get_current_account(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Get Dropbox account metadata for the saved token."""
    try:
        api_base, _, headers = _dropbox_config("dropbox_get_current_account", config)
        if isinstance(headers, str):
            return headers
        return _dump_json(_request_json("POST", f"{api_base}/users/get_current_account", headers=headers))
    except Exception as e:
        logger.error("dropbox_get_current_account failed", exc_info=True)
        return f"[Error]: Dropbox account lookup failed: {e}"


@tool
def dropbox_get_metadata(path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Get Dropbox metadata for a file or folder path.

    Args:
        path: Dropbox file or folder path.
    """
    try:
        api_base, _, headers = _dropbox_config("dropbox_get_metadata", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{api_base}/files/get_metadata", json_body={"path": _path(path)}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("dropbox_get_metadata failed", exc_info=True)
        return f"[Error]: Dropbox metadata lookup failed: {e}"


@tool
def dropbox_list_folder(
    path: str = "",
    recursive: bool = False,
    include_deleted: bool = False,
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Dropbox folder entries.

    Args:
        path: Folder path. Empty string or "/" lists the Dropbox root.
        recursive: Whether to recurse into child folders.
        include_deleted: Whether to include deleted entries.
        limit: Number of entries to return, 1-1000.
    """
    try:
        api_base, _, headers = _dropbox_config("dropbox_list_folder", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "POST",
            f"{api_base}/files/list_folder",
            json_body={
                "path": _path(path, allow_root=True),
                "recursive": bool(recursive),
                "include_deleted": bool(include_deleted),
                "limit": _limit(limit, max_value=1000),
            },
            headers=headers,
        )
        return _dump_json(data.get("entries", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("dropbox_list_folder failed", exc_info=True)
        return f"[Error]: Dropbox folder list failed: {e}"


@tool
def dropbox_search(
    query: str,
    path: str = "",
    filename_only: bool = True,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search Dropbox files and folders.

    Args:
        query: Search text.
        path: Optional folder path to search under.
        filename_only: Whether to search names only.
        limit: Number of matches to return, 1-1000.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        api_base, _, headers = _dropbox_config("dropbox_search", config)
        if isinstance(headers, str):
            return headers
        options: dict[str, Any] = {"filename_only": bool(filename_only), "max_results": _limit(limit, default=25, max_value=1000)}
        clean_path = _path(path, allow_root=True)
        if clean_path:
            options["path"] = clean_path
        data = _request_json("POST", f"{api_base}/files/search_v2", json_body={"query": query.strip(), "options": options}, headers=headers)
        matches = data.get("matches", data) if isinstance(data, dict) else data
        return _dump_json(matches)
    except Exception as e:
        logger.error("dropbox_search failed", exc_info=True)
        return f"[Error]: Dropbox search failed: {e}"


@tool
def dropbox_download_file(
    path: str,
    max_bytes: int = 200_000,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Download a Dropbox file and return a text/base64 preview.

    Args:
        path: Dropbox file path.
        max_bytes: Maximum response bytes to include, 1-1000000.
    """
    try:
        _, content_base, headers = _dropbox_config("dropbox_download_file", config)
        if isinstance(headers, str):
            return headers
        request_headers = dict(headers)
        request_headers["Dropbox-API-Arg"] = json.dumps({"path": _path(path)})
        request_headers.pop("Content-Type", None)
        content, response_headers = _request_bytes("POST", f"{content_base}/files/download", headers=request_headers)
        return _dump_json(_preview_bytes(content, response_headers, max_bytes=_limit(max_bytes, default=200_000, max_value=_MAX_TEXT_DOWNLOAD_BYTES)))
    except Exception as e:
        logger.error("dropbox_download_file failed", exc_info=True)
        return f"[Error]: Dropbox file download failed: {e}"


@tool
def dropbox_upload_text_file(
    path: str,
    content: str,
    mode: str = "overwrite",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload text content to a Dropbox file path.

    Args:
        path: Destination Dropbox path.
        content: Text content to upload.
        mode: Upload mode, "add" or "overwrite".
    """
    if mode not in {"add", "overwrite"}:
        return '[Error]: mode must be "add" or "overwrite".'
    try:
        _, content_base, headers = _dropbox_config("dropbox_upload_text_file", config)
        if isinstance(headers, str):
            return headers
        request_headers = dict(headers)
        request_headers["Content-Type"] = "application/octet-stream"
        request_headers["Dropbox-API-Arg"] = json.dumps({"path": _path(path), "mode": mode})
        data = _request_json("POST", f"{content_base}/files/upload", data=content.encode("utf-8"), headers=request_headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("dropbox_upload_text_file failed", exc_info=True)
        return f"[Error]: Dropbox file upload failed: {e}"


@tool
def dropbox_create_folder(path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Create a Dropbox folder.

    Args:
        path: Folder path to create.
    """
    try:
        api_base, _, headers = _dropbox_config("dropbox_create_folder", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{api_base}/files/create_folder_v2", json_body={"path": _path(path)}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("dropbox_create_folder failed", exc_info=True)
        return f"[Error]: Dropbox folder creation failed: {e}"


@tool
def dropbox_copy_path(from_path: str, to_path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Copy a Dropbox file or folder.

    Args:
        from_path: Source path.
        to_path: Destination path.
    """
    try:
        api_base, _, headers = _dropbox_config("dropbox_copy_path", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{api_base}/files/copy_v2", json_body={"from_path": _path(from_path), "to_path": _path(to_path)}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("dropbox_copy_path failed", exc_info=True)
        return f"[Error]: Dropbox copy failed: {e}"


@tool
def dropbox_move_path(from_path: str, to_path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Move or rename a Dropbox file or folder.

    Args:
        from_path: Source path.
        to_path: Destination path.
    """
    try:
        api_base, _, headers = _dropbox_config("dropbox_move_path", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{api_base}/files/move_v2", json_body={"from_path": _path(from_path), "to_path": _path(to_path)}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("dropbox_move_path failed", exc_info=True)
        return f"[Error]: Dropbox move failed: {e}"


@tool
def dropbox_delete_path(path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Delete a Dropbox file or folder.

    Args:
        path: File or folder path to delete.
    """
    try:
        api_base, _, headers = _dropbox_config("dropbox_delete_path", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{api_base}/files/delete_v2", json_body={"path": _path(path)}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("dropbox_delete_path failed", exc_info=True)
        return f"[Error]: Dropbox delete failed: {e}"


@tool
def nextcloud_list_folder(
    path: str = "/",
    depth: int = 1,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Nextcloud folder contents using WebDAV.

    Args:
        path: Folder path.
        depth: WebDAV depth, usually 0 or 1.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_list_folder", config)
        if error:
            return error
        request_headers = dict(headers)
        request_headers["Depth"] = str(max(0, min(1, int(depth))))
        request_headers["Content-Type"] = "application/xml"
        xml_text, _ = _request_text("PROPFIND", _nextcloud_webdav_url(webdav_url, path), headers=request_headers, auth=auth)
        return _dump_json(_parse_webdav_propfind(xml_text))
    except Exception as e:
        logger.error("nextcloud_list_folder failed", exc_info=True)
        return f"[Error]: Nextcloud folder list failed: {e}"


@tool
def nextcloud_download_file(
    path: str,
    max_bytes: int = 200_000,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Download a Nextcloud file and return a text/base64 preview.

    Args:
        path: File path.
        max_bytes: Maximum response bytes to include, 1-1000000.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_download_file", config)
        if error:
            return error
        content, response_headers = _request_bytes("GET", _nextcloud_webdav_url(webdav_url, path), headers=headers, auth=auth)
        return _dump_json(_preview_bytes(content, response_headers, max_bytes=_limit(max_bytes, default=200_000, max_value=_MAX_TEXT_DOWNLOAD_BYTES)))
    except Exception as e:
        logger.error("nextcloud_download_file failed", exc_info=True)
        return f"[Error]: Nextcloud file download failed: {e}"


@tool
def nextcloud_upload_text_file(
    path: str,
    content: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload text content to a Nextcloud file path.

    Args:
        path: Destination file path.
        content: Text content to upload.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_upload_text_file", config)
        if error:
            return error
        request_headers = dict(headers)
        request_headers["Content-Type"] = "text/plain; charset=utf-8"
        text, response_headers = _request_text("PUT", _nextcloud_webdav_url(webdav_url, path), content=content, headers=request_headers, auth=auth)
        return _dump_json({"status": "ok", "response": text[:1000], "etag": response_headers.get("etag")})
    except Exception as e:
        logger.error("nextcloud_upload_text_file failed", exc_info=True)
        return f"[Error]: Nextcloud file upload failed: {e}"


@tool
def nextcloud_create_folder(path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Create a Nextcloud folder using WebDAV.

    Args:
        path: Folder path to create.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_create_folder", config)
        if error:
            return error
        text, _ = _request_text("MKCOL", _nextcloud_webdav_url(webdav_url, path), headers=headers, auth=auth)
        return _dump_json({"status": "ok", "response": text[:1000]})
    except Exception as e:
        logger.error("nextcloud_create_folder failed", exc_info=True)
        return f"[Error]: Nextcloud folder creation failed: {e}"


@tool
def nextcloud_copy_path(from_path: str, to_path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Copy a Nextcloud file or folder.

    Args:
        from_path: Source path.
        to_path: Destination path.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_copy_path", config)
        if error:
            return error
        request_headers = dict(headers)
        request_headers["Destination"] = _nextcloud_webdav_url(webdav_url, to_path)
        text, _ = _request_text("COPY", _nextcloud_webdav_url(webdav_url, from_path), headers=request_headers, auth=auth)
        return _dump_json({"status": "ok", "response": text[:1000]})
    except Exception as e:
        logger.error("nextcloud_copy_path failed", exc_info=True)
        return f"[Error]: Nextcloud copy failed: {e}"


@tool
def nextcloud_move_path(from_path: str, to_path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Move or rename a Nextcloud file or folder.

    Args:
        from_path: Source path.
        to_path: Destination path.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_move_path", config)
        if error:
            return error
        request_headers = dict(headers)
        request_headers["Destination"] = _nextcloud_webdav_url(webdav_url, to_path)
        text, _ = _request_text("MOVE", _nextcloud_webdav_url(webdav_url, from_path), headers=request_headers, auth=auth)
        return _dump_json({"status": "ok", "response": text[:1000]})
    except Exception as e:
        logger.error("nextcloud_move_path failed", exc_info=True)
        return f"[Error]: Nextcloud move failed: {e}"


@tool
def nextcloud_delete_path(path: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Delete a Nextcloud file or folder.

    Args:
        path: File or folder path to delete.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_delete_path", config)
        if error:
            return error
        text, _ = _request_text("DELETE", _nextcloud_webdav_url(webdav_url, path), headers=headers, auth=auth)
        return _dump_json({"status": "ok", "response": text[:1000]})
    except Exception as e:
        logger.error("nextcloud_delete_path failed", exc_info=True)
        return f"[Error]: Nextcloud delete failed: {e}"


@tool
def nextcloud_list_users(search: str = "", limit: int = 50, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """List Nextcloud users through the OCS API.

    Args:
        search: Optional user search string.
        limit: Number of users to return, 1-500.
    """
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_list_users", config)
        if error:
            return error
        request_headers = dict(headers)
        request_headers["OCS-APIRequest"] = "true"
        data = _request_json(
            "GET",
            _nextcloud_ocs_url(webdav_url, "/ocs/v1.php/cloud/users"),
            params={"format": "json", "search": search, "limit": _limit(limit, max_value=500)},
            headers=request_headers,
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("nextcloud_list_users failed", exc_info=True)
        return f"[Error]: Nextcloud user list failed: {e}"


@tool
def nextcloud_get_user(user_id: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Get one Nextcloud user through the OCS API.

    Args:
        user_id: Nextcloud user ID.
    """
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        webdav_url, auth, headers, error = _nextcloud_config("nextcloud_get_user", config)
        if error:
            return error
        request_headers = dict(headers)
        request_headers["OCS-APIRequest"] = "true"
        data = _request_json(
            "GET",
            _nextcloud_ocs_url(webdav_url, f"/ocs/v1.php/cloud/users/{quote(user_id.strip(), safe='')}"),
            params={"format": "json"},
            headers=request_headers,
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("nextcloud_get_user failed", exc_info=True)
        return f"[Error]: Nextcloud user lookup failed: {e}"


@tool
def s3_list_buckets(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """List S3 buckets."""
    try:
        client, error = _s3_client("s3_list_buckets", config)
        if error:
            return error
        return _dump_json(client.list_buckets().get("Buckets", []))
    except Exception as e:
        logger.error("s3_list_buckets failed", exc_info=True)
        return _s3_error("S3 bucket list failed", e)


@tool
def s3_list_objects(
    bucket: str,
    prefix: str = "",
    delimiter: str = "",
    limit: int = 100,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List S3 objects in a bucket.

    Args:
        bucket: Bucket name.
        prefix: Optional key prefix.
        delimiter: Optional delimiter, usually "/".
        limit: Number of objects to return, 1-1000.
    """
    try:
        client, error = _s3_client("s3_list_objects", config)
        if error:
            return error
        data = client.list_objects_v2(Bucket=bucket, Prefix=prefix or "", Delimiter=delimiter or "", MaxKeys=_limit(limit, max_value=1000))
        return _dump_json({"objects": data.get("Contents", []), "common_prefixes": data.get("CommonPrefixes", [])})
    except Exception as e:
        logger.error("s3_list_objects failed", exc_info=True)
        return _s3_error("S3 object list failed", e)


@tool
def s3_get_object_text(
    bucket: str,
    key: str,
    max_bytes: int = 200_000,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an S3 object and return a text/base64 preview.

    Args:
        bucket: Bucket name.
        key: Object key.
        max_bytes: Maximum response bytes to include, 1-1000000.
    """
    try:
        client, error = _s3_client("s3_get_object_text", config)
        if error:
            return error
        response = client.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()
        headers = {"content-type": response.get("ContentType")}
        return _dump_json(_preview_bytes(content, headers, max_bytes=_limit(max_bytes, default=200_000, max_value=_MAX_TEXT_DOWNLOAD_BYTES)))
    except Exception as e:
        logger.error("s3_get_object_text failed", exc_info=True)
        return _s3_error("S3 object download failed", e)


@tool
def s3_upload_text_object(
    bucket: str,
    key: str,
    content: str,
    content_type: str = "text/plain; charset=utf-8",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Upload text content to an S3 object.

    Args:
        bucket: Bucket name.
        key: Object key.
        content: Text content to upload.
        content_type: Object content type.
    """
    try:
        client, error = _s3_client("s3_upload_text_object", config)
        if error:
            return error
        data = client.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"), ContentType=content_type)
        return _dump_json({"status": "ok", "etag": data.get("ETag"), "version_id": data.get("VersionId")})
    except Exception as e:
        logger.error("s3_upload_text_object failed", exc_info=True)
        return _s3_error("S3 object upload failed", e)


@tool
def s3_copy_object(
    source_bucket: str,
    source_key: str,
    destination_bucket: str,
    destination_key: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Copy an S3 object.

    Args:
        source_bucket: Source bucket name.
        source_key: Source object key.
        destination_bucket: Destination bucket name.
        destination_key: Destination object key.
    """
    try:
        client, error = _s3_client("s3_copy_object", config)
        if error:
            return error
        data = client.copy_object(
            Bucket=destination_bucket,
            Key=destination_key,
            CopySource={"Bucket": source_bucket, "Key": source_key},
        )
        return _dump_json({"status": "ok", "copy_object_result": data.get("CopyObjectResult")})
    except Exception as e:
        logger.error("s3_copy_object failed", exc_info=True)
        return _s3_error("S3 object copy failed", e)


@tool
def s3_delete_object(bucket: str, key: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Delete an S3 object.

    Args:
        bucket: Bucket name.
        key: Object key.
    """
    try:
        client, error = _s3_client("s3_delete_object", config)
        if error:
            return error
        data = client.delete_object(Bucket=bucket, Key=key)
        return _dump_json({"status": "ok", "delete_marker": data.get("DeleteMarker"), "version_id": data.get("VersionId")})
    except Exception as e:
        logger.error("s3_delete_object failed", exc_info=True)
        return _s3_error("S3 object delete failed", e)


@tool
def s3_create_folder(bucket: str, folder_key: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Create an S3 folder marker object.

    Args:
        bucket: Bucket name.
        folder_key: Folder key. A trailing slash is added when missing.
    """
    key = folder_key.strip()
    if not key:
        return "[Error]: folder_key is required."
    if not key.endswith("/"):
        key += "/"
    try:
        client, error = _s3_client("s3_create_folder", config)
        if error:
            return error
        data = client.put_object(Bucket=bucket, Key=key, Body=b"")
        return _dump_json({"status": "ok", "key": key, "etag": data.get("ETag")})
    except Exception as e:
        logger.error("s3_create_folder failed", exc_info=True)
        return _s3_error("S3 folder creation failed", e)


@tool
def s3_create_bucket(bucket: str, region: str = "", config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Create an S3 bucket.

    Args:
        bucket: Bucket name.
        region: Optional bucket region. Defaults to the configured client region.
    """
    try:
        client, error = _s3_client("s3_create_bucket", config)
        if error:
            return error
        kwargs: dict[str, Any] = {"Bucket": bucket}
        chosen_region = region.strip()
        if chosen_region and chosen_region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": chosen_region}
        data = client.create_bucket(**kwargs)
        return _dump_json({"status": "ok", "location": data.get("Location")})
    except Exception as e:
        logger.error("s3_create_bucket failed", exc_info=True)
        return _s3_error("S3 bucket creation failed", e)


@tool
def s3_delete_bucket(bucket: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Delete an empty S3 bucket.

    Args:
        bucket: Bucket name.
    """
    try:
        client, error = _s3_client("s3_delete_bucket", config)
        if error:
            return error
        client.delete_bucket(Bucket=bucket)
        return _dump_json({"status": "ok", "bucket": bucket})
    except Exception as e:
        logger.error("s3_delete_bucket failed", exc_info=True)
        return _s3_error("S3 bucket delete failed", e)


FILE_STORAGE_SERVICE_TOOLS = [
    dropbox_get_current_account,
    dropbox_get_metadata,
    dropbox_list_folder,
    dropbox_search,
    dropbox_download_file,
    dropbox_upload_text_file,
    dropbox_create_folder,
    dropbox_copy_path,
    dropbox_move_path,
    dropbox_delete_path,
    nextcloud_list_folder,
    nextcloud_download_file,
    nextcloud_upload_text_file,
    nextcloud_create_folder,
    nextcloud_copy_path,
    nextcloud_move_path,
    nextcloud_delete_path,
    nextcloud_list_users,
    nextcloud_get_user,
    s3_list_buckets,
    s3_list_objects,
    s3_get_object_text,
    s3_upload_text_object,
    s3_copy_object,
    s3_delete_object,
    s3_create_folder,
    s3_create_bucket,
    s3_delete_bucket,
]
