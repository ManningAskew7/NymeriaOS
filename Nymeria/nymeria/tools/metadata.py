"""Tool metadata and categorization for per-user tool customization.

Built-in metadata is generated from the registered LangChain tool objects so
tool descriptions stay tied to the tool docstrings. This module still owns the
policy metadata that cannot be inferred from a docstring: category, security
level, default-enabled state, and tool-specific config schemas.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set


class ToolCategory(str, Enum):
    """Categories for grouping related tools."""

    CORE = "core"
    PROFILE = "profile"
    NOTEPAD = "notepad"
    SELF_MODIFY = "self_modify"
    TODO = "todo"
    AUTONOMY = "autonomy"
    AUTH = "auth"
    THREAD_SPAWN = "thread_spawn"
    TRIGGER = "trigger"
    EMAIL = "email"
    BROWSER = "browser"
    IMAGE = "image"
    CALENDAR = "calendar"
    GOOGLE_DOCS = "google_docs"
    _PRV_B = "_prv_b"
    TWITCH = "twitch"
    _PRV_A = "_prv_a"
    SKILLS = "skills"
    INTEGRATIONS = "integrations"
    CUSTOM = "custom"
    MCP_SERVER = "mcp_server"


class SecurityLevel(str, Enum):
    """Security level determines default enabled state and opt-in requirements."""

    SAFE = "safe"
    MODERATE = "moderate"
    SENSITIVE = "sensitive"


@dataclass
class ToolMetadata:
    """Metadata for a single tool."""

    name: str
    category: ToolCategory
    security_level: SecurityLevel
    description: str
    default_enabled: bool = True
    config_schema: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.security_level == SecurityLevel.SENSITIVE:
            self.default_enabled = False


_REFRESHING_BUILTIN_METADATA = False


class _BuiltinToolMetadataRegistry(dict):
    """Dict-compatible lazy view over generated built-in tool metadata."""

    def _ensure_loaded(self) -> None:
        if not _REFRESHING_BUILTIN_METADATA:
            refresh_builtin_tool_metadata()

    def __contains__(self, key: object) -> bool:
        self._ensure_loaded()
        return dict.__contains__(self, key)

    def __getitem__(self, key: str) -> ToolMetadata:
        self._ensure_loaded()
        return dict.__getitem__(self, key)

    def __iter__(self):
        self._ensure_loaded()
        return dict.__iter__(self)

    def __len__(self) -> int:
        self._ensure_loaded()
        return dict.__len__(self)

    def get(self, key: str, default: Optional[ToolMetadata] = None) -> Optional[ToolMetadata]:
        self._ensure_loaded()
        return dict.get(self, key, default)

    def items(self):
        self._ensure_loaded()
        return dict.items(self)

    def keys(self):
        self._ensure_loaded()
        return dict.keys(self)

    def values(self):
        self._ensure_loaded()
        return dict.values(self)

    def copy(self) -> Dict[str, ToolMetadata]:
        self._ensure_loaded()
        return dict.copy(self)


TOOL_METADATA: Dict[str, ToolMetadata] = _BuiltinToolMetadataRegistry()


_CATEGORY_GROUPS: tuple[tuple[ToolCategory, tuple[str, ...]], ...] = (
    (ToolCategory.PROFILE, ("MEMORY_TOOLS",)),
    (ToolCategory.TODO, ("TODO_TOOLS",)),
    (ToolCategory.SELF_MODIFY, ("SELF_AGENT_TOOLS", "RUNTIME_ADMIN_TOOLS")),
    (ToolCategory.THREAD_SPAWN, ("SPAWN_THREAD_TOOLS",)),
    (ToolCategory.TRIGGER, ("TRIGGER_TOOLS",)),
    (ToolCategory.EMAIL, ("OUTLOOK_TOOLS", "GMAIL_AUTH_TOOLS", "OUTLOOK_ATTACHMENT_TOOLS")),
    (ToolCategory.BROWSER, ("BROWSER_TOOLS",)),
    (ToolCategory.IMAGE, ("IMAGE_GENERATION_TOOLS",)),
    (ToolCategory.CALENDAR, ("CALENDAR_TOOLS",)),
    (ToolCategory.GOOGLE_DOCS, ("GOOGLE_DOCS_TOOLS", "GOOGLE_SHEETS_TOOLS")),
    (ToolCategory._PRV_B, ("_PRV_TOOLS_B",)),
    (
        ToolCategory._PRV_A,
        (
            "_PRV_TOOLS_A1",
            "_PRV_TOOLS_A2",
            "_PRV_TOOLS_A3",
            "_PRV_TOOLS_A4",
            "_PRV_TOOLS_A5",
        ),
    ),
    (ToolCategory.TWITCH, ("TWITCH_TOOLS",)),
    (ToolCategory.SKILLS, ("SEARCH_SKILLS_TOOLS",)),
    (
        ToolCategory.INTEGRATIONS,
        (
            "UTILITY_INTEGRATION_TOOLS",
            "PUBLIC_INFO_TOOLS",
            "MEDIA_DISCOVERY_SERVICE_TOOLS",
            "COMMUNITY_PUBLISHING_SERVICE_TOOLS",
            "TIME_HR_SERVICE_TOOLS",
            "PERSONAL_DEVICE_SERVICE_TOOLS",
            "MARKETING_CONTACT_SERVICE_TOOLS",
            "DEVELOPER_PLATFORM_TOOLS",
            "BUSINESS_SERVICE_TOOLS",
            "PRODUCTIVITY_SERVICE_TOOLS",
            "WORK_TRACKING_SERVICE_TOOLS",
            "PROJECT_MANAGEMENT_SERVICE_TOOLS",
            "COLLABORATION_DATA_SERVICE_TOOLS",
            "CUSTOMER_ENGAGEMENT_SERVICE_TOOLS",
            "SUPPORT_SERVICE_TOOLS",
            "SALES_CRM_SERVICE_TOOLS",
            "MESSAGING_DELIVERY_SERVICE_TOOLS",
            "COMMERCE_BILLING_SERVICE_TOOLS",
            "NOTIFICATION_SERVICE_TOOLS",
            "CONTENT_MANAGEMENT_SERVICE_TOOLS",
            "OPERATIONS_MONITORING_SERVICE_TOOLS",
            "ENRICHMENT_SECURITY_SERVICE_TOOLS",
            "DATA_TABLE_SERVICE_TOOLS",
            "CHAT_PLATFORM_SERVICE_TOOLS",
        ),
    ),
    (ToolCategory.MCP_SERVER, ("SEARCH_MCP_TOOLS",)),
    (ToolCategory.AUTH, ("AUTH_MANAGER_TOOLS",)),
    (ToolCategory.AUTONOMY, ("WATCHDOG_TOOLS",)),
    (ToolCategory.CUSTOM, ("TOOL_CREATE_TOOLS", "SKILL_CONFIG_TOOLS", "SKILL_KIT_CREATE_TOOLS")),
)


_CORE_MODERATE_TOOL_NAMES = frozenset(
    {
        "bash_execute",
        "file_write",
        "file_edit",
        "claude_code",
        "notify",
        "slash_command",
        "http_request",
        "api_discover",
        "tool_enable",
        "manage_mcp",
        "skill_manage",
        "skill_kit_create",
    }
)

_EXPLICIT_CATEGORY_BY_TOOL_NAME: Mapping[str, ToolCategory] = {
    # These profile-management tools are optional, so they intentionally live
    # outside MEMORY_TOOLS while still belonging to the profile category.
    "memory_clear_all": ToolCategory.PROFILE,
    "rag_settings": ToolCategory.PROFILE,
}

_BROWSER_SAFE_TOOL_NAMES = frozenset(
    {
        "browser_get_content",
        "browser_screenshot",
        "browser_scroll",
        "browser_close",
        "browser_status",
    }
)

_EMAIL_SAFE_TOOL_NAMES = frozenset(
    {
        "outlook_list_authenticated_accounts",
        "outlook_list_emails",
        "outlook_get_email",
        "outlook_search_emails",
        "outlook_mark_email",
        "outlook_get_attachments",
        "outlook_set_category",
        "gmail_list_accounts",
    }
)

_CALENDAR_SAFE_TOOL_NAMES = frozenset(
    {
        "calendar_list_authenticated_accounts",
        "calendar_list_calendars",
        "calendar_list_events",
        "calendar_get_event",
        "calendar_search_events",
        "calendar_get_freebusy",
        "calendar_get_current_time",
        "calendar_list_colors",
    }
)

_GOOGLE_DOCS_SAFE_TOOL_NAMES = frozenset(
    {
        "google_docs_list_accounts",
        "google_docs_read",
        "google_docs_list",
        "google_docs_find_index",
        "google_sheets_search",
    }
)

_INTEGRATION_SAFE_TOOL_NAMES = frozenset(
    {
        "calculator",
        "wikipedia_search",
        "wolfram_alpha_query",
        "searxng_search",
        "coingecko_price",
        "coingecko_coin_markets",
        "hackernews_search",
        "hackernews_get_item",
        "hackernews_get_user",
        "npm_package_info",
        "npm_package_search",
        "open_thesaurus_synonyms",
        "nasa_apod",
        "openweathermap_current",
        "openweathermap_forecast",
        "quickchart_create_url",
        "google_books_search",
        "google_books_get_volume",
        "youtube_search",
        "youtube_get_videos",
        "youtube_get_channels",
        "youtube_list_playlist_items",
        "spotify_search",
        "spotify_get_track",
        "spotify_get_artist",
        "spotify_get_album",
        "spotify_get_playlist",
        "reddit_search_posts",
        "reddit_list_subreddit_posts",
        "reddit_get_post",
        "reddit_get_subreddit",
        "reddit_get_user",
        "discourse_search",
        "discourse_list_latest_topics",
        "discourse_get_topic",
        "discourse_get_post",
        "medium_get_me",
        "medium_list_publications",
        "bamboohr_list_employees",
        "bamboohr_get_employee",
        "bamboohr_get_company_report",
        "beeminder_get_user",
        "beeminder_list_goals",
        "beeminder_get_goal",
        "beeminder_list_datapoints",
        "clockify_list_workspaces",
        "clockify_list_users",
        "clockify_list_projects",
        "clockify_list_time_entries",
        "harvest_get_me",
        "harvest_get_company",
        "harvest_list_clients",
        "harvest_list_projects",
        "harvest_list_tasks",
        "harvest_list_time_entries",
        "oura_get_profile",
        "oura_get_daily_activity",
        "oura_get_daily_readiness",
        "oura_get_daily_sleep",
        "strava_list_activities",
        "strava_get_activity",
        "strava_list_activity_comments",
        "strava_get_activity_streams",
        "homeassistant_get_config",
        "homeassistant_list_states",
        "homeassistant_get_state",
        "homeassistant_list_services",
        "homeassistant_list_events",
        "homeassistant_get_logbook",
        "philips_hue_list_lights",
        "philips_hue_get_light",
        "activecampaign_list_contacts",
        "activecampaign_get_contact",
        "activecampaign_list_lists",
        "activecampaign_list_tags",
        "convertkit_get_account",
        "convertkit_list_forms",
        "convertkit_list_tags",
        "convertkit_list_subscribers",
        "getresponse_list_campaigns",
        "getresponse_list_contacts",
        "getresponse_get_contact",
        "mailerlite_list_subscribers",
        "mailerlite_get_subscriber",
        "mailerlite_list_groups",
        "github_get_repository",
        "github_search_repositories",
        "github_list_issues",
        "github_get_issue",
        "github_list_pull_requests",
        "github_list_releases",
        "github_get_release",
        "gitlab_get_project",
        "gitlab_search_projects",
        "gitlab_list_project_issues",
        "gitlab_get_project_issue",
        "gitlab_list_project_releases",
        "gitlab_get_project_release",
        "gitlab_list_user_projects",
        "bitly_get_bitlink",
        "brandfetch_get_brand",
        "brandfetch_get_brand_logos",
        "brandfetch_get_brand_colors",
        "marketstack_get_eod",
        "marketstack_get_ticker",
        "marketstack_get_exchange",
        "deepl_list_languages",
        "todoist_list_tasks",
        "todoist_get_task",
        "todoist_list_projects",
        "todoist_get_project",
        "trello_search",
        "trello_get_board",
        "trello_list_board_lists",
        "trello_list_cards",
        "trello_get_card",
        "asana_get_user",
        "asana_list_users",
        "asana_list_projects",
        "asana_get_project",
        "asana_list_tasks",
        "asana_search_tasks",
        "asana_get_task",
        "linear_list_teams",
        "linear_list_users",
        "linear_list_workflow_states",
        "linear_list_issues",
        "linear_get_issue",
        "jira_get_myself",
        "jira_list_projects",
        "jira_search_issues",
        "jira_get_issue",
        "jira_list_issue_transitions",
        "jira_list_users",
        "jira_list_issue_comments",
        "clickup_list_teams",
        "clickup_list_spaces",
        "clickup_list_folders",
        "clickup_list_lists",
        "clickup_get_task",
        "clickup_list_tasks",
        "clickup_list_task_comments",
        "slack_list_channels",
        "slack_get_channel_history",
        "slack_search_messages",
        "slack_list_users",
        "slack_get_user",
        "notion_search",
        "notion_get_page",
        "notion_get_block_children",
        "notion_query_data_source",
        "airtable_list_bases",
        "airtable_get_base_schema",
        "airtable_list_records",
        "airtable_get_record",
        "hubspot_list_crm_objects",
        "hubspot_search_crm_objects",
        "hubspot_get_crm_object",
        "zendesk_search",
        "zendesk_get_ticket",
        "zendesk_list_tickets",
        "zendesk_get_user",
        "zendesk_search_users",
        "mailchimp_list_audiences",
        "mailchimp_list_members",
        "mailchimp_get_member",
        "mailchimp_list_campaigns",
        "freshdesk_list_tickets",
        "freshdesk_search_tickets",
        "freshdesk_get_ticket",
        "freshdesk_list_contacts",
        "freshdesk_get_contact",
        "helpscout_list_mailboxes",
        "helpscout_get_mailbox",
        "helpscout_list_conversations",
        "helpscout_get_conversation",
        "helpscout_list_customers",
        "helpscout_get_customer",
        "intercom_list_contacts",
        "intercom_search_contacts",
        "intercom_get_contact",
        "intercom_list_conversations",
        "intercom_get_conversation",
        "pipedrive_list_records",
        "pipedrive_search_records",
        "pipedrive_get_record",
        "pipedrive_list_users",
        "twilio_list_messages",
        "twilio_get_message",
        "sendgrid_list_contacts",
        "sendgrid_get_contact",
        "sendgrid_list_lists",
        "mailgun_list_events",
        "mailgun_get_domain",
        "brevo_list_contacts",
        "brevo_get_contact",
        "brevo_list_senders",
        "mailjet_list_contacts",
        "mailjet_get_contact",
        "messagebird_get_balance",
        "mocean_get_balance",
        "stripe_list_records",
        "stripe_search_records",
        "stripe_get_record",
        "stripe_get_balance",
        "shopify_list_records",
        "shopify_get_record",
        "woocommerce_list_records",
        "woocommerce_get_record",
        "chargebee_list_records",
        "chargebee_get_record",
        "pushbullet_list_pushes",
        "gotify_list_messages",
        "wordpress_list_records",
        "wordpress_get_record",
        "strapi_list_entries",
        "strapi_get_entry",
        "contentful_list_records",
        "contentful_get_record",
        "ghost_list_posts",
        "ghost_get_post",
        "storyblok_list_stories",
        "storyblok_get_story",
        "netlify_list_sites",
        "netlify_get_site",
        "netlify_list_deploys",
        "netlify_get_deploy",
        "uptimerobot_get_account",
        "uptimerobot_list_monitors",
        "uptimerobot_get_monitor",
        "pagerduty_list_incidents",
        "pagerduty_get_incident",
        "pagerduty_list_services",
        "pagerduty_get_user",
        "sentry_list_organizations",
        "sentry_list_projects",
        "sentry_list_project_issues",
        "sentry_get_issue",
        "sentry_list_project_events",
        "sentry_get_event",
        "cloudflare_list_zones",
        "cloudflare_list_dns_records",
        "cloudflare_list_origin_certificates",
        "cloudflare_get_origin_certificate",
        "urlscan_search_scans",
        "urlscan_get_result",
        "hunter_domain_search",
        "hunter_email_finder",
        "hunter_email_verifier",
        "mailcheck_check_email",
        "peekalink_preview_url",
        "peekalink_check_availability",
        "jina_reader_fetch_url",
        "jina_search_web",
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
        "discord_list_guild_channels",
        "discord_get_channel",
        "discord_get_channel_messages",
        "mattermost_get_me",
        "mattermost_list_teams",
        "mattermost_list_channels",
        "mattermost_list_channel_posts",
        "matrix_whoami",
        "matrix_list_joined_rooms",
        "matrix_get_room_messages",
        "rocketchat_get_me",
        "rocketchat_list_channels",
        "rocketchat_get_channel_history",
        "zulip_get_profile",
        "zulip_list_streams",
        "zulip_get_messages",
    }
)

_TOOL_CONFIG_SCHEMAS: Mapping[str, Dict[str, Any]] = {
    "bash_execute": {
        "type": "object",
        "properties": {
            "timeout_seconds": {
                "type": "integer",
                "description": "Command timeout in seconds",
                "default": 120,
                "minimum": 1,
                "maximum": 600,
            }
        },
    },
    "image_generate": {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "title": "Provider",
                "description": "Image generation provider. API keys are read from OPENAI_API_KEY or GEMINI_API_KEY.",
                "enum": ["openai", "gemini"],
                "default": "openai",
            },
            "native_context_enabled": {
                "type": "boolean",
                "title": "Native Image Context",
                "description": "Let vision-capable chat models inspect generated images on the next reasoning step.",
                "default": True,
            },
            "openai_model": {
                "type": "string",
                "title": "OpenAI Model",
                "description": "OpenAI GPT Image model used when Provider is openai.",
                "enum": ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"],
                "default": "gpt-image-2",
            },
            "openai_size": {
                "type": "string",
                "title": "OpenAI Size",
                "description": "Image size for OpenAI generation.",
                "enum": [
                    "auto",
                    "1024x1024",
                    "1536x1024",
                    "1024x1536",
                    "2048x2048",
                    "2048x1152",
                    "3840x2160",
                    "2160x3840",
                ],
                "default": "auto",
            },
            "openai_quality": {
                "type": "string",
                "title": "OpenAI Quality",
                "description": "Rendering quality for OpenAI generation.",
                "enum": ["auto", "low", "medium", "high"],
                "default": "auto",
            },
            "openai_output_format": {
                "type": "string",
                "title": "OpenAI Output Format",
                "description": "File format for OpenAI output.",
                "enum": ["png", "jpeg", "webp"],
                "default": "png",
            },
            "openai_moderation": {
                "type": "string",
                "title": "OpenAI Moderation",
                "description": "Moderation strictness for OpenAI image generation.",
                "enum": ["auto", "low"],
                "default": "auto",
            },
            "gemini_model": {
                "type": "string",
                "title": "Gemini Model",
                "description": "Gemini Nano Banana model used when Provider is gemini.",
                "enum": [
                    "gemini-3-pro-image-preview",
                    "gemini-3.1-flash-image-preview",
                    "gemini-2.5-flash-image",
                ],
                "default": "gemini-3-pro-image-preview",
            },
            "gemini_aspect_ratio": {
                "type": "string",
                "title": "Gemini Aspect Ratio",
                "description": "Aspect ratio for Gemini image generation.",
                "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9"],
                "default": "1:1",
            },
            "gemini_image_size": {
                "type": "string",
                "title": "Gemini Image Size",
                "description": "Output size for Gemini 3 Pro Image and Gemini 3.1 Flash Image. Ignored by Gemini 2.5 Flash Image.",
                "enum": ["auto", "1K", "2K", "4K"],
                "default": "auto",
            },
        },
    },
}

_DESCRIPTION_STOP_MARKERS = (
    "\nArgs:",
    "\nArguments:",
    "\nParameters:",
    "\nReturns:",
    "\nRaises:",
    "\nSecurity:",
    "\nLimits:",
)


def _tool_names(tools: Iterable[Any]) -> Set[str]:
    names: Set[str] = set()
    for tool_obj in tools:
        name = getattr(tool_obj, "name", None)
        if name:
            names.add(str(name))
    return names


def _collection_tool_names(collection: Any) -> Set[str]:
    if collection is None:
        return set()
    if isinstance(collection, dict):
        return _tool_names(collection.values())
    return _tool_names(collection)


def _tools_package() -> Any:
    import importlib

    return importlib.import_module(__package__)


def _registered_builtin_tools() -> tuple[Dict[str, Any], Set[str]]:
    tools_pkg = _tools_package()
    all_tools = {tool_obj.name: tool_obj for tool_obj in tools_pkg.ALL_TOOLS}
    optional_tools = dict(tools_pkg.OPTIONAL_TOOLS)
    all_tools.update(optional_tools)
    return all_tools, set(optional_tools)


def _category_by_tool_name() -> Dict[str, ToolCategory]:
    tools_pkg = _tools_package()
    categories: Dict[str, ToolCategory] = {}
    for category, attr_names in _CATEGORY_GROUPS:
        for attr_name in attr_names:
            for name in _collection_tool_names(getattr(tools_pkg, attr_name, None)):
                categories[name] = category
    return categories


def _description_from_tool(tool_obj: Any) -> str:
    """Return a compact one-line summary derived from a tool description."""
    raw = str(getattr(tool_obj, "description", "") or "").strip()
    for marker in _DESCRIPTION_STOP_MARKERS:
        before, sep, _ = raw.partition(marker)
        if sep:
            raw = before.strip()
    raw = raw.split("\n\n", 1)[0]
    description = " ".join(raw.split())
    if description:
        return description
    return f"Tool: {getattr(tool_obj, 'name', 'unknown')}"


def _infer_security_level(
    tool_name: str,
    category: ToolCategory,
) -> SecurityLevel:
    if category == ToolCategory.SELF_MODIFY:
        return SecurityLevel.SENSITIVE
    if tool_name == "twitch_ban":
        return SecurityLevel.SENSITIVE
    if category in {ToolCategory.PROFILE, ToolCategory.TODO, ToolCategory._PRV_A}:
        return SecurityLevel.SAFE
    if category == ToolCategory.CORE:
        if tool_name in _CORE_MODERATE_TOOL_NAMES:
            return SecurityLevel.MODERATE
        return SecurityLevel.SAFE
    if category == ToolCategory.TRIGGER:
        return SecurityLevel.SAFE if tool_name == "trigger_info" else SecurityLevel.MODERATE
    if category == ToolCategory.EMAIL:
        return SecurityLevel.SAFE if tool_name in _EMAIL_SAFE_TOOL_NAMES else SecurityLevel.MODERATE
    if category == ToolCategory.BROWSER:
        return SecurityLevel.SAFE if tool_name in _BROWSER_SAFE_TOOL_NAMES else SecurityLevel.MODERATE
    if category == ToolCategory.IMAGE:
        return SecurityLevel.MODERATE
    if category == ToolCategory.CALENDAR:
        return SecurityLevel.SAFE if tool_name in _CALENDAR_SAFE_TOOL_NAMES else SecurityLevel.MODERATE
    if category == ToolCategory.GOOGLE_DOCS:
        return (
            SecurityLevel.SAFE
            if tool_name in _GOOGLE_DOCS_SAFE_TOOL_NAMES
            else SecurityLevel.MODERATE
        )
    if category == ToolCategory._PRV_B:
        return SecurityLevel.MODERATE
    if category == ToolCategory.TWITCH:
        return SecurityLevel.SAFE if tool_name.startswith(("twitch_read", "twitch_get")) else SecurityLevel.MODERATE
    if category == ToolCategory.SKILLS:
        return SecurityLevel.MODERATE if tool_name in {"install_skill", "skill_manage"} else SecurityLevel.SAFE
    if category == ToolCategory.INTEGRATIONS:
        return SecurityLevel.SAFE if tool_name in _INTEGRATION_SAFE_TOOL_NAMES else SecurityLevel.MODERATE
    if category == ToolCategory.MCP_SERVER:
        return SecurityLevel.MODERATE if tool_name in {"install_mcp_server", "manage_mcp"} else SecurityLevel.SAFE
    if category == ToolCategory.AUTH:
        return SecurityLevel.MODERATE
    if category == ToolCategory.AUTONOMY:
        return SecurityLevel.MODERATE if tool_name == "watchdog_dispatch" else SecurityLevel.SAFE
    if category in {ToolCategory.THREAD_SPAWN, ToolCategory.CUSTOM}:
        return SecurityLevel.MODERATE
    return SecurityLevel.MODERATE


def _generate_builtin_tool_metadata() -> Dict[str, ToolMetadata]:
    registered_tools, optional_tool_names = _registered_builtin_tools()
    categories = _category_by_tool_name()
    generated: Dict[str, ToolMetadata] = {}
    for name, tool_obj in sorted(registered_tools.items()):
        category = _EXPLICIT_CATEGORY_BY_TOOL_NAME.get(
            name,
            categories.get(name, ToolCategory.CORE),
        )
        security_level = _infer_security_level(name, category)
        generated[name] = ToolMetadata(
            name=name,
            category=category,
            security_level=security_level,
            description=_description_from_tool(tool_obj),
            default_enabled=name not in optional_tool_names,
            config_schema=_TOOL_CONFIG_SCHEMAS.get(name),
        )
    return generated


def refresh_builtin_tool_metadata() -> Dict[str, ToolMetadata]:
    """Regenerate built-in metadata from the currently registered tools."""
    global _REFRESHING_BUILTIN_METADATA
    if _REFRESHING_BUILTIN_METADATA:
        return TOOL_METADATA

    _REFRESHING_BUILTIN_METADATA = True
    try:
        generated = _generate_builtin_tool_metadata()
    except (AttributeError, ImportError):
        return TOOL_METADATA
    finally:
        _REFRESHING_BUILTIN_METADATA = False

    dict.clear(TOOL_METADATA)
    dict.update(TOOL_METADATA, generated)
    return TOOL_METADATA


def get_tool_metadata(tool_name: str) -> Optional[ToolMetadata]:
    """Get built-in metadata for a tool by name."""
    refresh_builtin_tool_metadata()
    return dict.get(TOOL_METADATA, tool_name)


def get_tools_by_category(category: ToolCategory) -> List[str]:
    """Get all built-in tool names in a category."""
    refresh_builtin_tool_metadata()
    return [
        name
        for name, meta in dict.items(TOOL_METADATA)
        if meta.category == category
    ]


def get_sensitive_tools() -> List[str]:
    """Get all built-in tools that require explicit opt-in."""
    refresh_builtin_tool_metadata()
    return [
        name
        for name, meta in dict.items(TOOL_METADATA)
        if meta.security_level == SecurityLevel.SENSITIVE
    ]


def get_default_enabled_tools() -> Set[str]:
    """Get all built-in tools that are enabled by default."""
    refresh_builtin_tool_metadata()
    return {
        name
        for name, meta in dict.items(TOOL_METADATA)
        if meta.default_enabled
    }


def get_all_categories() -> List[str]:
    """Get all category names."""
    return [category.value for category in ToolCategory]


def get_category_tools_summary() -> Dict[str, List[str]]:
    """Get a summary of built-in tools by category."""
    summary: Dict[str, List[str]] = {}
    refresh_builtin_tool_metadata()
    for category in ToolCategory:
        tools = get_tools_by_category(category)
        if tools:
            summary[category.value] = tools
    return summary


# MCP server tool metadata registry
MCP_SERVER_TOOL_METADATA: Dict[str, ToolMetadata] = {}


def register_mcp_server_tool_metadata(
    tool_name: str,
    description: str,
) -> ToolMetadata:
    """Register metadata for an MCP server tool."""
    metadata = ToolMetadata(
        name=tool_name,
        category=ToolCategory.MCP_SERVER,
        security_level=SecurityLevel.MODERATE,
        description=description,
        default_enabled=False,
    )
    MCP_SERVER_TOOL_METADATA[tool_name] = metadata
    return metadata


def unregister_mcp_server_tool_metadata(tool_name: str) -> bool:
    """Unregister metadata for an MCP server tool."""
    if tool_name in MCP_SERVER_TOOL_METADATA:
        del MCP_SERVER_TOOL_METADATA[tool_name]
        return True
    return False


def clear_mcp_server_tool_metadata() -> None:
    """Clear all MCP server tool metadata."""
    MCP_SERVER_TOOL_METADATA.clear()


# Custom tool metadata registry (separate from built-ins)
CUSTOM_TOOL_METADATA: Dict[str, ToolMetadata] = {}


def register_custom_tool_metadata(
    tool_id: str,
    description: str,
    security_level: SecurityLevel = SecurityLevel.MODERATE,
) -> ToolMetadata:
    """
    Register metadata for a custom tool.

    Args:
        tool_id: Unique identifier for the custom tool
        description: Tool description
        security_level: Security level for the tool

    Returns:
        The created ToolMetadata
    """
    metadata = ToolMetadata(
        name=tool_id,
        category=ToolCategory.CUSTOM,
        security_level=security_level,
        description=description,
        default_enabled=False,
    )
    CUSTOM_TOOL_METADATA[tool_id] = metadata
    return metadata


def unregister_custom_tool_metadata(tool_id: str) -> bool:
    """
    Unregister metadata for a custom tool.

    Args:
        tool_id: The tool ID to unregister

    Returns:
        True if tool was unregistered, False if not found
    """
    if tool_id in CUSTOM_TOOL_METADATA:
        del CUSTOM_TOOL_METADATA[tool_id]
        return True
    return False


def clear_custom_tool_metadata() -> None:
    """Clear all custom tool metadata."""
    CUSTOM_TOOL_METADATA.clear()


def get_all_tool_metadata(tool_name: str) -> Optional[ToolMetadata]:
    """
    Get metadata for any tool (built-in, MCP server, or custom).

    Args:
        tool_name: Name/ID of the tool

    Returns:
        ToolMetadata if found, None otherwise
    """
    builtin = get_tool_metadata(tool_name)
    if builtin is not None:
        return builtin
    if tool_name in MCP_SERVER_TOOL_METADATA:
        return MCP_SERVER_TOOL_METADATA[tool_name]
    return CUSTOM_TOOL_METADATA.get(tool_name)
