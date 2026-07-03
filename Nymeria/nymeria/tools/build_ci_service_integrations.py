"""Build and CI service integration tools."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .service_integration_base import (
    BASE_URL_ALIAS_FIELDS,
    USERNAME_FIELDS,
    base_url as _base_url,
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    filtered as _filtered,
    json_object as _json_object,
    settings_value as _settings_value,
    setup_hint as _setup_hint,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 70_000
_CIRCLECI_BASE_URL = "https://circleci.com/api/v2"
_TRAVISCI_BASE_URL = "https://api.travis-ci.com"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value / _setup_hint arguments from the specs; field-name
# tuple ORDER is behaviorally significant and must not be reordered.
_CIRCLECI = register_provider_spec(
    ProviderCredentialSpec(
        provider="circleci",
        aliases=("circle_ci", "circleci_api", "circleCiApi"),
        groups=(
            CredentialFieldGroup(role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False),
            CredentialFieldGroup(
                role="token",
                names=("api_key", "apiKey", "api_token", "apiToken", "token", "value"),
            ),
        ),
        hint_fields=("api_key", "api_token", "token", "value"),
        env_var="CIRCLECI_API_TOKEN",
        display_name="CircleCI",
    )
)

_TRAVISCI = register_provider_spec(
    ProviderCredentialSpec(
        provider="travisci",
        aliases=("travis_ci", "travisci_api", "travisCiApi"),
        groups=(
            CredentialFieldGroup(role="base_url", names=BASE_URL_ALIAS_FIELDS, required=False),
            CredentialFieldGroup(
                role="token",
                names=("api_token", "apiToken", "access_token", "accessToken", "token", "value"),
            ),
        ),
        hint_fields=("api_token", "access_token", "token", "value"),
        env_var="TRAVISCI_API_TOKEN",
        display_name="Travis CI",
    )
)

# Branch-variant provider: setup hints differ by which credential is missing
# (username vs token), each with its own env_var. The spec's hint_fields holds
# the token variant; the username hint reuses the username group and keeps its
# own env_var inline.
_JENKINS = register_provider_spec(
    ProviderCredentialSpec(
        provider="jenkins",
        aliases=("jenkins_api", "jenkinsApi"),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "baseUrl", "url", "host"), required=False
            ),
            CredentialFieldGroup(role="username", names=USERNAME_FIELDS),
            CredentialFieldGroup(
                role="token",
                names=("api_key", "apiKey", "api_token", "apiToken", "token", "password", "value"),
            ),
        ),
        hint_fields=("api_key", "api_token", "token", "password", "value"),
        env_var="JENKINS_API_TOKEN",
        display_name="Jenkins",
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _limit(value: int, *, default: int = 25, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    form_data: Optional[dict[str, Any]] = None,
    content: str | bytes | None = None,
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
                data=_filtered(form_data) if form_data is not None else None,
                content=content,
                headers=headers,
                auth=auth,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except ValueError:
                return {
                    "status": "ok",
                    "status_code": response.status_code,
                    "location": response.headers.get("location"),
                    "text": response.text[:1000],
                }
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            if isinstance(body, dict):
                detail = (
                    body.get("message")
                    or body.get("error")
                    or body.get("error_description")
                    or body.get("detail")
                    or ""
                )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _circleci_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_CIRCLECI.provider,
            provider_aliases=_CIRCLECI.aliases,
            field_names=_CIRCLECI.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("circleci_base_url")
        or _CIRCLECI_BASE_URL
    )
    token = _credential_value(
        provider=_CIRCLECI.provider,
        provider_aliases=_CIRCLECI.aliases,
        field_names=_CIRCLECI.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("circleci_api_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_CIRCLECI.provider,
            field_names=_CIRCLECI.hint_fields,
            tool_name=tool_name,
            env_var=_CIRCLECI.env_var,
            display_name=_CIRCLECI.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Circle-Token": token,
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _travisci_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    base = (
        _credential_value(
            provider=_TRAVISCI.provider,
            provider_aliases=_TRAVISCI.aliases,
            field_names=_TRAVISCI.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("travisci_base_url")
        or _TRAVISCI_BASE_URL
    )
    token = _credential_value(
        provider=_TRAVISCI.provider,
        provider_aliases=_TRAVISCI.aliases,
        field_names=_TRAVISCI.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("travisci_api_token")
    if not token:
        return _base_url(base), _setup_hint(
            provider=_TRAVISCI.provider,
            field_names=_TRAVISCI.hint_fields,
            tool_name=tool_name,
            env_var=_TRAVISCI.env_var,
            display_name=_TRAVISCI.display_name,
        )
    return _base_url(base), {
        "Accept": "application/json",
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
        "Travis-API-Version": "3",
        "User-Agent": "Nymeria",
    }


def _jenkins_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, httpx.BasicAuth | None, str | None]:
    base = (
        _credential_value(
            provider=_JENKINS.provider,
            provider_aliases=_JENKINS.aliases,
            field_names=_JENKINS.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("jenkins_base_url")
    )
    username = _credential_value(
        provider=_JENKINS.provider,
        provider_aliases=_JENKINS.aliases,
        field_names=_JENKINS.group("username"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jenkins_username")
    api_token = _credential_value(
        provider=_JENKINS.provider,
        provider_aliases=_JENKINS.aliases,
        field_names=_JENKINS.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("jenkins_api_token")
    if not base:
        return "", None, "[Error]: No Jenkins base URL found. Save a Jenkins credential with base_url, or set JENKINS_BASE_URL."
    if not username:
        return _base_url(base), None, _setup_hint(
            provider=_JENKINS.provider,
            field_names=_JENKINS.group("username"),
            tool_name=tool_name,
            env_var="JENKINS_USERNAME",
            display_name=_JENKINS.display_name,
        )
    if not api_token:
        return _base_url(base), None, _setup_hint(
            provider=_JENKINS.provider,
            field_names=_JENKINS.hint_fields,
            tool_name=tool_name,
            env_var=_JENKINS.env_var,
            display_name=_JENKINS.display_name,
        )
    return _base_url(base), httpx.BasicAuth(username, api_token), None


def _circleci_project_path(vcs: str, project_slug: str) -> str:
    clean_vcs = vcs.strip().lower()
    if clean_vcs not in {"github", "bitbucket"}:
        raise ValueError('vcs must be "github" or "bitbucket"')
    clean_slug = project_slug.strip().strip("/")
    if "/" not in clean_slug:
        raise ValueError('project_slug must look like "org/repo"')
    return f"/project/{clean_vcs}/{quote(clean_slug, safe='')}"


def _travis_repo_path(slug: str) -> str:
    clean = slug.strip().strip("/")
    if "/" not in clean:
        raise ValueError('slug must look like "owner/repo"')
    return quote(clean, safe="")


def _jenkins_job_path(job_name: str) -> str:
    parts = [part for part in job_name.strip().strip("/").split("/") if part]
    if not parts:
        raise ValueError("job_name is required")
    if parts[0] == "job":
        parts = [part for part in parts if part != "job"]
    return "".join(f"/job/{quote(part, safe='')}" for part in parts)


def _jenkins_headers(content_type: str = "application/json") -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": content_type,
        "User-Agent": "Nymeria",
    }


@tool
def circleci_list_pipelines(
    vcs: str,
    project_slug: str,
    branch: str = "",
    limit: int = 25,
    page_token: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List CircleCI pipelines for a GitHub or Bitbucket project.

    Args:
        vcs: Source provider, "github" or "bitbucket".
        project_slug: Project slug like "org/repo".
        branch: Optional branch filter.
        limit: Number of pipelines to return, 1-100.
        page_token: Optional CircleCI pagination token.
    """
    try:
        base_url, headers = _circleci_config("circleci_list_pipelines", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "GET",
            f"{base_url}{_circleci_project_path(vcs, project_slug)}/pipeline",
            params={"branch": branch, "limit": _limit(limit), "page-token": page_token},
            headers=headers,
        )
        return _dump_json(data.get("items", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("circleci_list_pipelines failed", exc_info=True)
        return f"[Error]: CircleCI pipeline list failed: {e}"


@tool
def circleci_get_pipeline(
    vcs: str,
    project_slug: str,
    pipeline_number: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one CircleCI pipeline by project and pipeline number.

    Args:
        vcs: Source provider, "github" or "bitbucket".
        project_slug: Project slug like "org/repo".
        pipeline_number: CircleCI pipeline number.
    """
    try:
        base_url, headers = _circleci_config("circleci_get_pipeline", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "GET",
            f"{base_url}{_circleci_project_path(vcs, project_slug)}/pipeline/{int(pipeline_number)}",
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("circleci_get_pipeline failed", exc_info=True)
        return f"[Error]: CircleCI pipeline lookup failed: {e}"


@tool
def circleci_trigger_pipeline(
    vcs: str,
    project_slug: str,
    branch: str = "",
    tag: str = "",
    parameters_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Trigger a CircleCI pipeline.

    Args:
        vcs: Source provider, "github" or "bitbucket".
        project_slug: Project slug like "org/repo".
        branch: Optional branch to build. Branch and tag are mutually exclusive.
        tag: Optional tag to build. Branch and tag are mutually exclusive.
        parameters_json: Optional JSON object of CircleCI pipeline parameters.
    """
    if branch and tag:
        return "[Error]: branch and tag are mutually exclusive."
    try:
        base_url, headers = _circleci_config("circleci_trigger_pipeline", config)
        if isinstance(headers, str):
            return headers
        body = _filtered({"branch": branch, "tag": tag, "parameters": _json_object(parameters_json, field_name="parameters_json")})
        data = _request_json(
            "POST",
            f"{base_url}{_circleci_project_path(vcs, project_slug)}/pipeline",
            json_body=body,
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("circleci_trigger_pipeline failed", exc_info=True)
        return f"[Error]: CircleCI pipeline trigger failed: {e}"


@tool
def travisci_list_builds(
    include: str = "",
    sort_by: str = "number",
    order: str = "desc",
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Travis CI builds visible to the token.

    Args:
        include: Optional comma-separated Travis include list, e.g. "build.commit".
        sort_by: Sort field such as "number", "id", or "created_at".
        order: "asc" or "desc".
        limit: Number of builds to return, 1-100.
    """
    try:
        base_url, headers = _travisci_config("travisci_list_builds", config)
        if isinstance(headers, str):
            return headers
        sort = sort_by.strip() or "number"
        if order.strip():
            sort = f"{sort}:{order.strip().lower()}"
        data = _request_json(
            "GET",
            f"{base_url}/builds",
            params={"include": include, "sort_by": sort, "limit": _limit(limit)},
            headers=headers,
        )
        return _dump_json(data.get("builds", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("travisci_list_builds failed", exc_info=True)
        return f"[Error]: Travis CI build list failed: {e}"


@tool
def travisci_get_build(
    build_id: str,
    include: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get one Travis CI build.

    Args:
        build_id: Travis CI build ID.
        include: Optional comma-separated Travis include list.
    """
    if not build_id.strip():
        return "[Error]: build_id is required."
    try:
        base_url, headers = _travisci_config("travisci_get_build", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("GET", f"{base_url}/build/{quote(build_id.strip(), safe='')}", params={"include": include}, headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("travisci_get_build failed", exc_info=True)
        return f"[Error]: Travis CI build lookup failed: {e}"


@tool
def travisci_trigger_build(
    slug: str,
    branch: str,
    message: str = "",
    merge_mode: str = "",
    config_json: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Trigger a Travis CI build request for a repository.

    Args:
        slug: Repository slug like "owner/repo".
        branch: Branch to build.
        message: Optional request message.
        merge_mode: Optional config merge mode.
        config_json: Optional JSON object for Travis request config.
    """
    if not branch.strip():
        return "[Error]: branch is required."
    try:
        base_url, headers = _travisci_config("travisci_trigger_build", config)
        if isinstance(headers, str):
            return headers
        request_body = _filtered(
            {
                "branch": branch.strip(),
                "message": message.strip(),
                "merge_mode": merge_mode.strip(),
                "config": _json_object(config_json, field_name="config_json"),
            }
        )
        data = _request_json(
            "POST",
            f"{base_url}/repo/{_travis_repo_path(slug)}/requests",
            json_body={"request": request_body},
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("travisci_trigger_build failed", exc_info=True)
        return f"[Error]: Travis CI build trigger failed: {e}"


@tool
def travisci_restart_build(
    build_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Restart a Travis CI build.

    Args:
        build_id: Travis CI build ID.
    """
    if not build_id.strip():
        return "[Error]: build_id is required."
    try:
        base_url, headers = _travisci_config("travisci_restart_build", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{base_url}/build/{quote(build_id.strip(), safe='')}/restart", headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("travisci_restart_build failed", exc_info=True)
        return f"[Error]: Travis CI build restart failed: {e}"


@tool
def travisci_cancel_build(
    build_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Cancel a Travis CI build.

    Args:
        build_id: Travis CI build ID.
    """
    if not build_id.strip():
        return "[Error]: build_id is required."
    try:
        base_url, headers = _travisci_config("travisci_cancel_build", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("POST", f"{base_url}/build/{quote(build_id.strip(), safe='')}/cancel", headers=headers)
        return _dump_json(data)
    except Exception as e:
        logger.error("travisci_cancel_build failed", exc_info=True)
        return f"[Error]: Travis CI build cancel failed: {e}"


@tool
def jenkins_get_instance(
    tree: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get Jenkins instance metadata.

    Args:
        tree: Optional Jenkins tree selector to limit fields.
    """
    try:
        base_url, auth, error = _jenkins_config("jenkins_get_instance", config)
        if error:
            return error
        data = _request_json("GET", f"{base_url}/api/json", params={"tree": tree}, headers=_jenkins_headers(), auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_get_instance failed", exc_info=True)
        return f"[Error]: Jenkins instance lookup failed: {e}"


@tool
def jenkins_list_jobs(
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Jenkins jobs with basic build status.

    Args:
        limit: Number of jobs to return, 1-100.
    """
    try:
        base_url, auth, error = _jenkins_config("jenkins_list_jobs", config)
        if error:
            return error
        tree = "jobs[name,url,color,buildable,lastBuild[number,result,timestamp,url],lastSuccessfulBuild[number,url],lastFailedBuild[number,url]]"
        data = _request_json("GET", f"{base_url}/api/json", params={"tree": tree}, headers=_jenkins_headers(), auth=auth)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            data["jobs"] = data["jobs"][: _limit(limit)]
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_list_jobs failed", exc_info=True)
        return f"[Error]: Jenkins job list failed: {e}"


@tool
def jenkins_list_job_builds(
    job_name: str,
    limit: int = 25,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List builds for one Jenkins job.

    Args:
        job_name: Jenkins job name. Use "folder/job" for nested jobs.
        limit: Number of builds to return, 1-100.
    """
    try:
        base_url, auth, error = _jenkins_config("jenkins_list_job_builds", config)
        if error:
            return error
        tree = f"builds[number,url,result,timestamp,duration,building,description]{{0,{_limit(limit)}}}"
        data = _request_json(
            "GET",
            f"{base_url}{_jenkins_job_path(job_name)}/api/json",
            params={"tree": tree},
            headers=_jenkins_headers(),
            auth=auth,
        )
        return _dump_json(data.get("builds", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("jenkins_list_job_builds failed", exc_info=True)
        return f"[Error]: Jenkins build list failed: {e}"


@tool
def jenkins_trigger_job(
    job_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Trigger a Jenkins job build.

    Args:
        job_name: Jenkins job name. Use "folder/job" for nested jobs.
    """
    try:
        base_url, auth, error = _jenkins_config("jenkins_trigger_job", config)
        if error:
            return error
        data = _request_json("POST", f"{base_url}{_jenkins_job_path(job_name)}/build", headers=_jenkins_headers(), auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_trigger_job failed", exc_info=True)
        return f"[Error]: Jenkins job trigger failed: {e}"


@tool
def jenkins_trigger_job_with_parameters(
    job_name: str,
    parameters_json: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Trigger a parameterized Jenkins job build.

    Args:
        job_name: Jenkins job name. Use "folder/job" for nested jobs.
        parameters_json: JSON object of Jenkins build parameters.
    """
    try:
        base_url, auth, error = _jenkins_config("jenkins_trigger_job_with_parameters", config)
        if error:
            return error
        params = _json_object(parameters_json, field_name="parameters_json")
        data = _request_json(
            "POST",
            f"{base_url}{_jenkins_job_path(job_name)}/buildWithParameters",
            form_data=params,
            headers=_jenkins_headers("application/x-www-form-urlencoded"),
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_trigger_job_with_parameters failed", exc_info=True)
        return f"[Error]: Jenkins parameterized job trigger failed: {e}"


@tool
def jenkins_copy_job(
    source_job_name: str,
    new_job_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Copy a Jenkins job to a new job name.

    Args:
        source_job_name: Existing Jenkins job name.
        new_job_name: New Jenkins job name.
    """
    if not new_job_name.strip():
        return "[Error]: new_job_name is required."
    try:
        base_url, auth, error = _jenkins_config("jenkins_copy_job", config)
        if error:
            return error
        data = _request_json(
            "POST",
            f"{base_url}/createItem",
            params={"name": new_job_name.strip(), "mode": "copy", "from": source_job_name.strip()},
            headers=_jenkins_headers(),
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_copy_job failed", exc_info=True)
        return f"[Error]: Jenkins job copy failed: {e}"


@tool
def jenkins_create_job(
    new_job_name: str,
    config_xml: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Jenkins job from XML config.

    Args:
        new_job_name: New Jenkins job name.
        config_xml: Jenkins job config.xml content.
    """
    if not new_job_name.strip():
        return "[Error]: new_job_name is required."
    if not config_xml.strip():
        return "[Error]: config_xml is required."
    try:
        base_url, auth, error = _jenkins_config("jenkins_create_job", config)
        if error:
            return error
        data = _request_json(
            "POST",
            f"{base_url}/createItem",
            params={"name": new_job_name.strip()},
            content=config_xml,
            headers=_jenkins_headers("application/xml"),
            auth=auth,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_create_job failed", exc_info=True)
        return f"[Error]: Jenkins job creation failed: {e}"


@tool
def jenkins_quiet_down(
    reason: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Put Jenkins into quiet-down mode.

    Args:
        reason: Optional reason to show in Jenkins.
    """
    try:
        base_url, auth, error = _jenkins_config("jenkins_quiet_down", config)
        if error:
            return error
        data = _request_json("POST", f"{base_url}/quietDown", params={"reason": reason}, headers=_jenkins_headers(), auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_quiet_down failed", exc_info=True)
        return f"[Error]: Jenkins quiet-down failed: {e}"


@tool
def jenkins_cancel_quiet_down(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Cancel Jenkins quiet-down mode."""
    try:
        base_url, auth, error = _jenkins_config("jenkins_cancel_quiet_down", config)
        if error:
            return error
        data = _request_json("POST", f"{base_url}/cancelQuietDown", headers=_jenkins_headers(), auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_cancel_quiet_down failed", exc_info=True)
        return f"[Error]: Jenkins quiet-down cancel failed: {e}"


@tool
def jenkins_restart_instance(
    mode: str = "safe_restart",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Restart a Jenkins instance.

    Args:
        mode: "safe_restart" to wait for builds or "restart" to restart immediately.
    """
    endpoints = {"restart": "/restart", "safe_restart": "/safeRestart"}
    endpoint = endpoints.get(mode.strip().lower())
    if not endpoint:
        return '[Error]: mode must be "safe_restart" or "restart".'
    try:
        base_url, auth, error = _jenkins_config("jenkins_restart_instance", config)
        if error:
            return error
        data = _request_json("POST", f"{base_url}{endpoint}", headers=_jenkins_headers(), auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_restart_instance failed", exc_info=True)
        return f"[Error]: Jenkins restart failed: {e}"


@tool
def jenkins_shutdown_instance(
    mode: str = "safe_shutdown",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Shut down a Jenkins instance.

    Args:
        mode: "safe_shutdown" to wait for builds or "shutdown" to shut down immediately.
    """
    endpoints = {"shutdown": "/exit", "safe_shutdown": "/safeExit"}
    endpoint = endpoints.get(mode.strip().lower())
    if not endpoint:
        return '[Error]: mode must be "safe_shutdown" or "shutdown".'
    try:
        base_url, auth, error = _jenkins_config("jenkins_shutdown_instance", config)
        if error:
            return error
        data = _request_json("POST", f"{base_url}{endpoint}", headers=_jenkins_headers(), auth=auth)
        return _dump_json(data)
    except Exception as e:
        logger.error("jenkins_shutdown_instance failed", exc_info=True)
        return f"[Error]: Jenkins shutdown failed: {e}"


BUILD_CI_SERVICE_TOOLS = [
    circleci_list_pipelines,
    circleci_get_pipeline,
    circleci_trigger_pipeline,
    travisci_list_builds,
    travisci_get_build,
    travisci_trigger_build,
    travisci_restart_build,
    travisci_cancel_build,
    jenkins_get_instance,
    jenkins_list_jobs,
    jenkins_list_job_builds,
    jenkins_trigger_job,
    jenkins_trigger_job_with_parameters,
    jenkins_copy_job,
    jenkins_create_job,
    jenkins_quiet_down,
    jenkins_cancel_quiet_down,
    jenkins_restart_instance,
    jenkins_shutdown_instance,
]
