"""Developer platform service integration tools."""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import quote, urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.http_policy import policy_http_client as _http_client, validate_http_egress_url

from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)
from .service_integration_base import (
    clamp_limit,
    credential_value as _credential_value,
    dump_json,
    request_with_policy as _request_with_policy,
    require_joined_destination as _require_joined_destination,
    settings_value as _settings_value,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0
_MAX_JSON_CHARS = 60_000
_GITHUB_DEFAULT_BASE_URL = "https://api.github.com"
_GITLAB_DEFAULT_BASE_URL = "https://gitlab.com/api/v4"

# Provider credential specs: the single source of truth for these providers'
# credential shapes (see credential_registry). The config helpers below source
# their _credential_value arguments from the specs; these providers surface no
# setup hint. Field-name tuple ORDER is behaviorally significant.
_GITHUB = register_provider_spec(
    ProviderCredentialSpec(
        provider="github",
        aliases=("github_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "api_base_url", "server", "url"), required=False
            ),
            CredentialFieldGroup(role="token", names=("access_token", "token", "api_key", "value")),
        ),
    )
)

_GITLAB = register_provider_spec(
    ProviderCredentialSpec(
        provider="gitlab",
        aliases=("gitlab_api",),
        groups=(
            CredentialFieldGroup(
                role="base_url", names=("base_url", "server", "url"), required=False
            ),
            CredentialFieldGroup(
                role="token", names=("access_token", "private_token", "token", "api_key", "value")
            ),
        ),
    )
)

# Generic GraphQL connector: endpoint plus several alternative, all-optional
# auth mechanisms, so every group is required=False.
_GRAPHQL = register_provider_spec(
    ProviderCredentialSpec(
        provider="graphql",
        aliases=("graphql_api",),
        groups=(
            CredentialFieldGroup(
                role="endpoint",
                names=("endpoint", "graphql_url", "api_url", "base_url", "url"),
                required=False,
            ),
            CredentialFieldGroup(
                role="headers", names=("headers_json", "headersJson", "headers"), required=False
            ),
            CredentialFieldGroup(
                role="authorization",
                names=("authorization", "auth_header", "authHeader"),
                required=False,
            ),
            CredentialFieldGroup(
                role="bearer_token",
                names=("bearer_token", "bearerToken", "access_token", "accessToken", "token", "value"),
                required=False,
            ),
            CredentialFieldGroup(role="api_key", names=("api_key", "apiKey"), required=False),
            CredentialFieldGroup(
                role="api_key_header", names=("api_key_header", "apiKeyHeader"), required=False
            ),
        ),
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    return dump_json(data, max_chars=max_chars)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _csv(value: str) -> str:
    return ",".join(_split_csv(value))


def _limit(value: int, *, default: int = 20, max_value: int = 100) -> int:
    return clamp_limit(value, default=default, max_value=max_value)


def _filtered_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != "" and value != []
    }


def _json_object(value: str, *, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _require_absolute_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return validate_http_egress_url(
        base_url.strip().rstrip("/"),
        label="base URL",
        resolve_dns=False,
    )


def _gitlab_api_base_url(base_url: str) -> str:
    base = _require_absolute_base_url(base_url)
    return base if base.endswith("/api/v4") else f"{base}/api/v4"


def _github_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str]]:
    base_url = (
        _credential_value(
            provider=_GITHUB.provider,
            provider_aliases=_GITHUB.aliases,
            field_names=_GITHUB.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("github_api_base_url")
        or _GITHUB_DEFAULT_BASE_URL
    )
    token = _credential_value(
        provider=_GITHUB.provider,
        provider_aliases=_GITHUB.aliases,
        field_names=_GITHUB.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("github_token")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Nymeria",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _require_absolute_base_url(base_url), headers


def _gitlab_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str]]:
    base_url = (
        _credential_value(
            provider=_GITLAB.provider,
            provider_aliases=_GITLAB.aliases,
            field_names=_GITLAB.group("base_url"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("gitlab_base_url")
        or _GITLAB_DEFAULT_BASE_URL
    )
    token = _credential_value(
        provider=_GITLAB.provider,
        provider_aliases=_GITLAB.aliases,
        field_names=_GITLAB.group("token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("gitlab_token")
    headers = {"Accept": "application/json", "User-Agent": "Nymeria"}
    if token:
        headers["Private-Token"] = token
    return _gitlab_api_base_url(base_url), headers


def _graphql_endpoint(
    tool_name: str,
    endpoint: str,
    config: Optional[RunnableConfig],
) -> tuple[str | None, str | None]:
    """Resolve the GraphQL endpoint, reporting where the address came from.

    Returns ``(endpoint_or_none, endpoint_from_vault)``. The second element is
    the provenance ``_require_joined_destination`` needs: this helper resolves
    the address while ``_graphql_headers`` resolves the credentials, so neither
    of the two can see the pairing alone, which is why the leak survived here.

    It is None whenever the address did not come from a saved record, and that
    deliberately includes the caller-supplied ``endpoint`` argument. A tool
    argument is not a vault record steering the request, so it is neither half
    of this join; treating it as one would refuse every explicit-endpoint call
    that leans on a configured key, which is the documented way to use this tool
    against an endpoint you have not saved. A caller-named address carrying the
    server's key is a different question (the tool argument is agent-supplied),
    and it is not what this control decides.
    """
    caller_endpoint = endpoint.strip()
    if caller_endpoint:
        return caller_endpoint, None
    endpoint_from_vault = _credential_value(
        provider=_GRAPHQL.provider,
        provider_aliases=_GRAPHQL.aliases,
        field_names=_GRAPHQL.group("endpoint"),
        tool_name=tool_name,
        config=config,
    )
    return (endpoint_from_vault or _settings_value("graphql_endpoint")), endpoint_from_vault


def _graphql_headers(
    *,
    tool_name: str,
    headers_json: str,
    config: Optional[RunnableConfig],
    endpoint_from_vault: str | None,
) -> dict[str, str]:
    """Build the request headers, joined to whoever supplied the address.

    ``endpoint_from_vault`` is ``_graphql_endpoint``'s second return value. Every
    credential this commits to the wire is guarded against it SEPARATELY, never
    against a disjunction of the three: graphql declares three independent anchor
    groups, so a record holding ``endpoint`` plus any one of them proves
    possession, wins the address lookup, and would satisfy a guard written over
    the alternatives while a different branch still carries the operator's
    environment-configured secret to the address that record chose.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }
    # The stored header blob is deliberately outside this control, not an
    # oversight of it. ``credential_registry._NON_PROOF_NAMES`` lists
    # ``headers``/``headers_json`` with the TLS fields as an independence defect
    # of the same shape tracked on its own: they are neither an address nor proof
    # of possession. Guarding it here would also refuse the ordinary setup of one
    # record holding endpoint + key alongside operator-set DEFAULT headers, which
    # is what GRAPHQL_HEADERS_JSON is documented to be. The residual is that an
    # operator who puts auth in that blob has put it outside the join.
    stored_headers = (
        _credential_value(
            provider=_GRAPHQL.provider,
            provider_aliases=_GRAPHQL.aliases,
            field_names=_GRAPHQL.group("headers"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("graphql_headers_json")
        or "{}"
    )
    parsed_headers = _json_object(stored_headers, field_name="stored GraphQL headers")
    request_headers = _json_object(headers_json, field_name="headers_json")
    headers.update({str(key): str(value) for key, value in parsed_headers.items() if value is not None})
    headers.update({str(key): str(value) for key, value in request_headers.items() if value is not None})

    # The one auth credential here that needs no guard: there is no
    # ``GRAPHQL_AUTHORIZATION`` setting behind it, so its value can only ever be
    # the vault's and the join is already made. Adding an ``or
    # _settings_value(...)`` leg later makes it exactly as steerable as the two
    # below and needs its own guard on its own ``*_from_vault`` local; a guard
    # written over ``authorization`` itself would be a permanent no-op that reads
    # as coverage.
    authorization = _credential_value(
        provider=_GRAPHQL.provider,
        provider_aliases=_GRAPHQL.aliases,
        field_names=_GRAPHQL.group("authorization"),
        tool_name=tool_name,
        config=config,
    )
    bearer_token_from_vault = _credential_value(
        provider=_GRAPHQL.provider,
        provider_aliases=_GRAPHQL.aliases,
        field_names=_GRAPHQL.group("bearer_token"),
        tool_name=tool_name,
        config=config,
    )
    bearer_token = bearer_token_from_vault or _settings_value("graphql_bearer_token")
    api_key_from_vault = _credential_value(
        provider=_GRAPHQL.provider,
        provider_aliases=_GRAPHQL.aliases,
        field_names=_GRAPHQL.group("api_key"),
        tool_name=tool_name,
        config=config,
    )
    api_key = api_key_from_vault or _settings_value("graphql_api_key")
    api_key_header = (
        _credential_value(
            provider=_GRAPHQL.provider,
            provider_aliases=_GRAPHQL.aliases,
            field_names=_GRAPHQL.group("api_key_header"),
            tool_name=tool_name,
            config=config,
        )
        or _settings_value("graphql_api_key_header")
        or "x-api-key"
    )
    # Each guard sits in the branch that actually puts its credential on the
    # wire, so a request is refused only when the secret it would disclose is one
    # this call is about to send. A stored ``authorization`` suppresses the
    # bearer branch entirely, and refusing there would reject a record that
    # discloses nothing. There is no setup hint to jump ahead of: these providers
    # surface none, and the caller has already returned its "save a credential"
    # message when no endpoint resolved at all.
    if authorization:
        headers["Authorization"] = authorization
    elif bearer_token:
        _require_joined_destination(
            destination_from_vault=endpoint_from_vault,
            secret_from_vault=bearer_token_from_vault,
            secret=bearer_token,
            provider=_GRAPHQL.provider,
        )
        headers["Authorization"] = f"Bearer {bearer_token}"
    if api_key:
        # Joined on the api key alone. This header is independent of the
        # Authorization one above, so a record holding ``endpoint`` plus a
        # ``bearer_token`` proves possession, wins the address, and would still
        # carry the operator's GRAPHQL_API_KEY here.
        _require_joined_destination(
            destination_from_vault=endpoint_from_vault,
            secret_from_vault=api_key_from_vault,
            secret=api_key,
            provider=_GRAPHQL.provider,
        )
        headers[str(api_key_header)] = api_key
    return headers


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with _http_client(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=0),
            trust_env=False,
        ) as client:
            response = _request_with_policy(
                client,
                method,
                url,
                params=_filtered_params(params),
                headers=headers,
            )
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            return response.json()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            detail = (
                body.get("message")
                or body.get("error")
                or body.get("error_description")
                or ""
            )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _request_json_body(
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with _http_client(timeout=_HTTP_TIMEOUT) as client:
            response = _request_with_policy(client, method, url, json=json_body, headers=headers)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            return response.json()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = e.response.json()
            if isinstance(body, dict):
                detail = (
                    body.get("message")
                    or body.get("error")
                    or body.get("error_description")
                    or ""
                )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _github_repo_summary(repo: dict[str, Any]) -> dict[str, Any]:
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    license_info = repo.get("license") if isinstance(repo.get("license"), dict) else {}
    return {
        "full_name": repo.get("full_name"),
        "owner": owner.get("login"),
        "description": repo.get("description"),
        "html_url": repo.get("html_url"),
        "private": repo.get("private"),
        "fork": repo.get("fork"),
        "archived": repo.get("archived"),
        "language": repo.get("language"),
        "topics": repo.get("topics"),
        "default_branch": repo.get("default_branch"),
        "stargazers_count": repo.get("stargazers_count"),
        "forks_count": repo.get("forks_count"),
        "open_issues_count": repo.get("open_issues_count"),
        "license": license_info.get("spdx_id") or license_info.get("name"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
    }


def _github_issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
    labels = issue.get("labels") if isinstance(issue.get("labels"), list) else []
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "html_url": issue.get("html_url"),
        "author": user.get("login"),
        "labels": [
            label.get("name")
            for label in labels
            if isinstance(label, dict) and label.get("name")
        ],
        "comments": issue.get("comments"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "is_pull_request": bool(issue.get("pull_request")),
    }


def _github_pull_summary(pull: dict[str, Any]) -> dict[str, Any]:
    user = pull.get("user") if isinstance(pull.get("user"), dict) else {}
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    return {
        "number": pull.get("number"),
        "title": pull.get("title"),
        "state": pull.get("state"),
        "html_url": pull.get("html_url"),
        "author": user.get("login"),
        "draft": pull.get("draft"),
        "head": head.get("ref"),
        "base": base.get("ref"),
        "created_at": pull.get("created_at"),
        "updated_at": pull.get("updated_at"),
        "merged_at": pull.get("merged_at"),
    }


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    author = release.get("author") if isinstance(release.get("author"), dict) else {}
    links = release.get("_links") if isinstance(release.get("_links"), dict) else {}
    return {
        "tag_name": release.get("tag_name"),
        "name": release.get("name"),
        "html_url": release.get("html_url") or links.get("self"),
        "author": author.get("login") or author.get("username"),
        "draft": release.get("draft"),
        "prerelease": release.get("prerelease"),
        "released_at": release.get("published_at") or release.get("released_at"),
        "created_at": release.get("created_at"),
        "description": release.get("body") or release.get("description"),
    }


def _gitlab_project_id(project: str) -> str:
    project = str(project).strip()
    if not project:
        raise ValueError("project is required")
    return quote(project, safe="")


def _gitlab_project_summary(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": project.get("id"),
        "path_with_namespace": project.get("path_with_namespace"),
        "name_with_namespace": project.get("name_with_namespace"),
        "description": project.get("description"),
        "web_url": project.get("web_url"),
        "visibility": project.get("visibility"),
        "default_branch": project.get("default_branch"),
        "star_count": project.get("star_count"),
        "forks_count": project.get("forks_count"),
        "open_issues_count": project.get("open_issues_count"),
        "last_activity_at": project.get("last_activity_at"),
    }


def _gitlab_issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    author = issue.get("author") if isinstance(issue.get("author"), dict) else {}
    return {
        "iid": issue.get("iid"),
        "id": issue.get("id"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "web_url": issue.get("web_url"),
        "author": author.get("username"),
        "labels": issue.get("labels"),
        "user_notes_count": issue.get("user_notes_count"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
    }


@tool
def github_get_repository(
    owner: str,
    repo: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get GitHub repository metadata.

    Args:
        owner: Repository owner or organization.
        repo: Repository name.
    """
    if not owner.strip() or not repo.strip():
        return "[Error]: owner and repo are required."
    try:
        base_url, headers = _github_config("github_get_repository", config)
        data = _request_json(
            "GET",
            f"{base_url}/repos/{quote(owner.strip(), safe='')}/{quote(repo.strip(), safe='')}",
            headers=headers,
        )
        return _dump_json(_github_repo_summary(data))
    except Exception as e:
        logger.error("github_get_repository failed", exc_info=True)
        return f"[Error]: GitHub repository lookup failed: {e}"


@tool
def github_search_repositories(
    query: str,
    sort: str = "stars",
    order: str = "desc",
    limit: int = 10,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search GitHub repositories.

    Args:
        query: GitHub repository search query.
        sort: Optional sort field, e.g. "stars", "forks", or "updated".
        order: "asc" or "desc".
        limit: Number of repositories to return, 1-100.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers = _github_config("github_search_repositories", config)
        data = _request_json(
            "GET",
            f"{base_url}/search/repositories",
            params={
                "q": query.strip(),
                "sort": sort.strip(),
                "order": order.strip() or "desc",
                "per_page": _limit(limit),
            },
            headers=headers,
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return _dump_json(
            {
                "total_count": data.get("total_count") if isinstance(data, dict) else None,
                "incomplete_results": data.get("incomplete_results") if isinstance(data, dict) else None,
                "items": [_github_repo_summary(item) for item in items],
            }
        )
    except Exception as e:
        logger.error("github_search_repositories failed", exc_info=True)
        return f"[Error]: GitHub repository search failed: {e}"


@tool
def github_list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    sort: str = "created",
    direction: str = "desc",
    since: str = "",
    limit: int = 20,
    include_pull_requests: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GitHub repository issues.

    Args:
        owner: Repository owner or organization.
        repo: Repository name.
        state: "open", "closed", or "all".
        labels: Optional comma-separated labels.
        sort: Sort field, e.g. "created", "updated", or "comments".
        direction: "asc" or "desc".
        since: Optional ISO 8601 timestamp.
        limit: Number of issues to return, 1-100.
        include_pull_requests: Include pull requests returned by GitHub's issues endpoint.
    """
    if not owner.strip() or not repo.strip():
        return "[Error]: owner and repo are required."
    try:
        base_url, headers = _github_config("github_list_issues", config)
        data = _request_json(
            "GET",
            f"{base_url}/repos/{quote(owner.strip(), safe='')}/{quote(repo.strip(), safe='')}/issues",
            params={
                "state": state.strip() or "open",
                "labels": _csv(labels),
                "sort": sort.strip() or "created",
                "direction": direction.strip() or "desc",
                "since": since.strip(),
                "per_page": _limit(limit),
            },
            headers=headers,
        )
        issues = data if isinstance(data, list) else []
        if not include_pull_requests:
            issues = [issue for issue in issues if not issue.get("pull_request")]
        return _dump_json([_github_issue_summary(issue) for issue in issues])
    except Exception as e:
        logger.error("github_list_issues failed", exc_info=True)
        return f"[Error]: GitHub issue list failed: {e}"


@tool
def github_get_issue(
    owner: str,
    repo: str,
    issue_number: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a single GitHub issue by repository issue number.

    Args:
        owner: Repository owner or organization.
        repo: Repository name.
        issue_number: Repository issue number.
    """
    if not owner.strip() or not repo.strip():
        return "[Error]: owner and repo are required."
    try:
        base_url, headers = _github_config("github_get_issue", config)
        data = _request_json(
            "GET",
            f"{base_url}/repos/{quote(owner.strip(), safe='')}/{quote(repo.strip(), safe='')}/issues/{int(issue_number)}",
            headers=headers,
        )
        summary = _github_issue_summary(data)
        summary["body"] = data.get("body")
        return _dump_json(summary)
    except Exception as e:
        logger.error("github_get_issue failed", exc_info=True)
        return f"[Error]: GitHub issue lookup failed: {e}"


@tool
def github_list_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
    sort: str = "created",
    direction: str = "desc",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GitHub repository pull requests.

    Args:
        owner: Repository owner or organization.
        repo: Repository name.
        state: "open", "closed", or "all".
        sort: Sort field, e.g. "created", "updated", or "popularity".
        direction: "asc" or "desc".
        limit: Number of pull requests to return, 1-100.
    """
    if not owner.strip() or not repo.strip():
        return "[Error]: owner and repo are required."
    try:
        base_url, headers = _github_config("github_list_pull_requests", config)
        data = _request_json(
            "GET",
            f"{base_url}/repos/{quote(owner.strip(), safe='')}/{quote(repo.strip(), safe='')}/pulls",
            params={
                "state": state.strip() or "open",
                "sort": sort.strip() or "created",
                "direction": direction.strip() or "desc",
                "per_page": _limit(limit),
            },
            headers=headers,
        )
        pulls = data if isinstance(data, list) else []
        return _dump_json([_github_pull_summary(pull) for pull in pulls if isinstance(pull, dict)])
    except Exception as e:
        logger.error("github_list_pull_requests failed", exc_info=True)
        return f"[Error]: GitHub pull request list failed: {e}"


@tool
def github_list_releases(
    owner: str,
    repo: str,
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GitHub repository releases.

    Args:
        owner: Repository owner or organization.
        repo: Repository name.
        limit: Number of releases to return, 1-100.
    """
    if not owner.strip() or not repo.strip():
        return "[Error]: owner and repo are required."
    try:
        base_url, headers = _github_config("github_list_releases", config)
        data = _request_json(
            "GET",
            f"{base_url}/repos/{quote(owner.strip(), safe='')}/{quote(repo.strip(), safe='')}/releases",
            params={"per_page": _limit(limit)},
            headers=headers,
        )
        releases = data if isinstance(data, list) else []
        return _dump_json([_release_summary(release) for release in releases if isinstance(release, dict)])
    except Exception as e:
        logger.error("github_list_releases failed", exc_info=True)
        return f"[Error]: GitHub release list failed: {e}"


@tool
def github_get_release(
    owner: str,
    repo: str,
    tag_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a GitHub release by tag name.

    Args:
        owner: Repository owner or organization.
        repo: Repository name.
        tag_name: Release tag name.
    """
    if not owner.strip() or not repo.strip() or not tag_name.strip():
        return "[Error]: owner, repo, and tag_name are required."
    try:
        base_url, headers = _github_config("github_get_release", config)
        data = _request_json(
            "GET",
            f"{base_url}/repos/{quote(owner.strip(), safe='')}/{quote(repo.strip(), safe='')}/releases/tags/{quote(tag_name.strip(), safe='')}",
            headers=headers,
        )
        return _dump_json(_release_summary(data))
    except Exception as e:
        logger.error("github_get_release failed", exc_info=True)
        return f"[Error]: GitHub release lookup failed: {e}"


@tool
def gitlab_get_project(
    project: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get GitLab project metadata by numeric ID or namespace path.

    Args:
        project: Numeric project ID or namespace path like "group/project".
    """
    try:
        base_url, headers = _gitlab_config("gitlab_get_project", config)
        data = _request_json(
            "GET",
            f"{base_url}/projects/{_gitlab_project_id(project)}",
            headers=headers,
        )
        return _dump_json(_gitlab_project_summary(data))
    except Exception as e:
        logger.error("gitlab_get_project failed", exc_info=True)
        return f"[Error]: GitLab project lookup failed: {e}"


@tool
def gitlab_search_projects(
    query: str,
    limit: int = 10,
    simple: bool = True,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search GitLab projects.

    Args:
        query: Project name, path, or description search text.
        limit: Number of projects to return, 1-100.
        simple: Return GitLab's simpler project payload.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        base_url, headers = _gitlab_config("gitlab_search_projects", config)
        data = _request_json(
            "GET",
            f"{base_url}/projects",
            params={"search": query.strip(), "simple": str(simple).lower(), "per_page": _limit(limit)},
            headers=headers,
        )
        projects = data if isinstance(data, list) else []
        return _dump_json([_gitlab_project_summary(project) for project in projects if isinstance(project, dict)])
    except Exception as e:
        logger.error("gitlab_search_projects failed", exc_info=True)
        return f"[Error]: GitLab project search failed: {e}"


@tool
def gitlab_list_project_issues(
    project: str,
    state: str = "opened",
    labels: str = "",
    order_by: str = "created_at",
    sort: str = "desc",
    search: str = "",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GitLab project issues.

    Args:
        project: Numeric project ID or namespace path like "group/project".
        state: "opened", "closed", or "all".
        labels: Optional comma-separated labels.
        order_by: Sort field, e.g. "created_at", "updated_at", or "priority".
        sort: "asc" or "desc".
        search: Optional issue search text.
        limit: Number of issues to return, 1-100.
    """
    try:
        base_url, headers = _gitlab_config("gitlab_list_project_issues", config)
        data = _request_json(
            "GET",
            f"{base_url}/projects/{_gitlab_project_id(project)}/issues",
            params={
                "state": state.strip() or "opened",
                "labels": _csv(labels),
                "order_by": order_by.strip() or "created_at",
                "sort": sort.strip() or "desc",
                "search": search.strip(),
                "per_page": _limit(limit),
            },
            headers=headers,
        )
        issues = data if isinstance(data, list) else []
        return _dump_json([_gitlab_issue_summary(issue) for issue in issues if isinstance(issue, dict)])
    except Exception as e:
        logger.error("gitlab_list_project_issues failed", exc_info=True)
        return f"[Error]: GitLab issue list failed: {e}"


@tool
def gitlab_get_project_issue(
    project: str,
    issue_iid: int,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a single GitLab project issue by internal issue ID.

    Args:
        project: Numeric project ID or namespace path like "group/project".
        issue_iid: GitLab project's internal issue ID.
    """
    try:
        base_url, headers = _gitlab_config("gitlab_get_project_issue", config)
        data = _request_json(
            "GET",
            f"{base_url}/projects/{_gitlab_project_id(project)}/issues/{int(issue_iid)}",
            headers=headers,
        )
        summary = _gitlab_issue_summary(data)
        summary["description"] = data.get("description")
        return _dump_json(summary)
    except Exception as e:
        logger.error("gitlab_get_project_issue failed", exc_info=True)
        return f"[Error]: GitLab issue lookup failed: {e}"


@tool
def gitlab_list_project_releases(
    project: str,
    order_by: str = "released_at",
    sort: str = "desc",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GitLab project releases.

    Args:
        project: Numeric project ID or namespace path like "group/project".
        order_by: Sort field, e.g. "released_at" or "created_at".
        sort: "asc" or "desc".
        limit: Number of releases to return, 1-100.
    """
    try:
        base_url, headers = _gitlab_config("gitlab_list_project_releases", config)
        data = _request_json(
            "GET",
            f"{base_url}/projects/{_gitlab_project_id(project)}/releases",
            params={"order_by": order_by.strip() or "released_at", "sort": sort.strip() or "desc", "per_page": _limit(limit)},
            headers=headers,
        )
        releases = data if isinstance(data, list) else []
        return _dump_json([_release_summary(release) for release in releases if isinstance(release, dict)])
    except Exception as e:
        logger.error("gitlab_list_project_releases failed", exc_info=True)
        return f"[Error]: GitLab release list failed: {e}"


@tool
def gitlab_get_project_release(
    project: str,
    tag_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a GitLab project release by tag name.

    Args:
        project: Numeric project ID or namespace path like "group/project".
        tag_name: Release tag name.
    """
    if not tag_name.strip():
        return "[Error]: tag_name is required."
    try:
        base_url, headers = _gitlab_config("gitlab_get_project_release", config)
        data = _request_json(
            "GET",
            f"{base_url}/projects/{_gitlab_project_id(project)}/releases/{quote(tag_name.strip(), safe='')}",
            headers=headers,
        )
        return _dump_json(_release_summary(data))
    except Exception as e:
        logger.error("gitlab_get_project_release failed", exc_info=True)
        return f"[Error]: GitLab release lookup failed: {e}"


@tool
def gitlab_list_user_projects(
    user_id: int,
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List GitLab projects owned by a user ID.

    Args:
        user_id: Numeric GitLab user ID.
        limit: Number of projects to return, 1-100.
    """
    try:
        base_url, headers = _gitlab_config("gitlab_list_user_projects", config)
        data = _request_json(
            "GET",
            f"{base_url}/users/{int(user_id)}/projects",
            params={"per_page": _limit(limit)},
            headers=headers,
        )
        projects = data if isinstance(data, list) else []
        return _dump_json([_gitlab_project_summary(project) for project in projects if isinstance(project, dict)])
    except Exception as e:
        logger.error("gitlab_list_user_projects failed", exc_info=True)
        return f"[Error]: GitLab user projects lookup failed: {e}"


@tool
def graphql_execute_query(
    query: str,
    variables_json: str = "{}",
    operation_name: str = "",
    endpoint: str = "",
    headers_json: str = "{}",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Execute a GraphQL query or mutation over HTTP POST.

    Args:
        query: GraphQL query or mutation text.
        variables_json: Optional GraphQL variables as a JSON object string.
        operation_name: Optional GraphQL operation name.
        endpoint: Optional absolute GraphQL endpoint URL. If omitted, uses a saved GraphQL connection or GRAPHQL_ENDPOINT.
        headers_json: Optional non-secret HTTP headers as a JSON object string. Prefer saved credentials for auth.
    """
    if not query.strip():
        return "[Error]: query is required."
    try:
        resolved_endpoint, endpoint_from_vault = _graphql_endpoint(
            "graphql_execute_query", endpoint, config
        )
        if not resolved_endpoint:
            return (
                "[Error]: endpoint is required. Pass endpoint, save a GraphQL credential "
                "with endpoint/url, or set GRAPHQL_ENDPOINT."
            )
        resolved_endpoint = _require_absolute_base_url(resolved_endpoint)
        variables = _json_object(variables_json, field_name="variables_json")
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        if operation_name.strip():
            body["operationName"] = operation_name.strip()
        data = _request_json_body(
            "POST",
            resolved_endpoint,
            json_body=body,
            headers=_graphql_headers(
                tool_name="graphql_execute_query",
                headers_json=headers_json,
                config=config,
                endpoint_from_vault=endpoint_from_vault,
            ),
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("graphql_execute_query failed", exc_info=True)
        return f"[Error]: GraphQL request failed: {e}"


DEVELOPER_PLATFORM_TOOLS = [
    github_get_repository,
    github_search_repositories,
    github_list_issues,
    github_get_issue,
    github_list_pull_requests,
    github_list_releases,
    github_get_release,
    gitlab_get_project,
    gitlab_search_projects,
    gitlab_list_project_issues,
    gitlab_get_project_issue,
    gitlab_list_project_releases,
    gitlab_get_project_release,
    gitlab_list_user_projects,
    graphql_execute_query,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="developer_platform", tools=tuple(DEVELOPER_PLATFORM_TOOLS)))
