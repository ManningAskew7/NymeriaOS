# Nymeria Tools Reference

Nymeria has a three-tier tool system: **core tools** always loaded, **dynamic callable thread tools** (one per callable thread), and a large set of **optional tools** available for per-thread enabling. Treat the counts below as approximate only when noted, because the optional surface evolves over time.

## Summary Table

### Core Tools

| # | Tool | Category | Security | Default | Description |
|---|------|----------|----------|---------|-------------|
| 1 | `bash_execute` | Core | MODERATE | On | Execute shell commands |
| 2 | `file_read` | Core | SAFE | On | Read file contents |
| 3 | `file_write` | Core | MODERATE | On | Write content to files |
| 4 | `web_search` | Core | SAFE | On | Search the web via Perplexity |
| 5 | `consult` | Core | SAFE | On | Ask Gemini for a second opinion (OpenRouter) |
| 6 | `memory_add` | Profile | SAFE | On | Save a memory. `scope="global"` (keyed user-profile fact) or `scope="thread"` (per-thread notepad). Empty content deletes. |
| 7 | `memory_edit` | Profile | SAFE | On | Surgical find/replace within an existing memory. Empty `replace` deletes the matched text. |
| 8 | `memory_read` | Profile | SAFE | On | Get one keyed memory, list all, or substring-filter via `query`. |
| 9 | `personality_set` | Profile | SAFE | On | Set communication preferences |
| 10 | `rag_search` | Profile | SAFE | On | Semantic search over past conversations |
| 11 | `nym_todo` | TODO | SAFE | On | Create or update a TODO — scheduled TODOs auto-wake the agent |
| 12 | `nym_todo_delete` | TODO | SAFE | On | Delete a TODO permanently |
| 13 | `nym_todo_list` | TODO | SAFE | On | List TODO items |
| 14 | `notify` | Core | MODERATE | On | Send in-app and external notifications |

> **Skill meta-tool:** A single `Skill(name)` tool is synthesized per-thread at graph-build time when any skills are active — it's not in `ALL_TOOLS`. Its description carries an `<available_skills>` index of `(name, description)` pairs; calling it returns that skill's full SKILL.md body. Skill Kits can additionally declare `metadata.nymeria.required_tools`; activation strictly binds those tools with a TTL before resuming the same turn. See `docs/skills.md`.

> **Capability expansion:** Tool discovery/enabling, MCP management, skill management, API probing, and Skill Kit authoring are no longer default tools. The bundled `self-improve` Skill Kit is enabled by default and binds `tool_search`, `tool_enable`, `manage_mcp`, `skill_manage`, `api_discover`, `http_request`, and `skill_kit_create` only when the agent activates it.

> **Note:** `claude_code`, `reload_all`, and `self_modify_rollback` are **not** in core `ALL_TOOLS`. They live in `OPTIONAL_TOOLS` (`reload_all` / `self_modify_rollback` via `RUNTIME_ADMIN_TOOLS`, `claude_code` directly) and are in `ADMIN_ONLY_OPTIONAL_TOOL_NAMES` — admins can enable them per-thread, non-admins are blocked at every enable boundary. See `nymeria/tools/__init__.py` for the canonical lists.

> **Tool output guard:** After any tool executes, Nymeria truncates oversized `ToolMessage` content before it is stored in thread history. `TOOL_OUTPUT_MAX_CHARS` defaults to `100000`; larger outputs keep the first ~75k and last ~25k characters with a marker showing the original and omitted sizes.

> **CLIProxy OAuth note:** Installed server tools keep the safe dynamic namespace `mcp__<server>__<tool>`. Nymeria-owned helper tools must avoid the `mcp_<name>`, `mcp.<name>`, and `mcp/<name>` namespaces because Claude OAuth classifies those as third-party MCP apps. The consolidated facade is named `manage_mcp`; legacy helpers remain `search_mcp` and `install_mcp_server` for compatibility. The observed probe matrix is documented in `docs/cliproxy.md`.

### Optional: Trigger Tools (2)

Not loaded by default. Enable per-thread via thread config, or use through SelfModifyAgent.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `trigger_config` | Trigger | MODERATE | Create, update, enable/disable, or delete event triggers |
| 2 | `trigger_info` | Trigger | SAFE | List triggers, inspect/test/history for one trigger, or show source schemas |

### Optional: Slash Command Tool (1)

Not loaded by default. Enable per-thread to let the agent invoke the same user-facing slash commands that the Discord/Telegram bots expose.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `slash_command` | Self | MODERATE | Run a Nymeria slash command on the current thread (config, env, tools, memory, TODOs, notepad, status). Destructive commands blocked. |

### Optional: Capability Expansion and Authoring

Not loaded by default. The normal path is to activate `Skill(name="self-improve")`,
which binds the facades below with a TTL.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `tool_search` | Core | SAFE | Search available tools |
| 2 | `tool_enable` | Core | MODERATE | Enable, disable, and inspect current-thread tool bindings |
| 3 | `manage_mcp` | MCP | MODERATE | Search, preview, install, and inspect MCP servers |
| 4 | `skill_manage` | Skills | MODERATE | List, search, install, enable, disable, and inspect skills |
| 5 | `http_request` | Core | MODERATE | Make a one-off HTTP request to a documented API endpoint |
| 6 | `api_discover` | Core | MODERATE | Discover OpenAPI/Swagger metadata for an API base URL |
| 7 | `skill_kit_create` | Custom | MODERATE | Create HTTP tools and package durable Skill Kits |
| 8 | `tool_create` | Custom | MODERATE | Compatibility low-level HTTP tool authoring |
| 9 | `skill_config` | Custom | MODERATE | Compatibility low-level Skill Kit authoring |
| 10 | `search_mcp` / `install_mcp_server` | MCP | SAFE/MODERATE | Compatibility low-level MCP helpers |
| 11 | `list_installed_skills` / `search_skills` / `install_skill` | Skills | SAFE/MODERATE | Compatibility low-level skill helpers |

### Optional: Service Integration Tools (475)

Not loaded by default. These are the first batch of general-purpose utility
integrations and public information services. Tools that need connection details first look
in the credential vault for provider-specific saved connections scoped to
`native_tool:<tool_name>` or `native_tool:*`, then fall back to env settings.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `calculator` | Integrations | SAFE | Evaluate deterministic arithmetic expressions with a safe local parser |
| 2 | `wikipedia_search` | Integrations | SAFE | Search Wikipedia via `langchain_community`'s Wikipedia wrapper |
| 3 | `wolfram_alpha_query` | Integrations | SAFE | Query Wolfram\|Alpha via `langchain_community`; uses vault provider `wolfram_alpha` or `WOLFRAM_ALPHA_APP_ID` |
| 4 | `searxng_search` | Integrations | SAFE | Search a configured SearXNG instance; uses vault provider `searxng` or `SEARXNG_BASE_URL` |
| 5 | `coingecko_price` | Integrations | SAFE | Get current crypto prices from CoinGecko |
| 6 | `coingecko_coin_markets` | Integrations | SAFE | List CoinGecko market data |
| 7 | `hackernews_search` | Integrations | SAFE | Search Hacker News via Algolia |
| 8 | `hackernews_get_item` | Integrations | SAFE | Fetch a Hacker News item by ID |
| 9 | `hackernews_get_user` | Integrations | SAFE | Fetch a Hacker News user profile |
| 10 | `npm_package_info` | Integrations | SAFE | Fetch npm package metadata; optional vault provider `npm` |
| 11 | `npm_package_search` | Integrations | SAFE | Search npm packages; optional vault provider `npm` |
| 12 | `open_thesaurus_synonyms` | Integrations | SAFE | Get German synonyms from OpenThesaurus |
| 13 | `rss_feed_read` | Integrations | MODERATE | Read an RSS/Atom feed URL |
| 14 | `nasa_apod` | Integrations | SAFE | Get NASA Astronomy Picture of the Day; uses vault provider `nasa` or `NASA_API_KEY` |
| 15 | `openweathermap_current` | Integrations | SAFE | Get current weather; uses vault provider `openweathermap` or `OPENWEATHERMAP_API_KEY` |
| 16 | `openweathermap_forecast` | Integrations | SAFE | Get a 5-day forecast; uses vault provider `openweathermap` or `OPENWEATHERMAP_API_KEY` |
| 17 | `quickchart_create_url` | Integrations | SAFE | Create a QuickChart chart URL from labels and data |
| 18 | `github_get_repository` | Integrations | SAFE | Get GitHub repository metadata; optional vault provider `github` |
| 19 | `github_search_repositories` | Integrations | SAFE | Search GitHub repositories; optional vault provider `github` |
| 20 | `github_list_issues` | Integrations | SAFE | List GitHub repository issues; optional vault provider `github` |
| 21 | `github_get_issue` | Integrations | SAFE | Get a GitHub issue by repository issue number; optional vault provider `github` |
| 22 | `github_list_pull_requests` | Integrations | SAFE | List GitHub repository pull requests; optional vault provider `github` |
| 23 | `github_list_releases` | Integrations | SAFE | List GitHub repository releases; optional vault provider `github` |
| 24 | `github_get_release` | Integrations | SAFE | Get a GitHub release by tag name; optional vault provider `github` |
| 25 | `gitlab_get_project` | Integrations | SAFE | Get GitLab project metadata; optional vault provider `gitlab` |
| 26 | `gitlab_search_projects` | Integrations | SAFE | Search GitLab projects; optional vault provider `gitlab` |
| 27 | `gitlab_list_project_issues` | Integrations | SAFE | List GitLab project issues; optional vault provider `gitlab` |
| 28 | `gitlab_get_project_issue` | Integrations | SAFE | Get a GitLab project issue by internal issue ID; optional vault provider `gitlab` |
| 29 | `gitlab_list_project_releases` | Integrations | SAFE | List GitLab project releases; optional vault provider `gitlab` |
| 30 | `gitlab_get_project_release` | Integrations | SAFE | Get a GitLab project release by tag name; optional vault provider `gitlab` |
| 31 | `gitlab_list_user_projects` | Integrations | SAFE | List projects owned by a GitLab user ID; optional vault provider `gitlab` |
| 32 | `bitly_get_bitlink` | Integrations | SAFE | Get Bitly bitlink metadata; uses vault provider `bitly` |
| 33 | `bitly_create_bitlink` | Integrations | MODERATE | Create a Bitly short link; uses vault provider `bitly` |
| 34 | `bitly_update_bitlink` | Integrations | MODERATE | Update Bitly bitlink metadata; uses vault provider `bitly` |
| 35 | `brandfetch_get_brand` | Integrations | SAFE | Get Brandfetch company, industry, colors, fonts, and logos |
| 36 | `brandfetch_get_brand_logos` | Integrations | SAFE | Get Brandfetch logo/icon metadata |
| 37 | `brandfetch_get_brand_colors` | Integrations | SAFE | Get Brandfetch color metadata |
| 38 | `marketstack_get_eod` | Integrations | SAFE | Get Marketstack end-of-day market data |
| 39 | `marketstack_get_ticker` | Integrations | SAFE | Get Marketstack ticker metadata |
| 40 | `marketstack_get_exchange` | Integrations | SAFE | Get Marketstack exchange metadata |
| 41 | `deepl_translate_text` | Integrations | MODERATE | Translate text with DeepL |
| 42 | `deepl_list_languages` | Integrations | SAFE | List DeepL source or target languages |
| 43 | `todoist_list_tasks` | Integrations | SAFE | List Todoist tasks; uses vault provider `todoist` |
| 44 | `todoist_get_task` | Integrations | SAFE | Get a Todoist task by ID |
| 45 | `todoist_create_task` | Integrations | MODERATE | Create a Todoist task |
| 46 | `todoist_update_task` | Integrations | MODERATE | Update a Todoist task |
| 47 | `todoist_close_task` | Integrations | MODERATE | Close a Todoist task |
| 48 | `todoist_list_projects` | Integrations | SAFE | List Todoist projects |
| 49 | `todoist_get_project` | Integrations | SAFE | Get a Todoist project by ID |
| 50 | `todoist_create_project` | Integrations | MODERATE | Create a Todoist project |
| 51 | `trello_search` | Integrations | SAFE | Search Trello boards and cards |
| 52 | `trello_get_board` | Integrations | SAFE | Get Trello board metadata |
| 53 | `trello_list_board_lists` | Integrations | SAFE | List Trello lists on a board |
| 54 | `trello_list_cards` | Integrations | SAFE | List Trello cards in a list |
| 55 | `trello_get_card` | Integrations | SAFE | Get Trello card metadata |
| 56 | `trello_create_card` | Integrations | MODERATE | Create a Trello card |
| 57 | `trello_update_card` | Integrations | MODERATE | Update a Trello card |
| 58 | `trello_add_card_comment` | Integrations | MODERATE | Add a comment to a Trello card |
| 59 | `asana_get_user` | Integrations | SAFE | Get an Asana user by ID or `me` |
| 60 | `asana_list_users` | Integrations | SAFE | List Asana users in a workspace |
| 61 | `asana_list_projects` | Integrations | SAFE | List Asana projects by workspace or team |
| 62 | `asana_get_project` | Integrations | SAFE | Get an Asana project by GID |
| 63 | `asana_create_project` | Integrations | MODERATE | Create an Asana project in a team |
| 64 | `asana_update_project` | Integrations | MODERATE | Update an Asana project |
| 65 | `asana_list_tasks` | Integrations | SAFE | List Asana tasks by project or workspace filters |
| 66 | `asana_search_tasks` | Integrations | SAFE | Search Asana tasks in a workspace |
| 67 | `asana_get_task` | Integrations | SAFE | Get an Asana task by GID |
| 68 | `asana_create_task` | Integrations | MODERATE | Create an Asana task |
| 69 | `asana_create_subtask` | Integrations | MODERATE | Create an Asana subtask |
| 70 | `asana_update_task` | Integrations | MODERATE | Update an Asana task |
| 71 | `asana_add_task_comment` | Integrations | MODERATE | Add a comment/story to an Asana task |
| 72 | `linear_list_teams` | Integrations | SAFE | List Linear teams |
| 73 | `linear_list_users` | Integrations | SAFE | List Linear users |
| 74 | `linear_list_workflow_states` | Integrations | SAFE | List Linear workflow states |
| 75 | `linear_list_issues` | Integrations | SAFE | List Linear issues |
| 76 | `linear_get_issue` | Integrations | SAFE | Get a Linear issue by ID or identifier |
| 77 | `linear_create_issue` | Integrations | MODERATE | Create a Linear issue |
| 78 | `linear_update_issue` | Integrations | MODERATE | Update a Linear issue |
| 79 | `linear_add_issue_comment` | Integrations | MODERATE | Add a comment to a Linear issue |
| 80 | `linear_add_issue_link` | Integrations | MODERATE | Attach a URL link to a Linear issue |
| 81 | `jira_get_myself` | Integrations | SAFE | Get the current Jira user |
| 82 | `jira_list_projects` | Integrations | SAFE | List Jira projects |
| 83 | `jira_search_issues` | Integrations | SAFE | Search Jira issues with JQL |
| 84 | `jira_get_issue` | Integrations | SAFE | Get a Jira issue by key or ID |
| 85 | `jira_create_issue` | Integrations | MODERATE | Create a Jira issue |
| 86 | `jira_update_issue` | Integrations | MODERATE | Update a Jira issue or transition it |
| 87 | `jira_list_issue_transitions` | Integrations | SAFE | List available Jira transitions for an issue |
| 88 | `jira_list_users` | Integrations | SAFE | Search Jira users |
| 89 | `jira_list_issue_comments` | Integrations | SAFE | List comments on a Jira issue |
| 90 | `jira_add_issue_comment` | Integrations | MODERATE | Add a comment to a Jira issue |
| 91 | `clickup_list_teams` | Integrations | SAFE | List ClickUp workspaces/teams |
| 92 | `clickup_list_spaces` | Integrations | SAFE | List ClickUp spaces |
| 93 | `clickup_list_folders` | Integrations | SAFE | List ClickUp folders |
| 94 | `clickup_list_lists` | Integrations | SAFE | List ClickUp lists |
| 95 | `clickup_get_task` | Integrations | SAFE | Get a ClickUp task by ID |
| 96 | `clickup_list_tasks` | Integrations | SAFE | List ClickUp tasks in a list |
| 97 | `clickup_create_task` | Integrations | MODERATE | Create a ClickUp task |
| 98 | `clickup_update_task` | Integrations | MODERATE | Update a ClickUp task |
| 99 | `clickup_list_task_comments` | Integrations | SAFE | List comments on a ClickUp task |
| 100 | `clickup_add_task_comment` | Integrations | MODERATE | Add a comment to a ClickUp task |
| 101 | `slack_list_channels` | Integrations | SAFE | List Slack conversations |
| 102 | `slack_get_channel_history` | Integrations | SAFE | Get recent Slack conversation messages |
| 103 | `slack_search_messages` | Integrations | SAFE | Search Slack messages |
| 104 | `slack_list_users` | Integrations | SAFE | List Slack users |
| 105 | `slack_get_user` | Integrations | SAFE | Get Slack user metadata |
| 106 | `slack_post_message` | Integrations | MODERATE | Post a Slack message |
| 107 | `slack_update_message` | Integrations | MODERATE | Update a Slack message |
| 108 | `slack_add_reaction` | Integrations | MODERATE | Add a Slack message reaction |
| 109 | `notion_search` | Integrations | SAFE | Search Notion pages and data sources |
| 110 | `notion_get_page` | Integrations | SAFE | Retrieve Notion page properties |
| 111 | `notion_get_block_children` | Integrations | SAFE | Retrieve Notion page or block children |
| 112 | `notion_query_data_source` | Integrations | SAFE | Query a Notion data source |
| 113 | `notion_create_page` | Integrations | MODERATE | Create a Notion page |
| 114 | `notion_update_page` | Integrations | MODERATE | Update a Notion page |
| 115 | `notion_append_block_children` | Integrations | MODERATE | Append children to a Notion page or block |
| 116 | `airtable_list_bases` | Integrations | SAFE | List Airtable bases |
| 117 | `airtable_get_base_schema` | Integrations | SAFE | Get Airtable base schema |
| 118 | `airtable_list_records` | Integrations | SAFE | List Airtable records |
| 119 | `airtable_get_record` | Integrations | SAFE | Get an Airtable record |
| 120 | `airtable_create_records` | Integrations | MODERATE | Create Airtable records |
| 121 | `airtable_update_records` | Integrations | MODERATE | Update Airtable records |
| 122 | `airtable_delete_record` | Integrations | MODERATE | Delete an Airtable record |
| 123 | `hubspot_list_crm_objects` | Integrations | SAFE | List HubSpot CRM objects |
| 124 | `hubspot_search_crm_objects` | Integrations | SAFE | Search HubSpot CRM objects |
| 125 | `hubspot_get_crm_object` | Integrations | SAFE | Get a HubSpot CRM object |
| 126 | `hubspot_create_crm_object` | Integrations | MODERATE | Create a HubSpot CRM object |
| 127 | `hubspot_update_crm_object` | Integrations | MODERATE | Update a HubSpot CRM object |
| 128 | `hubspot_archive_crm_object` | Integrations | MODERATE | Archive/delete a HubSpot CRM object |
| 129 | `zendesk_search` | Integrations | SAFE | Search Zendesk tickets, users, organizations, and groups |
| 130 | `zendesk_get_ticket` | Integrations | SAFE | Get a Zendesk ticket |
| 131 | `zendesk_list_tickets` | Integrations | SAFE | List Zendesk tickets |
| 132 | `zendesk_create_ticket` | Integrations | MODERATE | Create a Zendesk ticket |
| 133 | `zendesk_update_ticket` | Integrations | MODERATE | Update a Zendesk ticket |
| 134 | `zendesk_get_user` | Integrations | SAFE | Get a Zendesk user |
| 135 | `zendesk_search_users` | Integrations | SAFE | Search Zendesk users |
| 136 | `mailchimp_list_audiences` | Integrations | SAFE | List Mailchimp audiences |
| 137 | `mailchimp_list_members` | Integrations | SAFE | List Mailchimp audience members |
| 138 | `mailchimp_get_member` | Integrations | SAFE | Get a Mailchimp audience member |
| 139 | `mailchimp_add_or_update_member` | Integrations | MODERATE | Add or update a Mailchimp audience member |
| 140 | `mailchimp_update_member_tags` | Integrations | MODERATE | Add or remove Mailchimp member tags |
| 141 | `mailchimp_list_campaigns` | Integrations | SAFE | List Mailchimp campaigns |
| 142 | `freshdesk_list_tickets` | Integrations | SAFE | List Freshdesk tickets |
| 143 | `freshdesk_search_tickets` | Integrations | SAFE | Search Freshdesk tickets |
| 144 | `freshdesk_get_ticket` | Integrations | SAFE | Get a Freshdesk ticket |
| 145 | `freshdesk_create_ticket` | Integrations | MODERATE | Create a Freshdesk ticket |
| 146 | `freshdesk_update_ticket` | Integrations | MODERATE | Update a Freshdesk ticket |
| 147 | `freshdesk_delete_ticket` | Integrations | MODERATE | Delete a Freshdesk ticket |
| 148 | `freshdesk_list_contacts` | Integrations | SAFE | List Freshdesk contacts |
| 149 | `freshdesk_get_contact` | Integrations | SAFE | Get a Freshdesk contact |
| 150 | `freshdesk_create_contact` | Integrations | MODERATE | Create a Freshdesk contact |
| 151 | `freshdesk_update_contact` | Integrations | MODERATE | Update a Freshdesk contact |
| 152 | `helpscout_list_mailboxes` | Integrations | SAFE | List Help Scout mailboxes |
| 153 | `helpscout_get_mailbox` | Integrations | SAFE | Get a Help Scout mailbox |
| 154 | `helpscout_list_conversations` | Integrations | SAFE | List Help Scout conversations |
| 155 | `helpscout_get_conversation` | Integrations | SAFE | Get a Help Scout conversation |
| 156 | `helpscout_create_conversation` | Integrations | MODERATE | Create a Help Scout conversation |
| 157 | `helpscout_create_thread` | Integrations | MODERATE | Create a Help Scout conversation thread |
| 158 | `helpscout_list_customers` | Integrations | SAFE | List Help Scout customers |
| 159 | `helpscout_get_customer` | Integrations | SAFE | Get a Help Scout customer |
| 160 | `helpscout_create_customer` | Integrations | MODERATE | Create a Help Scout customer |
| 161 | `helpscout_update_customer` | Integrations | MODERATE | Update a Help Scout customer |
| 162 | `intercom_list_contacts` | Integrations | SAFE | List Intercom contacts |
| 163 | `intercom_search_contacts` | Integrations | SAFE | Search Intercom contacts |
| 164 | `intercom_get_contact` | Integrations | SAFE | Get an Intercom contact |
| 165 | `intercom_create_contact` | Integrations | MODERATE | Create an Intercom contact |
| 166 | `intercom_update_contact` | Integrations | MODERATE | Update an Intercom contact |
| 167 | `intercom_archive_contact` | Integrations | MODERATE | Archive an Intercom contact |
| 168 | `intercom_list_conversations` | Integrations | SAFE | List Intercom conversations |
| 169 | `intercom_get_conversation` | Integrations | SAFE | Get an Intercom conversation |
| 170 | `intercom_reply_conversation` | Integrations | MODERATE | Reply to an Intercom conversation |
| 171 | `pipedrive_list_records` | Integrations | SAFE | List Pipedrive deals, people, organizations, activities, leads, notes, or products |
| 172 | `pipedrive_search_records` | Integrations | SAFE | Search Pipedrive deals, people, organizations, products, or leads |
| 173 | `pipedrive_get_record` | Integrations | SAFE | Get a Pipedrive CRM record |
| 174 | `pipedrive_create_record` | Integrations | MODERATE | Create a Pipedrive CRM record |
| 175 | `pipedrive_update_record` | Integrations | MODERATE | Update a Pipedrive CRM record |
| 176 | `pipedrive_delete_record` | Integrations | MODERATE | Delete a Pipedrive CRM record |
| 177 | `pipedrive_list_users` | Integrations | SAFE | List Pipedrive users |
| 178 | `twilio_send_message` | Integrations | MODERATE | Send SMS, MMS, or WhatsApp messages with Twilio |
| 179 | `twilio_list_messages` | Integrations | SAFE | List Twilio messages |
| 180 | `twilio_get_message` | Integrations | SAFE | Get a Twilio message by SID |
| 181 | `twilio_make_call` | Integrations | MODERATE | Start an outbound Twilio voice call |
| 182 | `sendgrid_send_email` | Integrations | MODERATE | Send transactional email with SendGrid |
| 183 | `sendgrid_list_contacts` | Integrations | SAFE | List or search SendGrid marketing contacts |
| 184 | `sendgrid_get_contact` | Integrations | SAFE | Get a SendGrid marketing contact |
| 185 | `sendgrid_upsert_contacts` | Integrations | MODERATE | Create or update SendGrid marketing contacts |
| 186 | `sendgrid_list_lists` | Integrations | SAFE | List SendGrid marketing contact lists |
| 187 | `mailgun_send_email` | Integrations | MODERATE | Send email with Mailgun |
| 188 | `mailgun_list_events` | Integrations | SAFE | List Mailgun delivery events |
| 189 | `mailgun_get_domain` | Integrations | SAFE | Get Mailgun sending domain metadata |
| 190 | `brevo_send_email` | Integrations | MODERATE | Send transactional email with Brevo |
| 191 | `brevo_list_contacts` | Integrations | SAFE | List Brevo contacts |
| 192 | `brevo_get_contact` | Integrations | SAFE | Get a Brevo contact |
| 193 | `brevo_create_contact` | Integrations | MODERATE | Create a Brevo contact |
| 194 | `brevo_update_contact` | Integrations | MODERATE | Update a Brevo contact |
| 195 | `brevo_list_senders` | Integrations | SAFE | List Brevo senders |
| 196 | `mailjet_send_email` | Integrations | MODERATE | Send email with Mailjet |
| 197 | `mailjet_send_sms` | Integrations | MODERATE | Send SMS with Mailjet |
| 198 | `mailjet_list_contacts` | Integrations | SAFE | List Mailjet contacts |
| 199 | `mailjet_get_contact` | Integrations | SAFE | Get a Mailjet contact |
| 200 | `mandrill_send_email` | Integrations | MODERATE | Send email with Mandrill / Mailchimp Transactional |
| 201 | `mandrill_send_template` | Integrations | MODERATE | Send a Mandrill template email |
| 202 | `messagebird_send_sms` | Integrations | MODERATE | Send SMS with MessageBird |
| 203 | `messagebird_get_balance` | Integrations | SAFE | Get MessageBird account balance |
| 204 | `mocean_send_sms` | Integrations | MODERATE | Send SMS with Mocean |
| 205 | `mocean_send_voice` | Integrations | MODERATE | Start a Mocean text-to-speech voice call |
| 206 | `mocean_get_balance` | Integrations | SAFE | Get Mocean account balance |
| 207 | `msg91_send_sms` | Integrations | MODERATE | Send SMS with MSG91 |
| 208 | `stripe_list_records` | Integrations | SAFE | List Stripe customers, charges, payment intents, invoices, subscriptions, products, or prices |
| 209 | `stripe_search_records` | Integrations | SAFE | Search Stripe billing records with Stripe Search query syntax |
| 210 | `stripe_get_record` | Integrations | SAFE | Get a Stripe billing record by ID |
| 211 | `stripe_get_balance` | Integrations | SAFE | Get Stripe account balance details |
| 212 | `stripe_create_customer` | Integrations | MODERATE | Create a Stripe customer |
| 213 | `stripe_update_customer` | Integrations | MODERATE | Update a Stripe customer |
| 214 | `shopify_list_records` | Integrations | SAFE | List Shopify products, orders, or customers |
| 215 | `shopify_get_record` | Integrations | SAFE | Get a Shopify product, order, or customer by ID |
| 216 | `shopify_create_product` | Integrations | MODERATE | Create a Shopify product |
| 217 | `shopify_update_product` | Integrations | MODERATE | Update a Shopify product |
| 218 | `woocommerce_list_records` | Integrations | SAFE | List WooCommerce products, orders, or customers |
| 219 | `woocommerce_get_record` | Integrations | SAFE | Get a WooCommerce product, order, or customer by ID |
| 220 | `woocommerce_create_record` | Integrations | MODERATE | Create a WooCommerce product, order, or customer |
| 221 | `woocommerce_update_record` | Integrations | MODERATE | Update a WooCommerce product, order, or customer |
| 222 | `chargebee_list_records` | Integrations | SAFE | List Chargebee customers, subscriptions, invoices, transactions, items, item prices, or plans |
| 223 | `chargebee_get_record` | Integrations | SAFE | Get a Chargebee billing record by ID |
| 224 | `chargebee_create_customer` | Integrations | MODERATE | Create a Chargebee customer |
| 225 | `chargebee_update_customer` | Integrations | MODERATE | Update a Chargebee customer |
| 226 | `pushbullet_send_push` | Integrations | MODERATE | Send a Pushbullet note or link push |
| 227 | `pushbullet_list_pushes` | Integrations | SAFE | List Pushbullet push history |
| 228 | `pushbullet_update_push` | Integrations | MODERATE | Dismiss or update a Pushbullet push |
| 229 | `pushbullet_delete_push` | Integrations | MODERATE | Delete a Pushbullet push |
| 230 | `pushcut_send_notification` | Integrations | MODERATE | Send a Pushcut notification |
| 231 | `gotify_send_message` | Integrations | MODERATE | Send a Gotify message |
| 232 | `gotify_list_messages` | Integrations | SAFE | List Gotify messages |
| 233 | `gotify_delete_message` | Integrations | MODERATE | Delete a Gotify message |
| 234 | `pushover_send_message` | Integrations | MODERATE | Send a Pushover message |
| 235 | `signl4_send_alert` | Integrations | MODERATE | Send a SIGNL4 alert |
| 236 | `signl4_resolve_alert` | Integrations | MODERATE | Resolve a SIGNL4 alert by external ID |
| 237 | `wordpress_list_records` | Integrations | SAFE | List WordPress posts, pages, users, or media |
| 238 | `wordpress_get_record` | Integrations | SAFE | Get a WordPress post, page, user, or media item by ID |
| 239 | `wordpress_create_record` | Integrations | MODERATE | Create a WordPress post, page, media item, or user |
| 240 | `wordpress_update_record` | Integrations | MODERATE | Update a WordPress post, page, media item, or user |
| 241 | `wordpress_delete_record` | Integrations | MODERATE | Delete a WordPress post, page, media item, or user |
| 242 | `strapi_list_entries` | Integrations | SAFE | List Strapi entries in a collection |
| 243 | `strapi_get_entry` | Integrations | SAFE | Get a Strapi entry by collection and ID |
| 244 | `strapi_create_entry` | Integrations | MODERATE | Create a Strapi collection entry |
| 245 | `strapi_update_entry` | Integrations | MODERATE | Update a Strapi collection entry |
| 246 | `strapi_delete_entry` | Integrations | MODERATE | Delete a Strapi collection entry |
| 247 | `contentful_list_records` | Integrations | SAFE | List Contentful entries or assets |
| 248 | `contentful_get_record` | Integrations | SAFE | Get a Contentful entry or asset by ID |
| 249 | `ghost_list_posts` | Integrations | SAFE | List Ghost posts through the Content API |
| 250 | `ghost_get_post` | Integrations | SAFE | Get a Ghost post by ID or slug |
| 251 | `ghost_create_post` | Integrations | MODERATE | Create a Ghost post through the Admin API |
| 252 | `ghost_update_post` | Integrations | MODERATE | Update a Ghost post through the Admin API |
| 253 | `ghost_delete_post` | Integrations | MODERATE | Delete a Ghost post through the Admin API |
| 254 | `storyblok_list_stories` | Integrations | SAFE | List Storyblok stories through the Content API |
| 255 | `storyblok_get_story` | Integrations | SAFE | Get a Storyblok story by path, UUID, or ID |
| 256 | `storyblok_publish_story` | Integrations | MODERATE | Publish a Storyblok story through the Management API |
| 257 | `storyblok_unpublish_story` | Integrations | MODERATE | Unpublish a Storyblok story through the Management API |
| 258 | `storyblok_delete_story` | Integrations | MODERATE | Delete a Storyblok story through the Management API |
| 259 | `netlify_list_sites` | Integrations | SAFE | List Netlify sites |
| 260 | `netlify_get_site` | Integrations | SAFE | Get a Netlify site |
| 261 | `netlify_list_deploys` | Integrations | SAFE | List Netlify deploys for a site |
| 262 | `netlify_get_deploy` | Integrations | SAFE | Get a Netlify deploy |
| 263 | `netlify_cancel_deploy` | Integrations | MODERATE | Cancel a Netlify deploy |
| 264 | `netlify_delete_site` | Integrations | MODERATE | Delete a Netlify site |
| 265 | `uptimerobot_get_account` | Integrations | SAFE | Get UptimeRobot account details |
| 266 | `uptimerobot_list_monitors` | Integrations | SAFE | List UptimeRobot monitors |
| 267 | `uptimerobot_get_monitor` | Integrations | SAFE | Get an UptimeRobot monitor |
| 268 | `uptimerobot_create_monitor` | Integrations | MODERATE | Create an UptimeRobot monitor |
| 269 | `uptimerobot_update_monitor` | Integrations | MODERATE | Update an UptimeRobot monitor |
| 270 | `uptimerobot_delete_monitor` | Integrations | MODERATE | Delete an UptimeRobot monitor |
| 271 | `uptimerobot_reset_monitor` | Integrations | MODERATE | Reset an UptimeRobot monitor |
| 272 | `pagerduty_list_incidents` | Integrations | SAFE | List PagerDuty incidents |
| 273 | `pagerduty_get_incident` | Integrations | SAFE | Get a PagerDuty incident |
| 274 | `pagerduty_create_incident` | Integrations | MODERATE | Create a PagerDuty incident |
| 275 | `pagerduty_update_incident` | Integrations | MODERATE | Update a PagerDuty incident |
| 276 | `pagerduty_add_incident_note` | Integrations | MODERATE | Add a note to a PagerDuty incident |
| 277 | `pagerduty_list_services` | Integrations | SAFE | List PagerDuty services |
| 278 | `pagerduty_get_user` | Integrations | SAFE | Get a PagerDuty user |
| 279 | `sentry_list_organizations` | Integrations | SAFE | List Sentry organizations |
| 280 | `sentry_list_projects` | Integrations | SAFE | List Sentry projects |
| 281 | `sentry_list_project_issues` | Integrations | SAFE | List Sentry project issues |
| 282 | `sentry_get_issue` | Integrations | SAFE | Get a Sentry issue |
| 283 | `sentry_update_issue` | Integrations | MODERATE | Update a Sentry issue |
| 284 | `sentry_list_project_events` | Integrations | SAFE | List Sentry project events |
| 285 | `sentry_get_event` | Integrations | SAFE | Get a Sentry event |
| 286 | `cloudflare_list_zones` | Integrations | SAFE | List Cloudflare zones |
| 287 | `cloudflare_list_dns_records` | Integrations | SAFE | List Cloudflare DNS records |
| 288 | `cloudflare_create_dns_record` | Integrations | MODERATE | Create a Cloudflare DNS record |
| 289 | `cloudflare_update_dns_record` | Integrations | MODERATE | Update a Cloudflare DNS record |
| 290 | `cloudflare_delete_dns_record` | Integrations | MODERATE | Delete a Cloudflare DNS record |
| 291 | `cloudflare_list_origin_certificates` | Integrations | SAFE | List Cloudflare origin pull certificates |
| 292 | `cloudflare_get_origin_certificate` | Integrations | SAFE | Get a Cloudflare origin pull certificate |
| 293 | `cloudflare_upload_origin_certificate` | Integrations | MODERATE | Upload a Cloudflare origin pull certificate |
| 294 | `cloudflare_delete_origin_certificate` | Integrations | MODERATE | Delete a Cloudflare origin pull certificate |
| 295 | `urlscan_search_scans` | Integrations | SAFE | Search archived urlscan.io scans |
| 296 | `urlscan_get_result` | Integrations | SAFE | Get a urlscan.io scan result |
| 297 | `urlscan_submit_scan` | Integrations | MODERATE | Submit a URL to urlscan.io for scanning |
| 298 | `hunter_domain_search` | Integrations | SAFE | Find domain-associated email addresses with Hunter |
| 299 | `hunter_email_finder` | Integrations | SAFE | Find a likely professional email with Hunter |
| 300 | `hunter_email_verifier` | Integrations | SAFE | Verify email deliverability with Hunter |
| 301 | `mailcheck_check_email` | Integrations | SAFE | Check an email address with Mailcheck |
| 302 | `peekalink_preview_url` | Integrations | SAFE | Return link preview metadata with Peekalink |
| 303 | `peekalink_check_availability` | Integrations | SAFE | Check Peekalink preview availability |
| 304 | `jina_reader_fetch_url` | Integrations | SAFE | Fetch a URL through Jina Reader |
| 305 | `jina_search_web` | Integrations | SAFE | Search the web through Jina Search |
| 306 | `jina_deep_research` | Integrations | MODERATE | Run a Jina DeepSearch research query |
| 307 | `baserow_list_tables` | Integrations | SAFE | List Baserow tables |
| 308 | `baserow_list_fields` | Integrations | SAFE | List Baserow table fields |
| 309 | `baserow_list_rows` | Integrations | SAFE | List Baserow table rows |
| 310 | `baserow_get_row` | Integrations | SAFE | Get a Baserow row |
| 311 | `baserow_create_row` | Integrations | MODERATE | Create a Baserow row |
| 312 | `baserow_update_row` | Integrations | MODERATE | Update a Baserow row |
| 313 | `baserow_delete_row` | Integrations | MODERATE | Delete a Baserow row |
| 314 | `nocodb_list_bases` | Integrations | SAFE | List NocoDB bases |
| 315 | `nocodb_get_base` | Integrations | SAFE | Get NocoDB base metadata |
| 316 | `nocodb_list_records` | Integrations | SAFE | List NocoDB records |
| 317 | `nocodb_get_record` | Integrations | SAFE | Get a NocoDB record |
| 318 | `nocodb_count_records` | Integrations | SAFE | Count NocoDB records |
| 319 | `nocodb_create_record` | Integrations | MODERATE | Create a NocoDB record |
| 320 | `nocodb_update_record` | Integrations | MODERATE | Update a NocoDB record |
| 321 | `nocodb_delete_record` | Integrations | MODERATE | Delete a NocoDB record |
| 322 | `coda_list_docs` | Integrations | SAFE | List Coda docs |
| 323 | `coda_list_tables` | Integrations | SAFE | List Coda tables and views |
| 324 | `coda_list_table_rows` | Integrations | SAFE | List Coda table rows |
| 325 | `coda_get_table_row` | Integrations | SAFE | Get a Coda table row |
| 326 | `coda_create_table_row` | Integrations | MODERATE | Create a Coda table row |
| 327 | `coda_update_table_row` | Integrations | MODERATE | Update a Coda table row |
| 328 | `coda_delete_table_row` | Integrations | MODERATE | Delete a Coda table row |
| 329 | `coda_list_formulas` | Integrations | SAFE | List Coda formulas |
| 330 | `coda_list_controls` | Integrations | SAFE | List Coda controls |
| 331 | `grist_list_orgs` | Integrations | SAFE | List Grist organizations |
| 332 | `grist_list_workspaces` | Integrations | SAFE | List Grist workspaces |
| 333 | `grist_list_docs` | Integrations | SAFE | List Grist docs |
| 334 | `grist_list_tables` | Integrations | SAFE | List Grist tables |
| 335 | `grist_list_columns` | Integrations | SAFE | List Grist table columns |
| 336 | `grist_list_records` | Integrations | SAFE | List Grist table records |
| 337 | `grist_create_record` | Integrations | MODERATE | Create a Grist record |
| 338 | `grist_update_record` | Integrations | MODERATE | Update a Grist record |
| 339 | `grist_delete_records` | Integrations | MODERATE | Delete Grist records |
| 340 | `discord_list_guild_channels` | Integrations | SAFE | List Discord guild channels |
| 341 | `discord_get_channel` | Integrations | SAFE | Get Discord channel metadata |
| 342 | `discord_get_channel_messages` | Integrations | SAFE | Get Discord channel messages |
| 343 | `discord_send_channel_message` | Integrations | MODERATE | Send a Discord channel message |
| 344 | `discord_delete_message` | Integrations | MODERATE | Delete a Discord message |
| 345 | `mattermost_get_me` | Integrations | SAFE | Get the current Mattermost user |
| 346 | `mattermost_list_teams` | Integrations | SAFE | List Mattermost teams |
| 347 | `mattermost_list_channels` | Integrations | SAFE | List Mattermost team channels |
| 348 | `mattermost_list_channel_posts` | Integrations | SAFE | List Mattermost channel posts |
| 349 | `mattermost_create_post` | Integrations | MODERATE | Create a Mattermost post |
| 350 | `mattermost_delete_post` | Integrations | MODERATE | Delete a Mattermost post |
| 351 | `matrix_whoami` | Integrations | SAFE | Get the current Matrix account |
| 352 | `matrix_list_joined_rooms` | Integrations | SAFE | List joined Matrix rooms |
| 353 | `matrix_get_room_messages` | Integrations | SAFE | Get Matrix room messages |
| 354 | `matrix_send_room_message` | Integrations | MODERATE | Send a Matrix room message |
| 355 | `matrix_leave_room` | Integrations | MODERATE | Leave a Matrix room |
| 356 | `rocketchat_get_me` | Integrations | SAFE | Get the current Rocket.Chat user |
| 357 | `rocketchat_list_channels` | Integrations | SAFE | List Rocket.Chat public channels |
| 358 | `rocketchat_get_channel_history` | Integrations | SAFE | Get Rocket.Chat channel history |
| 359 | `rocketchat_post_message` | Integrations | MODERATE | Post a Rocket.Chat message |
| 360 | `rocketchat_delete_message` | Integrations | MODERATE | Delete a Rocket.Chat message |
| 361 | `zulip_get_profile` | Integrations | SAFE | Get the current Zulip profile |
| 362 | `zulip_list_streams` | Integrations | SAFE | List Zulip streams |
| 363 | `zulip_get_messages` | Integrations | SAFE | Get Zulip messages |
| 364 | `zulip_send_message` | Integrations | MODERATE | Send a Zulip message |
| 365 | `zulip_delete_message` | Integrations | MODERATE | Delete a Zulip message |
| 366 | `google_books_search` | Integrations | SAFE | Search Google Books volume metadata |
| 367 | `google_books_get_volume` | Integrations | SAFE | Get a Google Books volume by ID |
| 368 | `youtube_search` | Integrations | SAFE | Search YouTube videos, channels, or playlists |
| 369 | `youtube_get_videos` | Integrations | SAFE | Get YouTube video metadata |
| 370 | `youtube_get_channels` | Integrations | SAFE | Get YouTube channel metadata |
| 371 | `youtube_list_playlist_items` | Integrations | SAFE | List YouTube playlist items |
| 372 | `spotify_search` | Integrations | SAFE | Search Spotify catalog metadata |
| 373 | `spotify_get_track` | Integrations | SAFE | Get Spotify track metadata |
| 374 | `spotify_get_artist` | Integrations | SAFE | Get Spotify artist metadata |
| 375 | `spotify_get_album` | Integrations | SAFE | Get Spotify album metadata |
| 376 | `spotify_get_playlist` | Integrations | SAFE | Get Spotify playlist metadata |
| 377 | `reddit_search_posts` | Integrations | SAFE | Search Reddit posts |
| 378 | `reddit_list_subreddit_posts` | Integrations | SAFE | List subreddit posts |
| 379 | `reddit_get_post` | Integrations | SAFE | Get a Reddit post and comments |
| 380 | `reddit_get_subreddit` | Integrations | SAFE | Get subreddit metadata |
| 381 | `reddit_get_user` | Integrations | SAFE | Get Reddit user metadata |
| 382 | `reddit_create_post` | Integrations | MODERATE | Create a Reddit post |
| 383 | `reddit_create_comment` | Integrations | MODERATE | Create a Reddit comment or reply |
| 384 | `reddit_delete_thing` | Integrations | MODERATE | Delete a Reddit post or comment |
| 385 | `discourse_search` | Integrations | SAFE | Search a Discourse forum |
| 386 | `discourse_list_latest_topics` | Integrations | SAFE | List latest Discourse topics |
| 387 | `discourse_get_topic` | Integrations | SAFE | Get a Discourse topic |
| 388 | `discourse_get_post` | Integrations | SAFE | Get a Discourse post |
| 389 | `discourse_create_topic` | Integrations | MODERATE | Create a Discourse topic |
| 390 | `discourse_create_post` | Integrations | MODERATE | Create a Discourse reply post |
| 391 | `discourse_update_post` | Integrations | MODERATE | Update a Discourse post |
| 392 | `medium_get_me` | Integrations | SAFE | Get the authenticated Medium profile |
| 393 | `medium_list_publications` | Integrations | SAFE | List Medium publications for a user |
| 394 | `medium_create_post` | Integrations | MODERATE | Create a Medium profile post |
| 395 | `medium_create_publication_post` | Integrations | MODERATE | Create a Medium publication post |
| 396 | `bamboohr_list_employees` | Integrations | SAFE | List BambooHR employees |
| 397 | `bamboohr_get_employee` | Integrations | SAFE | Get a BambooHR employee |
| 398 | `bamboohr_create_employee` | Integrations | MODERATE | Create a BambooHR employee |
| 399 | `bamboohr_update_employee` | Integrations | MODERATE | Update BambooHR employee fields |
| 400 | `bamboohr_get_company_report` | Integrations | SAFE | Run a BambooHR company report |
| 401 | `beeminder_get_user` | Integrations | SAFE | Get the authenticated Beeminder user |
| 402 | `beeminder_list_goals` | Integrations | SAFE | List Beeminder goals |
| 403 | `beeminder_get_goal` | Integrations | SAFE | Get a Beeminder goal |
| 404 | `beeminder_list_datapoints` | Integrations | SAFE | List Beeminder datapoints |
| 405 | `beeminder_create_datapoint` | Integrations | MODERATE | Create a Beeminder datapoint |
| 406 | `beeminder_update_datapoint` | Integrations | MODERATE | Update a Beeminder datapoint |
| 407 | `beeminder_delete_datapoint` | Integrations | MODERATE | Delete a Beeminder datapoint |
| 408 | `clockify_list_workspaces` | Integrations | SAFE | List Clockify workspaces |
| 409 | `clockify_list_users` | Integrations | SAFE | List Clockify users |
| 410 | `clockify_list_projects` | Integrations | SAFE | List Clockify projects |
| 411 | `clockify_create_project` | Integrations | MODERATE | Create a Clockify project |
| 412 | `clockify_list_time_entries` | Integrations | SAFE | List Clockify time entries |
| 413 | `clockify_create_time_entry` | Integrations | MODERATE | Create a Clockify time entry |
| 414 | `clockify_update_time_entry` | Integrations | MODERATE | Update a Clockify time entry |
| 415 | `clockify_delete_time_entry` | Integrations | MODERATE | Delete a Clockify time entry |
| 416 | `harvest_get_me` | Integrations | SAFE | Get the authenticated Harvest user |
| 417 | `harvest_get_company` | Integrations | SAFE | Get Harvest company metadata |
| 418 | `harvest_list_clients` | Integrations | SAFE | List Harvest clients |
| 419 | `harvest_list_projects` | Integrations | SAFE | List Harvest projects |
| 420 | `harvest_list_tasks` | Integrations | SAFE | List Harvest tasks |
| 421 | `harvest_list_time_entries` | Integrations | SAFE | List Harvest time entries |
| 422 | `harvest_create_time_entry` | Integrations | MODERATE | Create a Harvest time entry |
| 423 | `harvest_update_time_entry` | Integrations | MODERATE | Update a Harvest time entry |
| 424 | `harvest_stop_time_entry` | Integrations | MODERATE | Stop a running Harvest time entry |
| 425 | `harvest_delete_time_entry` | Integrations | MODERATE | Delete a Harvest time entry |
| 426 | `oura_get_profile` | Integrations | SAFE | Get the authenticated Oura profile |
| 427 | `oura_get_daily_activity` | Integrations | SAFE | Get Oura daily activity summaries |
| 428 | `oura_get_daily_readiness` | Integrations | SAFE | Get Oura daily readiness summaries |
| 429 | `oura_get_daily_sleep` | Integrations | SAFE | Get Oura daily sleep summaries |
| 430 | `strava_list_activities` | Integrations | SAFE | List Strava activities |
| 431 | `strava_get_activity` | Integrations | SAFE | Get a Strava activity |
| 432 | `strava_create_activity` | Integrations | MODERATE | Create a manual Strava activity |
| 433 | `strava_update_activity` | Integrations | MODERATE | Update a Strava activity |
| 434 | `strava_list_activity_comments` | Integrations | SAFE | List Strava activity comments |
| 435 | `strava_get_activity_streams` | Integrations | SAFE | Get Strava activity streams |
| 436 | `homeassistant_get_config` | Integrations | SAFE | Get Home Assistant configuration metadata |
| 437 | `homeassistant_check_config` | Integrations | MODERATE | Run Home Assistant config checks |
| 438 | `homeassistant_list_states` | Integrations | SAFE | List Home Assistant states |
| 439 | `homeassistant_get_state` | Integrations | SAFE | Get a Home Assistant state |
| 440 | `homeassistant_set_state` | Integrations | MODERATE | Create or update a Home Assistant state |
| 441 | `homeassistant_list_services` | Integrations | SAFE | List Home Assistant services |
| 442 | `homeassistant_call_service` | Integrations | MODERATE | Call a Home Assistant service |
| 443 | `homeassistant_list_events` | Integrations | SAFE | List Home Assistant event types |
| 444 | `homeassistant_fire_event` | Integrations | MODERATE | Fire a Home Assistant event |
| 445 | `homeassistant_render_template` | Integrations | MODERATE | Render a Home Assistant template |
| 446 | `homeassistant_get_logbook` | Integrations | SAFE | Get Home Assistant logbook entries |
| 447 | `philips_hue_list_lights` | Integrations | SAFE | List Philips Hue lights |
| 448 | `philips_hue_get_light` | Integrations | SAFE | Get a Philips Hue light |
| 449 | `philips_hue_update_light_state` | Integrations | MODERATE | Update Philips Hue light state |
| 450 | `philips_hue_delete_light` | Integrations | MODERATE | Delete a Philips Hue light |
| 451 | `activecampaign_list_contacts` | Integrations | SAFE | List ActiveCampaign contacts |
| 452 | `activecampaign_get_contact` | Integrations | SAFE | Get an ActiveCampaign contact |
| 453 | `activecampaign_sync_contact` | Integrations | MODERATE | Create or update an ActiveCampaign contact |
| 454 | `activecampaign_update_contact` | Integrations | MODERATE | Update an ActiveCampaign contact |
| 455 | `activecampaign_list_lists` | Integrations | SAFE | List ActiveCampaign lists |
| 456 | `activecampaign_list_tags` | Integrations | SAFE | List ActiveCampaign tags |
| 457 | `activecampaign_add_contact_to_list` | Integrations | MODERATE | Subscribe or unsubscribe an ActiveCampaign contact to a list |
| 458 | `activecampaign_add_contact_tag` | Integrations | MODERATE | Add an ActiveCampaign tag to a contact |
| 459 | `convertkit_get_account` | Integrations | SAFE | Get ConvertKit account details |
| 460 | `convertkit_list_forms` | Integrations | SAFE | List ConvertKit forms |
| 461 | `convertkit_list_tags` | Integrations | SAFE | List ConvertKit tags |
| 462 | `convertkit_list_subscribers` | Integrations | SAFE | List ConvertKit subscribers |
| 463 | `convertkit_add_subscriber_to_form` | Integrations | MODERATE | Subscribe an email address to a ConvertKit form |
| 464 | `convertkit_add_subscriber_to_tag` | Integrations | MODERATE | Subscribe an email address to a ConvertKit tag |
| 465 | `getresponse_list_campaigns` | Integrations | SAFE | List GetResponse campaigns |
| 466 | `getresponse_list_contacts` | Integrations | SAFE | List GetResponse contacts |
| 467 | `getresponse_get_contact` | Integrations | SAFE | Get a GetResponse contact |
| 468 | `getresponse_create_contact` | Integrations | MODERATE | Create a GetResponse contact |
| 469 | `getresponse_update_contact` | Integrations | MODERATE | Update a GetResponse contact |
| 470 | `getresponse_delete_contact` | Integrations | MODERATE | Delete a GetResponse contact |
| 471 | `mailerlite_list_subscribers` | Integrations | SAFE | List MailerLite subscribers |
| 472 | `mailerlite_get_subscriber` | Integrations | SAFE | Get a MailerLite subscriber |
| 473 | `mailerlite_create_subscriber` | Integrations | MODERATE | Create a MailerLite subscriber |
| 474 | `mailerlite_update_subscriber` | Integrations | MODERATE | Update a MailerLite subscriber |
| 475 | `mailerlite_list_groups` | Integrations | SAFE | List MailerLite groups |

### Optional: Private B Tools (4)

Not loaded by default. Enable per-thread when the agent needs to manage Example University workload data from the LMS/Moodle. Configuration lives per user in `data/auth_tokens/<user_id>/_prv_b.json`; env fallbacks are `_PRV_B_CALENDAR_URL`, `_PRV_B_RSS_FEEDS`, `_PRV_B_MOODLE_BASE_URL`, and `_PRV_B_MOODLE_TOKEN`. See `docs/_prv_b.md`.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `_prv_b_auth` | Private B | MODERATE | Configure/inspect the LMS auth; actions: `status`, `setup_guide`, `start_mobile_token_flow`, `parse_mobile_redirect`, `configure_calendar`, `configure_rss`, `configure_moodle_token`, `clear` |
| 2 | `_prv_b_calendar` | Private B | MODERATE | Read the Moodle calendar `.ics` export URL; actions: `configure`, `status`, `clear`, `list`, `get`, `search` |
| 3 | `_prv_b_rss` | Private B | MODERATE | Read the LMS forum or announcement RSS feeds; actions: `configure`, `status`, `clear`, `list`, `get`, `search` |
| 4 | `_prv_b_moodle` | Private B | MODERATE | Read Moodle mobile API data from a `moodle_mobile_app` token; actions: `configure`, `status`, `clear`, `site_info`, `courses`, `course_contents`, `course_module`, `assignments`, `upcoming_events`, `grades`, `forums`, `forum_discussions`, `discussion_posts` |

`_prv_b_auth(start_mobile_token_flow)` returns an the LMS mobile-app launch URL. The user completes the LMS/Microsoft MFA in a browser and gives Nymeria the resulting `moodlemobile://token=...` redirect via `parse_mobile_redirect`; Nymeria then stores the decoded Moodle `wstoken` for the read tools. The tool does not store the user's MQ password or TOTP secret.

### Optional: File Editing (1)

Not loaded by default. Enable per-thread when the agent needs precise text edits instead of full-file rewrites.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `file_edit` | Core | MODERATE | Exact, all-or-nothing edits to existing text files |

### Optional: Image Generation (1)

Not loaded by default. Enable per-thread, or promote to Core in the Desktop global Tools settings. The tool writes generated images under `NYMERIA_WORKSPACE_DIR/image-generation/`, returns a workspace artifact via `[attach:/path]`, and stores only small artifact metadata in chat history. On the next reasoning step, Nymeria hydrates recent generated images into native vision input for supported chat providers: Anthropic vision models and OpenAI/OpenRouter models using `OPENAI_API_MODE=responses`. Other provider modes still see the file path and artifact.

API keys follow the existing provider-key pattern: set `OPENAI_API_KEY` for OpenAI GPT Image models and `GEMINI_API_KEY` for Gemini/Nano Banana models. The Desktop edit dialog configures provider/model/output options, not secret storage.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `image_generate` | Image | MODERATE | Generate a new image from a prompt using OpenAI `gpt-image-*` or Gemini Nano Banana models, attach it to the chat, and expose it to vision-capable follow-up reasoning |

Configurable options:

- `provider`: `openai` or `gemini`
- OpenAI: `openai_model`, `openai_size`, `openai_quality`, `openai_output_format`, `openai_moderation`
- Gemini: `gemini_model`, `gemini_aspect_ratio`, `gemini_image_size`
- `native_context_enabled`: whether supported chat models should inspect generated images natively on the next LLM call

### Optional: _PRV_A and Sheets Tools (8 + 1 attachment)

Google Sheets-based tools for Acme Hardware RFQ processing. Generic `google_sheets_*` tools use the current user's Google OAuth. The dedicated `_prv_a_*` reference tools live in the `nymeria/plugins/_prv_a/` package and use the app-level `_PRV_A_SERVICE_ACCOUNT_FILE` service account with 5-minute in-memory caching, so _PRV_A lookups do not depend on whichever user is authenticated for Google Docs. Products, Acme, and Vendor tools share a batch-search helper (`plugins/_prv_a/sheet_lookup.py`); Acme and Supplier have domain-specific search logic. The `outlook_get_attachments` tool (last in the table) is from `OUTLOOK_ATTACHMENT_TOOLS`, not a _PRV_A module; it's placed here as a general-purpose extraction utility.

| # | Tool | Security | Description |
|---|------|----------|-------------|
| 1 | `google_sheets_search` | SAFE | Generic search for any Google Sheet by ID |
| 2 | `google_sheets_append` | MODERATE | Append rows to a Google Sheet (for RFQ tracking) |
| 3 | `google_sheets_update` | MODERATE | Find and update existing rows by search value |
| 4 | `_prv_a_supplier_lookup` | SAFE | Find overseas suppliers by brand(s) from Y/N matrix. Supports multi-brand lookup with coverage indicators. Includes emails, websites, and inline vendor quality ratings. |
| 5 | `_prv_a_vendor_info` | SAFE | Vendor quality ratings, contacts, and notes from past dealings |
| 6 | `_prv_a_product_search` | SAFE | Search the master _PRV_A product catalog. Supports batch part numbers. |
| 7 | `_prv_a_acme_lifecycle` | SAFE | Acme part lifecycle status (Active/Mature/Discontinued/Obsolete) with migration paths. Supports batch part numbers. |
| 8 | `_prv_a_acme_pricelist` | SAFE | Acme Electric part details and list pricing (ex-GST). Supports batch part numbers. |
| 9 | `outlook_get_attachments` | SAFE | Extract text from email attachments (PDF/DOCX via Gemini, Excel via openpyxl, CSV/TXT direct) |

See `docs/_prv_a/setup-guide.md` for full setup instructions, Google Sheet IDs, and configuration.

### Optional: Watchdog Tools (4)

Not loaded by default. Enable per-thread for the Smart Watchdog scheduler.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `activity_feed` | Watchdog | SAFE | Structured activity summary across all threads (user messages, tasks, TODOs, notifications) |
| 2 | `watchdog_dispatch` | Watchdog | MODERATE | Create a TODO on a different thread (cannot self-target) |
| 3 | `watchdog_read_notepad` | Watchdog | SAFE | Read another thread's notepad for state awareness |
| 4 | `watchdog_todo_overview` | Watchdog | SAFE | List all active TODOs across all threads, grouped by thread |

### Optional: Thread Spawning (1)

Not loaded by default. Enable per-thread to let the agent create new conversation threads with scoped config.

| # | Tool | Category | Security | Description |
|---|------|----------|----------|-------------|
| 1 | `spawn_thread` | Subagent | MODERATE | Create or delete a sidebar thread with custom instructions, tool selection, and LLM overrides. New threads are callable (globally invocable) by default. Create mode optionally dispatches an initial message and blocks until the child responds; delete mode cleans up a previously-spawned thread. |

### Callable Thread Tools (Dynamic)

Any thread with `callable=True` in its thread config becomes a tool that other threads can invoke. There are no hardcoded agents — callable threads are fully configurable via the UI:

- **Model**: Set per-thread via `llm_config.model` in thread settings (inherits global default if not set)
- **Tools**: Enable/disable any optional tools per-thread
- **System prompt**: Custom `system_prompt` or `instructions` per-thread
- **Name**: The tool name equals the thread's sidebar title (synced via `callable_name` in thread config)

Create a callable thread: open thread settings → Agent → check "Make Callable" → set a name and description. On desktop, the thread row's Agent shortcut opens this tab directly. The thread becomes available as a tool to **the creator's own threads** after `sync_agent_tools()` runs — callables are scoped to their owner (the user who created them) and the `_thread_owners` table determines visibility. Two users can independently create callables with the same `callable_name`; each user's graph binds their own version, and the runtime ownership gate in `agents/tool_factory.py` blocks cross-user invocation.

Callable tools default to blocking `mode="ask"`, which returns the target thread's final answer. Use `mode="handoff"` to transfer work to the target thread without waiting; the caller receives only a dispatch receipt while the target thread streams through its normal autonomous output channels.

Callable teams can scope which callable threads a thread sees. When a thread has `callable_team_id`, graph building includes only the owner's callable threads with the same team id. Unteamed threads keep the existing owner-wide callable visibility for backward compatibility. The desktop sidebar has a folder/team organization toggle and a bulk Team action for creating teams from selected threads.

The frontend header count uses `GET /threads/{thread_id}/callable-tools`, which mirrors runtime visibility for that caller thread: ownership, team scoping, self-exclusion, disabled tools, and name conflicts.

---

## Core System Tools

### bash_execute

Execute shell commands on the local system.

```python
bash_execute(command: str, working_directory: Optional[str] = None, timeout_seconds: int = 120)
```

**Parameters:**
- `command` (`str`): Shell command to execute
- `working_directory` (`Optional[str]`, default `None`): Directory to run the command in
- `timeout_seconds` (`int`, default `120`): Maximum execution time in seconds

**Returns:** Command output (stdout + stderr combined) or error message. Non-zero exit codes are appended. Output truncated at 50,000 characters.

**Security:** MODERATE — runs commands without sandboxing.

---

### file_read

Read the contents of a file.

```python
file_read(file_path: str, encoding: str = "utf-8", max_lines: Optional[int] = None)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to the file
- `encoding` (`str`, default `"utf-8"`): File encoding
- `max_lines` (`Optional[int]`, default `None`): Limit number of lines to read

**Returns:** File contents, or error message.

**Limits:** 10 MB maximum file size.

---

### file_write

Write content to a file.

```python
file_write(file_path: str, content: str, encoding: str = "utf-8", create_directories: bool = True, append: bool = False, attach: bool = False)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to the file
- `content` (`str`): Content to write
- `encoding` (`str`, default `"utf-8"`): File encoding
- `create_directories` (`bool`, default `True`): Create parent directories if they don't exist
- `append` (`bool`, default `False`): Append to file instead of overwriting
- `attach` (`bool`, default `False`): Deliver the written file back to chat clients (Telegram, Discord, desktop/mobile artifact viewers). Only files inside `NYMERIA_WORKSPACE_DIR` (default `/workspace`) are attachable. When attach succeeds, the raw tool result includes an `[attach:/path]` tag for backward compatibility and the API emits a structured `workspace_artifact` SSE event.

**Returns:** Success/error message with character count. When `attach=True` and the file is inside the workspace directory, the raw tool result includes an `[attach:/path]` tag and clients receive a `workspace_artifact` event. If the file is outside the workspace, the write still succeeds but attachment delivery is skipped with an info note.

**Protected paths:** Writes to `nymeria/core/`, `nymeria/config/`, `nymeria/triggers/`, `nymeria/gateway/`, and `nymeria/__init__.py` are blocked. Use the SelfModifyAgent for those directories.

---

### file_edit (Optional)

Precisely edit an existing text file with exact, all-or-nothing operations. This is not loaded by default; enable it per-thread before use.

```python
file_edit(file_path: str, edits: list[dict], encoding: str = "utf-8", dry_run: bool = False, expected_sha256: Optional[str] = None, max_diff_chars: int = 20000)
```

**Parameters:**
- `file_path` (`str`): Absolute or relative path to an existing file
- `edits` (`list[dict]`): Ordered edit operations. Each operation is applied to the in-memory result of prior operations.
- `encoding` (`str`, default `"utf-8"`): File encoding
- `dry_run` (`bool`, default `False`): Return validation and diff without writing
- `expected_sha256` (`Optional[str]`, default `None`): Optional SHA-256 of the current file bytes; mismatches fail before edits are evaluated
- `max_diff_chars` (`int`, default `20000`): Maximum unified diff characters to return; `0` suppresses the diff

**Operations:**
- `replace`: requires `old_text`; writes `new_text` in its place
- `delete`: requires `old_text`; removes the match
- `insert_before`: requires `old_text`; inserts `new_text` before the match
- `insert_after`: requires `old_text`; inserts `new_text` after the match
- `replace_range`: requires `start_line`, `end_line`, and `old_text`; replaces the 1-based inclusive line range with `new_text` only if `old_text` exactly equals the selected range

**Matching rules:** `old_text` must be exact and non-empty. If `occurrence` is omitted, `old_text` must match exactly once. If `occurrence` is provided, it is 1-based and selects that exact match. Fuzzy matching is intentionally not used.

**Returns:** JSON with `ok`, `dry_run`, `file_path`, `edits_applied`, `original_sha256`, `new_sha256`, `changed`, `diff`, and `diff_truncated`; errors include a structured `error.type`, message, and `edit_index` when relevant.

**Safety:** Edits are all-or-nothing and written atomically. The tool does not create backups. It preserves the existing file mode and dominant newline style for inserted/replacement text.

**Limits and protected paths:** Same 10 MB text-file limit and protected Nymeria paths as `file_write`.

---

### ~~file_list~~ (removed)

**Deprecated.** Redundant with `bash_execute` — use `bash_execute("ls -la /path")` or `bash_execute("find /path -name '*.py'")` instead. Removed from `ALL_TOOLS` and `TOOL_METADATA`.

---

### web_search

Search the web using the Perplexity API.

```python
web_search(query: str, search_depth: Optional[str] = None, max_sources: Optional[int] = None)
```

**Parameters:**
- `query` (`str`): Search query
- `search_depth` (`Optional[str]`): `"quick"` (sonar), `"standard"` (sonar-pro), or `"deep"` (sonar-deep-research). Defaults to `settings.perplexity_search_model`.
- `max_sources` (`Optional[int]`): Maximum sources to cite (1-10, default 5)

**Returns:** Search results with citations.

**Requires:** `PERPLEXITY_API_KEY` environment variable.

**Timeouts:** 60s for quick/standard, 180s for deep research. Max tokens: 2000 for quick/standard, 4000 for deep.

---

### consult

Ask another AI (Gemini) for a second opinion. Sends the question to a Gemini model via OpenRouter with reasoning tokens enabled and returns its analysis. Use when you want an outside perspective, need help with a hard problem, or want to cross-check your own reasoning.

> **Previously named `think`.** Renamed to `consult` to clarify that this is an external LLM call (costs credits, takes seconds), not internal reasoning.

```python
consult(question: str, context: Optional[str] = None, model: Optional[str] = None)
```

**Parameters:**
- `question` (`str`): The question or problem to get help with
- `context` (`Optional[str]`, default `None`): Additional context to include
- `model` (`Optional[str]`, default `None`): Model alias — `"gemini-3-pro"` (default), `"gemini-2.5-pro"`, or `"gemini-2.5-flash"`

**Model mapping:**

| Alias | OpenRouter model ID |
|-------|-------------------|
| `gemini-3-pro` (default) | `google/gemini-3-pro-preview` |
| `gemini-2.5-pro` | `google/gemini-2.5-pro` |
| `gemini-2.5-flash` | `google/gemini-2.5-flash-preview` |

**Returns:** Gemini's reasoning text, content, and reasoning token count.

**Requires:** `OPENROUTER_API_KEY` environment variable. Uses OpenRouter credits.

**Config:** temperature=1.0, max_tokens=16000, reasoning enabled. Timeout: 180s.

---

### claude_code (Optional, admin-only)

Invoke Claude Code in headless mode to create, modify, or analyze code. **Not loaded by default** — lives in `OPTIONAL_TOOLS` and is gated by `ADMIN_ONLY_OPTIONAL_TOOL_NAMES` (only admins may enable it).

```python
claude_code(prompt: str, working_dir: Optional[str] = None, model: str = "sonnet",
            allow_edit: bool = True, allow_bash: bool = True, timeout: int = 300)
```

**Parameters:**
- `prompt` (`str`): The coding task or question
- `working_dir` (`Optional[str]`, default `None`): Directory to run in (defaults to current directory)
- `model` (`str`, default `"sonnet"`): Model — `"sonnet"`, `"opus"`, or `"haiku"`
- `allow_edit` (`bool`, default `True`): Allow Claude Code to edit files
- `allow_bash` (`bool`, default `True`): Allow Claude Code to run commands
- `timeout` (`int`, default `300`): Timeout in seconds

**Returns:** Claude Code's response or error message. Output truncated at 50,000 characters.

**Requires:** `claude` CLI binary in PATH (install with `npm install -g @anthropic-ai/claude-code`).

**Tools passed to Claude Code:** Always includes `Read`. Adds `Edit` if `allow_edit=True`, `Bash` if `allow_bash=True`.

---

## Memory Tools

Three unified primitives — `memory_add`, `memory_edit`, `memory_read` — cover both global user-profile facts and per-thread notepad content. The `scope` argument selects which store:

- `scope="global"` — keyed entries in the user's profile, **automatically injected** into Nymeria's system prompt across every future thread. Storage: `data/users/{user_id}/profile.json`.
- `scope="thread"` — free-form markdown notepad for the active thread, re-injected after context compaction. Storage: `data/thread_notes/{thread_id}.md`.

Empty `content` (in `memory_add`) or empty `replace` whose result empties the entry (in `memory_edit`) deletes cleanly: profile rows are popped, notepad files are unlinked. There is no separate `memory_forget` because the storage layer treats blank-as-delete, so edit-to-blank leaves no zombie entries.

> **Implementation note:** `memory_add`, `memory_edit`, `memory_read`, `personality_set`, and `rag_search` accept an `Annotated[RunnableConfig, InjectedToolArg]` parameter that LangGraph injects automatically. The LLM never passes it.

### memory_add

Save a memory. Creates a new entry or overwrites an existing one.

```python
memory_add(scope: str, content: str, key: Optional[str] = None)
```

**Parameters:**
- `scope` (`"global"` | `"thread"`): which store to write to.
- `content` (`str`): the memory text. Empty string deletes.
- `key` (`str`, required for `scope="global"`): identifier for the profile entry. Ignored for `scope="thread"`.

**Examples:**
```python
memory_add(scope="global", key="prefers_typescript", content="Yes")
memory_add(scope="global", key="timezone", content="Australia/Sydney")
memory_add(scope="thread", content="Working on auth refactor; deadline Friday.")
memory_add(scope="global", key="prefers_typescript", content="")   # deletes the entry
memory_add(scope="thread", content="")                             # deletes the notepad
```

**Behavior:**
- `scope="global"` upserts into `UserProfile.memories` and re-indexes in the RAG store if RAG is enabled.
- `scope="thread"` overwrites the notepad (replace semantics; for append-style writes, read-then-add).
- Notepad max size: 50 KB. Profile max entries: 100. Profile values are truncated to 1000 chars.

---

### memory_edit

Surgical find/replace within an existing memory.

```python
memory_edit(scope: str, find: str, replace: str = "", key: Optional[str] = None)
```

**Parameters:**
- `scope` (`"global"` | `"thread"`).
- `find` (`str`): exact substring to locate (first occurrence).
- `replace` (`str`, default `""`): replacement text. Empty string deletes the matched substring.
- `key` (`str`, required for `scope="global"`): which profile entry to edit.

**Examples:**
```python
memory_edit(scope="global", key="job_title", find="Engineer", replace="Senior Engineer")
memory_edit(scope="thread", find="deadline Friday", replace="deadline Monday")
memory_edit(scope="thread", find="obsolete bullet point\n", replace="")   # delete the line
```

**Behavior:**
- If the resulting value is empty, the entry/notepad is removed.
- For long profile values use `memory_add` to overwrite — `memory_edit` shines for thread-notepad surgical edits.

---

### memory_read

Read memory: get one entry, list everything, or substring-filter.

```python
memory_read(scope: str, key: Optional[str] = None, query: Optional[str] = None)
```

**Parameters:**
- `scope` (`"global"` | `"thread"`).
- `key` (`str`, global only): fetch a single profile memory by key.
- `query` (`str`): substring filter (case-insensitive). For semantic search use `rag_search`.

**Examples:**
```python
memory_read(scope="global")                              # list all memories + personality
memory_read(scope="global", key="timezone")              # get one
memory_read(scope="global", query="prefers")             # filter by substring
memory_read(scope="thread")                              # full notepad
memory_read(scope="thread", query="deadline")            # only matching notepad lines
```

**Notes:**
- Profile memories are auto-injected into the system prompt, but weaker models may struggle to extract exact keys from long prompts — `memory_read(scope="global")` gives an explicit listing.

---

### personality_set

Set a communication/personality preference.

```python
personality_set(trait: str, value: str)
```

**Parameters:**
- `trait` (`str`): Preference category (e.g., `"tone"`, `"verbosity"`, `"expertise_level"`)
- `value` (`str`): Desired behavior

**Returns:** Confirmation message.

---

### rag_search

Search past conversations and memories for relevant context using semantic vector search.

```python
rag_search(query: str, max_results: int = 5)
```

**Parameters:**
- `query` (`str`): What to search for
- `max_results` (`int`, default `5`): Maximum results (clamped 1-10)

**Returns:** Formatted results with content type, relevance score, and content. Results filtered by user's RAG preferences (`include_conversations`, `include_memories`, `include_todos`).

**Indexing is automatic.** As of 2026-04, `opt_in.rag_enabled` defaults to `True` for new profiles, and existing profiles are migrated to `True` on first load (one-time, watermarked by `opt_in.rag_migrated`). Conversation turns are indexed in four places, in this order of frequency:

1. **Per turn** — `_index_conversation_turn` runs after every chat turn (`core/agent.py`).
2. **Pre-compact** — manual `/compact`, async auto-compact, and sync auto-compact all flush via `_pre_trim_memory_flush` before clearing messages.
3. **Pre-clear** — `POST /threads/{id}/clear` flushes before deleting checkpoints.
4. **Delete cleanup** — `DELETE /threads/{id}` runs the full thread cascade, including `MemoryIndex.delete_by_thread`, so `rag_search` doesn't surface chunks from deleted threads and thread-bound TODOs/triggers cannot wake the deleted thread again.

To opt out, use the RAG settings API or the frontend settings UI. The migration watermark prevents re-flipping on subsequent loads.

---

## TODO Tools

TODOs are the **primary driver for autonomous operation**. Active TODOs are automatically injected into the system prompt. Every TODO must have a `scheduled_for` time — TODOs are for the agent's autonomous work queue, not a general task list.

> **Implementation note:** `nym_todo`, `nym_todo_delete`, and `nym_todo_list` all accept an injected `config` parameter for user/thread identification. The LLM never passes this.
>
> **Migration note:** legacy `todo`, `todo_delete`, and `todo_list` config names are migrated to `nym_todo`, `nym_todo_delete`, and `nym_todo_list`. Older `todo_add` and `todo_update` flows were merged into the single `nym_todo` tool. Priority, deadline, blocked status, and the permanent flag have been removed.

### nym_todo

Create or update a TODO item. Omit `todo_id` to create; provide it to update. Scheduled TODOs auto-wake the agent to execute them.

```python
nym_todo(todo_id: Optional[str] = None, task: Optional[str] = None,
         scheduled_for: Optional[str] = None, status: Optional[str] = None,
         notes: Optional[str] = None, recurrence: Optional[str] = None,
         clear_schedule: bool = False, clear_recurrence: bool = False)
```

**Parameters:**
- `todo_id` (`Optional[str]`): Omit to create a new TODO, provide the 8-character ID to update an existing one
- `task` (`str`): Task description — **required** for create, optional for update
- `scheduled_for` (`str`): When to execute — **required** for create, optional for update. Formats:
  - Relative: `"30s"`, `"5m"`, `"1h"`, `"1d"` (seconds, minutes, hours, days)
  - Absolute: `"YYYY-MM-DD HH:MM[:SS]"` or `"YYYY-MM-DDTHH:MM[:SS]"` (user timezone)
- `status` (`Optional[str]`): `"pending"`, `"in_progress"`, or `"done"` (update only)
- `notes` (`Optional[str]`): Add or update notes (max 1000 characters)
- `recurrence` (`Optional[str]`): `"5min"`, `"10min"`, `"15min"`, `"30min"`, `"hourly"`, `"daily"`, `"weekly"`, `"monthly"`
- `clear_schedule` (`bool`, default `False`): Remove scheduled time (update only)
- `clear_recurrence` (`bool`, default `False`): Remove recurrence pattern (update only)

**Returns:** Confirmation with TODO ID and details, or error.

**Limits:** 50 active TODOs per user (`MAX_TODOS` in `TodoList`).

**Thread scope:** Creates TODOs on the current thread. Updates only find TODOs that already belong to the current thread; a TODO ID from another thread is treated as not found.

**Statuses:** `pending` (default), `in_progress`, `done`. Use `nym_todo(todo_id=..., status="done")` to complete a TODO.

**Recurring TODOs:** Recurring TODOs **auto-reschedule when marked done** — regardless of whether the ticker executed them or the agent/user marked them done manually. The next `scheduled_for` is calculated from the `recurrence` pattern and the status resets to `pending`. This applies to all completion paths: the `nym_todo` tool, the REST API, and the MCP server. To permanently stop a recurring TODO, use `nym_todo(todo_id=..., clear_recurrence=True)` or `nym_todo_delete`.

**Auto-purge:** Completed TODOs remain visible to `nym_todo_list(filter_status="done")` and `GET /todos?filter_status=done` until the ticker cleanup removes them from `data/todos/{user_id}.json`. The retention is controlled by `TODO_AUTO_ARCHIVE_DAYS` (default 7 days, range 1-30). There is no separate completed-TODO archive file; use activity/RAG history for historical outcome lookup after cleanup.

---

### nym_todo_delete

Delete a TODO permanently. No archive — immediately removed. Cancels any scheduled execution.

```python
nym_todo_delete(todo_id: str)
```

**Parameters:**
- `todo_id` (`str`): The 8-character TODO ID

Only deletes TODOs that belong to the current thread. A TODO ID from another thread is treated as not found.

**Returns:** Confirmation with deleted task text, or error.

---

### nym_todo_list

List TODO items for the current thread. Shows active (non-done) by default.

```python
nym_todo_list(filter_status: Optional[str] = None)
```

**Parameters:**
- `filter_status` (`Optional[str]`): `"pending"`, `"in_progress"`, `"done"`, or `"all"`

`filter_status="all"` includes all statuses for the current thread only. It does not list TODOs from other threads; use explicit dashboard/API views or `watchdog_todo_overview` for cross-thread TODOs.

**Returns:** Formatted list sorted by: status (in_progress first, then pending, then done), then scheduled time, then creation date.

**Status icons:** `[ ]` pending, `[>]` in_progress, `[x]` done.

---

## Notification Tool

### notify

Send notifications to the in-app notification center and messaging platforms
(Telegram, Discord, Slack, Teams).

```python
notify(message: str, platform: Literal["auto", "desktop", "telegram", "discord", "slack", "teams"] = "auto")
```

**Parameters:**
- `message` (`str`): The message text to send
- `platform` (`Literal["auto", "desktop", "telegram", "discord", "slack", "teams"]`, default `"auto"`): Target platform. `"auto"` creates an in-app notification and tries all configured platforms.

**Returns:** Success/error message.

**Requires:** Platform-specific credentials in `.env`:
- Desktop/in-app: no external credentials
- Telegram: bound Telegram chat for the current thread, or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_DEFAULT_CHAT_ID` fallback
- Discord: `DISCORD_WEBHOOK_URL`
- Slack: `SLACK_WEBHOOK_URL`
- Teams: `TEAMS_TEAM_ID` + `TEAMS_CHANNEL_ID` plus Microsoft auth

**Behavior in auto mode:** Creates a notification-center row unless the thread disables in-app notifications, then tries all configured external platforms. If the current thread is a Telegram thread or is bound to a Telegram chat, Telegram delivery is routed through that chat; otherwise Telegram falls back to the configured default chat. If any destination succeeds, returns the success messages (failures are not reported in mixed outcomes). If all fail, returns all errors.

**Architecture:** Platform senders, notification-level helpers, and composite dispatch functions live in `core/notification_dispatch.py`. The `notify` tool, the ticker (scheduled TODO completions), the API chat endpoint (autonomous completions), and the watchdog (stale TODO alerts) all delegate to this shared module rather than owning independent notification logic. `create_autonomous_notification()` combines in-app notification creation with FCM push in a single call gated by the per-thread `in_app_notification_level` setting.

### tool_search

Search available tools by keyword/category. This is search-only; mutations live
in `tool_enable`.

```python
tool_search(query: str = "", category: str = "", top_k: int = 15, include_status: bool = True)
```

**Parameters:**
- `query` (`str`): Keyword to search tool names and descriptions.
- `category` (`str`): Optional category filter (e.g. `"email"`, `"twitch"`).
- `top_k` (`int`): Result count, default 15 and capped at 50.
- `include_status` (`bool`): Include current-thread enabled/disabled annotations.

`tool_search` uses the shared backend search service behind
`GET /users/{user_id}/tools/search`. The indexed catalog includes core tools,
optional tools, MCP-discovered metadata, custom tool metadata, and callable
thread tools visible from the current `thread_id`. Developer-only diagnostic
tools are hidden from non-admin users. Admin-only tools remain discoverable but
return enable hints that make the admin requirement explicit.

Ranking tries embeddings first when `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`,
and `EMBEDDING_MODEL` are configured. CLIProxy gatekeeper keys such as
`cpx-*` are rejected for embeddings. If semantic search is unavailable, the
service falls back to BM25, then fuzzy matching, then substring matching. Each
result includes status and an exact enable/disable hint such as
`/tools enable browser_open`.

The same backend ranking is used by one-shot command searches:
- CLI/plain slash: `/tools <query>` and `/tools search <query>`
- Discord: `/tools search query:<text>`
- Telegram: `/tools_search <query>`

Desktop and mobile typeahead filtering stays local for responsiveness, but uses
the shared frontend `utils/toolSearch.ts` fuzzy scorer over tool names, snake
case tokens, categories, descriptions, tags, and implementation/type fields.

## Service Integration Tools (Optional)

Credential-aware native tools use the existing encrypted credential vault. Save
connections in Settings > Connections with these provider names and fields:
`wolfram_alpha.app_id`, `searxng.base_url`, `nasa.api_key`,
`openweathermap.api_key`, optional `npm.registry_url` / `npm.token`,
`google_books.api_key`, `youtube.api_key`, `spotify.access_token` or
`spotify.client_id` plus `spotify.client_secret`, `reddit.access_token` or
`reddit.refresh_token` plus app credentials, `discourse.base_url`,
`medium.access_token`, `bamboohr.api_key` plus `bamboohr.subdomain`,
`beeminder.auth_token`, `clockify.api_key`, and `harvest.access_token` plus
`harvest.account_id`, `oura.access_token`, `strava.access_token`,
`homeassistant.base_url` plus `homeassistant.access_token`, and
`philips_hue.access_token` plus `philips_hue.username`,
`activecampaign.api_key` plus `activecampaign.api_url`,
`convertkit.api_secret`, `getresponse.api_key`, and
`mailerlite.api_key`, plus
`github.access_token` and `gitlab.access_token`. GitHub credentials can also
provide `base_url` / `api_base_url` / `server`; GitLab credentials can provide
`base_url` / `server`. Business-service credentials use `bitly.access_token`,
`brandfetch.api_key`, `marketstack.api_key`, and `deepl.api_key`; DeepL can also
use `deepl.api_plan = free` for the free endpoint. Productivity credentials use
`todoist.api_key`, `trello.api_key`, and `trello.api_token`. Work-tracking
credentials use `asana.access_token` and `linear.api_key`. Scope a
credential to one tool with `allowed target = native_tool:<tool_name>`, share it
across native tools with `native_tool:*`, or leave the target blank when it is
safe for any target. Tool responses never expose plaintext credential values.

### calculator

Evaluate a safe arithmetic expression locally.

```python
calculator(expression: str)
```

Supported operators are `+`, `-`, `*`, `/`, `//`, `%`, `**`, and parentheses.
Supported functions include `sqrt`, `sin`, `cos`, `tan`, `log`, `round`, `min`,
and `max`; constants are `pi`, `e`, and `tau`.

### wikipedia_search

Search Wikipedia and return article summaries.

```python
wikipedia_search(query: str, top_k_results: int = 3, language: str = "en", max_chars: int = 4000)
```

Requires the Python `langchain-community` and `wikipedia` packages from
`requirements.txt`.

### wolfram_alpha_query

Query Wolfram|Alpha for computational facts and calculations.

```python
wolfram_alpha_query(query: str)
```

Uses vault provider `wolfram_alpha` fields `app_id`, `appid`, or `value`, then
falls back to `WOLFRAM_ALPHA_APP_ID`. Also requires the Python
`langchain-community` and `wolframalpha` packages from `requirements.txt`.

### searxng_search

Search a configured SearXNG instance and return JSON results.

```python
searxng_search(
    query: str,
    num_results: int = 10,
    page_number: int = 1,
    language: str = "en",
    safesearch: int = 0,
    categories: str = "",
    engines: str = "",
)
```

Uses vault provider `searxng` fields `base_url`, `url`, or `value`, then falls
back to `SEARXNG_BASE_URL`, pointing at the endpoint accepted by
`langchain_community.utilities.SearxSearchWrapper`.

### Public Information Tools

The public information batch includes:
- `coingecko_price(ids, vs_currencies?, include_market_cap?, include_24hr_vol?, include_24hr_change?, include_last_updated_at?)`
- `coingecko_coin_markets(vs_currency?, ids?, category?, order?, limit?, page?, sparkline?, price_change_percentage?)`
- `hackernews_search(query?, tags?, limit?, page?)`, `hackernews_get_item(item_id, include_comments?)`, `hackernews_get_user(username)`
- `npm_package_info(package_name, version?)` and `npm_package_search(query, limit?, offset?)`; optional vault provider `npm` supports `registry_url` / `base_url` and `token` / `api_key` / `value`.
- `open_thesaurus_synonyms(text, ...)`
- `rss_feed_read(url, max_items?, ignore_ssl?)`; marked MODERATE because it fetches arbitrary user-provided URLs.
- `nasa_apod(date?, start_date?, end_date?, thumbs?)`; uses vault provider `nasa` fields `api_key` or `value`, then `NASA_API_KEY`.
- `openweathermap_current(...)` and `openweathermap_forecast(...)`; use vault provider `openweathermap` fields `api_key`, `access_token`, or `value`, then `OPENWEATHERMAP_API_KEY`.
- `quickchart_create_url(chart_type, labels_json, data_json, ...)`

### Media Discovery Service Tools

This batch includes:
- `google_books_search(query, print_type?, projection?, order_by?, filter_value?, language?, country?, limit?, start_index?)` and `google_books_get_volume(volume_id, projection?, country?)`. Google Books can use public data without a key; saved credentials can still supply `api_key` and `base_url`.
- `youtube_search(query, search_type?, channel_id?, order?, published_after?, published_before?, region_code?, safe_search?, page_token?, limit?)`, `youtube_get_videos(video_ids, parts?, max_width?, max_height?)`, `youtube_get_channels(channel_ids?, for_username?, parts?, limit?, page_token?)`, and `youtube_list_playlist_items(playlist_id, parts?, page_token?, limit?)`.
- `spotify_search(query, types?, market?, limit?, offset?, include_external?)`, `spotify_get_track(track_id, market?)`, `spotify_get_artist(artist_id)`, `spotify_get_album(album_id, market?)`, and `spotify_get_playlist(playlist_id, market?, fields?, additional_types?)`.

Credential providers and fallback env vars:
- Google Books: provider `google_books`, fields `api_key`, `key`, `token`, or `value`; optional env fallback `GOOGLE_BOOKS_API_KEY`.
- YouTube Data API: provider `youtube`, fields `api_key`, `key`, `token`, or `value`; env fallback `YOUTUBE_API_KEY`.
- Spotify: provider `spotify`, fields `access_token`, `bearer_token`, `token`, or `value`; env fallback `SPOTIFY_ACCESS_TOKEN`. If no access token is saved, catalog tools use `client_id` plus `client_secret` from the vault or `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` to request a client-credentials token.

### Community Publishing Service Tools

This batch includes:
- `reddit_search_posts(query, subreddit?, sort?, time_filter?, limit?, after?, include_over_18?)`, `reddit_list_subreddit_posts(subreddit, listing?, time_filter?, limit?, after?)`, `reddit_get_post(post_id, subreddit?, comment_limit?, comment_sort?)`, `reddit_get_subreddit(subreddit)`, and `reddit_get_user(username)`. Read tools use Reddit OAuth when available and otherwise fall back to public `.json` endpoints.
- `reddit_create_post(subreddit, title, kind?, text?, url?, resubmit?, send_replies?)`, `reddit_create_comment(parent_fullname, text)`, and `reddit_delete_thing(fullname)`. These require a user OAuth token or refresh token with the needed Reddit scopes.
- `discourse_search(query, page?)`, `discourse_list_latest_topics(page?)`, `discourse_get_topic(topic_id, include_raw?)`, and `discourse_get_post(post_id)`. Public forums can be read with only `base_url`; private forums can add an API key and username.
- `discourse_create_topic(title, raw, category_id?, tags?)`, `discourse_create_post(topic_id, raw)`, and `discourse_update_post(post_id, raw, edit_reason?)`; these require a Discourse API key plus API username.
- `medium_get_me()`, `medium_list_publications(user_id?)`, `medium_create_post(...)`, and `medium_create_publication_post(...)`. Medium's official API is archived upstream, but the token-based endpoints remain implemented for compatibility with existing accounts that still use them.

Credential providers and fallback env vars:
- Reddit: provider `reddit`, fields `access_token`, `refresh_token`, `client_id`, `client_secret`, and optional `base_url` / `public_base_url` / `token_url`; env fallbacks `REDDIT_ACCESS_TOKEN`, `REDDIT_REFRESH_TOKEN`, `REDDIT_CLIENT_ID`, and `REDDIT_CLIENT_SECRET`.
- Discourse: provider `discourse`, fields `base_url`, `api_key`, and `api_username`; env fallbacks `DISCOURSE_BASE_URL`, `DISCOURSE_API_KEY`, and `DISCOURSE_API_USERNAME`.
- Medium: provider `medium`, fields `access_token`, `token`, or `value`; env fallback `MEDIUM_ACCESS_TOKEN`.

### Time And HR Service Tools

This batch includes:
- `bamboohr_list_employees()`, `bamboohr_get_employee(employee_id, fields?)`, `bamboohr_create_employee(first_name, last_name, fields_json?)`, `bamboohr_update_employee(employee_id, fields_json)`, and `bamboohr_get_company_report(report_id, only_current?, format?)`.
- `beeminder_get_user()`, `beeminder_list_goals()`, `beeminder_get_goal(goal_slug)`, `beeminder_list_datapoints(goal_slug, page?, per_page?)`, `beeminder_create_datapoint(goal_slug, value, comment?, timestamp?, request_id?)`, `beeminder_update_datapoint(goal_slug, datapoint_id, fields_json)`, and `beeminder_delete_datapoint(goal_slug, datapoint_id)`.
- `clockify_list_workspaces()`, `clockify_list_users(workspace_id, status?, limit?)`, `clockify_list_projects(workspace_id, name?, archived?, limit?)`, `clockify_create_project(...)`, `clockify_list_time_entries(...)`, `clockify_create_time_entry(...)`, `clockify_update_time_entry(...)`, and `clockify_delete_time_entry(...)`.
- `harvest_get_me()`, `harvest_get_company()`, `harvest_list_clients(...)`, `harvest_list_projects(...)`, `harvest_list_tasks(...)`, `harvest_list_time_entries(...)`, `harvest_create_time_entry(...)`, `harvest_update_time_entry(...)`, `harvest_stop_time_entry(time_entry_id)`, and `harvest_delete_time_entry(time_entry_id)`.

Credential providers and fallback env vars:
- BambooHR: provider `bamboohr`, fields `api_key` and `subdomain`, optional `base_url`; env fallbacks `BAMBOOHR_API_KEY`, `BAMBOOHR_SUBDOMAIN`, and `BAMBOOHR_BASE_URL`.
- Beeminder: provider `beeminder`, fields `auth_token`, `access_token`, `api_key`, or `value`; env fallback `BEEMINDER_ACCESS_TOKEN`.
- Clockify: provider `clockify`, fields `api_key` or `value`; env fallback `CLOCKIFY_API_KEY`.
- Harvest: provider `harvest`, fields `access_token` and `account_id`, optional `base_url`; env fallbacks `HARVEST_ACCESS_TOKEN`, `HARVEST_ACCOUNT_ID`, and `HARVEST_BASE_URL`.

### Personal Device Service Tools

This batch includes:
- `oura_get_profile()`, `oura_get_daily_activity(start_date?, end_date?, limit?)`, `oura_get_daily_readiness(start_date?, end_date?, limit?)`, and `oura_get_daily_sleep(start_date?, end_date?, limit?)`.
- `strava_list_activities(before?, after?, limit?)`, `strava_get_activity(activity_id, include_all_efforts?)`, `strava_create_activity(...)`, `strava_update_activity(activity_id, fields_json)`, `strava_list_activity_comments(activity_id, limit?)`, and `strava_get_activity_streams(activity_id, keys?, key_by_type?)`.
- `homeassistant_get_config()`, `homeassistant_check_config()`, `homeassistant_list_states(limit?)`, `homeassistant_get_state(entity_id)`, `homeassistant_set_state(entity_id, state, attributes_json?)`, `homeassistant_list_services(limit?)`, `homeassistant_call_service(domain, service, data_json?)`, `homeassistant_list_events(limit?)`, `homeassistant_fire_event(event_type, data_json?)`, `homeassistant_render_template(template)`, and `homeassistant_get_logbook(...)`.
- `philips_hue_list_lights(limit?)`, `philips_hue_get_light(light_id)`, `philips_hue_update_light_state(...)`, and `philips_hue_delete_light(light_id)`. Hue tools require an existing bridge username; they do not press the bridge link button or create a Hue user implicitly.

Credential providers and fallback env vars:
- Oura: provider `oura`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `OURA_ACCESS_TOKEN`.
- Strava: provider `strava`, fields `access_token`, `token`, or `value`; env fallback `STRAVA_ACCESS_TOKEN`.
- Home Assistant: provider `homeassistant`, fields `base_url` and `access_token`; env fallbacks `HOMEASSISTANT_BASE_URL` and `HOMEASSISTANT_ACCESS_TOKEN`.
- Philips Hue: provider `philips_hue`, fields `access_token` and `username`, optional `base_url`; env fallbacks `PHILIPS_HUE_ACCESS_TOKEN`, `PHILIPS_HUE_USERNAME`, and `PHILIPS_HUE_BASE_URL`.

### Marketing Contact Service Tools

This batch includes:
- `activecampaign_list_contacts(...)`, `activecampaign_get_contact(contact_id)`, `activecampaign_sync_contact(...)`, `activecampaign_update_contact(contact_id, fields_json)`, `activecampaign_list_lists(limit?)`, `activecampaign_list_tags(...)`, `activecampaign_add_contact_to_list(...)`, and `activecampaign_add_contact_tag(contact_id, tag_id)`. Reads are SAFE; sync/update/list/tag membership changes are MODERATE.
- `convertkit_get_account()`, `convertkit_list_forms()`, `convertkit_list_tags()`, `convertkit_list_subscribers(...)`, `convertkit_add_subscriber_to_form(...)`, and `convertkit_add_subscriber_to_tag(...)`. Reads are SAFE; subscription writes are MODERATE.
- `getresponse_list_campaigns()`, `getresponse_list_contacts(...)`, `getresponse_get_contact(contact_id)`, `getresponse_create_contact(...)`, `getresponse_update_contact(...)`, and `getresponse_delete_contact(contact_id)`. Reads are SAFE; contact writes/deletes are MODERATE.
- `mailerlite_list_subscribers(...)`, `mailerlite_get_subscriber(subscriber_id)`, `mailerlite_create_subscriber(...)`, `mailerlite_update_subscriber(...)`, and `mailerlite_list_groups(...)`. Reads are SAFE; subscriber writes are MODERATE.

Credential providers and fallback env vars:
- ActiveCampaign: provider `activecampaign`, fields `api_key` and `api_url` / `base_url`; env fallbacks `ACTIVECAMPAIGN_API_KEY` and `ACTIVECAMPAIGN_BASE_URL`.
- ConvertKit: provider `convertkit`, fields `api_secret`, `api_key`, or `value`; env fallback `CONVERTKIT_API_SECRET`.
- GetResponse: provider `getresponse`, fields `api_key`, `access_token`, or `value`; env fallback `GETRESPONSE_API_KEY`.
- MailerLite: provider `mailerlite`, fields `api_key`, `access_token`, or `value`; env fallback `MAILERLITE_API_KEY`. Set `MAILERLITE_CLASSIC_API=true` or credential field `classic_api=true` for Classic API header style.

### Developer Platform Tools

The developer-platform batch includes:
- `github_get_repository(owner, repo)`, `github_search_repositories(query, sort?, order?, limit?)`, `github_list_issues(owner, repo, state?, labels?, sort?, direction?, since?, limit?, include_pull_requests?)`, `github_get_issue(owner, repo, issue_number)`, `github_list_pull_requests(owner, repo, state?, sort?, direction?, limit?)`, `github_list_releases(owner, repo, limit?)`, and `github_get_release(owner, repo, tag_name)`.
- `gitlab_get_project(project)`, `gitlab_search_projects(query, limit?, simple?)`, `gitlab_list_project_issues(project, state?, labels?, order_by?, sort?, search?, limit?)`, `gitlab_get_project_issue(project, issue_iid)`, `gitlab_list_project_releases(project, order_by?, sort?, limit?)`, `gitlab_get_project_release(project, tag_name)`, and `gitlab_list_user_projects(user_id, limit?)`.

GitHub tools use vault provider `github` fields `access_token`, `token`,
`api_key`, or `value`, then `GITHUB_TOKEN`. GitHub Enterprise can be configured
with vault field `base_url`, `api_base_url`, `server`, or env
`GITHUB_API_BASE_URL`.

GitLab tools use vault provider `gitlab` fields `access_token`,
`private_token`, `token`, `api_key`, or `value`, then `GITLAB_TOKEN`. GitLab
self-managed instances can be configured with vault field `base_url`, `server`,
or env `GITLAB_BASE_URL`; plain instance URLs automatically get `/api/v4`
appended.

### Business And Language Service Tools

This batch includes:
- `bitly_get_bitlink(bitlink_id)`, `bitly_create_bitlink(long_url, title?, domain?, group_guid?, tags?)`, and `bitly_update_bitlink(bitlink_id, long_url?, title?, archived?, group_guid?, tags?)`. Create/update are MODERATE because they change Bitly state.
- `brandfetch_get_brand(domain)`, `brandfetch_get_brand_logos(domain)`, and `brandfetch_get_brand_colors(domain)`.
- `marketstack_get_eod(symbols, latest?, date?, date_from?, date_to?, limit?)`, `marketstack_get_ticker(symbol)`, and `marketstack_get_exchange(exchange)`.
- `deepl_translate_text(text, target_lang, source_lang?, formality?, preserve_formatting?)` and `deepl_list_languages(language_type?)`. Translation is MODERATE because it sends user text to DeepL and consumes quota.

Credential providers and fallback env vars:
- Bitly: provider `bitly`, fields `access_token`, `token`, `api_key`, or `value`; env fallback `BITLY_TOKEN`.
- Brandfetch: provider `brandfetch`, fields `api_key`, `token`, or `value`; env fallback `BRANDFETCH_API_KEY`.
- Marketstack: provider `marketstack`, fields `api_key`, `access_key`, `token`, or `value`; env fallback `MARKETSTACK_API_KEY`.
- DeepL: provider `deepl`, fields `api_key`, `auth_key`, `token`, or `value`; env fallback `DEEPL_API_KEY`. Use `api_plan` / `plan` or `DEEPL_API_PLAN=free` for the free endpoint.

### Productivity Service Tools

This batch includes:
- `todoist_list_tasks(project_id?, section_id?, label?, filter_query?, limit?)`, `todoist_get_task(task_id)`, `todoist_create_task(...)`, `todoist_update_task(...)`, `todoist_close_task(task_id)`, `todoist_list_projects(limit?)`, `todoist_get_project(project_id)`, and `todoist_create_project(...)`. Create/update/close are MODERATE because they change Todoist state.
- `trello_search(query, model_types?, limit?, partial?)`, `trello_get_board(board_id, fields?)`, `trello_list_board_lists(board_id, filter_value?, limit?)`, `trello_list_cards(list_id, filter_value?, limit?)`, `trello_get_card(card_id, fields?)`, `trello_create_card(...)`, `trello_update_card(...)`, and `trello_add_card_comment(card_id, text)`. Create/update/comment are MODERATE because they change Trello state.

Credential providers and fallback env vars:
- Todoist: provider `todoist`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `TODOIST_API_KEY`. Use `base_url` / `url` or `TODOIST_BASE_URL` for non-default API roots.
- Trello: provider `trello`, fields `api_key` / `key` and `api_token` / `token` / `value`; env fallback `TRELLO_API_KEY` plus `TRELLO_API_TOKEN`. Use `base_url` / `url` or `TRELLO_BASE_URL` for non-default API roots.

### Work Tracking Service Tools

This batch includes:
- `asana_get_user(user_gid?)`, `asana_list_users(workspace_gid, limit?)`, `asana_list_projects(workspace_gid?, team_gid?, archived?, limit?)`, `asana_get_project(project_gid, opt_fields?)`, `asana_create_project(...)`, `asana_update_project(...)`, `asana_list_tasks(...)`, `asana_search_tasks(...)`, `asana_get_task(task_gid, opt_fields?)`, `asana_create_task(...)`, `asana_create_subtask(...)`, `asana_update_task(...)`, and `asana_add_task_comment(task_gid, text, is_html?)`. Create/update/comment operations are MODERATE because they change Asana state.
- `linear_list_teams(limit?)`, `linear_list_users(limit?)`, `linear_list_workflow_states(team_id?, limit?)`, `linear_list_issues(team_id?, assignee_id?, state_id?, limit?)`, `linear_get_issue(issue_id)`, `linear_create_issue(...)`, `linear_update_issue(...)`, `linear_add_issue_comment(issue_id, body, parent_id?)`, and `linear_add_issue_link(issue_id, url)`. Create/update/comment/link operations are MODERATE because they change Linear state.

Credential providers and fallback env vars:
- Asana: provider `asana`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `ASANA_ACCESS_TOKEN`. Use `base_url` / `url` or `ASANA_BASE_URL` for non-default API roots.
- Linear: provider `linear`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `LINEAR_API_KEY`. Use `api_url` / `graphql_url` / `base_url` / `url` or `LINEAR_API_URL` for non-default GraphQL endpoints.

### Project Management Service Tools

This batch includes:
- `jira_get_myself()`, `jira_list_projects(query?, limit?)`, `jira_search_issues(jql, fields?, limit?)`, `jira_get_issue(issue_key, fields?, expand?)`, `jira_create_issue(...)`, `jira_update_issue(...)`, `jira_list_issue_transitions(issue_key)`, `jira_list_users(query, limit?)`, `jira_list_issue_comments(issue_key, limit?)`, and `jira_add_issue_comment(issue_key, body)`. Create/update/comment operations are MODERATE because they change Jira state.
- `clickup_list_teams()`, `clickup_list_spaces(team_id, archived?)`, `clickup_list_folders(space_id, archived?)`, `clickup_list_lists(folder_id?, space_id?, archived?)`, `clickup_get_task(task_id, include_subtasks?, include_markdown_description?)`, `clickup_list_tasks(...)`, `clickup_create_task(...)`, `clickup_update_task(...)`, `clickup_list_task_comments(task_id)`, and `clickup_add_task_comment(task_id, comment_text, notify_all?)`. Create/update/comment operations are MODERATE because they change ClickUp state.

Credential providers and fallback env vars:
- Jira: provider `jira`, fields `access_token`, `bearer_token`, `token`, or `value` for bearer auth; or `email` / `username` plus `api_token` / `apiToken` / `password` for basic auth. Use `base_url` / `domain` / `url` / `site_url` or `JIRA_BASE_URL` for the Jira Cloud site, such as `https://example.atlassian.net`. Env fallback supports `JIRA_ACCESS_TOKEN` or `JIRA_EMAIL` plus `JIRA_API_TOKEN`.
- ClickUp: provider `clickup`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `CLICKUP_ACCESS_TOKEN`. Use `base_url` / `url` or `CLICKUP_BASE_URL` for non-default API roots.

### Collaboration And Data Service Tools

This batch includes:
- `slack_list_channels(types?, exclude_archived?, limit?)`, `slack_get_channel_history(channel_id, ...)`, `slack_search_messages(query, ...)`, `slack_list_users(...)`, `slack_get_user(user_id)`, `slack_post_message(...)`, `slack_update_message(...)`, and `slack_add_reaction(...)`. Post/update/reaction operations are MODERATE because they change Slack state.
- `notion_search(query?, object_type?, limit?)`, `notion_get_page(page_id)`, `notion_get_block_children(block_id, ...)`, `notion_query_data_source(...)`, `notion_create_page(...)`, `notion_update_page(...)`, and `notion_append_block_children(...)`. Create/update/append operations are MODERATE because they change Notion content.
- `airtable_list_bases()`, `airtable_get_base_schema(base_id)`, `airtable_list_records(...)`, `airtable_get_record(...)`, `airtable_create_records(...)`, `airtable_update_records(...)`, and `airtable_delete_record(...)`. Create/update/delete operations are MODERATE because they change Airtable data.

Credential providers and fallback env vars:
- Slack: provider `slack`, fields `bot_token`, `access_token`, `token`, or `value`; env fallback `SLACK_BOT_TOKEN` or `SLACK_ACCESS_TOKEN`. Use `base_url` / `url` or `SLACK_BASE_URL` for non-default API roots.
- Notion: provider `notion`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `NOTION_API_KEY`. Use `notion_version` / `version` or `NOTION_VERSION` for the Notion API version; default is `2026-03-11`.
- Airtable: provider `airtable`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `AIRTABLE_ACCESS_TOKEN` or `AIRTABLE_API_KEY`. Use `base_url` / `url` or `AIRTABLE_BASE_URL` for non-default API roots.

### Customer Engagement Service Tools

This batch includes:
- `hubspot_list_crm_objects(...)`, `hubspot_search_crm_objects(...)`, `hubspot_get_crm_object(...)`, `hubspot_create_crm_object(...)`, `hubspot_update_crm_object(...)`, and `hubspot_archive_crm_object(...)`. Create/update/archive operations are MODERATE because they change HubSpot CRM data.
- `zendesk_search(...)`, `zendesk_get_ticket(ticket_id)`, `zendesk_list_tickets(...)`, `zendesk_create_ticket(...)`, `zendesk_update_ticket(...)`, `zendesk_get_user(user_id)`, and `zendesk_search_users(query, ...)`. Create/update operations are MODERATE because they change Zendesk support data.
- `mailchimp_list_audiences(...)`, `mailchimp_list_members(...)`, `mailchimp_get_member(...)`, `mailchimp_add_or_update_member(...)`, `mailchimp_update_member_tags(...)`, and `mailchimp_list_campaigns(...)`. Member add/update and tag changes are MODERATE because they change Mailchimp audience data.

Credential providers and fallback env vars:
- HubSpot: provider `hubspot`, fields `private_app_token`, `app_token`, `access_token`, `token`, or `value`; env fallback `HUBSPOT_ACCESS_TOKEN`. Use `base_url` / `url` or `HUBSPOT_BASE_URL` for non-default API roots.
- Zendesk: provider `zendesk`, fields `access_token`, `token`, or `value` for bearer auth; or `email` plus `api_token` / `apiToken` / `password` and `subdomain` / `base_url` for API-token auth. Env fallback supports `ZENDESK_ACCESS_TOKEN` or `ZENDESK_EMAIL` plus `ZENDESK_API_TOKEN` and `ZENDESK_SUBDOMAIN`.
- Mailchimp: provider `mailchimp`, fields `api_key`, `access_token`, `token`, or `value`; env fallback `MAILCHIMP_API_KEY` or `MAILCHIMP_ACCESS_TOKEN`. Use `server_prefix` / `dc` / `data_center` or `MAILCHIMP_SERVER_PREFIX`; API keys ending in `-usX` derive the server prefix automatically.

### Support Service Tools

This batch includes:
- `freshdesk_list_tickets(...)`, `freshdesk_search_tickets(...)`, `freshdesk_get_ticket(ticket_id)`, `freshdesk_create_ticket(...)`, `freshdesk_update_ticket(...)`, `freshdesk_delete_ticket(ticket_id)`, `freshdesk_list_contacts(...)`, `freshdesk_get_contact(contact_id)`, `freshdesk_create_contact(...)`, and `freshdesk_update_contact(...)`. Create/update/delete operations are MODERATE because they change Freshdesk support data.
- `helpscout_list_mailboxes(...)`, `helpscout_get_mailbox(mailbox_id)`, `helpscout_list_conversations(...)`, `helpscout_get_conversation(conversation_id)`, `helpscout_create_conversation(...)`, `helpscout_create_thread(...)`, `helpscout_list_customers(...)`, `helpscout_get_customer(customer_id)`, `helpscout_create_customer(...)`, and `helpscout_update_customer(...)`. Create/update/thread operations are MODERATE because they change Help Scout inbox data.
- `intercom_list_contacts(...)`, `intercom_search_contacts(...)`, `intercom_get_contact(contact_id)`, `intercom_create_contact(...)`, `intercom_update_contact(...)`, `intercom_archive_contact(contact_id)`, `intercom_list_conversations(...)`, `intercom_get_conversation(conversation_id)`, and `intercom_reply_conversation(...)`. Create/update/archive/reply operations are MODERATE because they change Intercom workspace data or send customer-facing/admin messages.

Credential providers and fallback env vars:
- Freshdesk: provider `freshdesk`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `FRESHDESK_API_KEY`. Use `domain` / `subdomain` or `FRESHDESK_DOMAIN`, or `base_url` / `url` / `FRESHDESK_BASE_URL` for the full API root.
- Help Scout: provider `helpscout`, fields `access_token`, `token`, or `value`; env fallback `HELPSCOUT_ACCESS_TOKEN`. Use `base_url` / `url` or `HELPSCOUT_BASE_URL` for non-default API roots.
- Intercom: provider `intercom`, fields `access_token`, `api_key`, `token`, or `value`; env fallback `INTERCOM_ACCESS_TOKEN`. Use `base_url` / `url` or `INTERCOM_BASE_URL` for regional API roots; `intercom_version` / `version` or `INTERCOM_VERSION` overrides the API version header.

### Sales CRM Service Tools

This batch includes:
- `pipedrive_list_records(resource, ...)`, `pipedrive_search_records(resource, term, ...)`, and `pipedrive_get_record(resource, record_id)` for SAFE read access across deals, persons/people, organizations, activities, leads, notes, and products where supported by Pipedrive.
- `pipedrive_create_record(resource, fields_json)`, `pipedrive_update_record(resource, record_id, fields_json)`, and `pipedrive_delete_record(resource, record_id)`. These are MODERATE because they change Pipedrive CRM data.
- `pipedrive_list_users(...)` for resolving owner/user IDs.

Credential providers and fallback env vars:
- Pipedrive: provider `pipedrive`, fields `api_token`, `apiToken`, `token`, or `value` for API-token auth; or `access_token` / `bearer_token` for OAuth bearer auth. Env fallback supports `PIPEDRIVE_API_TOKEN` or `PIPEDRIVE_ACCESS_TOKEN`. Use `base_url` / `url` or `PIPEDRIVE_BASE_URL` for non-default API roots.

### Messaging Delivery Service Tools

This batch includes:
- `twilio_send_message(...)`, `twilio_list_messages(...)`, `twilio_get_message(message_sid)`, and `twilio_make_call(...)`. Send/call operations are MODERATE because they contact external recipients and can consume telecom spend.
- `sendgrid_send_email(...)`, `sendgrid_list_contacts(...)`, `sendgrid_get_contact(contact_id)`, `sendgrid_upsert_contacts(...)`, and `sendgrid_list_lists(...)`. Send/upsert operations are MODERATE because they send email or change marketing-contact data.
- `mailgun_send_email(...)`, `mailgun_list_events(...)`, and `mailgun_get_domain()`. Email sending is MODERATE because it contacts external recipients and consumes sending quota.

Credential providers and fallback env vars:
- Twilio: provider `twilio`, fields `account_sid` / `accountSid` / `sid`, `auth_token` / `authToken` / `api_key_secret` / `apiKeySecret` / `token` / `value`, and optional `api_key_sid` / `apiKeySid`. Env fallback supports `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and optional `TWILIO_API_KEY_SID`. Use `base_url` / `url` or `TWILIO_BASE_URL` for non-default API roots.
- SendGrid: provider `sendgrid`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `SENDGRID_API_KEY`. Use `base_url` / `url` or `SENDGRID_BASE_URL` for non-default API roots.
- Mailgun: provider `mailgun`, fields `api_key`, `apiKey`, `token`, or `value`, plus `domain` / `email_domain` / `emailDomain`; env fallback `MAILGUN_API_KEY` and `MAILGUN_DOMAIN`. Use `base_url` / `api_domain` / `url` or `MAILGUN_BASE_URL` for non-default API roots.
- Brevo: provider `brevo`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `BREVO_API_KEY`. Use `base_url` / `url` or `BREVO_BASE_URL` for non-default API roots.
- Mailjet: provider `mailjet`, fields `api_key` plus `secret_key` for email API auth, and `sms_token` / `token` for SMS auth. Env fallback supports `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`, and `MAILJET_SMS_TOKEN`. Use `base_url` / `url` or `MAILJET_BASE_URL` for regional API roots.
- Mandrill: provider `mandrill`, fields `api_key`, `key`, `token`, or `value`; env fallback `MANDRILL_API_KEY`. Use `base_url` / `url` or `MANDRILL_BASE_URL` for non-default API roots.
- MessageBird: provider `messagebird`, fields `access_key`, `accessKey`, `api_key`, `token`, or `value`; env fallback `MESSAGEBIRD_ACCESS_KEY`. Use `base_url` / `url` or `MESSAGEBIRD_BASE_URL` for non-default API roots.
- Mocean: provider `mocean`, fields `api_key` / `mocean-api-key` plus `api_secret` / `mocean-api-secret`; env fallback `MOCEAN_API_KEY` and `MOCEAN_API_SECRET`. Use `base_url` / `url` or `MOCEAN_BASE_URL` for non-default API roots.
- MSG91: provider `msg91`, fields `auth_key`, `authkey`, `api_key`, `token`, or `value`; env fallback `MSG91_AUTH_KEY`. Use `base_url` / `url` or `MSG91_BASE_URL` for non-default API roots.

### Commerce Billing Service Tools

This batch includes:
- `stripe_list_records(resource, ...)`, `stripe_search_records(resource, query, ...)`, `stripe_get_record(resource, record_id)`, and `stripe_get_balance()` for SAFE read/search access across common Stripe billing records. `stripe_create_customer(...)` and `stripe_update_customer(...)` are MODERATE because they change customer data.
- `shopify_list_records(resource, ...)` and `shopify_get_record(resource, record_id)` for SAFE Shopify Admin REST reads across products, orders, and customers. `shopify_create_product(...)` and `shopify_update_product(...)` are MODERATE because they change storefront catalog data.
- `woocommerce_list_records(resource, ...)` and `woocommerce_get_record(resource, record_id)` for SAFE WooCommerce reads across products, orders, and customers. `woocommerce_create_record(...)` and `woocommerce_update_record(...)` are MODERATE because they change store data.
- `chargebee_list_records(resource, ...)` and `chargebee_get_record(resource, record_id)` for SAFE Chargebee reads across customers, subscriptions, invoices, transactions, items, item prices, and plans. `chargebee_create_customer(...)` and `chargebee_update_customer(...)` are MODERATE because they change billing customer data.

Credential providers and fallback env vars:
- Stripe: provider `stripe`, fields `secret_key`, `secretKey`, `api_key`, `apiKey`, `token`, or `value`; env fallback `STRIPE_SECRET_KEY`. Use `base_url` / `url` or `STRIPE_BASE_URL` for non-default API roots.
- Shopify: provider `shopify`, fields `shop_subdomain` / `shopSubdomain` / `shop` / `domain` plus `access_token` / `accessToken` / `token` / `value` for modern Admin API token auth. Legacy basic auth can use `api_key` / `apiKey` plus `password`. Env fallback supports `SHOPIFY_SHOP`, `SHOPIFY_ACCESS_TOKEN`, optional `SHOPIFY_API_VERSION`, legacy `SHOPIFY_API_KEY` and `SHOPIFY_PASSWORD`, and `SHOPIFY_BASE_URL` for a full Admin REST root.
- WooCommerce: provider `woocommerce`, fields `url` / `site_url` / `base_url`, `consumer_key` / `consumerKey`, and `consumer_secret` / `consumerSecret`. Env fallback supports `WOOCOMMERCE_URL`, `WOOCOMMERCE_BASE_URL`, `WOOCOMMERCE_CONSUMER_KEY`, and `WOOCOMMERCE_CONSUMER_SECRET`.
- Chargebee: provider `chargebee`, fields `site` / `account_name` / `accountName` / `subdomain` plus `api_key` / `apiKey` / `token` / `value`; env fallback `CHARGEBEE_SITE` and `CHARGEBEE_API_KEY`. Use `base_url` / `url` or `CHARGEBEE_BASE_URL` for non-default API roots.

### Notification Service Tools

This batch includes:
- `pushbullet_send_push(...)`, `pushbullet_list_pushes(...)`, `pushbullet_update_push(...)`, and `pushbullet_delete_push(push_id)` for Pushbullet note/link pushes and push history. Listing is SAFE; send/update/delete are MODERATE because they notify users or change push history.
- `pushcut_send_notification(notification_name, ...)` for Pushcut smart notifications. Sending is MODERATE because it notifies devices and can trigger linked actions.
- `gotify_send_message(...)`, `gotify_list_messages(...)`, and `gotify_delete_message(message_id)`. Listing is SAFE; send/delete are MODERATE because they notify clients or change message history.
- `pushover_send_message(...)` for Pushover notifications, including emergency-priority retry/expire fields. Sending is MODERATE because it contacts devices and can consume app quota.
- `signl4_send_alert(...)` and `signl4_resolve_alert(external_id, ...)` for SIGNL4 alert events. Both are MODERATE because they notify on-call teams or change alert state.

Credential providers and fallback env vars:
- Pushbullet: provider `pushbullet`, fields `access_token`, `accessToken`, `api_key`, `apiKey`, `token`, or `value`; env fallback `PUSHBULLET_ACCESS_TOKEN`. Use `base_url` / `url` or `PUSHBULLET_BASE_URL` for non-default API roots.
- Pushcut: provider `pushcut`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `PUSHCUT_API_KEY`. Use `base_url` / `url` or `PUSHCUT_BASE_URL` for non-default API roots.
- Gotify: provider `gotify`, fields `base_url` / `url`, `app_token` / `appApiToken` for sending, and `client_token` / `clientApiToken` for list/delete operations. Env fallback supports `GOTIFY_BASE_URL`, `GOTIFY_APP_TOKEN`, and `GOTIFY_CLIENT_TOKEN`.
- Pushover: provider `pushover`, fields `api_token` / `api_key` / `apiKey` / `token` plus `user_key` / `userKey` / `user`; env fallback `PUSHOVER_API_TOKEN` and `PUSHOVER_USER_KEY`. Use `base_url` / `url` or `PUSHOVER_BASE_URL` for non-default API roots.
- SIGNL4: provider `signl4`, fields `team_secret` / `teamSecret` / `secret` or `webhook_url`; env fallback `SIGNL4_TEAM_SECRET` or `SIGNL4_WEBHOOK_URL`. Use `base_url` / `url` or `SIGNL4_BASE_URL` for non-default webhook roots.

### Content Management Service Tools

This batch includes:
- `wordpress_list_records(resource, ...)`, `wordpress_get_record(resource, record_id)`, `wordpress_create_record(resource, ...)`, `wordpress_update_record(resource, record_id, ...)`, and `wordpress_delete_record(resource, record_id)`. Reads are SAFE; create/update/delete are MODERATE because they change site content or users.
- `strapi_list_entries(collection, ...)`, `strapi_get_entry(collection, entry_id)`, `strapi_create_entry(collection, data)`, `strapi_update_entry(collection, entry_id, data)`, and `strapi_delete_entry(collection, entry_id)`. Reads are SAFE; mutations are MODERATE because they change CMS records.
- `contentful_list_records(resource, ...)` and `contentful_get_record(resource, record_id, ...)` for Contentful Delivery/Preview API reads. Both are SAFE.
- `ghost_list_posts(...)`, `ghost_get_post(...)`, `ghost_create_post(...)`, `ghost_update_post(post_id, ...)`, and `ghost_delete_post(post_id)`. Reads use the Content API and are SAFE; Admin API mutations are MODERATE.
- `storyblok_list_stories(...)`, `storyblok_get_story(...)`, `storyblok_publish_story(story_id)`, `storyblok_unpublish_story(story_id)`, and `storyblok_delete_story(story_id)`. Reads are SAFE; publish/unpublish/delete are MODERATE because they change public content state or remove content.

Credential providers and fallback env vars:
- WordPress: provider `wordpress`, fields `url` / `site_url` / `base_url`, `username`, and `password` / `application_password`; env fallback `WORDPRESS_URL`, `WORDPRESS_USERNAME`, and `WORDPRESS_PASSWORD`.
- Strapi: provider `strapi`, fields `url` / `base_url`, `api_token` / `jwt` / `token`, or `email` plus `password` for local auth. Env fallback supports `STRAPI_URL`, `STRAPI_API_TOKEN`, `STRAPI_EMAIL`, `STRAPI_PASSWORD`, and `STRAPI_API_VERSION`.
- Contentful: provider `contentful`, fields `space_id` / `spaceId`, delivery token (`access_token`, `delivery_token`, or Contentful-style names), and optional preview token. Env fallback supports `CONTENTFUL_SPACE_ID`, `CONTENTFUL_DELIVERY_TOKEN`, `CONTENTFUL_PREVIEW_TOKEN`, `CONTENTFUL_BASE_URL`, and `CONTENTFUL_PREVIEW_BASE_URL`.
- Ghost: provider `ghost`, fields `url`, `content_api_key`, and `admin_api_key` (`key_id:hex_secret`). Env fallback supports `GHOST_URL`, `GHOST_CONTENT_API_KEY`, `GHOST_ADMIN_API_KEY`, and `GHOST_API_VERSION`.
- Storyblok: provider `storyblok`, fields `content_token`, `management_token`, and `space_id` / `spaceId`; env fallback supports `STORYBLOK_CONTENT_TOKEN`, `STORYBLOK_MANAGEMENT_TOKEN`, `STORYBLOK_SPACE_ID`, `STORYBLOK_CONTENT_BASE_URL`, and `STORYBLOK_MANAGEMENT_BASE_URL`.

### Operations Monitoring Service Tools

This batch includes:
- `netlify_list_sites(...)`, `netlify_get_site(site_id)`, `netlify_list_deploys(site_id, ...)`, `netlify_get_deploy(site_id, deploy_id)`, `netlify_cancel_deploy(deploy_id)`, and `netlify_delete_site(site_id)`. Reads are SAFE; cancel/delete operations are MODERATE because they alter deploy or site state.
- `uptimerobot_get_account()`, `uptimerobot_list_monitors(...)`, `uptimerobot_get_monitor(monitor_id, ...)`, `uptimerobot_create_monitor(...)`, `uptimerobot_update_monitor(monitor_id, ...)`, `uptimerobot_delete_monitor(monitor_id)`, and `uptimerobot_reset_monitor(monitor_id)`. Reads are SAFE; create/update/delete/reset are MODERATE because they alter monitoring state.
- `pagerduty_list_incidents(...)`, `pagerduty_get_incident(incident_id)`, `pagerduty_create_incident(...)`, `pagerduty_update_incident(incident_id, ...)`, `pagerduty_add_incident_note(incident_id, content, ...)`, `pagerduty_list_services(...)`, and `pagerduty_get_user(user_id)`. Reads are SAFE; incident writes and notes are MODERATE because they change incident workflows.
- `sentry_list_organizations(...)`, `sentry_list_projects(...)`, `sentry_list_project_issues(...)`, `sentry_get_issue(issue_id)`, `sentry_update_issue(organization_slug, issue_id, ...)`, `sentry_list_project_events(...)`, and `sentry_get_event(...)`. Reads are SAFE; issue updates are MODERATE.
- `cloudflare_list_zones(...)`, `cloudflare_list_dns_records(zone_id, ...)`, `cloudflare_create_dns_record(...)`, `cloudflare_update_dns_record(...)`, `cloudflare_delete_dns_record(...)`, `cloudflare_list_origin_certificates(zone_id, ...)`, `cloudflare_get_origin_certificate(...)`, `cloudflare_upload_origin_certificate(...)`, and `cloudflare_delete_origin_certificate(...)`. Reads are SAFE; DNS/certificate writes are MODERATE.

Credential providers and fallback env vars:
- Netlify: provider `netlify`, fields `access_token` / `api_key` / `token` / `value`; env fallback `NETLIFY_ACCESS_TOKEN`. Use `base_url` / `url` or `NETLIFY_BASE_URL` for non-default API roots.
- UptimeRobot: provider `uptimerobot`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `UPTIMEROBOT_API_KEY`. Use `base_url` / `url` or `UPTIMEROBOT_BASE_URL` for non-default API roots.
- PagerDuty: provider `pagerduty`, fields `api_token` / `api_key` / `token` / `value` for REST API-token auth, or `access_token` for OAuth bearer auth. Env fallback supports `PAGERDUTY_API_TOKEN`, optional `PAGERDUTY_FROM_EMAIL`, and `PAGERDUTY_BASE_URL`.
- Sentry: provider `sentry`, fields `auth_token` / `access_token` / `api_key` / `token` / `value`; env fallback `SENTRY_AUTH_TOKEN`. Use `base_url` / `url` or `SENTRY_BASE_URL` for self-hosted Sentry.
- Cloudflare: provider `cloudflare`, fields `api_token` / `access_token` / `token` / `value`; env fallback `CLOUDFLARE_API_TOKEN`. Use `base_url` / `url` or `CLOUDFLARE_BASE_URL` for non-default API roots.

### Enrichment Security Service Tools

This batch includes:
- `urlscan_search_scans(query, ...)`, `urlscan_get_result(scan_id)`, and `urlscan_submit_scan(url, ...)`. Searches and result reads are SAFE; scan submission is MODERATE because it sends a URL to an external scanner and may make scan artifacts discoverable depending on visibility.
- `hunter_domain_search(domain, ...)`, `hunter_email_finder(domain, first_name, last_name)`, and `hunter_email_verifier(email)` for Hunter email discovery and deliverability data. These are SAFE read/enrichment calls.
- `mailcheck_check_email(email)` for Mailcheck email validation. This is SAFE.
- `peekalink_preview_url(url)` and `peekalink_check_availability(url)` for Peekalink link metadata. These are SAFE.
- `jina_reader_fetch_url(url, ...)`, `jina_search_web(query, ...)`, and `jina_deep_research(query, ...)` for Jina Reader/Search/DeepSearch. Reader and Search are SAFE extraction/search calls; DeepSearch is MODERATE because it can perform broader external research and consume hosted AI quota.

Credential providers and fallback env vars:
- urlscan.io: provider `urlscan`, fields `api_key`, `apiKey`, `access_token`, `token`, or `value`; env fallback `URLSCAN_API_KEY`. Use `base_url` / `url` or `URLSCAN_BASE_URL` for non-default API roots.
- Hunter: provider `hunter`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `HUNTER_API_KEY`. Use `base_url` / `url` or `HUNTER_BASE_URL` for non-default API roots.
- Mailcheck: provider `mailcheck`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `MAILCHECK_API_KEY`. Use `base_url` / `url` or `MAILCHECK_BASE_URL` for non-default API roots.
- Peekalink: provider `peekalink`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `PEEKALINK_API_KEY`. Use `base_url` / `url` or `PEEKALINK_BASE_URL` for non-default API roots.
- Jina AI: provider `jina`, fields `api_key`, `apiKey`, `access_token`, `token`, or `value`; env fallback `JINA_API_KEY`. Reader/Search can run without a key where Jina allows anonymous usage; DeepSearch requires a saved credential or env key. Base URL overrides are `JINA_READER_BASE_URL`, `JINA_SEARCH_BASE_URL`, and `JINA_DEEPSEARCH_BASE_URL`.

### Data Table Service Tools

This batch includes:
- `baserow_list_tables(...)`, `baserow_list_fields(table_id)`, `baserow_list_rows(table_id, ...)`, `baserow_get_row(table_id, row_id)`, `baserow_create_row(table_id, fields_json, ...)`, `baserow_update_row(table_id, row_id, fields_json, ...)`, and `baserow_delete_row(table_id, row_id)`. Reads are SAFE; create/update/delete are MODERATE because they change table data.
- `nocodb_list_bases(...)`, `nocodb_get_base(base_id, ...)`, `nocodb_list_records(base_id, table_id, ...)`, `nocodb_get_record(base_id, table_id, record_id)`, `nocodb_count_records(base_id, table_id, ...)`, `nocodb_create_record(base_id, table_id, fields_json)`, `nocodb_update_record(base_id, table_id, record_id, fields_json)`, and `nocodb_delete_record(base_id, table_id, record_id)`. Reads are SAFE; create/update/delete are MODERATE.
- `coda_list_docs(...)`, `coda_list_tables(doc_id, ...)`, `coda_list_table_rows(doc_id, table_id, ...)`, `coda_get_table_row(doc_id, table_id, row_id, ...)`, `coda_create_table_row(doc_id, table_id, cells_json, ...)`, `coda_update_table_row(doc_id, table_id, row_id, cells_json, ...)`, `coda_delete_table_row(doc_id, table_id, row_id)`, `coda_list_formulas(doc_id, ...)`, and `coda_list_controls(doc_id, ...)`. Reads are SAFE; row writes/deletes are MODERATE.
- `grist_list_orgs()`, `grist_list_workspaces(org_id)`, `grist_list_docs(workspace_id)`, `grist_list_tables(doc_id)`, `grist_list_columns(doc_id, table_id)`, `grist_list_records(doc_id, table_id, ...)`, `grist_create_record(doc_id, table_id, fields_json)`, `grist_update_record(doc_id, table_id, record_id, fields_json)`, and `grist_delete_records(doc_id, table_id, row_ids)`. Reads are SAFE; record writes/deletes are MODERATE.

Credential providers and fallback env vars:
- Baserow: provider `baserow`, fields `token`, `api_token`, `apiKey`, `api_key`, `database_token`, or `value`; env fallback `BASEROW_API_TOKEN`. Use `base_url` / `host` / `url` or `BASEROW_BASE_URL` for self-hosted Baserow.
- NocoDB: provider `nocodb`, fields `api_token`, `apiToken`, `token`, `access_token`, `api_key`, or `value`; env fallback `NOCODB_API_TOKEN`. Use `base_url` / `host` / `url` or `NOCODB_BASE_URL` for self-hosted NocoDB. `NOCODB_AUTH_HEADER` defaults to `xc-token`; set `xc-auth` when using a user token.
- Coda: provider `coda`, fields `access_token`, `api_token`, `api_key`, `token`, or `value`; env fallback `CODA_API_TOKEN`. Use `base_url` / `url` or `CODA_BASE_URL` for non-default API roots.
- Grist: provider `grist`, fields `api_key`, `apiKey`, `token`, or `value`; env fallback `GRIST_API_KEY`. Use `base_url` / `url` or `GRIST_BASE_URL` for paid-team or self-hosted API roots.

### Chat Platform Service Tools

This batch includes:
- `discord_list_guild_channels(guild_id)`, `discord_get_channel(channel_id)`, `discord_get_channel_messages(channel_id, ...)`, `discord_send_channel_message(channel_id, content, ...)`, and `discord_delete_message(channel_id, message_id)`. Reads are SAFE; send/delete are MODERATE.
- `mattermost_get_me()`, `mattermost_list_teams(...)`, `mattermost_list_channels(team_id, ...)`, `mattermost_list_channel_posts(channel_id, ...)`, `mattermost_create_post(channel_id, message, ...)`, and `mattermost_delete_post(post_id)`. Reads are SAFE; create/delete are MODERATE.
- `matrix_whoami()`, `matrix_list_joined_rooms()`, `matrix_get_room_messages(room_id, ...)`, `matrix_send_room_message(room_id, body, ...)`, and `matrix_leave_room(room_id)`. Reads are SAFE; sending/leaving are MODERATE.
- `rocketchat_get_me()`, `rocketchat_list_channels(...)`, `rocketchat_get_channel_history(...)`, `rocketchat_post_message(channel, text, ...)`, and `rocketchat_delete_message(room_id, message_id)`. Reads are SAFE; post/delete are MODERATE.
- `zulip_get_profile()`, `zulip_list_streams(...)`, `zulip_get_messages(...)`, `zulip_send_message(message_type, to, content, ...)`, and `zulip_delete_message(message_id)`. Reads are SAFE; send/delete are MODERATE.

Credential providers and fallback env vars:
- Discord: provider `discord`, fields `bot_token`, `botToken`, `token`, or `value`; env fallback reuses `DISCORD_BOT_TOKEN`. Optional base override `DISCORD_BASE_URL`.
- Mattermost: provider `mattermost`, fields `access_token`, `accessToken`, `api_token`, `token`, or `value`; env fallback `MATTERMOST_ACCESS_TOKEN`. Save `base_url` / `baseUrl` or set `MATTERMOST_BASE_URL`; the tool appends `/api/v4` when needed.
- Matrix: provider `matrix`, fields `access_token`, `accessToken`, `token`, or `value`; env fallback `MATRIX_ACCESS_TOKEN`. Save `homeserverUrl` / `base_url` or set `MATRIX_BASE_URL`; the tool appends `/_matrix/client/v3` when needed.
- Rocket.Chat: provider `rocketchat`, fields `auth_token`, `authKey`, `token`, or `value`, plus `user_id` / `userId`; env fallbacks `ROCKETCHAT_AUTH_TOKEN`, `ROCKETCHAT_USER_ID`, and `ROCKETCHAT_BASE_URL`.
- Zulip: provider `zulip`, fields `api_key`, `apiKey`, `token`, or `value`, plus `email`; env fallbacks `ZULIP_API_KEY`, `ZULIP_EMAIL`, and `ZULIP_BASE_URL`.

### tool_enable

Enable, disable, or inspect current-thread tool bindings. This is normally
available after the agent activates `Skill(name="self-improve")`.

```python
tool_enable(action: str, tools: list[str] = None, category: str = "", ttl: str = "2h", force: bool = False)
```

**Actions:**
- `enable` — Enable tools by name (`tools`) or by category (`category`). In `astream()` (REST/SSE and sync-worker bridge callers) and `chat()` (MCP final-string path), this triggers an in-turn graph rebuild so the tools are callable in the very next step of the same user message.
- `disable` — Disable tools for the thread (`tools`). Takes effect on the next agent step. Refuses core tools (`bash_execute`, `file_read`, etc.) unless `force=True`. Mixed batches partially succeed: non-core names are disabled, core names are listed under `[Refused]` with a hint to retry that subset with `force=True`. Disable is non-destructive — it only appends to `disabled_tools`; entries in `enabled_tools` / `temporary_tools` are preserved, so a subsequent `enable` restores the tool's original permanent/TTL state. "Core" here is the hardcoded `ALL_TOOLS` set, which is a **superset** of what the `already_default` classifier bucket calls default-bound (user profile's `default_thread_tools` curates a subset of `ALL_TOOLS`).
- `list_categories` — List all tool categories with tool counts.
- `status` / `inspect` — Show currently enabled/disabled tools for this thread, with TTL remaining per entry.

**Parameters:**
- `action` (`str`): One of: `enable`, `disable`, `list_categories`, `status`.
- `tools` (`list[str]`): Specific tool names to enable or disable.
- `category` (`str`): Category name to enable all tools in.
- `ttl` (`str`): For `enable` only — how long to keep tools bound before lazy eviction. One of: `"30m"`, `"2h"` (default), `"6h"`, `"24h"`, `"permanent"`.
- `force` (`bool`): For `disable` only — set `True` to allow disabling core tools. Default `False`.

**Enable response buckets:** every input tool is classified in exactly one bucket, checked in this priority order — (1) `Un-disabled` (was in `disabled_tools`, now removed; if the tool has a preserved `enabled_tools` or `temporary_tools` entry, it is restored AS-IS — the requested `ttl` does NOT apply, so a batch-level TTL can't silently promote/demote an unrelated tool; a fresh entry is only written when there is no preserved state and no default binding), (2) `Already permanent` (in `tc.enabled_tools`; TTL requests are rejected, no demotion), (3) `Already bound (default set)` (in the thread's default-bound set — `ALL_TOOLS` or the user-profile-level `default_thread_tools` override; already callable, no write), (4) `TTL refreshed` (in `tc.temporary_tools`; `expires_at` pushed out), (5) `Promoted to permanent` (in `tc.temporary_tools`, `ttl="permanent"` → moved to `tc.enabled_tools`), (6) `Newly loaded` (none of the above; written fresh to `enabled_tools` or `temporary_tools` depending on `ttl`).

The classifier sources its default-bound set from the same place as graph-build (`agent._build_graph_with_prompt`: `profile.tool_preferences.default_thread_tools` if set, else `{t.name for t in ALL_TOOLS}`). Tools that live in `ALL_TOOLS` but are excluded from the user's `default_thread_tools` list are correctly treated as optional (priority-6 newly-loaded) rather than already-bound. Note: the bucket is called `Already bound (default set)` — not "core" — to avoid conflating it with the `disable` guard's "core" protection, which uses the broader `ALL_TOOLS` list.

**`disabled_tools` is authoritative in graph-build.** The graph-build pipeline is: start with the default-bound set, filter out `disabled_tools`, then add extras from `enabled_tools ∪ live_temporary_tools` — BUT extras are also filtered by `disabled_tools` before merging. So a tool listed in both `enabled_tools` and `disabled_tools` is unbound (disable wins). This lets `disable` be non-destructive: it only appends to `disabled_tools` and leaves `enabled_tools` / `temporary_tools` alone. An `enable` on that same tool just removes it from `disabled_tools`; the preserved permanent/TTL entry comes back automatically. Without this rule, `disable` would have to destructively mutate `enabled_tools` to actually disable an overlapping tool, and a disable→enable round-trip would silently strip the permanent badge.

**Status display filters disabled tools from the enabled sections.** Because `disabled_tools` is authoritative, a tool that has a preserved `enabled_tools` or `temporary_tools` entry while ALSO being in `disabled_tools` is currently unbound. The `status` and `search` renderers suppress such tools from the `Enabled (permanent)` / `Enabled (TTL)` sections and annotate them in the `Disabled` section with `(preserved: permanent)` or `(preserved: Xm left)`, so the user can still see what will round-trip back on un-disable without seeing the same tool in two places.

### manage_mcp

Search, preview, install, and inspect MCP servers through the capability
expansion path.

```python
manage_mcp(action: str, query: str = "", source: str = "", name: str = "", confirmed: bool = False, config_values: dict = {}, ttl: str = "2h", auto_enable_thread: bool = True)
```

Actions are `search`, `preview`, `install`, and `inspect`/`status`/`list`.
Successful installs reload MCP server tools, enable discovered
`mcp__<server>__<tool>` tools on the current thread when
`auto_enable_thread=true`, and queue a same-turn reload with
`source="mcp_install"`. Installing MCP servers remains admin-only at execution
time because stdio servers can launch local commands.

### skill_manage

List, search, install, enable, disable, or inspect Agent Skills.

```python
skill_manage(action: str, query: str = "", name: str = "", source: str = "installed", scope: str = "user", activate_current_thread: bool = False)
```

Actions are `list`, `search`, `install`, `enable`, `disable`, and `inspect`.
Marketplace installs default to user scope; global scope requires admin.
When a skill is installed or enabled on the current thread and a graph rebuild
is needed, reload metadata uses `source="skill_install"`.

#### In-turn auto-continue

When the agent calls `tool_enable(action="enable", tools=[...])` during a turn, the enable result explicitly tells the agent to stop after that tool result. It should not write a final answer, explain the enablement, or attempt to call the newly enabled tool in the same graph invocation. The harness then:

1. Persists the enablement to the thread config (with TTL) and invalidates the cached graph.
2. Finishes the current graph invocation normally.
3. Emits a `tool_reload` SSE event (`{type: "tool_reload", tools, ttl, ttl_seconds, source, skill_name, reason}`).
4. Builds a fresh graph with the new tools bound to the LLM.
5. Injects an internal resume message (`internal_type="tool_reload_resume"`) and drives the new graph against it, streaming into the same SSE connection.

To the client this looks like one continuous turn: no extra `done` event, no separate user message. The thread lock stays held the whole time. The loop is capped at `AgentCore.MAX_TOOL_RELOADS_PER_TURN` rebuilds per user turn to bound token usage. Once the cap is hit, `tool_enable(action="enable")` and Skill Kit activation stop returning `Command(goto=END)` and instead return a plain string whose body includes a `[Reload cap hit]` notice — the agent can still respond in-turn, and the new binding takes effect on the next user message. This prevents an orphaned `tool_result` with no LLM follow-up (symptom: the stream looks like it froze because the last enable's `Command` ended the graph but the reload loop was already exhausted).

`astream()` (REST/SSE and sync-worker bridge callers) and `chat()` (MCP final-string path) honor the auto-continue. The streaming path emits `tool_reload` and then drives the fresh post-reload graph through the same live event conversion, so resumed `thinking`, `tool_call`, `tool_result`, `workspace_artifact`, and `response` chunks remain visible in the same turn. Scheduled TODOs, triggers, callable threads, spawned threads, and the CLI consume `astream()` through `core/stream_bridge.py`, which keeps async-only tools such as `tool_create` available outside regular chat. The bridge uses one process-local asyncio loop for synchronous callers, and async graph caches plus provider SDK HTTP pools are loop-local so FastAPI-loop chat and bridge-loop callable calls do not share loop-bound transports. Autonomous callers use `stream_and_collect()` for shared response/thinking collection, error propagation, and iteration-limit tracking before publishing their own completion payloads. `chat()` remains non-streaming and returns only the final string.

#### TTL and eviction

Each enablement (other than `ttl="permanent"`) gets an `expires_at` timestamp stored in `ThreadConfig.temporary_tools`. At the start of every new turn, `_build_graph_with_prompt` calls `_resolve_temporary_tools(tc)` which:

1. Drops entries whose `expires_at` has passed.
2. Persists the cleaned config back to disk.
3. Returns the still-live set for inclusion in the tool list.

Eviction never happens mid-invocation, so a tool that was bound at the start of a graph run is callable for the whole run — there are no surprise eviction errors. Calling `enable` on a tool already in `temporary_tools` refreshes `expires_at`; calling `enable` with `ttl="permanent"` promotes the entry into `enabled_tools` (which has no expiry and is also what the UI/API writes to). Calling `disable` adds the name to `disabled_tools` without deleting preserved permanent/TTL state, so a later enable restores that state.

Pick the shortest TTL that covers your task. `2h` is a sensible default for multi-step tasks; `30m` for one-shots; `6h`/`24h` for sustained workflows; `permanent` only if the tool should remain as a standing capability on the thread.

---

## Agent Management / Self-Modify Tools

### reload_all

Reload all tools, agents, and trigger sources. Call after SelfModifyAgent creates or modifies code, or after manual file edits.

```python
reload_all()
```

**Returns:** Count of reloaded tools and trigger sources.

**Availability:** Present in `RUNTIME_ADMIN_TOOLS` / optional tooling, not in the always-loaded core `ALL_TOOLS` list.

**Important:** Due to how LangGraph works, newly created tools are not available in the same conversation turn. They work on the next user message.

---

### self_modify_rollback

Rollback a file to its previous version from a SelfModifyAgent backup.

```python
self_modify_rollback(file_path: str)
```

**Parameters:**
- `file_path` (`str`): Path to file to rollback (e.g., `"nymeria/tools/my_tool.py"`)

**Returns:** Success or error message.

**Security:** **SENSITIVE** — disabled by default. Requires explicit opt-in via user tool preferences or per-thread config.

**Availability:** This is not an always-loaded core tool. It is surfaced through self-modify or optional tool paths.

---

## Trigger Tools (Optional)

Event-driven automation — triggers fire agent prompts or actions in response to external events. These complement recurring TODOs, which handle time-based work.

> **Note:** Trigger tools are **not loaded by default** for the main agent. They are available in `OPTIONAL_TOOLS` for per-thread enabling, and are always available to SelfModifyAgent.

### trigger_config

Create, update, enable/disable, or delete event triggers.

```python
trigger_config(
    action: str,
    trigger_id: Optional[str] = None,
    name: Optional[str] = None,
    source_type: Optional[str] = None,
    action_type: Optional[str] = None,
    action_config: Optional[dict] = None,
    source_config: Optional[dict] = None,
    cooldown_seconds: Optional[int] = None,
    conditions: Optional[list] = None,
    enabled: Optional[bool] = None,
)
```

**Actions:**
- `create` — Create a new trigger, bound to the current thread.
- `update` — Patch an existing trigger's display name, enabled state, source/action config, cooldown, or conditions.
- `delete` — Delete a trigger permanently.

**Parameters:**
- `action` (`str`): `"create"`, `"update"`, or `"delete"`
- `trigger_id` (`Optional[str]`): Required for `update` and `delete`
- `name` (`str`): Human-friendly trigger name (e.g., `"Wake-up morning briefing"`)
- `source_type` (`str`): Event source type. Use `"webhook"` for HTTP push triggers. Call `trigger_info(action="sources")` to see available sources.
- `action_type` (`str`): What to do when triggered:
  - `"agent_prompt"` — send a prompt to the agent (most powerful, triggers an LLM call)
  - `"notify"` — send a notification to the user (no LLM call)
  - `"create_todo"` — create a TODO item (no LLM call)
- `action_config` (`dict`): Action-specific configuration:
  - `agent_prompt`: `{"prompt_template": "...", "thread_id": "optional"}`
  - `notify`: `{"message_template": "...", "platform": "auto"}`
  - `create_todo`: `{"task_template": "..."}`
  - Templates support `{variable}` interpolation from event data.
- `source_config` (`Optional[dict]`, default `None`): Source-specific config (e.g., `{"secret": "mykey"}` for webhooks)
- `cooldown_seconds` (`int`, default `0`): Minimum seconds between trigger firings
- `conditions` (`Optional[list]`): Filter conditions; pass `[]` on update to clear conditions
- `enabled` (`Optional[bool]`): Enable or disable a trigger on update

**Returns:** Success or error message. Create returns the trigger ID and webhook URL for webhook sources.

**Examples:**
```python
trigger_config(
    action="create",
    name="Wake-up briefing",
    source_type="webhook",
    action_type="agent_prompt",
    action_config={"prompt_template": "User woke up at {fired_at}. Create morning briefing."},
)

trigger_config(action="update", trigger_id="a1b2c3d4", enabled=False)
trigger_config(action="delete", trigger_id="a1b2c3d4")
```

---

### trigger_info

List triggers, inspect one trigger, dry-run test one trigger, fetch execution history, or list trigger source schemas.

```python
trigger_info(
    action: str = "list",
    trigger_id: Optional[str] = None,
    enabled_only: bool = False,
    current_thread_only: bool = False,
    limit: int = 10,
)
```

**Actions:**
- `list` — List trigger summaries.
- `detail` — Show one trigger's configuration, health, conditions, pending events, and thread binding.
- `test` — Dry-run one trigger with sample event data. Does not fire the trigger.
- `history` — Show recent execution history for one trigger.
- `sources` — Show available trigger source types, config fields, template variables, and examples.

**Parameters:**
- `action` (`str`, default `"list"`): `"list"`, `"detail"`, `"test"`, `"history"`, or `"sources"`
- `trigger_id` (`Optional[str]`): Required for `detail`, `test`, and `history`
- `enabled_only` (`bool`, default `False`): If `True`, only show enabled triggers
- `current_thread_only` (`bool`, default `False`): If `True`, only show triggers bound to the current thread
- `limit` (`int`, default `10`, max `50`): Number of executions for `action="history"`

**Returns:** Formatted trigger summaries, trigger details, test output, execution history, or source catalog.

**Examples:**
```python
trigger_info(action="list", current_thread_only=True)
trigger_info(action="detail", trigger_id="a1b2c3d4")
trigger_info(action="test", trigger_id="a1b2c3d4")
trigger_info(action="history", trigger_id="a1b2c3d4", limit=5)
trigger_info(action="sources")
```

---

## Slash Command Tool (Optional)

Gives the agent a single dispatch tool that invokes the same user-facing slash commands exposed by the Discord and Telegram bots — so the agent can inspect and change its own backend (LLM model, tool set, memories, TODOs, env vars, notepad) without dedicated per-setting tools bloating the tool list.

> **Note:** Not loaded by default. Lives in `OPTIONAL_TOOLS` — enable per-thread via thread config UI or `PATCH /threads/{id}/config {"enabled_tools": ["slash_command"]}`.

### slash_command

Run a Nymeria slash command on the agent's own thread.

```python
slash_command(command: str)
```

**Parameters:**
- `command` (`str`): The slash command string (with or without a leading `/`). Values with spaces may be quoted.

**How to use:** Tell the agent to call `/help` first. The help output is the source of truth for syntax — the tool's own description only lists a handful of examples to keep the tool schema small.

**Example commands:**
- `/help` — list every supported command
- `/status` — model, context, tools, tasks summary
- `/config set llm_model claude-opus-4-6` — change global model
- `/env get PERPLEXITY_API_KEY` — fetch unmasked secret
- `/memory save color "deep blue"` — save a user memory
- `/tools enable browser` — turn on a category on this thread
- `/todos add Check logs | 2h | daily` — scheduled repeating TODO
- `/notepad write replace:new notepad contents` — overwrite the thread notepad

**Blocked commands:** `/ask`, `/stop`, `/clear`, `/compact`, `/restart`, `/start` — these would interrupt or destroy the current conversation and are rejected before any API call.

**Returns:** Plain-text result prefixed with `[Success]`, `[Error]`, or `[Info]`.

**Runtime behavior:** Works in both normal conversation turns and autonomous scheduled TODO runs. Some tool callers still need a synchronous return value, so `slash_command` provides both sync and async invocation modes even though the underlying dispatcher talks to the local API asynchronously.

**Requirements:**
- `NYMERIA_API_URL` — defaults to `http://api:8000` inside Docker or `http://localhost:8000` outside.
- Authenticates with `NYMERIA_SERVICE_TOKEN` (admin service token) plus `X-Nymeria-Act-As: <caller_user_id>` so each invocation runs under the requesting user. The legacy shared `NYMERIA_API_KEY` was retired — see `docs/accounts.md`.

**Security note:** The tool runs in-process against the local API as the calling user (via act-as routing). `/env get` returns unmasked secrets and is admin-only at the API layer — non-admin callers will get 403 if they try to invoke admin-gated slash commands like `/env_get`, `/restart`, or `/config_*`.

**Implementation:** See `nymeria/tools/slash_command.py` (parser + denylist + tool entry point) and `nymeria/triggers/slash_dispatcher.py` (command → API-method routing and plain-text formatting). Mirrors the Telegram bot's command handlers but emits plain text instead of HTML.

---

## HTTP/API and Skill Authoring Tools (Optional)

General-purpose API primitives and authoring tools for one-off integration work, reusable HTTP tools, and generated Skill Kits. These are not loaded by default; normally load `Skill(name="self-improve")`, then enable per-thread with `tool_enable` when needed.

### http_request

Make a single HTTP request and return structured JSON.

```python
http_request(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    query: Optional[dict] = None,
    body: Optional[Any] = None,
    timeout_seconds: int = 30,
    follow_redirects: bool = True,
    response_format: str = "auto",
    max_response_chars: int = 20000,
)
```

**Parameters:**
- `method` (`str`): `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, or `OPTIONS`
- `url` (`str`): Absolute `http://` or `https://` URL
- `headers` (`dict`, optional): Request headers
- `query` (`dict`, optional): Query parameters
- `body` (`Any`, optional): JSON-serializable body; strings are sent as raw content
- `timeout_seconds` (`int`, default `30`): Clamped to 1-300 seconds
- `follow_redirects` (`bool`, default `True`): Follow redirects
- `response_format` (`str`, default `"auto"`): `"auto"`, `"json"`, or `"text"`
- `max_response_chars` (`int`, default `20000`): Body truncation limit, clamped to 1-200000

**Returns:** JSON with `tool_version`, `ok`, `http_ok`, `format_ok`, request method/url, response status/final URL/selected headers/elapsed time, policy metadata, body metadata, and error details. `ok` means the HTTP status was successful and the requested response format was satisfied. `http_ok` only reflects the HTTP status. `format_ok` is false when, for example, `response_format="json"` was requested but the response body was not JSON. Validation/network/policy errors set `http_ok` and `format_ok` to `null` because no HTTP response body was parsed.

**Body shape:** Complete JSON responses return `body_type="json"` and `body` as an object/list. Complete text returns `body_type="text"` and `body` as a string. Truncated responses keep `body` as `null` and put the returned excerpt in `body_preview`, so agents do not confuse truncated JSON text for a complete parsed object. JSON parse attempts include `json_parse_ok`; failed forced-JSON parsing includes `parse_error`. Binary responses set `body_type="binary"`, `body_omitted=true`, and describe the omitted payload in `body_preview`.

**Network policy:** HTTP tools are public-internet-only by default. The runtime blocks loopback, private, link-local, reserved, unspecified, multicast, and metadata targets, including hostnames that resolve to those addresses. Internal/local access requires server-side `HTTP_INTERNAL_ALLOWLIST` entries; the model cannot opt into it per request. Blocked requests return `error.type="blocked_network_target"` and a `policy` object with the reason.

Redirects are followed manually by default so every hop is policy-checked. Redirects to blocked targets are refused before the target is requested. HTTPS-to-HTTP redirects are blocked unless `HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT=true`. Successful redirect-following responses include `redirect_chain`. When `follow_redirects=false`, 3xx responses return `error.type="http_redirect"` instead of the generic `http_status`; if a `Location` header is present, the error includes both `location` and an absolute `redirect_url`.

Response headers are limited to operational headers such as content type, content length, date, server, etag, location, retry-after, and rate-limit headers.

**Example:**

```json
{
  "method": "POST",
  "url": "https://api.example.com/v1/items",
  "headers": {"Authorization": "Bearer ..."},
  "query": {"source": "nymeria"},
  "body": {"name": "Test item"}
}
```

### api_discover

Probe an API base URL for OpenAPI/Swagger metadata and summarize the spec.

```python
api_discover(
    base_url: str,
    docs_url: Optional[str] = None,
    timeout_seconds: int = 20,
    max_response_chars: int = 50000,
)
```

**Discovery order:** optional `docs_url`, then common paths including `/openapi.json`, `/openapi.yaml`, `/swagger.json`, `/swagger.yaml`, `/api-docs`, `/v3/api-docs`, `/docs/openapi.json`, and `/.well-known/openapi.json`. HTML docs are scanned for direct OpenAPI/Swagger/API-doc links and Swagger UI config scripts such as `swagger-initializer.js`; those scripts are inspected for spec URLs before falling back to generic hints.

**Returns:** JSON with `tool_version`, `found`, `spec_url`, `spec_format`, a summary containing title/version/servers/path count/sample methods/security scheme names, every tried URL, and hints when no spec is found. Discovery requests use the same HTTP egress policy as `http_request`.

**Relationship to custom HTTP tools:** `http_request` is the ad hoc primitive for one-off API calls. Custom HTTP tools remain the reusable connector mechanism: they store parameterized URL/header/body templates in `data/custom_tools/` and expose a named tool after setup. Custom HTTP tools use the same egress policy and audit redaction as `http_request`.

### tool_create

Draft, test, and publish reusable HTTP tools from inside an agent conversation.

```python
tool_create(
    action: str,
    tool_id: str = "",
    name: str = "",
    description: str = "",
    parameters: Optional[dict] = None,
    http_config: Optional[dict] = None,
    draft_id: str = "",
    sample_params: Optional[dict] = None,
    ttl: str = "2h",
)
```

**Actions:**
- `draft` — Save or update a per-user draft in `data/tool_drafts/{user_id}/`. Requires `tool_id`, `description`, `parameters`, and `http_config`.
- `test` — Execute a saved draft with `sample_params` and record whether the request succeeded.
- `publish` — Save a successfully tested draft into the global `data/custom_tools/` registry, reload custom tools, and enable the new tool on the current thread.
- `list` — Show this user's drafts plus globally published custom tools without exposing request headers or bodies.
- `delete` — Delete this user's draft only. It does not delete a globally published tool.

**Publish semantics:** Published tools are global registry entries, so any user can discover and enable them later. They are not added to `default_thread_tools` and are not enabled by default for other users or threads. The publishing thread gets the new tool enabled with a TTL (`30m`, `2h`, `6h`, `24h`, or `permanent`; default `2h`) using the same in-turn auto-reload path as `tool_enable(action="enable")`, but reload metadata uses `source="tool_create"` and `reason="tool_published"`.

**V1 limits:** Only `implementation_type="http"` is supported. Agent-created tools reject inline secrets and `${env:...}` references. Sensitive headers are allowed only when their value uses a credential-vault reference like `${credential:cred_id.value}`.

**Credential vault auth:** Authenticated custom HTTP tools should use `${credential:<credential_id>.<field>}` references in headers, query params, URLs, or bodies instead of raw values. Runtime execution resolves the reference server-side, checks the credential's allowed target, and audits the use without returning secret material to the agent.

**Audit and deferred production safety:** HTTP tool calls append redacted HTTP events to the audit log when `AUDIT_LOG_ENABLED=true`. Raw bearer/API-key-like values are best-effort redacted and custom HTTP `${env:VAR}` usage records the variable names, not the values. Credential-vault usage records credential IDs, not plaintext values. Future hardening still needs stronger per-user rate-limit budgets per task, pagination helpers, and policy hooks for actions that send messages, delete data, spend money, modify production systems, post publicly, or change infrastructure.

### auth_manager

Agent-safe credential management facade. Optional tool, disabled by default.

```python
auth_manager(
    action: str,
    credential_id: str = "",
    provider: str = "",
    kind: str = "api_key",
    name: str = "",
    target_type: str = "",
    target_id: str = "",
    binding_name: str = "",
    binding_id: str = "",
    metadata: Optional[dict] = None,
    required_fields: Optional[list[str]] = None,
)
```

Actions: `list`, `status`, `request_setup`, `bind`, `unbind`, `test`, `disable`.
The tool returns credential metadata only. It can manage user-owned credentials
but cannot alter system credentials. It never returns plaintext secrets,
ciphertext, or partial key material. Native built-in integrations use target
type `native_tool`; for example, request setup for NASA with
`target_type="native_tool"`, `target_id="nasa_apod"`, and
`required_fields=["api_key"]`.

### skill_config

Draft, validate, publish, list, and delete Nymeria Skills and Skill Kits from inside an agent conversation.

```python
skill_config(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    allowed_tools: Optional[list[str] | str] = None,
    required_tools: Optional[list[str] | str] = None,
    tool_ttl: str = "2h",
    draft_id: str = "",
    scope: str = "user",
    overwrite: bool = False,
    activate_current_thread: bool = True,
)
```

**Actions:**
- `draft` — Validate and save a per-user draft in `data/skill_drafts/{user_id}/`.
- `validate` — Validate inline fields or a saved draft without publishing.
- `publish` — Write a validated `SKILL.md` to `data/skills/users/{user_id}/` or admin-only `data/skills/global/`, reload skills, and by default enable it on the current thread.
- `list` — Show this user's drafts plus installed skills visible to the user.
- `delete` — With `scope="draft"`, delete a draft. With `scope="user"` or `scope="global"`, uninstall that skill scope; global delete requires admin.

**V1 limits:** `skill_config` writes only `SKILL.md`. It cannot create scripts, assets, references, or arbitrary paths. It rejects body text that includes YAML frontmatter; agents pass `name`, `description`, `allowed_tools`, `required_tools`, and `tool_ttl` as structured parameters.

**Skill Kit dependency checks:** `required_tools` are validated before any publish write. Unknown, unloadable, or admin-blocked tools fail strictly. Publishing a user skill that would shadow an existing bundled/global skill is rejected; replacing an existing generated skill requires `overwrite=true` and the existing directory must contain only `SKILL.md`.

**Same-turn activation:** When `activate_current_thread=true`, publish adds the skill name to `ThreadConfig.enabled_skills`, reloads the skill manager, invalidates graph caches, and queues a same-turn `tool_reload` with `source="skill_config"` and `reason="skill_published"`. The event may have an empty `tools` list because the reload refreshes the `Skill` meta-tool index rather than binding a new normal tool.

### skill_kit_create

Preferred facade for durable capability authoring from `self-improve`.

```python
skill_kit_create(
    action: str,
    name: str = "",
    description: str = "",
    body: str = "",
    required_tools: Optional[list[str] | str] = None,
    draft_id: str = "",
    tool_id: str = "",
    parameters: Optional[dict] = None,
    http_config: Optional[dict] = None,
    sample_params: Optional[dict] = None,
)
```

Actions:
- `draft`, `validate`, `publish`, `package`, `list` — Skill Kit lifecycle.
- `draft_http_tool`, `test_http_tool`, `publish_http_tool` — guided HTTP tool
  creation before packaging a Skill Kit around it.

`publish`/`package` use the Skill publish reload path with
`source="skill_kit_create"` and `reason="skill_kit_created"`. Publishing an
HTTP tool through this facade enables the new tool on the current thread with
`source="skill_kit_create"` and `reason="http_tool_published_for_skill_kit"`.

---

## Watchdog Tools (Optional)

Tools for the Smart Watchdog — an intelligent scheduler thread that observes system activity and dispatches work to other threads. Not loaded by default; enable per-thread via thread config.

### activity_feed

Get a structured activity summary across all threads since a given time window.

```python
activity_feed(minutes_ago: int = 10)
```

**Parameters:**
- `minutes_ago` (`int`): Look-back window in minutes (default 10)

**Returns:** Structured text report grouped by thread showing user messages, autonomous tasks, TODO changes, and notifications. Returns "No activity" if the window is empty.

**Data source:** Reads from the persisted activity log (`data/activity/{user_id}.json`). Only as complete as what gets logged — user messages, TODO state changes, autonomous task execution, and notifications are all captured.

### watchdog_dispatch

Create a TODO on a different thread. Cannot target the calling thread.

```python
watchdog_dispatch(target_thread_id: str, task: str, scheduled_for: str = "now", notes: str = "")
```

**Parameters:**
- `target_thread_id` (`str`): Thread ID to dispatch the TODO to (must differ from caller)
- `task` (`str`): Clear, specific description of what the target thread should do
- `scheduled_for` (`str`): When to fire — `"now"`, `"30s"`, `"5m"`, `"1h"`, `"1d"`, or `"YYYY-MM-DD HH:MM"`
- `notes` (`str`): Supporting context for the target thread

**Returns:** Confirmation with the created TODO ID, or error if self-targeting or limit reached.

**Implementation:** `nymeria/tools/watchdog_dispatch.py`. Wraps `TodoManager.add()` with a cross-thread guard. Logs activity as `WATCHDOG_NUDGE`.

### watchdog_read_notepad

Read another thread's notepad to understand what it's currently focused on.

```python
watchdog_read_notepad(target_thread_id: str)
```

**Parameters:**
- `target_thread_id` (`str`): Thread ID whose notepad to read

**Returns:** Notepad content, or message indicating the notepad is empty.

### watchdog_todo_overview

List all active TODOs across all threads, grouped by thread. Shows thread assignment, schedule, and recurrence for each TODO.

```python
watchdog_todo_overview()
```

**Returns:** All active TODOs grouped by thread with status icons, schedule times, and recurrence info. Use this before dispatching to avoid creating duplicate TODOs.

**Implementation:** All watchdog tools live in `nymeria/tools/watchdog_dispatch.py`.

---

## Thread Spawning (Optional)

### spawn_thread

Create or delete a conversation thread with scoped configuration. Two modes via the `action` parameter: `"create"` (default) and `"delete"`. Spawned threads appear in the desktop sidebar inside a **"Spawned by Nymeria"** folder so they stay separate from user-created threads.

```python
spawn_thread(
    title: Optional[str] = None,
    instructions: Optional[str] = None,
    optional_tools: Optional[List[str]] = None,
    tool_categories: Optional[List[str]] = None,
    disabled_tools: Optional[List[str]] = None,
    make_callable: bool = True,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_temperature: Optional[float] = None,
    llm_max_tokens: Optional[int] = None,
    llm_extended_thinking: Optional[bool] = None,
    llm_reasoning_effort: Optional[str] = None,
    initial_message: Optional[str] = None,
    action: str = "create",
    delete_thread_id: Optional[str] = None,
)
```

**Create mode (`action="create"`, default):**

- `title` (required for create): User-visible thread title. Truncated to 80 chars.
- `instructions`: Extra system-prompt instructions **APPENDED** to `soul.md` (max 5000 chars). Cannot replace the base personality. Also used as the callable tool's description if provided.
- `optional_tools`: List of optional tool names to enable (e.g. `["sticky_note", "browser_navigate"]`). Core tools are inherited automatically — only list extras.
- `tool_categories`: List of categories (e.g. `["email", "browser"]`) to bulk-enable every optional tool in that category. Merged with `optional_tools`.
- `disabled_tools`: List of core tool names to EXCLUDE from the new thread.
- `make_callable` (default `True`): If `True`, the new thread is registered as a callable tool with an auto-derived name (`spawned_{slug}_{rand8}`) and ownership is **claimed for the spawning user** in `thread_owners`. Threads owned by that same user (including the parent) can invoke it; threads owned by any other user cannot — the runtime gate in `agents/tool_factory.py` rejects cross-user invocations. Set `False` for a single-use thread.
- `llm_*`: Optional LLM overrides. Omit to inherit global settings.
- `initial_message`: If provided, dispatches this message and **blocks** until the child responds. The child's response becomes part of this tool's output.

**Create returns:**
- Preamble with the new `thread_id` (`spawned-{slug}-{rand8}`).
- If `make_callable=True`: the generated callable tool name (e.g. `spawned_research_a3f21c9d`) the parent can invoke later.
- If `initial_message` provided: the child's response text appended.
- A reminder of the `action="delete"` call needed to remove the thread.

**Delete mode (`action="delete"`):**

- `delete_thread_id` (required for delete): The spawned thread's ID (must start with `"spawned-"`).
- Only the **calling thread** (the original spawn parent, tracked via `platform_meta.spawn_parent`) can delete a given spawned thread. If the stored spawn_parent is empty (e.g. an older spawn without lineage), any thread may delete it.
- Cleans up: metadata, config, checkpoints (SQLite or Postgres), notepad, and — if the thread was callable — unregisters the tool globally via `sync_agent_tools()`.
- Publishes a `thread_deleted` sync event so all connected clients remove it from their sidebars.

**Delete returns:** `[Deleted]: thread_id=spawned-...` on success.

**Safety limits (create only):**
- **Spawn depth**: capped at 3 by default (`NYMERIA_MAX_SPAWN_DEPTH` env). Depth stored in `platform_meta.spawn_depth`.
- **Rate limit**: 10 spawns per parent per hour (`NYMERIA_MAX_SPAWNS_PER_HOUR` env). Process-local (resets on API restart); this is intentional for single-process deployments since restart breaks any active spawn loop and the depth limit is the hard guard against recursion.
- **Abort cascade**: parent→child invocation is registered so stopping the parent stops its children.

**Frontend behavior:** When the `thread_created` sync event arrives with a `spawned-` thread_id, the desktop client lazily creates a "Spawned by Nymeria" folder and files the thread there. `thread_deleted` events trigger removal from the sidebar (and the folder).

**System prompt rule:** The tool only exposes the *append* path (`instructions`). It cannot set `system_prompt` (which would replace `soul.md` entirely).

**Implementation:** `nymeria/tools/spawn_thread.py`. Mirrors `thread_agent_executor.invoke()` for live event streaming (child activity appears in the autonomous pane). Delete path mirrors `DELETE /threads/{id}` from `triggers/api.py`.

**Non-blocking (deferred):** Fire-and-forget spawning is not currently supported. Would need cross-thread TODO creation (extending `nym_todo` to target other threads) or a thread mailbox channel. Today's blocking-only mode covers the common "delegate a task, get the answer" use case.

---

## Callable Thread Tools (Dynamic)

Any thread with `callable=True` in its thread config becomes a callable tool — there are no hardcoded agent names or fixed configurations. Each callable thread is fully configurable via the UI:

- **Name**: The tool name equals the thread's sidebar title (synced via `callable_name` in thread config)
- **Model**: Set per-thread via `llm_config.model` (inherits global default if not set)
- **Tools**: Enable/disable any optional tools per-thread
- **System prompt**: Custom `system_prompt` or `instructions` per-thread

Create a callable thread: open thread settings → Agent → check "Make Callable" → set a name and description. On desktop, the thread row's Agent shortcut opens this tab directly. The callable becomes available as a tool to **threads owned by the same user** after `sync_agent_tools()` runs. Other users do not see the callable in their tool list, and the runtime ownership gate rejects any invocation attempt by a non-owner. Admins can act-as the owning user via `X-Nymeria-Act-As` to test or trigger another user's callable.

The tool signature for any callable thread is:

```python
ThreadName(
    task: str,
    mode: Literal["ask", "handoff"] = "ask",
    scheduled_for: Optional[str] = None,
    if_busy: Literal["queue", "error"] = "queue",
) -> str
```

The `task` parameter is the instruction. `mode="ask"` preserves the legacy behavior: the caller waits and receives the target thread's final response. `mode="handoff"` starts an autonomous run in the target thread and returns `[HandedOff]` metadata (`handoff_id`, target thread, and optional TODO id) without returning the target's final output.

For handoffs, `scheduled_for` can delay execution with values such as `"30s"`, `"5m"`, `"1h"`, `"1d"`, or `"YYYY-MM-DD HH:MM"`; delayed handoffs are stored as scheduled TODOs on the target thread. Omit `scheduled_for` for immediate handoff. For immediate handoffs, `if_busy="queue"` lets the target thread wait for its lock in the background, while `if_busy="error"` returns a busy response if the target is already running. In blocking ask mode, `if_busy="error"` performs a best-effort busy check before waiting.

Handoff prompts include source-thread metadata so the target thread can call the original callable thread later if useful. There is no automatic completion callback. If the target thread is bound to Telegram and `telegram_autonomous_delivery="full"`, its handoff output is delivered through Telegram like other autonomous output.

An injected `config` parameter provides user/thread context. Callable threads are created dynamically by `agents/tool_factory.py` via `create_callable_thread_tool()`, which wraps `thread_agent_executor` in a LangChain `BaseTool`. The factory handles circular call detection for blocking asks; non-blocking handoffs intentionally skip the wait graph so a child can hand work back to its caller. The factory also performs a runtime team-visibility check so stale cached graphs cannot invoke callables outside the caller's team.

### Browser Tools (9)

Used internally by BrowserAgent. Defined in `tools/browser.py`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `browser_navigate` | `(url: str)` | Navigate to an absolute `http://` or `https://` URL. Falls back to requests+BeautifulSoup if Playwright unavailable. |
| `browser_click` | `(selector: str)` | Click element by CSS selector or `text=` selector. |
| `browser_type` | `(selector: str, text: str)` | Type text into an input field. |
| `browser_get_content` | `(include_links: bool = True)` | Get page text content and optionally links. |
| `browser_screenshot` | `()` | Take a screenshot. Returns a data URI preview string (first 100 chars of base64 + total length). |
| `browser_scroll` | `(direction: str = "down", amount: int = 500)` | Scroll page up or down by pixel amount. |
| `browser_press_key` | `(key: str)` | Press a keyboard key (e.g., `"Enter"`, `"Tab"`). |
| `browser_close` | `()` | Close the browser and reset the thread. |
| `browser_status` | `()` | Check Playwright availability and browser state. |

**Architecture:** All browser operations run on a dedicated `BrowserThread` to satisfy Playwright's single-thread requirement. Operations are queued and results retrieved via thread-safe queues. The browser persists between calls until explicitly closed.

**Fallback mode:** Set `BROWSER_FORCE_FALLBACK=true` in `.env` to skip Playwright entirely and use requests+BeautifulSoup for navigation and content extraction. Fallback HTTP requests verify TLS certificates by default; set `BROWSER_VERIFY_SSL=false` only in trusted environments with known TLS interception.

---

### Outlook Tools (18)

Used internally by OutlookAgent. Also available as **optional tools** for per-thread enabling. Defined in `tools/outlook_auth.py` (4 auth), `tools/outlook_email.py` (13 email), and `tools/outlook_attachments.py` (1 attachment).
Microsoft tokens are stored with the shared cache I/O helpers in
`tools/auth_cache_utils.py` at `data/auth_tokens/<user_id>/microsoft.json`.

**Authentication tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_auth_start` | `()` | Start Microsoft OAuth device code flow. Returns URL and code. |
| `outlook_auth_complete` | `()` | Complete auth after user signs in. Polls Microsoft (up to 5 min). |
| `outlook_auth_clear` | `(account_id?)` | Clear one saved Microsoft account by ID, or all Microsoft accounts plus any pending device-code flow when omitted. |
| `outlook_list_authenticated_accounts` | `()` | List all authenticated Microsoft accounts with IDs. |

**Email tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `outlook_list_emails` | `(account_id?, limit=10, folder="inbox", unread_only=False)` | List recent emails with preview and thread ID. Limit max 50. |
| `outlook_get_email` | `(email_id?, email_ids?, account_id?)` | Get full email details including body and attachment metadata. Batch via comma-separated IDs. |
| `outlook_search_emails` | `(query?, queries?, sender?, to?, subject?, folder?, category?, days_back=0, has_attachments=False, thread_id?, kql?, account_id?, limit=10)` | Search emails with filters. Results include body preview (120 chars) and thread ID. Without `days_back`, results ranked by relevance not date. Use `thread_id` to pull full conversation chain. Use `kql` for raw KQL queries (OR logic, etc). Use `category` to find tagged emails. |
| `outlook_send_email` | `(to, subject, body, account_id?, cc?, bcc?, is_html=False)` | Send a new email. |
| `outlook_reply_email` | `(email_id, body, account_id?, reply_all=False)` | Reply to an email (sends immediately). |
| `outlook_draft_reply` | `(email_id, body, reply_all=False, is_html=False, account_id?)` | Create an unsent reply draft that preserves the email thread. Staff reviews and sends manually. |
| `outlook_create_draft` | `(to, subject, body, account_id?, cc?, bcc?, is_html=False)` | Create a standalone draft without sending. |
| `outlook_edit_draft` | `(draft_id, body?, subject?, to?, cc?, bcc?, is_html=False, account_id?)` | Edit an existing draft. Only provided fields are updated. Works on drafts from create_draft or draft_reply. |
| `outlook_delete_email` | `(email_id, account_id?, permanent=False)` | Move to trash or permanently delete. |
| `outlook_mark_email` | `(email_id, is_read, account_id?)` | Mark email as read or unread. |
| `outlook_move_email` | `(email_id, folder, account_id?)` | Move email to a folder. |
| `outlook_forward_email` | `(email_id, to, comment?, account_id?)` | Forward an email. |
| `outlook_set_category` | `(email_id, category, action="add", account_id?)` | Add or remove a category tag on an email. Use to tag emails for processing ("Nymeria") and clear after done. |
| `outlook_get_attachments` | `(email_id, skip?, account_id?)` | Download and extract text from all email attachments. CSV/TXT decoded directly, Excel via openpyxl, PDF/DOCX/images via Gemini AI. Optional `skip` to ignore irrelevant attachments by name. |

**Folder names** (case-insensitive):
- `outlook_list_emails` accepts: `inbox`, `sent`/`sentitems`, `drafts`, `deleted`/`deleteditems`, `junk`/`junkemail`, `archive`
- `outlook_search_emails` accepts same folders, or omit for all mail
- `outlook_move_email` additionally accepts: `trash` (→ deleteditems), `spam` (→ junkemail)

---

### Gmail Auth Tools (4)

Optional Google Gmail OAuth tools for MCP auth bridging. They use the same
Google OAuth factory as Calendar and Docs, but request Gmail scopes and store
tokens at `data/auth_tokens/<user_id>/google_gmail.json`.

After `gmail_auth_complete` succeeds, Nymeria exports a google-auth-library
compatible token file to `data/auth_tokens/<user_id>/mcp/gmail/credentials.json`.
Managed MCP install/retry/create automatically applies this file as
`GMAIL_CREDENTIALS_PATH` for `@gongrzhe/server-gmail-autoauth-mcp` and applies
`GOOGLE_OAUTH_CREDENTIALS` as `GMAIL_OAUTH_PATH`. Existing Calendar or Docs
Google tokens are exported only if they already include the Gmail scopes; most
older tokens will require `gmail_auth_start` because Google scopes are fixed at
consent time.

| Tool | Signature | Description |
|------|-----------|-------------|
| `gmail_auth_start` | `()` | Start Google Gmail OAuth flow. Returns authorization URL for the user. |
| `gmail_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in and export MCP credentials. |
| `gmail_auth_clear` | `(account_id?)` | Clear one saved Gmail account by ID, or all Gmail accounts plus any pending Gmail OAuth flow when omitted. |
| `gmail_list_accounts` | `()` | List authenticated Google accounts for Gmail with verified token/scopes status. Invalid refresh tokens are pruned. |

---

### Calendar Tools (15)

Native Python Google Calendar API client. Defined in `tools/calendar_auth.py` (4 auth) and `tools/calendar.py` (11 event). Uses `google-api-python-client` for direct API calls with agent-guided OAuth flow.
Calendar auth tools are generated from the shared Google OAuth factory in
`tools/auth_cache_utils.py`; Calendar API calls use the same module's
credential refresh and request wrapper.

**Authentication tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_auth_start` | `()` | Start Google OAuth flow. Returns authorization URL for the user. |
| `calendar_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in. Accepts optional redirect URL for manual fallback. |
| `calendar_auth_clear` | `(account_id?)` | Clear one saved Google Calendar account by ID, or all Calendar accounts plus any pending Calendar OAuth flow when omitted. |
| `calendar_list_authenticated_accounts` | `()` | List authenticated Google accounts with verified token/scopes status. Invalid refresh tokens are pruned. |

**Event tools:**

| Tool | Signature | Description |
|------|-----------|-------------|
| `calendar_list_calendars` | `(account_id?)` | List all available Google calendars. |
| `calendar_list_events` | `(calendar_id="primary", max_results=10, time_min?, time_max?, account_id?)` | List events from a calendar. Output includes full event IDs for chaining into `calendar_get_event`. |
| `calendar_get_event` | `(event_id, calendar_id="primary", account_id?)` | Get full event details. |
| `calendar_search_events` | `(query, calendar_id="primary", max_results=10, account_id?)` | Search events by text. Output includes full event IDs for chaining into `calendar_get_event`. |
| `calendar_create_event` | `(summary, start_time, end_time, calendar_id="primary", description?, location?, attendees?, timezone?, account_id?)` | Create a new event. Supports all-day (date-only) and timed events. |
| `calendar_update_event` | `(event_id, calendar_id="primary", summary?, start_time?, end_time?, description?, location?, account_id?)` | Update an existing event (patch — only sends changed fields). |
| `calendar_delete_event` | `(event_id, calendar_id="primary", account_id?)` | Delete a calendar event. |
| `calendar_respond_to_event` | `(event_id, response, calendar_id="primary", account_id?)` | Respond to invitation: `"accepted"`, `"declined"`, `"tentative"`. |
| `calendar_get_freebusy` | `(time_min, time_max, calendars?, account_id?)` | Get free/busy info. `calendars` is comma-separated IDs. |
| `calendar_get_current_time` | `()` | Get current time in ISO 8601 (no API call — local system time). |
| `calendar_list_colors` | `(account_id?)` | List available event colors. |

**Requires:** `GOOGLE_OAUTH_CREDENTIALS` env var pointing to the OAuth Desktop App credentials JSON from Google Cloud Console. Tokens are stored per Nymeria user at `data/auth_tokens/<user_id>/google_calendar.json` with auto-refresh. Node.js/npx are **not** required.

---

### Google Docs/Drive/Sheets Auth Tools (4)

The Google Docs, Drive, and Sheets tools share one OAuth cache at `data/auth_tokens/<user_id>/google_docs.json`. Account-list output verifies scopes and refreshability before presenting an account as usable.
The auth tools are generated by the same `tools/auth_cache_utils.py` Google
OAuth factory used by Calendar. Docs and Drive API calls share its credential
refresh and request wrapper, and generic Sheets uses the Docs/Drive/Sheets
credential cache.

| Tool | Signature | Description |
|------|-----------|-------------|
| `google_docs_auth_start` | `()` | Start Google Docs/Drive/Sheets OAuth flow. Returns authorization URL for the user. |
| `google_docs_auth_complete` | `(redirect_url?)` | Complete auth after browser sign-in. Accepts optional redirect URL for manual fallback. |
| `google_docs_auth_clear` | `(account_id?)` | Clear one saved Google Docs account by ID, or all Docs/Drive/Sheets accounts plus any pending OAuth flow when omitted. |
| `google_docs_list_accounts` | `()` | List authenticated Google accounts for Docs/Drive/Sheets with verified token/scopes status. Invalid refresh tokens are pruned. |

---

### SelfModify Tools (8)

Used internally by SelfModifyAgent. Defined in `core/self_agent.py`. **Read** and **list** operations work on any path within the project root. **Write** and **delete** are restricted to `nymeria/tools/`, `nymeria/agents/`, and `nymeria/triggers/sources/`.

| Tool | Signature | Description |
|------|-----------|-------------|
| `self_modify_instructions` | `()` | Return the agent's system prompt (instructions for self-modification) |
| `self_file_read` | `(file_path: str)` | Read a file from the Nymeria codebase. |
| `self_file_write` | `(file_path: str, content: str)` | Write content to tools/, agents/, or triggers/sources/. Auto-backups. |
| `self_file_list` | `(directory: str = "nymeria/tools")` | List files in a directory. |
| `self_file_delete` | `(file_path: str)` | Delete a file from tools/, agents/, or triggers/sources/. |
| `self_test_import` | `()` | Test that all tools can be imported successfully. |
| `self_reload` | `()` | Reload all tools and agents after making changes. |
| `self_invoke_tool` | `(tool_name: str, arguments_json: str)` | Test a tool by invoking it with JSON arguments. |

**Workflow:** Write code → `self_test_import()` → `self_reload()` → `self_invoke_tool()` → report results.

---

## Optional Tools System

Optional tools are NOT loaded by default. They're available for per-thread enabling via the thread config UI.

**Currently available (representative categories):**
- Outlook tools: 4 auth + 13 email + 1 attachment = 18 total
- Trigger tools: 2
- Browser tools: 9
- Calendar tools: 4 auth + 11 event = 15 total
- Self-modify tools: 8
- Subagent tools (reload/rollback): 2
- Google Docs tools: 4 auth + 17 document = 21 total
- Google Sheets / _PRV_A tools: 3 base + 5 _PRV_A = 8 total
- Twitch tools: 22
- Watchdog tools: `activity_feed`, `watchdog_dispatch`, `watchdog_read_notepad`, `watchdog_todo_overview` = 4
- Utility tools: `claude_code`, `sticky_note`, `tool_search`, `tool_enable`, `manage_mcp`, `skill_manage`, `http_request`, `api_discover`, `tool_create`, `skill_config`, `skill_kit_create` plus the admin-only diagnostic `hello_test` used for dynamic-load validation

**How it works:**
1. `OPTIONAL_TOOLS` in `tools/__init__.py` maps tool names to tool objects
2. Per-thread config has an `enabled_tools` list (tool names)
3. The profile-level `default_thread_tools` list is the default-bound core set for each thread; an empty list means no core tools
4. During `_build_graph_with_prompt()`, enabled optional tools are added to the thread's tool set
5. Users enable or disable optional tools via thread settings or `PATCH /threads/{id}/config`

The desktop/mobile Thread Settings UI mirrors this split: the Tools tab shows non-MCP tools from `default_thread_tools` plus non-MCP optional tools, while the MCP tab shows MCP-discovered tools. Default MCP tools can be disabled per thread; non-default MCP tools can be enabled per thread. Tool discovery is role-filtered; `hello_test` remains in `OPTIONAL_TOOLS` for admin/test validation but is hidden from non-admin search/listing surfaces and rejected by non-admin enable paths.

**Important:** `OPTIONAL_TOOLS` currently includes more than just integrations. It also contains tools like `claude_code`, `sticky_note`, `reload_all`, and `self_modify_rollback`.

---

## Custom Tools

Custom tools extend Nymeria's capabilities without writing Python. Created via the **Desktop UI** (Settings → Tools), the **REST API**, or the agent-facing `tool_create` workflow for public unauthenticated HTTP tools.

### Tool Types

| Type | Description | Use Case |
|------|-------------|----------|
| **HTTP** | Makes REST API calls to external services | Integrate with APIs, webhooks, web services |
| **MCP** | Connects to Model Context Protocol servers | Use existing MCP tools, complex integrations |

### HTTP Tools

HTTP tools make REST API calls with configurable:
- **Method**: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
- **URL**: Supports `${param}` interpolation for dynamic URLs
- **Headers**: Including `${env:VAR_NAME}` for secrets from environment
- **Body Template**: JSON template with parameter placeholders
- **Response Path**: JSONPath to extract specific data from response

**Example: Weather API Tool**
```json
{
  "id": "get_weather",
  "name": "Get Weather",
  "description": "Get current weather for a city",
  "implementation_type": "http",
  "parameters": {
    "city": {
      "type": "string",
      "description": "City name",
      "required": true
    }
  },
  "http_config": {
    "method": "GET",
    "url": "https://api.weather.com/v1/current?city=${city}",
    "headers": {
      "Authorization": "Bearer ${env:WEATHER_API_KEY}"
    },
    "response_path": "$.data.temperature"
  }
}
```

### MCP Tools

MCP tools connect to external MCP servers via JSON-RPC over stdio or HTTP.

The MCP paste installer accepts Claude Desktop JSON, bare stdio commands, HTTP/SSE URLs, npm package pages, PyPI package pages, Git repository URLs, registry ids, bundle URLs, and uploaded `.mcpb`/`.dxt`/`.zip` bundles. The desktop installer previews the plan before running anything, asks for confirmation before Git/local-path/bundle installs, and saves failed installs as disabled drafts with logs so they can be retried. Sensitive pasted config values are encrypted with `NYMERIA_SECRETS_KEY`.

Managed MCP servers and their discovered tools are configured from the dedicated **Settings → MCP** tab on desktop and mobile. Legacy user-created MCP custom tools remain under **Settings → Tools** with the other custom tools.

Known MCP auth presets are applied during install, retry, create, and update.
For `@gongrzhe/server-gmail-autoauth-mcp`, Nymeria wires
`GMAIL_OAUTH_PATH` from `GOOGLE_OAUTH_CREDENTIALS` and
`GMAIL_CREDENTIALS_PATH` from the current user's Gmail MCP credential export.

For stdio servers, Nymeria launches the command from the backend process. In Docker, that means paths and Python/Node dependencies must exist inside the `nymeria-api` container, and host services should normally be referenced as `host.docker.internal:<port>` rather than `localhost:<port>`. Startup and discovery failures include the server process's recent stderr when available.

**Configuration:**
- **Server Command**: Command to start the MCP server (e.g., `npx`, `python`)
- **Server Args**: Command-line arguments
- **Tool Name**: The specific tool exposed by the MCP server
- **Environment Variables**: Variables to pass to the server
- **Idle Timeout**: Server shutdown after inactivity (default: 5 minutes)

**Example: Filesystem MCP Tool**
```json
{
  "id": "read_file_mcp",
  "name": "Read File (MCP)",
  "description": "Read a file using the MCP filesystem server",
  "implementation_type": "mcp",
  "parameters": {
    "path": {
      "type": "string",
      "description": "File path to read",
      "required": true
    }
  },
  "mcp_config": {
    "server_command": "npx",
    "server_args": ["-y", "@anthropic/mcp-server-filesystem", "/allowed/path"],
    "tool_name": "read_file",
    "idle_timeout_seconds": 300
  }
}
```

**MCP Server Lifecycle:**
- Servers start on-demand when the tool is first called
- Servers stay alive for the configured idle timeout
- Multiple tools can share the same MCP server through the process-wide `MCPServerManager`
- Managed MCP server tools and legacy custom MCP tools use the same shared manager and connection pool
- Servers are gracefully shutdown when Nymeria stops

### Managing Custom Tools

**Via Desktop UI:**
1. Open Settings → Tools tab
2. Click "+ New Tool" to create
3. Fill in the form (HTTP or MCP configuration)
4. Test the tool with sample parameters
5. Enable/disable tools as needed

**Via REST API:**
```bash
GET /tools/custom              # List all custom tools
POST /tools/custom             # Create a new tool
GET /tools/custom/export       # Export custom tools
POST /tools/custom/import      # Import custom tools
PUT /tools/custom/{tool_id}    # Update a tool
DELETE /tools/custom/{tool_id} # Delete a tool
POST /tools/custom/{tool_id}/test  # Test a tool
```

**Storage:** Custom tools are stored as JSON files in `data/custom_tools/`, one `.json` per tool ID.

**Agent-created tools:** `tool_create(action="publish")` writes the same JSON definition format into `data/custom_tools/`, then reloads the custom-tool loader and registers metadata so `tool_search(query=...)` can find the new tool. Agent-created tools are global but remain opt-in per thread.

### HexStrike MCP Sidecar

HexStrike AI is available as an optional Kali-based sidecar through
`docker-compose.hexstrike.yml`. It exposes a curated subset of upstream
HexStrike tools over Streamable HTTP at `http://hexstrike-mcp:8889/mcp` for
Nymeria containers and `http://localhost:8889/mcp` for local MCP clients. See
`docs/hexstrike-mcp.md` for build, registration, and allowlist details.

---

## Scheduled TODO Execution

Nymeria operates autonomously 24/7 through **scheduled TODOs** — TODOs with a `scheduled_for` datetime that are automatically executed when due.

### How It Works

1. `nym_todo(task=..., scheduled_for=...)` creates a TODO and registers it in `TodoScheduleDB` (SQLite at `data/todo_schedule.db`).
2. The **Ticker** daemon polls every 5 seconds for due TODOs.
3. When a TODO is due, the ticker sends its `task` text as a prompt to the agent on the TODO's `thread_id`.
4. For recurring TODOs, **any completion** (ticker execution, agent marking done, API, or MCP) auto-reschedules to the next `scheduled_for` based on the recurrence pattern. Use `clear_recurrence` or `nym_todo_delete` to stop.
5. Completed TODOs are removed from the active TODO JSON list after `TODO_AUTO_ARCHIVE_DAYS` (default 7) by hourly cleanup in the ticker. They are not moved to a separate archive file.

**Durable scheduling:** Scheduled TODOs survive application restarts. Missed TODOs are recovered and executed on startup.

**Concurrency:** Scheduled TODOs and trigger actions run through the ticker's autonomous worker pool (`MAX_CONCURRENT_AUTONOMOUS`, default 5). Poll-based trigger source checks and hourly TODO archival run in a separate housekeeping pool so slow source I/O does not consume autonomous workers.

---

## Security Levels & Metadata

Tool metadata is generated in `tools/metadata.py` from the registered LangChain
tool objects. Descriptions come from the tool docstrings; `metadata.py` owns the
policy fields that cannot be inferred from a docstring: category, security
level, default-enabled state, and config schemas.

### Security Levels

| Level | Description |
|-------|-------------|
| **SAFE** | Read-only or low-risk behavior |
| **MODERATE** | Mutates external/local state or performs broader actions |
| **SENSITIVE** | Code/runtime mutation or similarly high-risk behavior |

Default availability is separate from security level: tools in `ALL_TOOLS` are
enabled for new threads by default, and tools in `OPTIONAL_TOOLS` are opt-in by
default even when they are classified `SAFE`.

### Tools by Security Level

Representative examples:

**SAFE:** `file_read`, `web_search`, `consult`, `memory_add`, `memory_edit`, `memory_read`, `personality_set`, `rag_search`, `nym_todo`, `nym_todo_delete`, `nym_todo_list`

**MODERATE:** `bash_execute`, `file_write`, `file_edit`, `claude_code`, `notify`, `http_request`, `api_discover`, `tool_create`, many trigger/email/calendar/browser actions

**SENSITIVE:** self-modify file mutation and rollback tools

For the precise current registry, call `get_all_tool_metadata()` or check
`nymeria/tools/metadata.py` for the generated metadata policy.

---

## Adding New Tools

### 1. Create a Tool File

```python
# nymeria/tools/my_tool.py
from langchain_core.tools import tool

@tool
def my_tool(param: str) -> str:
    """
    Description shown to the LLM. Be specific about when to use this tool.

    Args:
        param: What this parameter does

    Returns:
        What the tool returns
    """
    # Implementation
    return f"[Success]: Result is {result}"
```

### 2. Export in `__init__.py`

```python
# nymeria/tools/__init__.py
from .my_tool import my_tool

ALL_TOOLS = [
    # ... existing tools
    my_tool,
]
```

### 3. Tool is Automatically Available

The tool is available on next startup, or call `reload_all()` for hot-reload.
Metadata is generated automatically from the registered tool object, so adding a
tool to `ALL_TOOLS` or `OPTIONAL_TOOLS` is enough to get a metadata entry.
Use clear docstrings: the first paragraph becomes the discovery description.

---

## Tool Design Guidelines

| Guideline | Example |
|-----------|---------|
| **Clear docstrings** | The LLM uses these to decide when to call the tool |
| **Return strings** | All outputs should be serializable strings |
| **Handle errors gracefully** | Return `"[Error]: ..."`, don't raise exceptions |
| **Prefix results** | Use `[Success]`, `[Error]`, `[Info]` prefixes |
| **Be specific** | One tool = one job |
| **Log operations** | Use `logger.info()` for audit trail |
| **Use InjectedToolArg** | For user/thread context: `config: Annotated[RunnableConfig, InjectedToolArg]` |
