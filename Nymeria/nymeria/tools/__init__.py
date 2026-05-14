"""Nymeria tools module.

Consolidated tool set for maximum autonomy with minimal complexity.
Callable threads replace the old sub-agent system. Any thread can become
a callable tool with its own system prompt, LLM config, and tool set.

Callable thread tools are added per-graph in _build_graph_with_prompt(), not globally.
"""

from .bash import bash_execute
from .filesystem import file_read, file_write
from .file_edit import file_edit, FILE_EDIT_TOOLS
from .web import web_search
from .think import consult, CONSULT_TOOLS
from .claude_code import claude_code
from .memory import (
    memory_add,
    memory_edit,
    memory_read,
    memory_clear_all,
    personality_set,
    rag_search,
    rag_settings,
    MEMORY_TOOLS,
)
from .todo import (
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
    TODO_TOOLS,
)
from .runtime_admin import (
    reload_all,
    self_modify_rollback,
    RUNTIME_ADMIN_TOOLS,
)
from .outlook_auth import AUTH_TOOLS
from .outlook_email import EMAIL_TOOLS
from .browser import BROWSER_TOOLS
from .calendar import CALENDAR_TOOLS
from .notify import notify, NOTIFY_TOOLS
from .triggers import (
    trigger_config,
    trigger_info,
    TRIGGER_TOOLS,
)
from .hello_test import hello_test
from .sticky_note import sticky_note, STICKY_NOTE_TOOLS
from .google_docs import GOOGLE_DOCS_TOOLS
from .google_sheets import GOOGLE_SHEETS_TOOLS
from .gmail_auth import (
    gmail_auth_start,
    gmail_auth_complete,
    gmail_auth_clear,
    gmail_list_accounts,
    GMAIL_AUTH_TOOLS,
)
from ..plugins._prv_a import (
    _PRV_TOOLS_A1,
    _PRV_TOOLS_A2,
    _PRV_TOOLS_A3,
    _PRV_TOOLS_A4,
    _PRV_TOOLS_A5,
)
from .outlook_attachments import OUTLOOK_ATTACHMENT_TOOLS
from .twitch import TWITCH_TOOLS
from .slash_command import slash_command, SLASH_COMMAND_TOOLS
from .tool_search import tool_enable, tool_search, TOOL_SEARCH_TOOLS
from .http_api import http_request, api_discover, HTTP_API_TOOLS
from .tool_create import tool_create, TOOL_CREATE_TOOLS
from .auth_manager import auth_manager, AUTH_MANAGER_TOOLS
from ._prv_b import _PRV_TOOLS_B
from .skill_config import (
    skill_config,
    skill_kit_create,
    SKILL_CONFIG_TOOLS,
    SKILL_KIT_CREATE_TOOLS,
)
from .search_skills import (
    skill_manage,
    list_installed_skills,
    search_skills,
    install_skill,
    SEARCH_SKILLS_TOOLS,
)
from .search_mcp import (
    mcp_manage,
    search_mcp,
    install_mcp_server,
    SEARCH_MCP_TOOLS,
)
from .activity_feed import activity_feed, ACTIVITY_FEED_TOOLS
from .watchdog_dispatch import watchdog_dispatch, watchdog_read_notepad, watchdog_todo_overview, WATCHDOG_DISPATCH_TOOLS
from .spawn_thread import spawn_thread, SPAWN_THREAD_TOOLS
from .image_generation import image_generate, IMAGE_GENERATION_TOOLS
from .utility_integrations import (
    calculator,
    wikipedia_search,
    wolfram_alpha_query,
    searxng_search,
    UTILITY_INTEGRATION_TOOLS,
)
from .public_info_integrations import (
    coingecko_price,
    coingecko_coin_markets,
    hackernews_search,
    hackernews_get_item,
    hackernews_get_user,
    npm_package_info,
    npm_package_search,
    open_thesaurus_synonyms,
    rss_feed_read,
    nasa_apod,
    openweathermap_current,
    openweathermap_forecast,
    quickchart_create_url,
    PUBLIC_INFO_TOOLS,
)
from .developer_platform_integrations import (
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
    DEVELOPER_PLATFORM_TOOLS,
)
from .business_service_integrations import (
    bitly_get_bitlink,
    bitly_create_bitlink,
    bitly_update_bitlink,
    brandfetch_get_brand,
    brandfetch_get_brand_logos,
    brandfetch_get_brand_colors,
    marketstack_get_eod,
    marketstack_get_ticker,
    marketstack_get_exchange,
    deepl_translate_text,
    deepl_list_languages,
    BUSINESS_SERVICE_TOOLS,
)
from .productivity_service_integrations import (
    todoist_list_tasks,
    todoist_get_task,
    todoist_create_task,
    todoist_update_task,
    todoist_close_task,
    todoist_list_projects,
    todoist_get_project,
    todoist_create_project,
    trello_search,
    trello_get_board,
    trello_list_board_lists,
    trello_list_cards,
    trello_get_card,
    trello_create_card,
    trello_update_card,
    trello_add_card_comment,
    PRODUCTIVITY_SERVICE_TOOLS,
)
from .work_tracking_service_integrations import (
    asana_get_user,
    asana_list_users,
    asana_list_projects,
    asana_get_project,
    asana_create_project,
    asana_update_project,
    asana_list_tasks,
    asana_search_tasks,
    asana_get_task,
    asana_create_task,
    asana_create_subtask,
    asana_update_task,
    asana_add_task_comment,
    linear_list_teams,
    linear_list_users,
    linear_list_workflow_states,
    linear_list_issues,
    linear_get_issue,
    linear_create_issue,
    linear_update_issue,
    linear_add_issue_comment,
    linear_add_issue_link,
    WORK_TRACKING_SERVICE_TOOLS,
)
from .project_management_service_integrations import (
    jira_get_myself,
    jira_list_projects,
    jira_search_issues,
    jira_get_issue,
    jira_create_issue,
    jira_update_issue,
    jira_list_issue_transitions,
    jira_list_users,
    jira_list_issue_comments,
    jira_add_issue_comment,
    clickup_list_teams,
    clickup_list_spaces,
    clickup_list_folders,
    clickup_list_lists,
    clickup_get_task,
    clickup_list_tasks,
    clickup_create_task,
    clickup_update_task,
    clickup_list_task_comments,
    clickup_add_task_comment,
    PROJECT_MANAGEMENT_SERVICE_TOOLS,
)
from .collaboration_data_service_integrations import (
    slack_list_channels,
    slack_get_channel_history,
    slack_search_messages,
    slack_list_users,
    slack_get_user,
    slack_post_message,
    slack_update_message,
    slack_add_reaction,
    notion_search,
    notion_get_page,
    notion_get_block_children,
    notion_query_data_source,
    notion_create_page,
    notion_update_page,
    notion_append_block_children,
    airtable_list_bases,
    airtable_get_base_schema,
    airtable_list_records,
    airtable_get_record,
    airtable_create_records,
    airtable_update_records,
    airtable_delete_record,
    COLLABORATION_DATA_SERVICE_TOOLS,
)
from .customer_engagement_service_integrations import (
    hubspot_list_crm_objects,
    hubspot_search_crm_objects,
    hubspot_get_crm_object,
    hubspot_create_crm_object,
    hubspot_update_crm_object,
    hubspot_archive_crm_object,
    zendesk_search,
    zendesk_get_ticket,
    zendesk_list_tickets,
    zendesk_create_ticket,
    zendesk_update_ticket,
    zendesk_get_user,
    zendesk_search_users,
    mailchimp_list_audiences,
    mailchimp_list_members,
    mailchimp_get_member,
    mailchimp_add_or_update_member,
    mailchimp_update_member_tags,
    mailchimp_list_campaigns,
    CUSTOMER_ENGAGEMENT_SERVICE_TOOLS,
)
from .support_service_integrations import (
    freshdesk_list_tickets,
    freshdesk_search_tickets,
    freshdesk_get_ticket,
    freshdesk_create_ticket,
    freshdesk_update_ticket,
    freshdesk_delete_ticket,
    freshdesk_list_contacts,
    freshdesk_get_contact,
    freshdesk_create_contact,
    freshdesk_update_contact,
    helpscout_list_mailboxes,
    helpscout_get_mailbox,
    helpscout_list_conversations,
    helpscout_get_conversation,
    helpscout_create_conversation,
    helpscout_create_thread,
    helpscout_list_customers,
    helpscout_get_customer,
    helpscout_create_customer,
    helpscout_update_customer,
    intercom_list_contacts,
    intercom_search_contacts,
    intercom_get_contact,
    intercom_create_contact,
    intercom_update_contact,
    intercom_archive_contact,
    intercom_list_conversations,
    intercom_get_conversation,
    intercom_reply_conversation,
    SUPPORT_SERVICE_TOOLS,
)
from .sales_crm_service_integrations import (
    pipedrive_list_records,
    pipedrive_search_records,
    pipedrive_get_record,
    pipedrive_create_record,
    pipedrive_update_record,
    pipedrive_delete_record,
    pipedrive_list_users,
    SALES_CRM_SERVICE_TOOLS,
)
from .messaging_delivery_service_integrations import (
    twilio_send_message,
    twilio_list_messages,
    twilio_get_message,
    twilio_make_call,
    sendgrid_send_email,
    sendgrid_list_contacts,
    sendgrid_get_contact,
    sendgrid_upsert_contacts,
    sendgrid_list_lists,
    mailgun_send_email,
    mailgun_list_events,
    mailgun_get_domain,
    brevo_send_email,
    brevo_list_contacts,
    brevo_get_contact,
    brevo_create_contact,
    brevo_update_contact,
    brevo_list_senders,
    mailjet_send_email,
    mailjet_send_sms,
    mailjet_list_contacts,
    mailjet_get_contact,
    mandrill_send_email,
    mandrill_send_template,
    messagebird_send_sms,
    messagebird_get_balance,
    mocean_send_sms,
    mocean_send_voice,
    mocean_get_balance,
    msg91_send_sms,
    MESSAGING_DELIVERY_SERVICE_TOOLS,
)
from .commerce_billing_service_integrations import (
    stripe_list_records,
    stripe_search_records,
    stripe_get_record,
    stripe_get_balance,
    stripe_create_customer,
    stripe_update_customer,
    shopify_list_records,
    shopify_get_record,
    shopify_create_product,
    shopify_update_product,
    woocommerce_list_records,
    woocommerce_get_record,
    woocommerce_create_record,
    woocommerce_update_record,
    chargebee_list_records,
    chargebee_get_record,
    chargebee_create_customer,
    chargebee_update_customer,
    COMMERCE_BILLING_SERVICE_TOOLS,
)
from ..core.self_agent import SELF_AGENT_TOOLS

WATCHDOG_TOOLS = ACTIVITY_FEED_TOOLS + WATCHDOG_DISPATCH_TOOLS

# Combined Outlook tools list
OUTLOOK_TOOLS = AUTH_TOOLS + EMAIL_TOOLS

# Combined _PRV_A tools list (all Google Sheets-based _PRV_A tools)
_PRV_TOOLS_A = (
    GOOGLE_SHEETS_TOOLS
    + _PRV_TOOLS_A1
    + _PRV_TOOLS_A2
    + _PRV_TOOLS_A3
    + _PRV_TOOLS_A4
    + _PRV_TOOLS_A5
)

# Optional tools — available for per-thread enabling but NOT loaded by default.
# Maps tool name -> tool object. Users enable these via thread config UI.
OPTIONAL_TOOLS = {t.name: t for t in (
    [claude_code, sticky_note, hello_test, memory_clear_all, rag_settings]
    + FILE_EDIT_TOOLS
    + OUTLOOK_TOOLS
    + GMAIL_AUTH_TOOLS
    + OUTLOOK_ATTACHMENT_TOOLS
    + TRIGGER_TOOLS
    + BROWSER_TOOLS
    + CALENDAR_TOOLS
    + SELF_AGENT_TOOLS
    + RUNTIME_ADMIN_TOOLS
    + GOOGLE_DOCS_TOOLS
    + _PRV_TOOLS_A
    + TWITCH_TOOLS
    + SLASH_COMMAND_TOOLS
    + TOOL_SEARCH_TOOLS
    + SEARCH_SKILLS_TOOLS
    + SEARCH_MCP_TOOLS
    + HTTP_API_TOOLS
    + TOOL_CREATE_TOOLS
    + AUTH_MANAGER_TOOLS
    + _PRV_TOOLS_B
    + SKILL_CONFIG_TOOLS
    + SKILL_KIT_CREATE_TOOLS
    + WATCHDOG_TOOLS
    + SPAWN_THREAD_TOOLS
    + IMAGE_GENERATION_TOOLS
    + UTILITY_INTEGRATION_TOOLS
    + PUBLIC_INFO_TOOLS
    + DEVELOPER_PLATFORM_TOOLS
    + BUSINESS_SERVICE_TOOLS
    + PRODUCTIVITY_SERVICE_TOOLS
    + WORK_TRACKING_SERVICE_TOOLS
    + PROJECT_MANAGEMENT_SERVICE_TOOLS
    + COLLABORATION_DATA_SERVICE_TOOLS
    + CUSTOMER_ENGAGEMENT_SERVICE_TOOLS
    + SUPPORT_SERVICE_TOOLS
    + SALES_CRM_SERVICE_TOOLS
    + MESSAGING_DELIVERY_SERVICE_TOOLS
    + COMMERCE_BILLING_SERVICE_TOOLS
)}

# Capability expansion tools are deliberately opt-in through the bundled
# self-improve Skill Kit. They remain valid explicit per-thread enablements for
# compatibility, but should not live in profile default_thread_tools.
CAPABILITY_EXPANSION_TOOL_NAMES = frozenset(
    t.name
    for t in (
        TOOL_SEARCH_TOOLS
        + SEARCH_MCP_TOOLS
        + SEARCH_SKILLS_TOOLS
        + HTTP_API_TOOLS
        + TOOL_CREATE_TOOLS
        + SKILL_CONFIG_TOOLS
        + SKILL_KIT_CREATE_TOOLS
    )
)

# Tools that mutate the running codebase (read/write/delete project source,
# reload modules, roll back self-modifications, run arbitrary bash via
# claude_code). In multi-user mode these are admin-only — a non-admin
# enabling them on their own thread would effectively be authenticated
# remote code modification of the shared backend. Enforced at every API
# write boundary (thread config, default tool set, unified enable), at
# every agent-callable write site (tool_search, spawn_thread, slash
# /tools), and as defense-in-depth at graph-build time. Names — not tool
# objects — so the gate survives reload_all().
ADMIN_ONLY_OPTIONAL_TOOL_NAMES = frozenset(
    [t.name for t in (SELF_AGENT_TOOLS + RUNTIME_ADMIN_TOOLS)] + [claude_code.name]
)

# Optional tools that exist for development/regression validation rather than
# production use. Admins can still discover and bind them when deliberately
# testing dynamic tool loading; regular users should not see or enable them.
DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES = frozenset([hello_test.name])


def filter_developer_only_tools(
    tool_names,
    user_role: str,
) -> tuple[set, set]:
    """Filter developer-only diagnostic tool names out for non-admin users."""
    names = set(tool_names)
    if user_role == "admin":
        return names, set()
    blocked = names & DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES
    return names - blocked, blocked


def filter_discoverable_optional_tool_names(
    tool_names,
    user_role: str,
) -> set:
    """Return optional tool names that should be shown in discovery surfaces."""
    allowed, _ = filter_developer_only_tools(tool_names, user_role)
    return allowed


def filter_admin_only_tools(
    tool_names,
    user_role: str,
) -> tuple[set, set]:
    """Filter admin-only tool names out for non-admin users.

    Returns ``(allowed, blocked)``: the input names split into a set the
    caller may have, and a set the caller is not allowed to enable.
    Admins see everything allowed; for any other role, names in
    ``ADMIN_ONLY_OPTIONAL_TOOL_NAMES`` are stripped into ``blocked``.

    This is the single chokepoint shared by tool_search, spawn_thread, the
    REST gate at PATCH /threads/{id}/config, and the graph-build defense-
    in-depth filter. Keeping the logic here means a future addition to the
    admin-only set propagates everywhere.
    """
    names = set(tool_names)
    if user_role == "admin":
        return names, set()
    blocked = names & ADMIN_ONLY_OPTIONAL_TOOL_NAMES
    return names - blocked, blocked

# All available tools
ALL_TOOLS = [
    # Core system tools
    bash_execute,
    file_read,
    file_write,
    web_search,
    consult,
    # Memory tools (unified profile + thread-notepad CRUD)
    memory_add,
    memory_edit,
    memory_read,
    personality_set,
    rag_search,
    # TODO tools
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
    # Unified notification tool
    notify,
]

__all__ = [
    "bash_execute",
    "file_read",
    "file_write",
    "file_edit",
    "FILE_EDIT_TOOLS",
    "image_generate",
    "IMAGE_GENERATION_TOOLS",
    "calculator",
    "wikipedia_search",
    "wolfram_alpha_query",
    "searxng_search",
    "UTILITY_INTEGRATION_TOOLS",
    "coingecko_price",
    "coingecko_coin_markets",
    "hackernews_search",
    "hackernews_get_item",
    "hackernews_get_user",
    "npm_package_info",
    "npm_package_search",
    "open_thesaurus_synonyms",
    "rss_feed_read",
    "nasa_apod",
    "openweathermap_current",
    "openweathermap_forecast",
    "quickchart_create_url",
    "PUBLIC_INFO_TOOLS",
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
    "DEVELOPER_PLATFORM_TOOLS",
    "bitly_get_bitlink",
    "bitly_create_bitlink",
    "bitly_update_bitlink",
    "brandfetch_get_brand",
    "brandfetch_get_brand_logos",
    "brandfetch_get_brand_colors",
    "marketstack_get_eod",
    "marketstack_get_ticker",
    "marketstack_get_exchange",
    "deepl_translate_text",
    "deepl_list_languages",
    "BUSINESS_SERVICE_TOOLS",
    "todoist_list_tasks",
    "todoist_get_task",
    "todoist_create_task",
    "todoist_update_task",
    "todoist_close_task",
    "todoist_list_projects",
    "todoist_get_project",
    "todoist_create_project",
    "trello_search",
    "trello_get_board",
    "trello_list_board_lists",
    "trello_list_cards",
    "trello_get_card",
    "trello_create_card",
    "trello_update_card",
    "trello_add_card_comment",
    "PRODUCTIVITY_SERVICE_TOOLS",
    "asana_get_user",
    "asana_list_users",
    "asana_list_projects",
    "asana_get_project",
    "asana_create_project",
    "asana_update_project",
    "asana_list_tasks",
    "asana_search_tasks",
    "asana_get_task",
    "asana_create_task",
    "asana_create_subtask",
    "asana_update_task",
    "asana_add_task_comment",
    "linear_list_teams",
    "linear_list_users",
    "linear_list_workflow_states",
    "linear_list_issues",
    "linear_get_issue",
    "linear_create_issue",
    "linear_update_issue",
    "linear_add_issue_comment",
    "linear_add_issue_link",
    "WORK_TRACKING_SERVICE_TOOLS",
    "jira_get_myself",
    "jira_list_projects",
    "jira_search_issues",
    "jira_get_issue",
    "jira_create_issue",
    "jira_update_issue",
    "jira_list_issue_transitions",
    "jira_list_users",
    "jira_list_issue_comments",
    "jira_add_issue_comment",
    "clickup_list_teams",
    "clickup_list_spaces",
    "clickup_list_folders",
    "clickup_list_lists",
    "clickup_get_task",
    "clickup_list_tasks",
    "clickup_create_task",
    "clickup_update_task",
    "clickup_list_task_comments",
    "clickup_add_task_comment",
    "PROJECT_MANAGEMENT_SERVICE_TOOLS",
    "slack_list_channels",
    "slack_get_channel_history",
    "slack_search_messages",
    "slack_list_users",
    "slack_get_user",
    "slack_post_message",
    "slack_update_message",
    "slack_add_reaction",
    "notion_search",
    "notion_get_page",
    "notion_get_block_children",
    "notion_query_data_source",
    "notion_create_page",
    "notion_update_page",
    "notion_append_block_children",
    "airtable_list_bases",
    "airtable_get_base_schema",
    "airtable_list_records",
    "airtable_get_record",
    "airtable_create_records",
    "airtable_update_records",
    "airtable_delete_record",
    "COLLABORATION_DATA_SERVICE_TOOLS",
    "hubspot_list_crm_objects",
    "hubspot_search_crm_objects",
    "hubspot_get_crm_object",
    "hubspot_create_crm_object",
    "hubspot_update_crm_object",
    "hubspot_archive_crm_object",
    "zendesk_search",
    "zendesk_get_ticket",
    "zendesk_list_tickets",
    "zendesk_create_ticket",
    "zendesk_update_ticket",
    "zendesk_get_user",
    "zendesk_search_users",
    "mailchimp_list_audiences",
    "mailchimp_list_members",
    "mailchimp_get_member",
    "mailchimp_add_or_update_member",
    "mailchimp_update_member_tags",
    "mailchimp_list_campaigns",
    "CUSTOMER_ENGAGEMENT_SERVICE_TOOLS",
    "freshdesk_list_tickets",
    "freshdesk_search_tickets",
    "freshdesk_get_ticket",
    "freshdesk_create_ticket",
    "freshdesk_update_ticket",
    "freshdesk_delete_ticket",
    "freshdesk_list_contacts",
    "freshdesk_get_contact",
    "freshdesk_create_contact",
    "freshdesk_update_contact",
    "helpscout_list_mailboxes",
    "helpscout_get_mailbox",
    "helpscout_list_conversations",
    "helpscout_get_conversation",
    "helpscout_create_conversation",
    "helpscout_create_thread",
    "helpscout_list_customers",
    "helpscout_get_customer",
    "helpscout_create_customer",
    "helpscout_update_customer",
    "intercom_list_contacts",
    "intercom_search_contacts",
    "intercom_get_contact",
    "intercom_create_contact",
    "intercom_update_contact",
    "intercom_archive_contact",
    "intercom_list_conversations",
    "intercom_get_conversation",
    "intercom_reply_conversation",
    "SUPPORT_SERVICE_TOOLS",
    "pipedrive_list_records",
    "pipedrive_search_records",
    "pipedrive_get_record",
    "pipedrive_create_record",
    "pipedrive_update_record",
    "pipedrive_delete_record",
    "pipedrive_list_users",
    "SALES_CRM_SERVICE_TOOLS",
    "twilio_send_message",
    "twilio_list_messages",
    "twilio_get_message",
    "twilio_make_call",
    "sendgrid_send_email",
    "sendgrid_list_contacts",
    "sendgrid_get_contact",
    "sendgrid_upsert_contacts",
    "sendgrid_list_lists",
    "mailgun_send_email",
    "mailgun_list_events",
    "mailgun_get_domain",
    "brevo_send_email",
    "brevo_list_contacts",
    "brevo_get_contact",
    "brevo_create_contact",
    "brevo_update_contact",
    "brevo_list_senders",
    "mailjet_send_email",
    "mailjet_send_sms",
    "mailjet_list_contacts",
    "mailjet_get_contact",
    "mandrill_send_email",
    "mandrill_send_template",
    "messagebird_send_sms",
    "messagebird_get_balance",
    "mocean_send_sms",
    "mocean_send_voice",
    "mocean_get_balance",
    "msg91_send_sms",
    "MESSAGING_DELIVERY_SERVICE_TOOLS",
    "stripe_list_records",
    "stripe_search_records",
    "stripe_get_record",
    "stripe_get_balance",
    "stripe_create_customer",
    "stripe_update_customer",
    "shopify_list_records",
    "shopify_get_record",
    "shopify_create_product",
    "shopify_update_product",
    "woocommerce_list_records",
    "woocommerce_get_record",
    "woocommerce_create_record",
    "woocommerce_update_record",
    "chargebee_list_records",
    "chargebee_get_record",
    "chargebee_create_customer",
    "chargebee_update_customer",
    "COMMERCE_BILLING_SERVICE_TOOLS",
    "web_search",
    "consult",
    "CONSULT_TOOLS",
    "claude_code",
    "memory_add",
    "memory_edit",
    "memory_read",
    "memory_clear_all",
    "personality_set",
    "rag_search",
    "rag_settings",
    "MEMORY_TOOLS",
    "nym_todo",
    "nym_todo_delete",
    "nym_todo_list",
    "TODO_TOOLS",
    "reload_all",
    "self_modify_rollback",
    "RUNTIME_ADMIN_TOOLS",
    "notify",
    "NOTIFY_TOOLS",
    "trigger_config",
    "trigger_info",
    "TRIGGER_TOOLS",
    "AUTH_TOOLS",
    "EMAIL_TOOLS",
    "OUTLOOK_TOOLS",
    "BROWSER_TOOLS",
    "CALENDAR_TOOLS",
    "SELF_AGENT_TOOLS",
    "OPTIONAL_TOOLS",
    "ADMIN_ONLY_OPTIONAL_TOOL_NAMES",
    "DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES",
    "CAPABILITY_EXPANSION_TOOL_NAMES",
    "filter_admin_only_tools",
    "filter_developer_only_tools",
    "filter_discoverable_optional_tool_names",
    "ALL_TOOLS",
    "hello_test",
    "sticky_note",
    "STICKY_NOTE_TOOLS",
    "GOOGLE_DOCS_TOOLS",
    "GOOGLE_SHEETS_TOOLS",
    "gmail_auth_start",
    "gmail_auth_complete",
    "gmail_auth_clear",
    "gmail_list_accounts",
    "GMAIL_AUTH_TOOLS",
    "_PRV_TOOLS_A1",
    "_PRV_TOOLS_A2",
    "_PRV_TOOLS_A3",
    "_PRV_TOOLS_A4",
    "_PRV_TOOLS_A5",
    "_PRV_TOOLS_A",
    "TWITCH_TOOLS",
    "slash_command",
    "SLASH_COMMAND_TOOLS",
    "tool_search",
    "tool_enable",
    "TOOL_SEARCH_TOOLS",
    "http_request",
    "api_discover",
    "HTTP_API_TOOLS",
    "tool_create",
    "TOOL_CREATE_TOOLS",
    "auth_manager",
    "AUTH_MANAGER_TOOLS",
    "_PRV_TOOLS_B",
    "skill_config",
    "skill_kit_create",
    "SKILL_CONFIG_TOOLS",
    "SKILL_KIT_CREATE_TOOLS",
    "skill_manage",
    "list_installed_skills",
    "search_skills",
    "install_skill",
    "SEARCH_SKILLS_TOOLS",
    "mcp_manage",
    "search_mcp",
    "install_mcp_server",
    "SEARCH_MCP_TOOLS",
    "activity_feed",
    "ACTIVITY_FEED_TOOLS",
    "watchdog_dispatch",
    "watchdog_read_notepad",
    "watchdog_todo_overview",
    "WATCHDOG_DISPATCH_TOOLS",
    "WATCHDOG_TOOLS",
    "spawn_thread",
    "SPAWN_THREAD_TOOLS",
]
