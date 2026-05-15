"""Global settings and model-catalog routes."""

import logging
import os
from collections.abc import Callable
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...config.llm_providers import (
    is_openai_compatible_provider,
    list_llm_provider_specs,
    normalize_llm_provider,
    provider_requires_api_key,
    provider_supports_responses,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from ...config.settings import get_env_file_paths, get_env_write_path
from ...config.model_capabilities import (
    get_max_output_tokens,
    list_all_models,
    register_model_metadata,
)
from ...core.accounts import AuthenticatedUser
from ...core.llm_credentials import get_llm_provider_credential
from ...core.llm_provider_test_suite import (
    ProviderTestSuiteOptions,
    run_provider_test_suite,
)
from ...core.llm_provider_utils import (
    base_url_allows_no_api_key,
    extract_model_metadata,
    http_error_detail,
    redact_secrets,
)
from ...vendor.react_agent.cliproxy import looks_like_cliproxy_url
from ..schemas.settings import (
    HIDDEN_CONFIG_SETTINGS,
    LLMProviderSpecResponse,
    LLMProviderTestRequest,
    LLMProviderTestResponse,
    LLMProviderTestSuiteRequest,
    LLMProviderTestSuiteResponse,
    LLMRuntimeDiagnosticsResponse,
    OpenRouterKeyDiagnostics,
    ServerSettingsResponse,
    ServerSettingsUpdate,
)

logger = logging.getLogger(__name__)


def _env_mapping() -> dict[str, str]:
    """Return settings-field to environment-variable mapping for PATCH /settings."""
    return {
        "llm_provider": "LLM_PROVIDER",
        "llm_model": "LLM_MODEL",
        "llm_fast_model": "LLM_FAST_MODEL",
        "llm_fallback_models": "LLM_FALLBACK_MODELS",
        "llm_temperature": "LLM_TEMPERATURE",
        "llm_max_tokens": "LLM_MAX_TOKENS",
        "llm_top_p": "LLM_TOP_P",
        "llm_top_k": "LLM_TOP_K",
        "llm_frequency_penalty": "LLM_FREQUENCY_PENALTY",
        "llm_presence_penalty": "LLM_PRESENCE_PENALTY",
        "llm_reasoning_effort": "LLM_REASONING_EFFORT",
        "llm_extended_thinking": "LLM_EXTENDED_THINKING",
        "llm_use_model_defaults": "LLM_USE_MODEL_DEFAULTS",
        "llm_base_url": "LLM_BASE_URL",
        "openai_api_mode": "OPENAI_API_MODE",
        "llm_stream_max_retries": "LLM_STREAM_MAX_RETRIES",
        "llm_stream_retry_initial_delay": "LLM_STREAM_RETRY_INITIAL_DELAY",
        "llm_stream_retry_max_delay": "LLM_STREAM_RETRY_MAX_DELAY",
        "context_management": "CONTEXT_MANAGEMENT",
        "compact_threshold": "COMPACT_THRESHOLD",
        "compact_keep_messages": "COMPACT_KEEP_MESSAGES",
        "compact_model": "COMPACT_MODEL",
        "sliding_window_cycles": "SLIDING_WINDOW_CYCLES",
        "tool_output_max_chars": "TOOL_OUTPUT_MAX_CHARS",
        "log_level": "LOG_LEVEL",
        "watchdog_enabled": "WATCHDOG_ENABLED",
        "watchdog_interval_minutes": "WATCHDOG_INTERVAL_MINUTES",
        "todo_staleness_minutes": "TODO_STALENESS_MINUTES",
        "activity_retention_hours": "ACTIVITY_RETENTION_HOURS",
        "tts_provider": "TTS_PROVIDER",
        "tts_base_url": "TTS_BASE_URL",
        "tts_model": "TTS_MODEL",
        "tts_voice": "TTS_VOICE",
        "tts_output_format": "TTS_OUTPUT_FORMAT",
        "tts_speed": "TTS_SPEED",
        "stt_provider": "STT_PROVIDER",
        "stt_base_url": "STT_BASE_URL",
        "stt_model": "STT_MODEL",
        "stt_language": "STT_LANGUAGE",
        "voice_default_thread_id": "VOICE_DEFAULT_THREAD_ID",
        "perplexity_api_key": "PERPLEXITY_API_KEY",
        "perplexity_search_model": "PERPLEXITY_SEARCH_MODEL",
        "wolfram_alpha_app_id": "WOLFRAM_ALPHA_APP_ID",
        "searxng_base_url": "SEARXNG_BASE_URL",
        "nasa_api_key": "NASA_API_KEY",
        "openweathermap_api_key": "OPENWEATHERMAP_API_KEY",
        "npm_registry_url": "NPM_REGISTRY_URL",
        "graphql_endpoint": "GRAPHQL_ENDPOINT",
        "graphql_bearer_token": "GRAPHQL_BEARER_TOKEN",
        "graphql_api_key": "GRAPHQL_API_KEY",
        "graphql_api_key_header": "GRAPHQL_API_KEY_HEADER",
        "graphql_headers_json": "GRAPHQL_HEADERS_JSON",
        "totp_secret": "TOTP_SECRET",
        "crypto_hmac_secret": "CRYPTO_HMAC_SECRET",
        "crypto_sign_private_key": "CRYPTO_SIGN_PRIVATE_KEY",
        "crypto_sign_private_key_passphrase": "CRYPTO_SIGN_PRIVATE_KEY_PASSPHRASE",
        "jwt_secret": "JWT_SECRET",
        "jwt_private_key": "JWT_PRIVATE_KEY",
        "jwt_public_key": "JWT_PUBLIC_KEY",
        "jwt_algorithm": "JWT_ALGORITHM",
        "github_token": "GITHUB_TOKEN",
        "github_api_base_url": "GITHUB_API_BASE_URL",
        "gitlab_token": "GITLAB_TOKEN",
        "gitlab_base_url": "GITLAB_BASE_URL",
        "circleci_api_token": "CIRCLECI_API_TOKEN",
        "circleci_base_url": "CIRCLECI_BASE_URL",
        "travisci_api_token": "TRAVISCI_API_TOKEN",
        "travisci_base_url": "TRAVISCI_BASE_URL",
        "jenkins_base_url": "JENKINS_BASE_URL",
        "jenkins_username": "JENKINS_USERNAME",
        "jenkins_api_token": "JENKINS_API_TOKEN",
        "dropbox_access_token": "DROPBOX_ACCESS_TOKEN",
        "dropbox_api_base_url": "DROPBOX_API_BASE_URL",
        "dropbox_content_base_url": "DROPBOX_CONTENT_BASE_URL",
        "nextcloud_webdav_url": "NEXTCLOUD_WEBDAV_URL",
        "nextcloud_username": "NEXTCLOUD_USERNAME",
        "nextcloud_password": "NEXTCLOUD_PASSWORD",
        "nextcloud_access_token": "NEXTCLOUD_ACCESS_TOKEN",
        "s3_access_key_id": "AWS_ACCESS_KEY_ID",
        "s3_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "s3_session_token": "AWS_SESSION_TOKEN",
        "s3_region": "AWS_REGION",
        "s3_endpoint_url": "AWS_ENDPOINT_URL_S3",
        "s3_force_path_style": "S3_FORCE_PATH_STYLE",
        "clearbit_api_key": "CLEARBIT_API_KEY",
        "clearbit_company_base_url": "CLEARBIT_COMPANY_BASE_URL",
        "clearbit_person_base_url": "CLEARBIT_PERSON_BASE_URL",
        "clearbit_autocomplete_base_url": "CLEARBIT_AUTOCOMPLETE_BASE_URL",
        "uplead_api_key": "UPLEAD_API_KEY",
        "uplead_base_url": "UPLEAD_BASE_URL",
        "dropcontact_api_key": "DROPCONTACT_API_KEY",
        "dropcontact_base_url": "DROPCONTACT_BASE_URL",
        "humantic_api_key": "HUMANTIC_API_KEY",
        "humantic_base_url": "HUMANTIC_BASE_URL",
        "lonescale_api_key": "LONESCALE_API_KEY",
        "lonescale_base_url": "LONESCALE_BASE_URL",
        "uproc_email": "UPROC_EMAIL",
        "uproc_api_key": "UPROC_API_KEY",
        "uproc_base_url": "UPROC_BASE_URL",
        "bitly_token": "BITLY_TOKEN",
        "bitly_base_url": "BITLY_BASE_URL",
        "brandfetch_api_key": "BRANDFETCH_API_KEY",
        "brandfetch_base_url": "BRANDFETCH_BASE_URL",
        "marketstack_api_key": "MARKETSTACK_API_KEY",
        "marketstack_base_url": "MARKETSTACK_BASE_URL",
        "deepl_api_key": "DEEPL_API_KEY",
        "deepl_api_plan": "DEEPL_API_PLAN",
        "deepl_base_url": "DEEPL_BASE_URL",
        "lingvanex_api_key": "LINGVANEX_API_KEY",
        "lingvanex_base_url": "LINGVANEX_BASE_URL",
        "apitemplate_api_key": "APITEMPLATE_API_KEY",
        "apitemplate_base_url": "APITEMPLATE_BASE_URL",
        "onesimple_api_token": "ONESIMPLE_API_TOKEN",
        "onesimple_base_url": "ONESIMPLE_BASE_URL",
        "dhl_api_key": "DHL_API_KEY",
        "dhl_base_url": "DHL_BASE_URL",
        "onfleet_api_key": "ONFLEET_API_KEY",
        "onfleet_base_url": "ONFLEET_BASE_URL",
        "phantombuster_api_key": "PHANTOMBUSTER_API_KEY",
        "phantombuster_base_url": "PHANTOMBUSTER_BASE_URL",
        "erpnext_api_key": "ERPNEXT_API_KEY",
        "erpnext_api_secret": "ERPNEXT_API_SECRET",
        "erpnext_base_url": "ERPNEXT_BASE_URL",
        "erpnext_subdomain": "ERPNEXT_SUBDOMAIN",
        "erpnext_cloud_domain": "ERPNEXT_CLOUD_DOMAIN",
        "odoo_url": "ODOO_URL",
        "odoo_username": "ODOO_USERNAME",
        "odoo_password": "ODOO_PASSWORD",
        "odoo_database": "ODOO_DATABASE",
        "invoiceninja_api_token": "INVOICENINJA_API_TOKEN",
        "invoiceninja_secret": "INVOICENINJA_SECRET",
        "invoiceninja_base_url": "INVOICENINJA_BASE_URL",
        "invoiceninja_api_version": "INVOICENINJA_API_VERSION",
        "demio_api_key": "DEMIO_API_KEY",
        "demio_api_secret": "DEMIO_API_SECRET",
        "demio_base_url": "DEMIO_BASE_URL",
        "zoom_access_token": "ZOOM_ACCESS_TOKEN",
        "zoom_base_url": "ZOOM_BASE_URL",
        "gotowebinar_access_token": "GOTOWEBINAR_ACCESS_TOKEN",
        "gotowebinar_account_key": "GOTOWEBINAR_ACCOUNT_KEY",
        "gotowebinar_organizer_key": "GOTOWEBINAR_ORGANIZER_KEY",
        "gotowebinar_base_url": "GOTOWEBINAR_BASE_URL",
        "todoist_api_key": "TODOIST_API_KEY",
        "todoist_base_url": "TODOIST_BASE_URL",
        "trello_api_key": "TRELLO_API_KEY",
        "trello_api_token": "TRELLO_API_TOKEN",
        "trello_base_url": "TRELLO_BASE_URL",
        "raindrop_access_token": "RAINDROP_ACCESS_TOKEN",
        "raindrop_base_url": "RAINDROP_BASE_URL",
        "yourls_url": "YOURLS_URL",
        "yourls_signature": "YOURLS_SIGNATURE",
        "yourls_username": "YOURLS_USERNAME",
        "yourls_password": "YOURLS_PASSWORD",
        "asana_access_token": "ASANA_ACCESS_TOKEN",
        "asana_base_url": "ASANA_BASE_URL",
        "linear_api_key": "LINEAR_API_KEY",
        "linear_api_url": "LINEAR_API_URL",
        "jira_email": "JIRA_EMAIL",
        "jira_api_token": "JIRA_API_TOKEN",
        "jira_access_token": "JIRA_ACCESS_TOKEN",
        "jira_base_url": "JIRA_BASE_URL",
        "clickup_access_token": "CLICKUP_ACCESS_TOKEN",
        "clickup_base_url": "CLICKUP_BASE_URL",
        "monday_api_token": "MONDAY_API_TOKEN",
        "monday_api_url": "MONDAY_API_URL",
        "taiga_auth_token": "TAIGA_AUTH_TOKEN",
        "taiga_username": "TAIGA_USERNAME",
        "taiga_password": "TAIGA_PASSWORD",
        "taiga_base_url": "TAIGA_BASE_URL",
        "wekan_base_url": "WEKAN_BASE_URL",
        "wekan_token": "WEKAN_TOKEN",
        "wekan_username": "WEKAN_USERNAME",
        "wekan_password": "WEKAN_PASSWORD",
        "slack_bot_token": "SLACK_BOT_TOKEN",
        "slack_access_token": "SLACK_ACCESS_TOKEN",
        "slack_base_url": "SLACK_BASE_URL",
        "microsoft_graph_access_token": "MICROSOFT_GRAPH_ACCESS_TOKEN",
        "microsoft_graph_base_url": "MICROSOFT_GRAPH_BASE_URL",
        "notion_api_key": "NOTION_API_KEY",
        "notion_version": "NOTION_VERSION",
        "notion_base_url": "NOTION_BASE_URL",
        "airtable_access_token": "AIRTABLE_ACCESS_TOKEN",
        "airtable_api_key": "AIRTABLE_API_KEY",
        "airtable_base_url": "AIRTABLE_BASE_URL",
        "hubspot_access_token": "HUBSPOT_ACCESS_TOKEN",
        "hubspot_base_url": "HUBSPOT_BASE_URL",
        "zendesk_email": "ZENDESK_EMAIL",
        "zendesk_api_token": "ZENDESK_API_TOKEN",
        "zendesk_access_token": "ZENDESK_ACCESS_TOKEN",
        "zendesk_subdomain": "ZENDESK_SUBDOMAIN",
        "zendesk_base_url": "ZENDESK_BASE_URL",
        "mailchimp_api_key": "MAILCHIMP_API_KEY",
        "mailchimp_access_token": "MAILCHIMP_ACCESS_TOKEN",
        "mailchimp_server_prefix": "MAILCHIMP_SERVER_PREFIX",
        "mailchimp_base_url": "MAILCHIMP_BASE_URL",
        "mautic_base_url": "MAUTIC_BASE_URL",
        "mautic_access_token": "MAUTIC_ACCESS_TOKEN",
        "mautic_username": "MAUTIC_USERNAME",
        "mautic_password": "MAUTIC_PASSWORD",
        "freshdesk_api_key": "FRESHDESK_API_KEY",
        "freshdesk_domain": "FRESHDESK_DOMAIN",
        "freshdesk_base_url": "FRESHDESK_BASE_URL",
        "freshservice_api_key": "FRESHSERVICE_API_KEY",
        "freshservice_domain": "FRESHSERVICE_DOMAIN",
        "freshservice_base_url": "FRESHSERVICE_BASE_URL",
        "servicenow_base_url": "SERVICENOW_BASE_URL",
        "servicenow_instance": "SERVICENOW_INSTANCE",
        "servicenow_access_token": "SERVICENOW_ACCESS_TOKEN",
        "servicenow_username": "SERVICENOW_USERNAME",
        "servicenow_password": "SERVICENOW_PASSWORD",
        "zammad_base_url": "ZAMMAD_BASE_URL",
        "zammad_token": "ZAMMAD_TOKEN",
        "zammad_username": "ZAMMAD_USERNAME",
        "zammad_password": "ZAMMAD_PASSWORD",
        "helpscout_access_token": "HELPSCOUT_ACCESS_TOKEN",
        "helpscout_base_url": "HELPSCOUT_BASE_URL",
        "intercom_access_token": "INTERCOM_ACCESS_TOKEN",
        "intercom_base_url": "INTERCOM_BASE_URL",
        "intercom_version": "INTERCOM_VERSION",
        "drift_access_token": "DRIFT_ACCESS_TOKEN",
        "drift_base_url": "DRIFT_BASE_URL",
        "salesforce_instance_url": "SALESFORCE_INSTANCE_URL",
        "salesforce_access_token": "SALESFORCE_ACCESS_TOKEN",
        "salesforce_base_url": "SALESFORCE_BASE_URL",
        "salesforce_api_version": "SALESFORCE_API_VERSION",
        "zoho_crm_access_token": "ZOHO_CRM_ACCESS_TOKEN",
        "zoho_crm_api_domain": "ZOHO_CRM_API_DOMAIN",
        "zoho_crm_base_url": "ZOHO_CRM_BASE_URL",
        "freshworks_crm_api_key": "FRESHWORKS_CRM_API_KEY",
        "freshworks_crm_domain": "FRESHWORKS_CRM_DOMAIN",
        "freshworks_crm_base_url": "FRESHWORKS_CRM_BASE_URL",
        "salesmate_session_token": "SALESMATE_SESSION_TOKEN",
        "salesmate_link_name": "SALESMATE_LINK_NAME",
        "salesmate_base_url": "SALESMATE_BASE_URL",
        "pipedrive_api_token": "PIPEDRIVE_API_TOKEN",
        "pipedrive_access_token": "PIPEDRIVE_ACCESS_TOKEN",
        "pipedrive_base_url": "PIPEDRIVE_BASE_URL",
        "copper_api_key": "COPPER_API_KEY",
        "copper_email": "COPPER_EMAIL",
        "copper_base_url": "COPPER_BASE_URL",
        "agilecrm_email": "AGILECRM_EMAIL",
        "agilecrm_api_key": "AGILECRM_API_KEY",
        "agilecrm_subdomain": "AGILECRM_SUBDOMAIN",
        "agilecrm_base_url": "AGILECRM_BASE_URL",
        "monica_access_token": "MONICA_ACCESS_TOKEN",
        "monica_base_url": "MONICA_BASE_URL",
        "affinity_api_key": "AFFINITY_API_KEY",
        "affinity_base_url": "AFFINITY_BASE_URL",
        "keap_access_token": "KEAP_ACCESS_TOKEN",
        "keap_base_url": "KEAP_BASE_URL",
        "twilio_account_sid": "TWILIO_ACCOUNT_SID",
        "twilio_auth_token": "TWILIO_AUTH_TOKEN",
        "twilio_api_key_sid": "TWILIO_API_KEY_SID",
        "twilio_base_url": "TWILIO_BASE_URL",
        "sendgrid_api_key": "SENDGRID_API_KEY",
        "sendgrid_base_url": "SENDGRID_BASE_URL",
        "mailgun_api_key": "MAILGUN_API_KEY",
        "mailgun_domain": "MAILGUN_DOMAIN",
        "mailgun_base_url": "MAILGUN_BASE_URL",
        "brevo_api_key": "BREVO_API_KEY",
        "brevo_base_url": "BREVO_BASE_URL",
        "mailjet_api_key": "MAILJET_API_KEY",
        "mailjet_secret_key": "MAILJET_SECRET_KEY",
        "mailjet_sms_token": "MAILJET_SMS_TOKEN",
        "mailjet_base_url": "MAILJET_BASE_URL",
        "mandrill_api_key": "MANDRILL_API_KEY",
        "mandrill_base_url": "MANDRILL_BASE_URL",
        "messagebird_access_key": "MESSAGEBIRD_ACCESS_KEY",
        "messagebird_base_url": "MESSAGEBIRD_BASE_URL",
        "mocean_api_key": "MOCEAN_API_KEY",
        "mocean_api_secret": "MOCEAN_API_SECRET",
        "mocean_base_url": "MOCEAN_BASE_URL",
        "msg91_auth_key": "MSG91_AUTH_KEY",
        "msg91_base_url": "MSG91_BASE_URL",
        "plivo_auth_id": "PLIVO_AUTH_ID",
        "plivo_auth_token": "PLIVO_AUTH_TOKEN",
        "plivo_base_url": "PLIVO_BASE_URL",
        "vonage_api_key": "VONAGE_API_KEY",
        "vonage_api_secret": "VONAGE_API_SECRET",
        "vonage_base_url": "VONAGE_BASE_URL",
        "seven_api_key": "SEVEN_API_KEY",
        "seven_base_url": "SEVEN_BASE_URL",
        "stripe_secret_key": "STRIPE_SECRET_KEY",
        "stripe_base_url": "STRIPE_BASE_URL",
        "shopify_shop": "SHOPIFY_SHOP",
        "shopify_access_token": "SHOPIFY_ACCESS_TOKEN",
        "shopify_api_key": "SHOPIFY_API_KEY",
        "shopify_password": "SHOPIFY_PASSWORD",
        "shopify_api_version": "SHOPIFY_API_VERSION",
        "shopify_base_url": "SHOPIFY_BASE_URL",
        "woocommerce_url": "WOOCOMMERCE_URL",
        "woocommerce_base_url": "WOOCOMMERCE_BASE_URL",
        "woocommerce_consumer_key": "WOOCOMMERCE_CONSUMER_KEY",
        "woocommerce_consumer_secret": "WOOCOMMERCE_CONSUMER_SECRET",
        "chargebee_api_key": "CHARGEBEE_API_KEY",
        "chargebee_site": "CHARGEBEE_SITE",
        "chargebee_base_url": "CHARGEBEE_BASE_URL",
        "paddle_vendor_id": "PADDLE_VENDOR_ID",
        "paddle_vendor_auth_code": "PADDLE_VENDOR_AUTH_CODE",
        "paddle_sandbox": "PADDLE_SANDBOX",
        "paddle_base_url": "PADDLE_BASE_URL",
        "profitwell_api_token": "PROFITWELL_API_TOKEN",
        "profitwell_base_url": "PROFITWELL_BASE_URL",
        "tapfiliate_api_key": "TAPFILIATE_API_KEY",
        "tapfiliate_base_url": "TAPFILIATE_BASE_URL",
        "magento_host": "MAGENTO_HOST",
        "magento_base_url": "MAGENTO_BASE_URL",
        "magento_access_token": "MAGENTO_ACCESS_TOKEN",
        "unleashed_api_id": "UNLEASHED_API_ID",
        "unleashed_api_key": "UNLEASHED_API_KEY",
        "unleashed_base_url": "UNLEASHED_BASE_URL",
        "quickbooks_access_token": "QUICKBOOKS_ACCESS_TOKEN",
        "quickbooks_realm_id": "QUICKBOOKS_REALM_ID",
        "quickbooks_environment": "QUICKBOOKS_ENVIRONMENT",
        "quickbooks_base_url": "QUICKBOOKS_BASE_URL",
        "xero_access_token": "XERO_ACCESS_TOKEN",
        "xero_tenant_id": "XERO_TENANT_ID",
        "xero_base_url": "XERO_BASE_URL",
        "xero_connections_url": "XERO_CONNECTIONS_URL",
        "pushbullet_access_token": "PUSHBULLET_ACCESS_TOKEN",
        "pushbullet_base_url": "PUSHBULLET_BASE_URL",
        "pushcut_api_key": "PUSHCUT_API_KEY",
        "pushcut_base_url": "PUSHCUT_BASE_URL",
        "gotify_base_url": "GOTIFY_BASE_URL",
        "gotify_app_token": "GOTIFY_APP_TOKEN",
        "gotify_client_token": "GOTIFY_CLIENT_TOKEN",
        "pushover_api_token": "PUSHOVER_API_TOKEN",
        "pushover_user_key": "PUSHOVER_USER_KEY",
        "pushover_base_url": "PUSHOVER_BASE_URL",
        "signl4_team_secret": "SIGNL4_TEAM_SECRET",
        "signl4_webhook_url": "SIGNL4_WEBHOOK_URL",
        "signl4_base_url": "SIGNL4_BASE_URL",
        "wordpress_url": "WORDPRESS_URL",
        "wordpress_username": "WORDPRESS_USERNAME",
        "wordpress_password": "WORDPRESS_PASSWORD",
        "strapi_url": "STRAPI_URL",
        "strapi_api_token": "STRAPI_API_TOKEN",
        "strapi_email": "STRAPI_EMAIL",
        "strapi_password": "STRAPI_PASSWORD",
        "strapi_api_version": "STRAPI_API_VERSION",
        "contentful_space_id": "CONTENTFUL_SPACE_ID",
        "contentful_delivery_token": "CONTENTFUL_DELIVERY_TOKEN",
        "contentful_preview_token": "CONTENTFUL_PREVIEW_TOKEN",
        "contentful_base_url": "CONTENTFUL_BASE_URL",
        "contentful_preview_base_url": "CONTENTFUL_PREVIEW_BASE_URL",
        "ghost_url": "GHOST_URL",
        "ghost_content_api_key": "GHOST_CONTENT_API_KEY",
        "ghost_admin_api_key": "GHOST_ADMIN_API_KEY",
        "ghost_api_version": "GHOST_API_VERSION",
        "storyblok_content_token": "STORYBLOK_CONTENT_TOKEN",
        "storyblok_management_token": "STORYBLOK_MANAGEMENT_TOKEN",
        "storyblok_space_id": "STORYBLOK_SPACE_ID",
        "storyblok_content_base_url": "STORYBLOK_CONTENT_BASE_URL",
        "storyblok_management_base_url": "STORYBLOK_MANAGEMENT_BASE_URL",
        "webflow_access_token": "WEBFLOW_ACCESS_TOKEN",
        "webflow_base_url": "WEBFLOW_BASE_URL",
        "netlify_access_token": "NETLIFY_ACCESS_TOKEN",
        "netlify_base_url": "NETLIFY_BASE_URL",
        "rundeck_base_url": "RUNDECK_BASE_URL",
        "rundeck_token": "RUNDECK_TOKEN",
        "uptimerobot_api_key": "UPTIMEROBOT_API_KEY",
        "uptimerobot_base_url": "UPTIMEROBOT_BASE_URL",
        "pagerduty_api_token": "PAGERDUTY_API_TOKEN",
        "pagerduty_from_email": "PAGERDUTY_FROM_EMAIL",
        "pagerduty_base_url": "PAGERDUTY_BASE_URL",
        "sentry_auth_token": "SENTRY_AUTH_TOKEN",
        "sentry_base_url": "SENTRY_BASE_URL",
        "cloudflare_api_token": "CLOUDFLARE_API_TOKEN",
        "cloudflare_base_url": "CLOUDFLARE_BASE_URL",
        "grafana_api_token": "GRAFANA_API_TOKEN",
        "grafana_base_url": "GRAFANA_BASE_URL",
        "metabase_base_url": "METABASE_BASE_URL",
        "metabase_session_token": "METABASE_SESSION_TOKEN",
        "metabase_api_key": "METABASE_API_KEY",
        "metabase_username": "METABASE_USERNAME",
        "metabase_password": "METABASE_PASSWORD",
        "elasticsearch_base_url": "ELASTICSEARCH_BASE_URL",
        "elasticsearch_api_key": "ELASTICSEARCH_API_KEY",
        "elasticsearch_bearer_token": "ELASTICSEARCH_BEARER_TOKEN",
        "elasticsearch_username": "ELASTICSEARCH_USERNAME",
        "elasticsearch_password": "ELASTICSEARCH_PASSWORD",
        "elasticsearch_ignore_ssl_issues": "ELASTICSEARCH_IGNORE_SSL_ISSUES",
        "splunk_base_url": "SPLUNK_BASE_URL",
        "splunk_auth_token": "SPLUNK_AUTH_TOKEN",
        "splunk_allow_unauthorized_certs": "SPLUNK_ALLOW_UNAUTHORIZED_CERTS",
        "urlscan_api_key": "URLSCAN_API_KEY",
        "urlscan_base_url": "URLSCAN_BASE_URL",
        "hunter_api_key": "HUNTER_API_KEY",
        "hunter_base_url": "HUNTER_BASE_URL",
        "mailcheck_api_key": "MAILCHECK_API_KEY",
        "mailcheck_base_url": "MAILCHECK_BASE_URL",
        "peekalink_api_key": "PEEKALINK_API_KEY",
        "peekalink_base_url": "PEEKALINK_BASE_URL",
        "jina_api_key": "JINA_API_KEY",
        "jina_reader_base_url": "JINA_READER_BASE_URL",
        "jina_search_base_url": "JINA_SEARCH_BASE_URL",
        "jina_deepsearch_base_url": "JINA_DEEPSEARCH_BASE_URL",
        "misp_base_url": "MISP_BASE_URL",
        "misp_api_key": "MISP_API_KEY",
        "misp_allow_unauthorized_certs": "MISP_ALLOW_UNAUTHORIZED_CERTS",
        "thehive_base_url": "THEHIVE_BASE_URL",
        "thehive_api_key": "THEHIVE_API_KEY",
        "thehive_api_version": "THEHIVE_API_VERSION",
        "thehive_allow_unauthorized_certs": "THEHIVE_ALLOW_UNAUTHORIZED_CERTS",
        "securityscorecard_api_key": "SECURITYSCORECARD_API_KEY",
        "securityscorecard_base_url": "SECURITYSCORECARD_BASE_URL",
        "okta_access_token": "OKTA_ACCESS_TOKEN",
        "okta_domain": "OKTA_DOMAIN",
        "okta_base_url": "OKTA_BASE_URL",
        "elastic_security_base_url": "ELASTIC_SECURITY_BASE_URL",
        "elastic_security_api_key": "ELASTIC_SECURITY_API_KEY",
        "elastic_security_username": "ELASTIC_SECURITY_USERNAME",
        "elastic_security_password": "ELASTIC_SECURITY_PASSWORD",
        "baserow_api_token": "BASEROW_API_TOKEN",
        "baserow_base_url": "BASEROW_BASE_URL",
        "supabase_url": "SUPABASE_URL",
        "supabase_service_role_key": "SUPABASE_SERVICE_ROLE_KEY",
        "supabase_api_key": "SUPABASE_API_KEY",
        "supabase_base_url": "SUPABASE_BASE_URL",
        "quickbase_hostname": "QUICKBASE_HOSTNAME",
        "quickbase_user_token": "QUICKBASE_USER_TOKEN",
        "quickbase_base_url": "QUICKBASE_BASE_URL",
        "seatable_api_token": "SEATABLE_API_TOKEN",
        "seatable_base_url": "SEATABLE_BASE_URL",
        "stackby_api_key": "STACKBY_API_KEY",
        "stackby_base_url": "STACKBY_BASE_URL",
        "nocodb_api_token": "NOCODB_API_TOKEN",
        "nocodb_base_url": "NOCODB_BASE_URL",
        "nocodb_auth_header": "NOCODB_AUTH_HEADER",
        "coda_api_token": "CODA_API_TOKEN",
        "coda_base_url": "CODA_BASE_URL",
        "grist_api_key": "GRIST_API_KEY",
        "grist_base_url": "GRIST_BASE_URL",
        "adalo_api_key": "ADALO_API_KEY",
        "adalo_app_id": "ADALO_APP_ID",
        "adalo_base_url": "ADALO_BASE_URL",
        "bubble_api_token": "BUBBLE_API_TOKEN",
        "bubble_app_name": "BUBBLE_APP_NAME",
        "bubble_environment": "BUBBLE_ENVIRONMENT",
        "bubble_domain": "BUBBLE_DOMAIN",
        "bubble_base_url": "BUBBLE_BASE_URL",
        "cockpit_base_url": "COCKPIT_BASE_URL",
        "cockpit_access_token": "COCKPIT_ACCESS_TOKEN",
        "kobotoolbox_api_token": "KOBOTOOLBOX_API_TOKEN",
        "kobotoolbox_base_url": "KOBOTOOLBOX_BASE_URL",
        "telegram_api_base_url": "TELEGRAM_API_BASE_URL",
        "webex_access_token": "WEBEX_ACCESS_TOKEN",
        "webex_base_url": "WEBEX_BASE_URL",
        "whatsapp_access_token": "WHATSAPP_ACCESS_TOKEN",
        "whatsapp_business_account_id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
        "whatsapp_phone_number_id": "WHATSAPP_PHONE_NUMBER_ID",
        "whatsapp_base_url": "WHATSAPP_BASE_URL",
        "discord_base_url": "DISCORD_BASE_URL",
        "mattermost_access_token": "MATTERMOST_ACCESS_TOKEN",
        "mattermost_base_url": "MATTERMOST_BASE_URL",
        "matrix_access_token": "MATRIX_ACCESS_TOKEN",
        "matrix_base_url": "MATRIX_BASE_URL",
        "rocketchat_auth_token": "ROCKETCHAT_AUTH_TOKEN",
        "rocketchat_user_id": "ROCKETCHAT_USER_ID",
        "rocketchat_base_url": "ROCKETCHAT_BASE_URL",
        "zulip_api_key": "ZULIP_API_KEY",
        "zulip_email": "ZULIP_EMAIL",
        "zulip_base_url": "ZULIP_BASE_URL",
        "google_books_api_key": "GOOGLE_BOOKS_API_KEY",
        "google_books_base_url": "GOOGLE_BOOKS_BASE_URL",
        "youtube_api_key": "YOUTUBE_API_KEY",
        "youtube_base_url": "YOUTUBE_BASE_URL",
        "spotify_access_token": "SPOTIFY_ACCESS_TOKEN",
        "spotify_client_id": "SPOTIFY_CLIENT_ID",
        "spotify_client_secret": "SPOTIFY_CLIENT_SECRET",
        "spotify_base_url": "SPOTIFY_BASE_URL",
        "spotify_accounts_base_url": "SPOTIFY_ACCOUNTS_BASE_URL",
        "reddit_access_token": "REDDIT_ACCESS_TOKEN",
        "reddit_refresh_token": "REDDIT_REFRESH_TOKEN",
        "reddit_client_id": "REDDIT_CLIENT_ID",
        "reddit_client_secret": "REDDIT_CLIENT_SECRET",
        "reddit_base_url": "REDDIT_BASE_URL",
        "reddit_public_base_url": "REDDIT_PUBLIC_BASE_URL",
        "reddit_token_url": "REDDIT_TOKEN_URL",
        "discourse_api_key": "DISCOURSE_API_KEY",
        "discourse_api_username": "DISCOURSE_API_USERNAME",
        "discourse_base_url": "DISCOURSE_BASE_URL",
        "medium_access_token": "MEDIUM_ACCESS_TOKEN",
        "medium_base_url": "MEDIUM_BASE_URL",
        "bamboohr_api_key": "BAMBOOHR_API_KEY",
        "bamboohr_subdomain": "BAMBOOHR_SUBDOMAIN",
        "bamboohr_base_url": "BAMBOOHR_BASE_URL",
        "beeminder_access_token": "BEEMINDER_ACCESS_TOKEN",
        "beeminder_base_url": "BEEMINDER_BASE_URL",
        "clockify_api_key": "CLOCKIFY_API_KEY",
        "clockify_base_url": "CLOCKIFY_BASE_URL",
        "harvest_access_token": "HARVEST_ACCESS_TOKEN",
        "harvest_account_id": "HARVEST_ACCOUNT_ID",
        "harvest_base_url": "HARVEST_BASE_URL",
        "oura_access_token": "OURA_ACCESS_TOKEN",
        "oura_base_url": "OURA_BASE_URL",
        "strava_access_token": "STRAVA_ACCESS_TOKEN",
        "strava_base_url": "STRAVA_BASE_URL",
        "homeassistant_access_token": "HOMEASSISTANT_ACCESS_TOKEN",
        "homeassistant_base_url": "HOMEASSISTANT_BASE_URL",
        "philips_hue_access_token": "PHILIPS_HUE_ACCESS_TOKEN",
        "philips_hue_username": "PHILIPS_HUE_USERNAME",
        "philips_hue_base_url": "PHILIPS_HUE_BASE_URL",
        "activecampaign_api_key": "ACTIVECAMPAIGN_API_KEY",
        "activecampaign_base_url": "ACTIVECAMPAIGN_BASE_URL",
        "convertkit_api_secret": "CONVERTKIT_API_SECRET",
        "convertkit_base_url": "CONVERTKIT_BASE_URL",
        "getresponse_api_key": "GETRESPONSE_API_KEY",
        "getresponse_base_url": "GETRESPONSE_BASE_URL",
        "mailerlite_api_key": "MAILERLITE_API_KEY",
        "mailerlite_base_url": "MAILERLITE_BASE_URL",
        "mailerlite_classic_api": "MAILERLITE_CLASSIC_API",
        "customerio_tracking_site_id": "CUSTOMERIO_TRACKING_SITE_ID",
        "customerio_tracking_api_key": "CUSTOMERIO_TRACKING_API_KEY",
        "customerio_app_api_key": "CUSTOMERIO_APP_API_KEY",
        "customerio_region": "CUSTOMERIO_REGION",
        "customerio_tracking_base_url": "CUSTOMERIO_TRACKING_BASE_URL",
        "customerio_app_base_url": "CUSTOMERIO_APP_BASE_URL",
        "iterable_api_key": "ITERABLE_API_KEY",
        "iterable_base_url": "ITERABLE_BASE_URL",
        "posthog_api_key": "POSTHOG_API_KEY",
        "posthog_base_url": "POSTHOG_BASE_URL",
        "segment_write_key": "SEGMENT_WRITE_KEY",
        "segment_base_url": "SEGMENT_BASE_URL",
        "actionnetwork_api_key": "ACTIONNETWORK_API_KEY",
        "actionnetwork_base_url": "ACTIONNETWORK_BASE_URL",
        "autopilot_api_key": "AUTOPILOT_API_KEY",
        "autopilot_base_url": "AUTOPILOT_BASE_URL",
        "egoi_api_key": "EGOI_API_KEY",
        "egoi_base_url": "EGOI_BASE_URL",
        "vero_auth_token": "VERO_AUTH_TOKEN",
        "vero_base_url": "VERO_BASE_URL",
        "lemlist_api_key": "LEMLIST_API_KEY",
        "lemlist_base_url": "LEMLIST_BASE_URL",
        "sendy_url": "SENDY_URL",
        "sendy_base_url": "SENDY_BASE_URL",
        "sendy_api_key": "SENDY_API_KEY",
        "emelia_api_key": "EMELIA_API_KEY",
        "emelia_graphql_url": "EMELIA_GRAPHQL_URL",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "anthropic_direct_api_key": "ANTHROPIC_DIRECT_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "embedding_api_key": "EMBEDDING_API_KEY",
        "embedding_base_url": "EMBEDDING_BASE_URL",
        "embedding_model": "EMBEDDING_MODEL",
        "gemini_api_key": "GEMINI_API_KEY",
        "gemini_extraction_model": "GEMINI_EXTRACTION_MODEL",
        "_prv_a_service_account_file": "_PRV_A_SERVICE_ACCOUNT_FILE",
        "user_timezone": "USER_TIMEZONE",
        "ticker_poll_interval": "TICKER_POLL_INTERVAL",
        "max_concurrent_autonomous": "MAX_CONCURRENT_AUTONOMOUS",
        "tool_timeout": "TOOL_TIMEOUT",
        "lock_timeout": "LOCK_TIMEOUT",
        "todo_auto_archive_days": "TODO_AUTO_ARCHIVE_DAYS",
        "redis_url": "REDIS_URL",
        "redis_enabled": "REDIS_ENABLED",
        "postgres_uri": "POSTGRES_URI",
        "nymeria_data_dir": "NYMERIA_DATA_DIR",
        "discord_bot_token": "DISCORD_BOT_TOKEN",
        "discord_webhook_url": "DISCORD_WEBHOOK_URL",
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "telegram_default_chat_id": "TELEGRAM_DEFAULT_CHAT_ID",
    }


def _restart_required_keys() -> set[str]:
    """Settings that persist immediately but require process restart to apply."""
    return {
        "redis_url",
        "redis_enabled",
        "postgres_uri",
        "nymeria_data_dir",
        "discord_bot_token",
        "discord_webhook_url",
        "telegram_bot_token",
        "telegram_default_chat_id",
    }


def _env_categories() -> dict[str, list[str]]:
    """Return grouped admin environment entries for GET /settings/env."""
    return {
        "LLM": [
            "llm_provider",
            "llm_model",
            "llm_fast_model",
            "llm_fallback_models",
            "llm_temperature",
            "llm_max_tokens",
            "llm_top_p",
            "llm_top_k",
            "llm_frequency_penalty",
            "llm_presence_penalty",
            "llm_reasoning_effort",
            "llm_extended_thinking",
            "llm_use_model_defaults",
            "llm_base_url",
            "openai_api_mode",
            "llm_stream_max_retries",
            "llm_stream_retry_initial_delay",
            "llm_stream_retry_max_delay",
        ],
        "API Keys": [
            "openai_api_key",
            "anthropic_api_key",
            "anthropic_direct_api_key",
            "openrouter_api_key",
            "embedding_api_key",
            "perplexity_api_key",
            "perplexity_search_model",
            "gemini_api_key",
            "gemini_extraction_model",
        ],
        "Context": [
            "context_management",
            "compact_threshold",
            "compact_keep_messages",
            "compact_model",
            "sliding_window_cycles",
        ],
        "System": [
            "log_level",
            "watchdog_enabled",
            "watchdog_interval_minutes",
            "user_timezone",
            "nymeria_data_dir",
            "tool_timeout",
            "tool_output_max_chars",
            "lock_timeout",
        ],
        "Tasks": [
            "ticker_poll_interval",
            "max_concurrent_autonomous",
            "todo_staleness_minutes",
            "todo_auto_archive_days",
            "activity_retention_hours",
        ],
        "Voice": [
            "tts_provider",
            "tts_base_url",
            "tts_api_key",
            "tts_model",
            "tts_voice",
            "tts_output_format",
            "tts_speed",
            "stt_provider",
            "stt_base_url",
            "stt_api_key",
            "stt_model",
            "stt_language",
            "voice_default_thread_id",
        ],
        "Infrastructure": [
            "redis_url",
            "redis_enabled",
            "postgres_uri",
        ],
        "Discord": [
            "discord_bot_token",
            "discord_webhook_url",
        ],
        "Telegram": [
            "telegram_bot_token",
            "telegram_default_chat_id",
        ],
        "Twitch": [
            "twitch_client_id",
            "twitch_client_secret",
            "twitch_bot_access_token",
            "twitch_bot_refresh_token",
            "twitch_bot_user_id",
            "twitch_broadcaster_token",
            "twitch_broadcaster_refresh_token",
            "twitch_channel",
            "twitch_system_prompt",
            "twitch_buffer_size",
            "twitch_pulse_enabled",
            "twitch_pulse_interval",
            "twitch_respond_mode",
        ],
    }


def _secret_keys() -> set[str]:
    """Settings that should be masked in GET /settings/env."""
    return {
        "openai_api_key",
        "anthropic_api_key",
        "anthropic_direct_api_key",
        "openrouter_api_key",
        "embedding_api_key",
        "perplexity_api_key",
        "gemini_api_key",
        "discord_bot_token",
        "discord_webhook_url",
        "telegram_bot_token",
        "twitch_client_secret",
        "twitch_bot_access_token",
        "twitch_bot_refresh_token",
        "twitch_broadcaster_token",
        "twitch_broadcaster_refresh_token",
        "slack_bot_token",
        "microsoft_graph_access_token",
        "postgres_uri",
        "redis_url",
        "tts_api_key",
        "stt_api_key",
        "fcm_credentials_json",
    }


def _fallback_model_list(value: Any) -> list[str]:
    """Return a normalized fallback model list for settings responses."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = [value]
    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def _mask_value(val: str) -> str:
    """Mask a secret value, showing first 4 and last 3 chars."""
    s = str(val)
    if len(s) <= 10:
        return s[:2] + "..." + s[-1:] if len(s) > 3 else "***"
    return s[:4] + "..." + s[-3:]


def _sync_process_env(new_lines: list[str], mapped_env_vars: set[str]) -> None:
    """Sync mapped dotenv values into os.environ after a settings update."""
    for line in new_lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            if key in mapped_env_vars:
                os.environ[key] = val


def _clear_settings_cache(get_settings_fn: Callable[[], Any]) -> None:
    cache_clear = getattr(get_settings_fn, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()



def _normalize_openai_test_base_url(provider: str, base_url: str | None) -> str:
    provider = normalize_llm_provider(provider)
    if not base_url:
        resolved = resolve_provider_base_url(provider)
        if resolved:
            return resolved
        if provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        return "https://api.openai.com/v1"

    clean = base_url.strip().rstrip("/")
    if provider == "openai" and looks_like_cliproxy_url(clean) and not clean.endswith("/v1"):
        return f"{clean}/v1"
    return clean




async def _post_llm_test_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> None:
    request_headers = {
        "Content-Type": "application/json",
        **headers,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=request_headers, json=payload)
        response.raise_for_status()


async def _test_llm_provider_config(
    request: LLMProviderTestRequest,
    settings: Settings | None = None,
    vault: Any | None = None,
    owner_user_id: str | None = None,
) -> LLMProviderTestResponse:
    provider = normalize_llm_provider(request.llm_provider)
    model = request.llm_model
    api_key = request.api_key.get_secret_value() if request.api_key else None
    base_url = request.llm_base_url
    openai_api_mode = request.openai_api_mode or "chat_completions"

    if not api_key:
        credential = get_llm_provider_credential(
            provider, vault=vault, owner_user_id=owner_user_id,
        )
        if credential and credential.api_key:
            api_key = credential.api_key
            if not base_url and credential.base_url:
                base_url = credential.base_url

    if not api_key:
        api_key = resolve_provider_api_key(provider, settings=settings)

    if not api_key:
        resolved_base = base_url or resolve_provider_base_url(provider, settings=settings)
        if base_url_allows_no_api_key(resolved_base):
            api_key = "not-needed"
        elif provider_requires_api_key(provider):
            return LLMProviderTestResponse(
                ok=False,
                provider=provider,
                model=model,
                openai_api_mode=None,
                message="No API key provided and none found in vault, settings, or environment.",
                error_type="missing_api_key",
            )
        else:
            api_key = "not-needed"

    if provider == "anthropic":
        clean_base = base_url or "https://api.anthropic.com"
        url = f"{clean_base}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        if base_url:
            headers["User-Agent"] = "claude-cli/2.1.113"
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply with ok."}],
        }
        response_api_mode = None
    else:
        if not is_openai_compatible_provider(provider) and not base_url:
            return LLMProviderTestResponse(
                ok=False,
                provider=provider,
                model=model,
                openai_api_mode=None,
                message=(
                    f"Provider '{provider}' is not in Nymeria's OpenAI-compatible "
                    "registry. Provide an API base URL to test it as a custom endpoint."
                ),
                error_type="unknown_provider",
            )
        clean_base = _normalize_openai_test_base_url(provider, base_url)
        headers = {"Authorization": f"Bearer {api_key}"}
        if provider == "openrouter":
            headers.update({
                "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
                "X-Title": "Nymeria",
            })

        effective_api_mode = (
            openai_api_mode
            if openai_api_mode == "responses"
            and (
                provider_supports_responses(provider)
                or bool(base_url and not is_openai_compatible_provider(provider))
            )
            else "chat_completions"
        )

        if effective_api_mode == "chat_completions":
            url = f"{clean_base}/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with ok."}],
                "max_tokens": 16,
            }
        else:
            url = f"{clean_base}/responses"
            payload = {
                "model": model,
                "input": "Reply with ok.",
                "max_output_tokens": 16,
            }
        response_api_mode = effective_api_mode

    try:
        await _post_llm_test_json(url, headers=headers, payload=payload)
    except httpx.TimeoutException:
        logger.info("LLM provider test timed out: provider=%s", provider)
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            openai_api_mode=response_api_mode,
            message="Provider did not respond before the 15s timeout.",
            error_type="timeout",
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.info(
            "LLM provider test returned HTTP error: provider=%s status=%s",
            provider,
            status_code,
        )
        detail = http_error_detail(exc.response, api_key, base_url)
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            openai_api_mode=response_api_mode,
            message=f"Provider returned HTTP {status_code}: {detail}",
            status_code=status_code,
            error_type="http_error",
        )
    except httpx.HTTPError as exc:
        logger.info(
            "LLM provider test transport error: provider=%s error=%s",
            provider,
            type(exc).__name__,
        )
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            openai_api_mode=response_api_mode,
            message=redact_secrets(str(exc), api_key, base_url)[:300],
            error_type=type(exc).__name__,
        )

    return LLMProviderTestResponse(
        ok=True,
        provider=provider,
        model=model,
        openai_api_mode=response_api_mode,
        message="Provider test succeeded.",
    )


def create_settings_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the settings/model-catalog router with app dependencies injected."""
    router = APIRouter(tags=["Settings"])

    @router.get("/settings", response_model=ServerSettingsResponse)
    async def get_server_settings(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get current server settings."""
        return ServerSettingsResponse(
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            llm_fast_model=settings.llm_fast_model,
            llm_fallback_models=_fallback_model_list(settings.llm_fallback_models),
            llm_temperature=settings.llm_temperature,
            llm_max_tokens=settings.llm_max_tokens,
            llm_top_p=settings.llm_top_p,
            llm_top_k=settings.llm_top_k,
            llm_frequency_penalty=settings.llm_frequency_penalty,
            llm_presence_penalty=settings.llm_presence_penalty,
            llm_reasoning_effort=settings.llm_reasoning_effort,
            llm_extended_thinking=settings.llm_extended_thinking,
            llm_use_model_defaults=settings.llm_use_model_defaults,
            llm_base_url=settings.llm_base_url,
            openai_api_mode=settings.openai_api_mode,
            llm_stream_max_retries=settings.llm_stream_max_retries,
            llm_stream_retry_initial_delay=settings.llm_stream_retry_initial_delay,
            llm_stream_retry_max_delay=settings.llm_stream_retry_max_delay,
            context_management=settings.context_management,
            compact_threshold=settings.compact_threshold,
            compact_keep_messages=settings.compact_keep_messages,
            compact_model=settings.compact_model,
            sliding_window_cycles=settings.sliding_window_cycles,
            tool_output_max_chars=settings.tool_output_max_chars,
            log_level=settings.log_level,
            watchdog_enabled=settings.watchdog_enabled,
            watchdog_interval_minutes=settings.watchdog_interval_minutes,
            todo_staleness_minutes=settings.todo_staleness_minutes,
            activity_retention_hours=settings.activity_retention_hours,
            tts_provider=settings.tts_provider,
            tts_base_url=settings.tts_base_url,
            tts_model=settings.tts_model,
            tts_voice=settings.tts_voice,
            tts_output_format=settings.tts_output_format,
            tts_speed=settings.tts_speed,
            stt_provider=settings.stt_provider,
            stt_base_url=settings.stt_base_url,
            stt_model=settings.stt_model,
            stt_language=settings.stt_language,
            voice_default_thread_id=settings.voice_default_thread_id,
        )

    @router.post("/settings/llm/test", response_model=LLMProviderTestResponse)
    async def test_llm_provider_config(
        request: LLMProviderTestRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Test an arbitrary LLM provider configuration without writing it."""
        agent = get_agent_fn()
        return await _test_llm_provider_config(
            request,
            settings=settings,
            vault=getattr(agent, "credential_vault", None),
            owner_user_id=user.id,
        )

    @router.post(
        "/settings/llm/test-suite",
        response_model=LLMProviderTestSuiteResponse,
    )
    async def test_llm_provider_suite(
        request: LLMProviderTestSuiteRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Run the production-readiness suite for an LLM provider setup."""
        agent = get_agent_fn()
        api_key = request.api_key.get_secret_value() if request.api_key else None
        report = await run_provider_test_suite(
            ProviderTestSuiteOptions(
                provider=request.llm_provider,
                model=request.llm_model,
                api_key=api_key,
                base_url=request.llm_base_url,
                api_mode=request.openai_api_mode,
                settings=settings,
                vault=getattr(agent, "credential_vault", None),
                owner_user_id=user.id,
                run_model_list=request.run_model_list,
                run_chat_completion=request.run_chat_completion,
                run_tool_call=request.run_tool_call,
                allow_billable=request.allow_billable,
                prefer_free_model=request.prefer_free_model,
                timeout_seconds=request.timeout_seconds,
            )
        )
        return LLMProviderTestSuiteResponse(**report.to_dict())

    @router.get(
        "/settings/llm/providers",
        response_model=list[LLMProviderSpecResponse],
    )
    async def get_llm_provider_catalog(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return known LLM provider compatibility metadata."""
        return [
            LLMProviderSpecResponse(
                id=spec.id,
                label=spec.label,
                api_format=spec.api_format,
                default_base_url=spec.default_base_url,
                api_key_env_vars=list(spec.api_key_env_vars),
                base_url_env_vars=list(spec.base_url_env_vars),
                default_model=spec.default_model,
                default_api_mode=spec.default_api_mode,
                supports_chat_completions=spec.supports_chat_completions,
                supports_responses=spec.supports_responses,
                requires_api_key=spec.requires_api_key,
                requires_base_url=spec.requires_base_url,
                docs_url=spec.docs_url,
                notes=spec.notes,
                aliases=list(spec.aliases),
            )
            for spec in list_llm_provider_specs()
        ]

    @router.get(
        "/settings/llm/runtime",
        response_model=LLMRuntimeDiagnosticsResponse,
    )
    async def get_llm_runtime_diagnostics(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get runtime LLM diagnostics including active OpenRouter key budget details."""
        agent = get_agent_fn()
        llm_cfg = agent._get_llm_config_for_thread("")

        effective_max_tokens = llm_cfg.max_tokens
        if effective_max_tokens is None and llm_cfg.provider == "openrouter":
            try:
                effective_max_tokens = get_max_output_tokens(llm_cfg.model)
            except Exception as e:
                logger.warning("Failed to resolve OpenRouter max output tokens: %s", e)

        source_env_files = [
            str(path)
            for path in get_env_file_paths(settings.project_root)
            if path.exists()
        ]

        response = LLMRuntimeDiagnosticsResponse(
            provider=llm_cfg.provider,
            model=llm_cfg.model,
            llm_max_tokens=settings.llm_max_tokens,
            effective_max_tokens=effective_max_tokens,
            source_env_files=source_env_files,
        )

        if llm_cfg.provider == "openrouter":
            if not llm_cfg.api_key:
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error="OPENROUTER_API_KEY is missing in active runtime settings"
                )
                return response

            try:
                async with httpx.AsyncClient(timeout=6) as client:
                    api_response = await client.get(
                        "https://openrouter.ai/api/v1/key",
                        headers={
                            "Authorization": f"Bearer {llm_cfg.api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    api_response.raise_for_status()
                    payload = api_response.json()
                data = payload.get("data", {}) if isinstance(payload, dict) else {}

                if isinstance(data, dict):
                    response.openrouter = OpenRouterKeyDiagnostics(
                        label=data.get("label"),
                        limit=data.get("limit"),
                        limit_remaining=data.get("limit_remaining"),
                        usage=data.get("usage"),
                        limit_reset=data.get("limit_reset"),
                        include_byok_in_limit=data.get("include_byok_in_limit"),
                        is_management_key=data.get("is_management_key"),
                    )
                else:
                    response.openrouter = OpenRouterKeyDiagnostics(
                        fetch_error="Unexpected response shape from OpenRouter /key endpoint"
                    )
            except httpx.HTTPStatusError as e:
                body = e.response.text[:300]
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"HTTP {e.response.status_code}: {body}"
                )
            except Exception as e:
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"{type(e).__name__}: {e}"
                )

        return response

    @router.patch("/settings")
    async def update_server_settings(
        updates: ServerSettingsUpdate,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """
        Update server settings with hot-reload. Admin-only because settings are
        global and can include provider credentials.
        """
        env_path = get_env_write_path(settings.project_root)

        existing_lines = []
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()

        env_mapping = _env_mapping()
        updates_dict = {
            k: v for k, v in updates.model_dump().items() if v is not None
        }

        if not updates_dict:
            return {"message": "No updates provided", "restart_required": False}

        updated_vars = set()
        new_lines = []

        for line in existing_lines:
            updated = False
            for setting_name, env_var in env_mapping.items():
                if setting_name in updates_dict and line.startswith(f"{env_var}="):
                    value = updates_dict[setting_name]
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")
                    updated_vars.add(setting_name)
                    updated = True
                    break

            if not updated:
                new_lines.append(line)

        for setting_name, value in updates_dict.items():
            if setting_name not in updated_vars:
                env_var = env_mapping.get(setting_name)
                if env_var:
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        _sync_process_env(new_lines, set(env_mapping.values()))

        _clear_settings_cache(get_settings_fn)
        new_settings = get_settings_fn()
        logger.info(
            "[SETTINGS] After hot-reload: TTS_PROVIDER=%s, STT_PROVIDER=%s, "
            "env TTS_PROVIDER=%s",
            new_settings.tts_provider,
            new_settings.stt_provider,
            os.environ.get("TTS_PROVIDER"),
        )

        agent = get_agent_fn()
        agent.settings = new_settings

        llm_fields = {
            "llm_provider",
            "llm_model",
            "llm_fast_model",
            "llm_fallback_models",
            "llm_temperature",
            "llm_max_tokens",
            "llm_top_p",
            "llm_top_k",
            "llm_frequency_penalty",
            "llm_presence_penalty",
            "llm_reasoning_effort",
            "llm_extended_thinking",
            "llm_use_model_defaults",
            "llm_base_url",
            "openai_api_mode",
            "llm_stream_max_retries",
            "llm_stream_retry_initial_delay",
            "llm_stream_retry_max_delay",
        }
        graph_fields = llm_fields | {"tool_output_max_chars"}
        llm_credential_fields = {
            "anthropic_api_key",
            "anthropic_direct_api_key",
            "openai_api_key",
            "openrouter_api_key",
        }
        graph_fields = graph_fields | llm_credential_fields
        if graph_fields & set(updates_dict.keys()):
            with agent._graph_cache_lock:
                agent._user_graphs.clear()
                agent._async_user_graphs.clear()
            agent._default_graph = agent._build_graph_with_prompt(
                agent._base_system_prompt
            )
            agent._default_async_graph = agent._build_async_graph_with_prompt(
                agent._base_system_prompt
            )
            logger.info(
                "Hot-reloaded graph settings: %s",
                graph_fields & set(updates_dict.keys()),
            )

        needs_restart = bool(_restart_required_keys() & set(updates_dict.keys()))
        return {
            "message": "Settings updated and applied" + (
                " (some changes require /restart api to take effect)"
                if needs_restart
                else ""
            ),
            "updated": list(updates_dict.keys()),
            "restart_required": needs_restart,
        }

    @router.get("/settings/env")
    async def get_env_vars(
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get settable environment variables with masked sensitive values."""
        entries = []
        secret_keys = _secret_keys()
        for category, keys in _env_categories().items():
            for key in keys:
                if key in HIDDEN_CONFIG_SETTINGS:
                    continue
                val = getattr(settings, key, None)
                env_var = key.upper()
                is_secret = key in secret_keys
                display_val = None
                if val is not None:
                    display_val = _mask_value(str(val)) if is_secret else str(val)
                entries.append({
                    "name": key,
                    "env_var": env_var,
                    "value": display_val,
                    "is_set": val is not None and str(val) != "",
                    "is_secret": is_secret,
                    "category": category,
                })

        return {"entries": entries}

    @router.get("/settings/env/{key}")
    async def get_env_var(
        key: str,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get a single environment variable's unmasked value. Admin-only."""
        key_lower = key.lower()
        if key_lower in HIDDEN_CONFIG_SETTINGS:
            raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

        val = getattr(settings, key, None)
        if val is None:
            val = getattr(settings, key_lower, None)
            if val is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown setting: {key}",
                )
            key = key_lower
        return {
            "name": key,
            "env_var": key.upper(),
            "value": str(val) if val is not None else None,
        }

    @router.get("/models")
    async def get_cached_models(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return cached model metadata for frontend enrichment."""
        models = list_all_models()
        return [
            {
                "id": m.id,
                "name": m.name,
                "context_length": m.context_length,
                "max_completion_tokens": m.max_completion_tokens,
                "pricing_prompt": m.pricing_prompt,
                "pricing_completion": m.pricing_completion,
                "supported_parameters": sorted(m.supported_parameters),
                "input_modalities": sorted(m.input_modalities),
                "tokenizer": m.tokenizer,
                "default_temperature": m.default_temperature,
                "default_top_p": m.default_top_p,
                "default_frequency_penalty": m.default_frequency_penalty,
            }
            for m in models
        ]

    @router.get("/models/available")
    async def get_available_models(
        provider: Optional[str] = Query(
            default=None,
            description=(
                "Provider to fetch models for. Defaults to global provider."
            ),
        ),
        base_url: Optional[str] = Query(
            default=None,
            description="Optional OpenAI-compatible base URL override for unsaved provider settings.",
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Fetch available models from the configured LLM provider or CLIProxy."""
        effective_provider = normalize_llm_provider(provider or settings.llm_provider)
        agent = get_agent_fn()
        credential = get_llm_provider_credential(
            effective_provider,
            vault=getattr(agent, "credential_vault", None),
            owner_user_id=user.id,
        )

        effective_base_url = base_url
        api_key = credential.api_key if credential else None
        if effective_base_url:
            effective_base_url = effective_base_url.strip().rstrip("/")
        if (
            not effective_base_url
            and effective_provider == normalize_llm_provider(settings.llm_provider)
        ):
            effective_base_url = settings.llm_base_url
        if not effective_base_url and credential and credential.base_url:
            effective_base_url = credential.base_url

        if effective_provider == "anthropic":
            api_key = api_key or (
                settings.anthropic_direct_api_key or settings.anthropic_api_key
            )
            effective_base_url = effective_base_url or "https://api.anthropic.com"
            clean_base = effective_base_url.rstrip("/")
            models_url = (
                f"{clean_base}/models"
                if clean_base.endswith("/v1")
                else f"{clean_base}/v1/models"
            )
        elif is_openai_compatible_provider(effective_provider):
            api_key = api_key or resolve_provider_api_key(
                effective_provider,
                settings=settings,
            )
            effective_base_url = effective_base_url or resolve_provider_base_url(
                effective_provider,
                settings=settings,
            )
            if not effective_base_url:
                return []
            models_url = f"{effective_base_url.rstrip('/')}/models"
        else:
            return []

        if (
            not api_key
            and provider_requires_api_key(effective_provider)
            and not base_url_allows_no_api_key(effective_base_url)
        ):
            return []
        if not api_key:
            api_key = "not-needed"

        if effective_provider == "anthropic":
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            headers = {
                "Authorization": f"Bearer {api_key}",
            }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(models_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            raw_models = data.get("data", [])
            result = []
            for m in sorted(raw_models, key=lambda x: x.get("id", "")):
                model_id = m.get("id", "")
                if not model_id:
                    continue
                model_name = m.get("name") or model_id
                metadata = extract_model_metadata(m)
                register_model_metadata(
                    model_id=model_id,
                    name=model_name,
                    context_length=metadata["context_length"],
                    max_completion_tokens=metadata["max_completion_tokens"],
                    input_modalities=set(metadata["input_modalities"]),
                    supported_parameters=set(metadata["supported_parameters"]),
                    default_temperature=metadata["default_temperature"],
                    default_top_p=metadata["default_top_p"],
                    default_frequency_penalty=metadata["default_frequency_penalty"],
                    pricing_prompt=metadata["pricing_prompt"],
                    pricing_completion=metadata["pricing_completion"],
                    tokenizer=metadata["tokenizer"],
                )
                result.append({
                    "id": model_id,
                    "name": model_name,
                    "owned_by": m.get("owned_by", ""),
                    "created": m.get("created"),
                    **metadata,
                })
            return result
        except Exception as e:
            logger.warning("Failed to fetch models from %s: %s", models_url, e)
            return []

    return router
