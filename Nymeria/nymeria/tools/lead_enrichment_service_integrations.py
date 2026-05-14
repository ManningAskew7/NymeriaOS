"""Lead enrichment and contact intelligence service tools."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated, Any, Optional
from urllib.parse import urlparse

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 45.0
_MAX_JSON_CHARS = 70_000
_CLEARBIT_COMPANY_BASE_URL = "https://company-stream.clearbit.com"
_CLEARBIT_PERSON_BASE_URL = "https://person-stream.clearbit.com"
_CLEARBIT_AUTOCOMPLETE_BASE_URL = "https://autocomplete.clearbit.com"
_UPLEAD_BASE_URL = "https://api.uplead.com/v2"
_DROPCONTACT_BASE_URL = "https://api.dropcontact.io"
_HUMANTIC_BASE_URL = "https://api.humantic.ai/v1"
_LONESCALE_BASE_URL = "https://public-api.lonescale.com"
_UPROC_BASE_URL = "https://api.uproc.io/api/v2"


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _filtered(values: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (values or {}).items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.strip().rstrip("/")


def _json_object(value: str, *, field_name: str) -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _settings_value(name: str) -> Optional[str]:
    from ..config import get_settings

    return getattr(get_settings(), name)


def _credential_value(
    *,
    provider: str,
    field_names: tuple[str, ...],
    tool_name: str,
    config: Optional[RunnableConfig],
    provider_aliases: tuple[str, ...] = (),
) -> Optional[str]:
    from .native_credentials import get_native_credential_value

    credential = get_native_credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    )
    return credential.value if credential else None


def _setup_hint(
    *,
    provider: str,
    field_names: tuple[str, ...],
    tool_name: str,
    env_var: str,
    display_name: str,
) -> str:
    from .native_credentials import native_credential_setup_hint

    return native_credential_setup_hint(
        provider=provider,
        field_names=field_names,
        tool_name=tool_name,
        env_var=env_var,
        display_name=display_name,
    )


def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    import httpx

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.request(
                method,
                url,
                params=_filtered(params),
                json=json_body,
                headers=headers,
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
                    "text": response.text[:2000],
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
                    or body.get("description")
                    or body.get("reason")
                    or body.get("detail")
                    or ""
                )
        except Exception:
            detail = e.response.text[:300]
        raise RuntimeError(f"HTTP {e.response.status_code}: {detail}".strip()) from e


def _api_key(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    settings_name: str,
    env_var: str,
    tool_name: str,
    display_name: str,
    config: Optional[RunnableConfig],
    field_names: tuple[str, ...] = ("api_key", "apiKey", "token", "access_token", "value"),
) -> str | None:
    value = _credential_value(
        provider=provider,
        provider_aliases=provider_aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    ) or _settings_value(settings_name)
    if not value:
        return _setup_hint(
            provider=provider,
            field_names=field_names,
            tool_name=tool_name,
            env_var=env_var,
            display_name=display_name,
        )
    return value


def _configured_base(
    *,
    provider: str,
    provider_aliases: tuple[str, ...],
    field_names: tuple[str, ...],
    settings_name: str,
    default_base: str,
    tool_name: str,
    config: Optional[RunnableConfig],
) -> str:
    return _base_url(
        _credential_value(
            provider=provider,
            provider_aliases=provider_aliases,
            field_names=field_names,
            tool_name=tool_name,
            config=config,
        )
        or _settings_value(settings_name)
        or default_base
    )


def _clearbit_headers(tool_name: str, config: Optional[RunnableConfig]) -> dict[str, str] | str:
    api_key = _api_key(
        provider="clearbit",
        provider_aliases=("clearbit_api", "clearbitApi"),
        settings_name="clearbit_api_key",
        env_var="CLEARBIT_API_KEY",
        tool_name=tool_name,
        display_name="Clearbit",
        config=config,
    )
    if api_key and api_key.startswith("[Error]:"):
        return api_key
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _clearbit_base(kind: str, tool_name: str, config: Optional[RunnableConfig]) -> str:
    if kind == "person":
        settings_name = "clearbit_person_base_url"
        default = _CLEARBIT_PERSON_BASE_URL
    elif kind == "autocomplete":
        settings_name = "clearbit_autocomplete_base_url"
        default = _CLEARBIT_AUTOCOMPLETE_BASE_URL
    else:
        settings_name = "clearbit_company_base_url"
        default = _CLEARBIT_COMPANY_BASE_URL
    return _configured_base(
        provider="clearbit",
        provider_aliases=("clearbit_api", "clearbitApi"),
        field_names=(settings_name, "base_url", "baseUrl", "url"),
        settings_name=settings_name,
        default_base=default,
        tool_name=tool_name,
        config=config,
    )


def _uplead_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    api_key = _api_key(
        provider="uplead",
        provider_aliases=("uplead_api", "upleadApi"),
        settings_name="uplead_api_key",
        env_var="UPLEAD_API_KEY",
        tool_name=tool_name,
        display_name="Uplead",
        config=config,
    )
    base = _configured_base(
        provider="uplead",
        provider_aliases=("uplead_api", "upleadApi"),
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
        settings_name="uplead_base_url",
        default_base=_UPLEAD_BASE_URL,
        tool_name=tool_name,
        config=config,
    )
    if api_key and api_key.startswith("[Error]:"):
        return base, api_key
    return base, {
        "Accept": "application/json",
        "Authorization": str(api_key),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


def _dropcontact_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    api_key = _api_key(
        provider="dropcontact",
        provider_aliases=("dropcontact_api", "dropcontactApi"),
        settings_name="dropcontact_api_key",
        env_var="DROPCONTACT_API_KEY",
        tool_name=tool_name,
        display_name="Dropcontact",
        config=config,
    )
    base = _configured_base(
        provider="dropcontact",
        provider_aliases=("dropcontact_api", "dropcontactApi"),
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
        settings_name="dropcontact_base_url",
        default_base=_DROPCONTACT_BASE_URL,
        tool_name=tool_name,
        config=config,
    )
    if api_key and api_key.startswith("[Error]:"):
        return base, api_key
    return base, {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "X-Access-Token": str(api_key),
    }


def _humantic_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str, str | None]:
    api_key = _api_key(
        provider="humantic",
        provider_aliases=("humantic_ai", "humantic_ai_api", "humanticAiApi"),
        settings_name="humantic_api_key",
        env_var="HUMANTIC_API_KEY",
        tool_name=tool_name,
        display_name="Humantic AI",
        config=config,
    )
    base = _configured_base(
        provider="humantic",
        provider_aliases=("humantic_ai", "humantic_ai_api", "humanticAiApi"),
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
        settings_name="humantic_base_url",
        default_base=_HUMANTIC_BASE_URL,
        tool_name=tool_name,
        config=config,
    )
    if api_key and api_key.startswith("[Error]:"):
        return base, api_key, None
    return base, {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Nymeria"}, str(api_key)


def _lonescale_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    api_key = _api_key(
        provider="lonescale",
        provider_aliases=("lone_scale", "lonescale_api", "loneScaleApi"),
        settings_name="lonescale_api_key",
        env_var="LONESCALE_API_KEY",
        tool_name=tool_name,
        display_name="LoneScale",
        config=config,
    )
    base = _configured_base(
        provider="lonescale",
        provider_aliases=("lone_scale", "lonescale_api", "loneScaleApi"),
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
        settings_name="lonescale_base_url",
        default_base=_LONESCALE_BASE_URL,
        tool_name=tool_name,
        config=config,
    )
    if api_key and api_key.startswith("[Error]:"):
        return base, api_key
    return base, {
        "Accept": "application/json",
        "Authorization": str(api_key),
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
        "X-API-KEY": str(api_key),
    }


def _uproc_config(tool_name: str, config: Optional[RunnableConfig]) -> tuple[str, dict[str, str] | str]:
    email = _credential_value(
        provider="uproc",
        provider_aliases=("uproc_api", "uProcApi"),
        field_names=("email", "username", "user"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("uproc_email")
    api_key = _credential_value(
        provider="uproc",
        provider_aliases=("uproc_api", "uProcApi"),
        field_names=("api_key", "apiKey", "token", "value"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("uproc_api_key")
    base = _configured_base(
        provider="uproc",
        provider_aliases=("uproc_api", "uProcApi"),
        field_names=("base_url", "baseUrl", "api_url", "apiUrl", "url"),
        settings_name="uproc_base_url",
        default_base=_UPROC_BASE_URL,
        tool_name=tool_name,
        config=config,
    )
    if not email:
        return base, _setup_hint(
            provider="uproc",
            field_names=("email",),
            tool_name=tool_name,
            env_var="UPROC_EMAIL",
            display_name="uProc",
        )
    if not api_key:
        return base, _setup_hint(
            provider="uproc",
            field_names=("api_key", "token", "value"),
            tool_name=tool_name,
            env_var="UPROC_API_KEY",
            display_name="uProc",
        )
    token = base64.b64encode(f"{email}:{api_key}".encode("utf-8")).decode("ascii")
    return base, {
        "Accept": "application/json",
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "User-Agent": "Nymeria",
    }


@tool
def clearbit_enrich_company(
    domain: str,
    company_name: str = "",
    linkedin: str = "",
    twitter: str = "",
    facebook: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Enrich company data from a domain and optional social/company hints.

    Args:
        domain: Company domain to enrich.
        company_name: Optional company name hint.
        linkedin: Optional LinkedIn URL hint.
        twitter: Optional Twitter/X handle hint.
        facebook: Optional Facebook URL hint.
    """
    if not domain.strip():
        return "[Error]: domain is required."
    try:
        headers = _clearbit_headers("clearbit_enrich_company", config)
        if isinstance(headers, str):
            return headers
        base = _clearbit_base("company", "clearbit_enrich_company", config)
        data = _request_json(
            "GET",
            f"{base}/v2/companies/find",
            params={
                "domain": domain.strip(),
                "company_name": company_name,
                "linkedin": linkedin,
                "twitter": twitter,
                "facebook": facebook,
            },
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("clearbit_enrich_company failed", exc_info=True)
        return f"[Error]: Clearbit company enrichment failed: {e}"


@tool
def clearbit_autocomplete_company(
    name: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Autocomplete company names and return likely domains/logos.

    Args:
        name: Partial company name.
    """
    if not name.strip():
        return "[Error]: name is required."
    try:
        headers = _clearbit_headers("clearbit_autocomplete_company", config)
        if isinstance(headers, str):
            return headers
        base = _clearbit_base("autocomplete", "clearbit_autocomplete_company", config)
        return _dump_json(_request_json("GET", f"{base}/v1/companies/suggest", params={"query": name.strip()}, headers=headers))
    except Exception as e:
        logger.error("clearbit_autocomplete_company failed", exc_info=True)
        return f"[Error]: Clearbit company autocomplete failed: {e}"


@tool
def clearbit_enrich_person(
    email: str,
    given_name: str = "",
    family_name: str = "",
    ip_address: str = "",
    location: str = "",
    company: str = "",
    company_domain: str = "",
    linkedin: str = "",
    twitter: str = "",
    facebook: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Enrich person and company data from an email address and optional hints.

    Args:
        email: Email address to enrich.
        given_name: Optional first-name hint.
        family_name: Optional last-name hint.
        ip_address: Optional IP address hint.
        location: Optional location hint.
        company: Optional company name hint.
        company_domain: Optional company domain hint.
        linkedin: Optional LinkedIn URL hint.
        twitter: Optional Twitter/X handle hint.
        facebook: Optional Facebook URL hint.
    """
    if not email.strip():
        return "[Error]: email is required."
    try:
        headers = _clearbit_headers("clearbit_enrich_person", config)
        if isinstance(headers, str):
            return headers
        base = _clearbit_base("person", "clearbit_enrich_person", config)
        data = _request_json(
            "GET",
            f"{base}/v2/people/find",
            params={
                "email": email.strip(),
                "given_name": given_name,
                "family_name": family_name,
                "ip_address": ip_address,
                "location": location,
                "company": company,
                "company_domain": company_domain,
                "linkedin": linkedin,
                "twitter": twitter,
                "facebook": facebook,
            },
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("clearbit_enrich_person failed", exc_info=True)
        return f"[Error]: Clearbit person enrichment failed: {e}"


@tool
def uplead_enrich_company(
    domain: str = "",
    company: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Enrich company data by domain or company name.

    Args:
        domain: Company domain.
        company: Company name.
    """
    if not domain.strip() and not company.strip():
        return "[Error]: domain or company is required."
    try:
        base, headers = _uplead_config("uplead_enrich_company", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "GET",
            f"{base}/company-search",
            params={"domain": domain.strip(), "company": company.strip()},
            headers=headers,
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("uplead_enrich_company failed", exc_info=True)
        return f"[Error]: Uplead company enrichment failed: {e}"


@tool
def uplead_enrich_person(
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    domain: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Enrich person data by email or first name, last name, and domain.

    Args:
        email: Email address.
        first_name: First name.
        last_name: Last name.
        domain: Company domain.
    """
    if not email.strip() and not (first_name.strip() and last_name.strip() and domain.strip()):
        return "[Error]: email or first_name + last_name + domain is required."
    try:
        base, headers = _uplead_config("uplead_enrich_person", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "GET",
            f"{base}/person-search",
            params={
                "email": email.strip(),
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
                "domain": domain.strip(),
            },
            headers=headers,
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("uplead_enrich_person failed", exc_info=True)
        return f"[Error]: Uplead person enrichment failed: {e}"


@tool
def dropcontact_submit_enrichment(
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    full_name: str = "",
    company: str = "",
    website: str = "",
    linkedin: str = "",
    phone: str = "",
    country: str = "",
    french_company_enrich: bool = False,
    language: str = "en",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Submit one contact enrichment request to Dropcontact.

    Args:
        email: Contact email address.
        first_name: Contact first name.
        last_name: Contact last name.
        full_name: Contact full name.
        company: Company name.
        website: Company website.
        linkedin: LinkedIn profile URL.
        phone: Phone number.
        country: Country hint.
        french_company_enrich: Whether to request French company enrichment.
        language: Response language, "en" or "fr".
    """
    body = _filtered(
        {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "company": company,
            "website": website,
            "linkedin": linkedin,
            "phone": phone,
            "country": country,
        }
    )
    if not body:
        return "[Error]: at least one contact field is required."
    try:
        base, headers = _dropcontact_config("dropcontact_submit_enrichment", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "POST",
            f"{base}/batch",
            json_body={
                "data": [body],
                "siren": bool(french_company_enrich),
                "language": language if language in {"en", "fr"} else "en",
            },
            headers=headers,
        )
        return _dump_json(data)
    except Exception as e:
        logger.error("dropcontact_submit_enrichment failed", exc_info=True)
        return f"[Error]: Dropcontact enrichment submit failed: {e}"


@tool
def dropcontact_fetch_request(
    request_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Fetch a completed Dropcontact request by request ID.

    Args:
        request_id: Dropcontact request ID returned by submit.
    """
    if not request_id.strip():
        return "[Error]: request_id is required."
    try:
        base, headers = _dropcontact_config("dropcontact_fetch_request", config)
        if isinstance(headers, str):
            return headers
        return _dump_json(_request_json("GET", f"{base}/batch/{request_id.strip()}", headers=headers))
    except Exception as e:
        logger.error("dropcontact_fetch_request failed", exc_info=True)
        return f"[Error]: Dropcontact request fetch failed: {e}"


@tool
def humantic_create_profile(
    user_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a Humantic AI profile from a LinkedIn URL, email, or unique user ID.

    Args:
        user_id: LinkedIn URL, email, or unique user ID.
    """
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        base, headers, api_key = _humantic_config("humantic_create_profile", config)
        if isinstance(headers, str):
            return headers
        data = _request_json("GET", f"{base}/user-profile/create", params={"userid": user_id.strip(), "apikey": api_key}, headers=headers)
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("humantic_create_profile failed", exc_info=True)
        return f"[Error]: Humantic AI profile creation failed: {e}"


@tool
def humantic_get_profile(
    user_id: str,
    persona: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get a Humantic AI profile.

    Args:
        user_id: LinkedIn URL, email, or unique user ID used when creating the profile.
        persona: Optional comma-separated persona list, e.g. "sales,hiring".
    """
    if not user_id.strip():
        return "[Error]: user_id is required."
    try:
        base, headers, api_key = _humantic_config("humantic_get_profile", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "GET",
            f"{base}/user-profile",
            params={"userid": user_id.strip(), "persona": persona, "apikey": api_key},
            headers=headers,
        )
        return _dump_json(data.get("results", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("humantic_get_profile failed", exc_info=True)
        return f"[Error]: Humantic AI profile lookup failed: {e}"


@tool
def humantic_update_profile_text(
    user_id: str,
    text: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update a Humantic AI profile with additional text.

    Args:
        user_id: LinkedIn URL, email, or unique user ID used when creating the profile.
        text: Additional profile text to analyze.
    """
    if not user_id.strip():
        return "[Error]: user_id is required."
    if not text.strip():
        return "[Error]: text is required."
    try:
        base, headers, api_key = _humantic_config("humantic_update_profile_text", config)
        if isinstance(headers, str):
            return headers
        data = _request_json(
            "POST",
            f"{base}/user-profile/create",
            params={"userid": user_id.strip(), "apikey": api_key},
            json_body={"text": text},
            headers=headers,
        )
        return _dump_json(data.get("data", data) if isinstance(data, dict) else data)
    except Exception as e:
        logger.error("humantic_update_profile_text failed", exc_info=True)
        return f"[Error]: Humantic AI profile update failed: {e}"


@tool
def lonescale_create_list(
    name: str,
    entity_type: str = "COMPANY",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create a LoneScale list.

    Args:
        name: List name.
        entity_type: "COMPANY" or "PEOPLE".
    """
    if not name.strip():
        return "[Error]: name is required."
    entity = entity_type.strip().upper()
    if entity not in {"COMPANY", "PEOPLE"}:
        return '[Error]: entity_type must be "COMPANY" or "PEOPLE".'
    try:
        base, headers = _lonescale_config("lonescale_create_list", config)
        if isinstance(headers, str):
            return headers
        return _dump_json(_request_json("POST", f"{base}/lists", json_body={"name": name.strip(), "entity": entity}, headers=headers))
    except Exception as e:
        logger.error("lonescale_create_list failed", exc_info=True)
        return f"[Error]: LoneScale list creation failed: {e}"


@tool
def lonescale_add_people_item(
    list_id: str,
    first_name: str,
    last_name: str,
    email: str = "",
    full_name: str = "",
    company_name: str = "",
    current_position: str = "",
    domain: str = "",
    linkedin_url: str = "",
    location: str = "",
    contact_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a person item to a LoneScale list.

    Args:
        list_id: LoneScale list ID.
        first_name: Contact first name.
        last_name: Contact last name.
        email: Contact email.
        full_name: Contact full name.
        company_name: Company name.
        current_position: Contact role/title.
        domain: Company domain.
        linkedin_url: LinkedIn profile URL.
        location: Contact location.
        contact_id: External contact ID.
    """
    if not list_id.strip() or not first_name.strip() or not last_name.strip():
        return "[Error]: list_id, first_name, and last_name are required."
    try:
        base, headers = _lonescale_config("lonescale_add_people_item", config)
        if isinstance(headers, str):
            return headers
        body = _filtered(
            {
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
                "full_name": full_name,
                "company_name": company_name,
                "current_position": current_position,
                "domain": domain,
                "linkedin_url": linkedin_url,
                "location": location,
                "contact_id": contact_id,
            }
        )
        return _dump_json(_request_json("POST", f"{base}/lists/{list_id.strip()}/item", json_body=body, headers=headers))
    except Exception as e:
        logger.error("lonescale_add_people_item failed", exc_info=True)
        return f"[Error]: LoneScale people item creation failed: {e}"


@tool
def lonescale_add_company_item(
    list_id: str,
    company_name: str,
    domain: str = "",
    linkedin_url: str = "",
    location: str = "",
    contact_id: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Add a company item to a LoneScale list.

    Args:
        list_id: LoneScale list ID.
        company_name: Company name.
        domain: Company domain.
        linkedin_url: LinkedIn company URL.
        location: Company location.
        contact_id: External contact/company ID.
    """
    if not list_id.strip() or not company_name.strip():
        return "[Error]: list_id and company_name are required."
    try:
        base, headers = _lonescale_config("lonescale_add_company_item", config)
        if isinstance(headers, str):
            return headers
        body = _filtered(
            {
                "company_name": company_name,
                "domain": domain,
                "linkedin_url": linkedin_url,
                "location": location,
                "contact_id": contact_id,
            }
        )
        return _dump_json(_request_json("POST", f"{base}/lists/{list_id.strip()}/item", json_body=body, headers=headers))
    except Exception as e:
        logger.error("lonescale_add_company_item failed", exc_info=True)
        return f"[Error]: LoneScale company item creation failed: {e}"


@tool
def uproc_get_profile(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:
    """Get the uProc account profile for the saved credential."""
    try:
        base, headers = _uproc_config("uproc_get_profile", config)
        if isinstance(headers, str):
            return headers
        return _dump_json(_request_json("GET", f"{base}/profile", headers=headers))
    except Exception as e:
        logger.error("uproc_get_profile failed", exc_info=True)
        return f"[Error]: uProc profile lookup failed: {e}"


@tool
def uproc_process(
    processor: str,
    params_json: str,
    callback_url: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Run a uProc processor with explicit JSON parameters.

    Args:
        processor: uProc processor key, such as an email, company, URL, or text processor.
        params_json: JSON object of processor parameters.
        callback_url: Optional callback URL for asynchronous data delivery.
    """
    if not processor.strip():
        return "[Error]: processor is required."
    try:
        params = _json_object(params_json, field_name="params_json")
        base, headers = _uproc_config("uproc_process", config)
        if isinstance(headers, str):
            return headers
        body: dict[str, Any] = {"processor": processor.strip(), "params": params}
        if callback_url.strip():
            body["callback"] = {"data": callback_url.strip()}
        return _dump_json(_request_json("POST", f"{base}/process", json_body=body, headers=headers))
    except Exception as e:
        logger.error("uproc_process failed", exc_info=True)
        return f"[Error]: uProc processor request failed: {e}"


LEAD_ENRICHMENT_SERVICE_TOOLS = [
    clearbit_enrich_company,
    clearbit_autocomplete_company,
    clearbit_enrich_person,
    uplead_enrich_company,
    uplead_enrich_person,
    dropcontact_submit_enrichment,
    dropcontact_fetch_request,
    humantic_create_profile,
    humantic_get_profile,
    humantic_update_profile_text,
    lonescale_create_list,
    lonescale_add_people_item,
    lonescale_add_company_item,
    uproc_get_profile,
    uproc_process,
]
