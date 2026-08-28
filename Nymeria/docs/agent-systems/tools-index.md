# Tools Index

Auto-generated from `@tool`-decorated functions in `Nymeria/nymeria/tools/`.
Regenerate from `Nymeria/` with:
`python3 scripts/generate_tools_index.py > docs/agent-systems/tools-index.md`

This is an AST walk over statically defined tools, so it cannot list
dynamically constructed ones: the per-thread `Skill` meta-tool,
callable-thread and kit-template tools, custom HTTP/Python tools,
`mcp__*` tools, or workflow tools.

**1258 tools found.**

| Tool | File | Description |
|------|------|-------------|
| `actionnetwork_add_person_tag` | `nymeria/tools/marketing_contact_service_integrations.py` | Tag an Action Network person. |
| `actionnetwork_create_attendance` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Action Network attendance for an event and person. |
| `actionnetwork_create_event` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Action Network event. |
| `actionnetwork_create_person` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Action Network person. |
| `actionnetwork_create_petition` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Action Network petition. |
| `actionnetwork_create_signature` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Action Network petition signature for a person. |
| `actionnetwork_get_record` | `nymeria/tools/marketing_contact_service_integrations.py` | Get an Action Network record by ID. |
| `actionnetwork_list_records` | `nymeria/tools/marketing_contact_service_integrations.py` | List Action Network records. |
| `actionnetwork_remove_person_tag` | `nymeria/tools/marketing_contact_service_integrations.py` | Remove an Action Network person tag by tagging ID. |
| `actionnetwork_update_person` | `nymeria/tools/marketing_contact_service_integrations.py` | Update an Action Network person with a JSON object of fields. |
| `activecampaign_add_contact_tag` | `nymeria/tools/marketing_contact_service_integrations.py` | Add an ActiveCampaign tag to a contact. |
| `activecampaign_add_contact_to_list` | `nymeria/tools/marketing_contact_service_integrations.py` | Subscribe or unsubscribe an ActiveCampaign contact to a list. Use status 1 to subscribe, 2 to unsubscribe. |
| `activecampaign_get_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Get one ActiveCampaign contact by ID. |
| `activecampaign_list_contacts` | `nymeria/tools/marketing_contact_service_integrations.py` | List ActiveCampaign contacts with optional search, email, list, or tag filters. |
| `activecampaign_list_lists` | `nymeria/tools/marketing_contact_service_integrations.py` | List ActiveCampaign contact lists. |
| `activecampaign_list_tags` | `nymeria/tools/marketing_contact_service_integrations.py` | List ActiveCampaign tags. |
| `activecampaign_sync_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Create or update an ActiveCampaign contact using contact sync. |
| `activecampaign_update_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Update an ActiveCampaign contact by ID with a JSON object of contact fields. |
| `activity_feed` | `nymeria/tools/activity_feed.py` | Get a structured activity summary across all threads. |
| `adalo_create_record` | `nymeria/tools/data_table_service_integrations.py` | Create an Adalo collection record from a JSON field mapping. |
| `adalo_delete_record` | `nymeria/tools/data_table_service_integrations.py` | Delete an Adalo collection record. |
| `adalo_get_record` | `nymeria/tools/data_table_service_integrations.py` | Get an Adalo collection record. |
| `adalo_list_records` | `nymeria/tools/data_table_service_integrations.py` | List Adalo collection records. |
| `adalo_update_record` | `nymeria/tools/data_table_service_integrations.py` | Update an Adalo collection record from a JSON field mapping. |
| `affinity_create_list_entry` | `nymeria/tools/relationship_crm_service_integrations.py` | Create an Affinity list entry for a person, organization, or opportunity entity. |
| `affinity_create_organization` | `nymeria/tools/relationship_crm_service_integrations.py` | Create an Affinity organization. |
| `affinity_create_person` | `nymeria/tools/relationship_crm_service_integrations.py` | Create an Affinity person with one or more email addresses. |
| `affinity_delete_list_entry` | `nymeria/tools/relationship_crm_service_integrations.py` | Delete an Affinity list entry by ID. |
| `affinity_delete_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Delete an Affinity person or organization by ID. |
| `affinity_get_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Get one Affinity list, person, or organization by ID. |
| `affinity_list_entries` | `nymeria/tools/relationship_crm_service_integrations.py` | List entries in an Affinity list. |
| `affinity_list_records` | `nymeria/tools/relationship_crm_service_integrations.py` | List Affinity lists, people, or organizations. |
| `affinity_update_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Update an Affinity person or organization from a JSON object. |
| `agilecrm_create_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Create an Agile CRM contact, company, or deal from a JSON object. |
| `agilecrm_delete_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Delete an Agile CRM contact, company, or deal by ID. |
| `agilecrm_get_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Get one Agile CRM contact, company, or deal by ID. |
| `agilecrm_list_records` | `nymeria/tools/relationship_crm_service_integrations.py` | List Agile CRM contacts, companies, or deals. |
| `agilecrm_update_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Update an Agile CRM contact, company, or deal from a JSON object. |
| `airtable_create_records` | `nymeria/tools/collaboration_data_service_integrations.py` | Create one or more Airtable records. |
| `airtable_delete_record` | `nymeria/tools/collaboration_data_service_integrations.py` | Delete an Airtable record. |
| `airtable_get_base_schema` | `nymeria/tools/collaboration_data_service_integrations.py` | Get Airtable base schema. |
| `airtable_get_record` | `nymeria/tools/collaboration_data_service_integrations.py` | Get an Airtable record by ID. |
| `airtable_list_bases` | `nymeria/tools/collaboration_data_service_integrations.py` | List Airtable bases visible to the credential. |
| `airtable_list_records` | `nymeria/tools/collaboration_data_service_integrations.py` | List Airtable records in a table. |
| `airtable_update_records` | `nymeria/tools/collaboration_data_service_integrations.py` | Update one or more Airtable records. |
| `api_discover` | `nymeria/tools/http_api.py` | Discover OpenAPI/Swagger details for an API base URL. |
| `apitemplate_create_image` | `nymeria/tools/business_service_integrations.py` | Create an image from an APITemplate image template. |
| `apitemplate_create_pdf` | `nymeria/tools/business_service_integrations.py` | Create a PDF from an APITemplate PDF template. |
| `apitemplate_get_account` | `nymeria/tools/business_service_integrations.py` | Get APITemplate account information. |
| `apitemplate_list_templates` | `nymeria/tools/business_service_integrations.py` | List APITemplate templates. |
| `asana_add_task_comment` | `nymeria/tools/work_tracking_service_integrations.py` | Add a comment/story to an Asana task. |
| `asana_create_project` | `nymeria/tools/work_tracking_service_integrations.py` | Create an Asana project in a team. |
| `asana_create_subtask` | `nymeria/tools/work_tracking_service_integrations.py` | Create an Asana subtask under a parent task. |
| `asana_create_task` | `nymeria/tools/work_tracking_service_integrations.py` | Create an Asana task. |
| `asana_get_project` | `nymeria/tools/work_tracking_service_integrations.py` | Get an Asana project by GID. |
| `asana_get_task` | `nymeria/tools/work_tracking_service_integrations.py` | Get an Asana task by GID. |
| `asana_get_user` | `nymeria/tools/work_tracking_service_integrations.py` | Get an Asana user by ID or "me". |
| `asana_list_projects` | `nymeria/tools/work_tracking_service_integrations.py` | List Asana projects by workspace or team. |
| `asana_list_tasks` | `nymeria/tools/work_tracking_service_integrations.py` | List Asana tasks by project or workspace filters. |
| `asana_list_users` | `nymeria/tools/work_tracking_service_integrations.py` | List users in an Asana workspace. |
| `asana_search_tasks` | `nymeria/tools/work_tracking_service_integrations.py` | Search Asana tasks in a workspace. |
| `asana_update_project` | `nymeria/tools/work_tracking_service_integrations.py` | Update an Asana project. |
| `asana_update_task` | `nymeria/tools/work_tracking_service_integrations.py` | Update an Asana task. |
| `auth_bindings` | `nymeria/tools/auth_manager.py` | Manage credential-to-target bindings without exposing secret values. |
| `auth_cleanup` | `nymeria/tools/auth_manager.py` | Disable stale or unwanted user-owned credentials without exposing secrets. |
| `auth_inspect` | `nymeria/tools/auth_manager.py` | Inspect Nymeria credential metadata without exposing secret values. |
| `auth_test` | `nymeria/tools/auth_manager.py` | Test whether a saved credential is usable, without exposing secret values. |
| `auth_write` | `nymeria/tools/auth_manager.py` | Write credentials into the vault, without ever reading secrets back. |
| `autopilot_add_contact_to_journey` | `nymeria/tools/marketing_contact_service_integrations.py` | Add an Autopilot contact to a journey trigger. |
| `autopilot_create_list` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Autopilot list. |
| `autopilot_delete_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Delete an Autopilot contact. |
| `autopilot_get_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Get an Autopilot contact by ID. |
| `autopilot_list_contacts` | `nymeria/tools/marketing_contact_service_integrations.py` | List Autopilot contacts, optionally scoped to a list. |
| `autopilot_list_lists` | `nymeria/tools/marketing_contact_service_integrations.py` | List Autopilot lists. |
| `autopilot_update_contact_list_membership` | `nymeria/tools/marketing_contact_service_integrations.py` | Add, remove, or check an Autopilot contact's list membership. |
| `autopilot_upsert_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Create or update an Autopilot contact. |
| `aws_lambda_invoke` | `nymeria/tools/aws_service_integrations.py` | Invoke an AWS Lambda function. |
| `aws_lambda_list_functions` | `nymeria/tools/aws_service_integrations.py` | List AWS Lambda functions. |
| `aws_ses_create_template` | `nymeria/tools/aws_service_integrations.py` | Create an Amazon SES email template. |
| `aws_ses_delete_template` | `nymeria/tools/aws_service_integrations.py` | Delete an Amazon SES email template. |
| `aws_ses_get_template` | `nymeria/tools/aws_service_integrations.py` | Get an Amazon SES email template. |
| `aws_ses_list_identities` | `nymeria/tools/aws_service_integrations.py` | List Amazon SES identities. |
| `aws_ses_list_templates` | `nymeria/tools/aws_service_integrations.py` | List Amazon SES email templates. |
| `aws_ses_send_email` | `nymeria/tools/aws_service_integrations.py` | Send an email with Amazon SES. |
| `aws_ses_update_template` | `nymeria/tools/aws_service_integrations.py` | Update an Amazon SES email template. |
| `aws_ses_verify_email_identity` | `nymeria/tools/aws_service_integrations.py` | Start Amazon SES email identity verification. |
| `aws_sns_create_topic` | `nymeria/tools/aws_service_integrations.py` | Create an AWS SNS topic. |
| `aws_sns_delete_topic` | `nymeria/tools/aws_service_integrations.py` | Delete an AWS SNS topic. |
| `aws_sns_list_topics` | `nymeria/tools/aws_service_integrations.py` | List AWS SNS topics. |
| `aws_sns_publish` | `nymeria/tools/aws_service_integrations.py` | Publish a message to an AWS SNS topic. |
| `aws_textract_analyze_expense` | `nymeria/tools/aws_service_integrations.py` | Analyze a receipt or invoice image/PDF with Amazon Textract expense analysis. |
| `aws_transcribe_delete_job` | `nymeria/tools/aws_service_integrations.py` | Delete an Amazon Transcribe job. |
| `aws_transcribe_get_job` | `nymeria/tools/aws_service_integrations.py` | Get an Amazon Transcribe job. |
| `aws_transcribe_list_jobs` | `nymeria/tools/aws_service_integrations.py` | List Amazon Transcribe jobs. |
| `aws_transcribe_start_job` | `nymeria/tools/aws_service_integrations.py` | Start an Amazon Transcribe transcription job. |
| `bamboohr_create_employee` | `nymeria/tools/time_hr_service_integrations.py` | Create a BambooHR employee with optional extra fields as a JSON object. |
| `bamboohr_get_company_report` | `nymeria/tools/time_hr_service_integrations.py` | Run a BambooHR company report. |
| `bamboohr_get_employee` | `nymeria/tools/time_hr_service_integrations.py` | Get a BambooHR employee by ID with selected field names. |
| `bamboohr_list_employees` | `nymeria/tools/time_hr_service_integrations.py` | List employees from the BambooHR company directory. |
| `bamboohr_update_employee` | `nymeria/tools/time_hr_service_integrations.py` | Update BambooHR employee fields from a JSON object. |
| `baserow_create_row` | `nymeria/tools/data_table_service_integrations.py` | Create a Baserow row from a JSON field mapping. |
| `baserow_delete_row` | `nymeria/tools/data_table_service_integrations.py` | Delete a Baserow row. |
| `baserow_get_row` | `nymeria/tools/data_table_service_integrations.py` | Get a single Baserow row. |
| `baserow_list_fields` | `nymeria/tools/data_table_service_integrations.py` | List fields for a Baserow table. |
| `baserow_list_rows` | `nymeria/tools/data_table_service_integrations.py` | List rows from a Baserow table. |
| `baserow_list_tables` | `nymeria/tools/data_table_service_integrations.py` | List Baserow tables available to the credential. |
| `baserow_update_row` | `nymeria/tools/data_table_service_integrations.py` | Update a Baserow row from a JSON field mapping. |
| `bash_execute` | `nymeria/tools/bash.py` | Execute a shell command and return the output. |
| `bash_job` | `nymeria/tools/bash_job.py` | Inspect or control background bash jobs (from bash_execute run_in_background). |
| `beeminder_create_datapoint` | `nymeria/tools/time_hr_service_integrations.py` | Create a Beeminder datapoint. |
| `beeminder_delete_datapoint` | `nymeria/tools/time_hr_service_integrations.py` | Delete a Beeminder datapoint. |
| `beeminder_get_goal` | `nymeria/tools/time_hr_service_integrations.py` | Get a Beeminder goal by slug. |
| `beeminder_get_user` | `nymeria/tools/time_hr_service_integrations.py` | Get the authenticated Beeminder user. |
| `beeminder_list_datapoints` | `nymeria/tools/time_hr_service_integrations.py` | List datapoints for a Beeminder goal. |
| `beeminder_list_goals` | `nymeria/tools/time_hr_service_integrations.py` | List Beeminder goals for the authenticated user. |
| `beeminder_update_datapoint` | `nymeria/tools/time_hr_service_integrations.py` | Update a Beeminder datapoint from a JSON object. |
| `bitly_create_bitlink` | `nymeria/tools/business_service_integrations.py` | Create a Bitly short link. |
| `bitly_get_bitlink` | `nymeria/tools/business_service_integrations.py` | Get Bitly bitlink metadata. |
| `bitly_update_bitlink` | `nymeria/tools/business_service_integrations.py` | Update Bitly bitlink metadata. |
| `brandfetch_get_brand` | `nymeria/tools/business_service_integrations.py` | Get Brandfetch company, industry, color, font, and logo metadata. |
| `brandfetch_get_brand_colors` | `nymeria/tools/business_service_integrations.py` | Get Brandfetch color metadata for a company domain. |
| `brandfetch_get_brand_logos` | `nymeria/tools/business_service_integrations.py` | Get Brandfetch logo and icon metadata for a company domain. |
| `brevo_create_contact` | `nymeria/tools/messaging_delivery_service_integrations.py` | Create a Brevo contact. |
| `brevo_get_contact` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get a Brevo contact by email, ID, SMS attribute, or external ID. |
| `brevo_list_contacts` | `nymeria/tools/messaging_delivery_service_integrations.py` | List Brevo contacts. |
| `brevo_list_senders` | `nymeria/tools/messaging_delivery_service_integrations.py` | List Brevo senders available for transactional email. |
| `brevo_send_email` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send a transactional email with Brevo. |
| `brevo_update_contact` | `nymeria/tools/messaging_delivery_service_integrations.py` | Update a Brevo contact. |
| `browser_click` | `nymeria/tools/browser.py` | Click an element on the page. |
| `browser_close` | `nymeria/tools/browser.py` | Close the browser. |
| `browser_get_content` | `nymeria/tools/browser.py` | Get the text content of the current page. |
| `browser_navigate` | `nymeria/tools/browser.py` | Navigate browser to a URL. Opens browser if not already open. |
| `browser_press_key` | `nymeria/tools/browser.py` | Press a keyboard key. |
| `browser_screenshot` | `nymeria/tools/browser.py` | Take a screenshot of the current page so you can visually inspect it. |
| `browser_scroll` | `nymeria/tools/browser.py` | Scroll the page. |
| `browser_status` | `nymeria/tools/browser.py` | Check the browser status and Playwright availability. |
| `browser_type` | `nymeria/tools/browser.py` | Type text into an input field. |
| `bubble_create_object` | `nymeria/tools/data_table_service_integrations.py` | Create a Bubble Data API object. |
| `bubble_delete_object` | `nymeria/tools/data_table_service_integrations.py` | Delete a Bubble Data API object. |
| `bubble_get_object` | `nymeria/tools/data_table_service_integrations.py` | Get a Bubble Data API object. |
| `bubble_list_objects` | `nymeria/tools/data_table_service_integrations.py` | List Bubble Data API objects. |
| `bubble_update_object` | `nymeria/tools/data_table_service_integrations.py` | Update a Bubble Data API object. |
| `calculator` | `nymeria/tools/utility_integrations.py` | Evaluate a safe arithmetic expression. |
| `calendar_create_event` | `nymeria/tools/calendar.py` | Create a new calendar event. |
| `calendar_delete_event` | `nymeria/tools/calendar.py` | Delete a calendar event. |
| `calendar_get_current_time` | `nymeria/tools/calendar.py` | Get the current time in ISO 8601 format. |
| `calendar_get_event` | `nymeria/tools/calendar.py` | Get detailed information about a specific calendar event. |
| `calendar_get_freebusy` | `nymeria/tools/calendar.py` | Get free/busy information for calendars. |
| `calendar_list_calendars` | `nymeria/tools/calendar.py` | List all available Google calendars for the authenticated account. |
| `calendar_list_colors` | `nymeria/tools/calendar.py` | List available calendar and event colors. |
| `calendar_list_events` | `nymeria/tools/calendar.py` | List events from a Google Calendar. |
| `calendar_respond_to_event` | `nymeria/tools/calendar.py` | Respond to a calendar event invitation. |
| `calendar_search_events` | `nymeria/tools/calendar.py` | Search for events by text query. |
| `calendar_update_event` | `nymeria/tools/calendar.py` | Update an existing calendar event. |
| `chargebee_create_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Chargebee customer. |
| `chargebee_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Chargebee record by ID. |
| `chargebee_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List Chargebee billing records. |
| `chargebee_update_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Chargebee customer. |
| `chrome_act` | `nymeria/tools/chrome_browser.py` | Do one thing to a Chrome page: click, type, choose, scroll, drag, wait. |
| `chrome_batch` | `nymeria/tools/chrome_browser.py` | Run several browser commands in one round trip. |
| `chrome_cdp` | `nymeria/tools/chrome_browser.py` | Raw Chrome DevTools Protocol call. LAST RESORT. |
| `chrome_console` | `nymeria/tools/chrome_browser.py` | Read console messages and uncaught exceptions from a Chrome tab. |
| `chrome_dialog` | `nymeria/tools/chrome_browser.py` | Answer the JS dialog standing on a tab you are driving. |
| `chrome_find` | `nymeria/tools/chrome_browser.py` | Find elements on a Chrome tab by describing them in plain language. |
| `chrome_health` | `nymeria/tools/chrome_browser.py` | One read that says whether a Chrome tab is healthy and what state it is in. |
| `chrome_navigate` | `nymeria/tools/chrome_browser.py` | Point a Chrome tab at a URL, or move through its history. |
| `chrome_network` | `nymeria/tools/chrome_browser.py` | Read the network requests a Chrome tab made, with status codes. |
| `chrome_read_page` | `nymeria/tools/chrome_browser.py` | Read a Chrome tab's accessibility tree: the map you act on. |
| `chrome_read_text` | `nymeria/tools/chrome_browser.py` | Read the visible text of a Chrome tab. Cheaper than a screenshot for prose. |
| `chrome_reload_extension` | `nymeria/tools/chrome_browser.py` | Reload the Nymeria browser extension from disk (dev-loop helper). |
| `chrome_screenshot` | `nymeria/tools/chrome_browser.py` | Capture what the user's Chrome tab looks like, and see it. |
| `chrome_tabs` | `nymeria/tools/chrome_browser.py` | List or manage tabs in the user's Chrome. Start here to get a tab_id. |
| `circleci_get_pipeline` | `nymeria/tools/build_ci_service_integrations.py` | Get one CircleCI pipeline by project and pipeline number. |
| `circleci_list_pipelines` | `nymeria/tools/build_ci_service_integrations.py` | List CircleCI pipelines for a GitHub or Bitbucket project. |
| `circleci_trigger_pipeline` | `nymeria/tools/build_ci_service_integrations.py` | Trigger a CircleCI pipeline. |
| `claude_code` | `nymeria/tools/claude_code.py` | Drive Claude Code (the CLI coding agent) to do real work in a project. |
| `clearbit_autocomplete_company` | `nymeria/tools/lead_enrichment_service_integrations.py` | Autocomplete company names and return likely domains/logos. |
| `clearbit_enrich_company` | `nymeria/tools/lead_enrichment_service_integrations.py` | Enrich company data from a domain and optional social/company hints. |
| `clearbit_enrich_person` | `nymeria/tools/lead_enrichment_service_integrations.py` | Enrich person and company data from an email address and optional hints. |
| `cli_statusbar_get` | `nymeria/tools/cli_statusbar.py` | Read the user's current CLI status-bar layout. |
| `cli_statusbar_set` | `nymeria/tools/cli_statusbar.py` | Reconfigure one of the user's CLI status bars. |
| `clickup_add_task_comment` | `nymeria/tools/project_management_service_integrations.py` | Add a comment to a ClickUp task. |
| `clickup_create_task` | `nymeria/tools/project_management_service_integrations.py` | Create a ClickUp task. |
| `clickup_get_task` | `nymeria/tools/project_management_service_integrations.py` | Get a ClickUp task by ID. |
| `clickup_list_folders` | `nymeria/tools/project_management_service_integrations.py` | List ClickUp folders in a space. |
| `clickup_list_lists` | `nymeria/tools/project_management_service_integrations.py` | List ClickUp lists in a folder or folderless space. |
| `clickup_list_spaces` | `nymeria/tools/project_management_service_integrations.py` | List ClickUp spaces in a workspace/team. |
| `clickup_list_task_comments` | `nymeria/tools/project_management_service_integrations.py` | List comments on a ClickUp task. |
| `clickup_list_tasks` | `nymeria/tools/project_management_service_integrations.py` | List ClickUp tasks in a list. |
| `clickup_list_teams` | `nymeria/tools/project_management_service_integrations.py` | List ClickUp workspaces/teams visible to the credential. |
| `clickup_update_task` | `nymeria/tools/project_management_service_integrations.py` | Update a ClickUp task. |
| `clockify_create_project` | `nymeria/tools/time_hr_service_integrations.py` | Create a Clockify project. |
| `clockify_create_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Create a Clockify time entry using ISO 8601 start/end timestamps. |
| `clockify_delete_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Delete a Clockify time entry. |
| `clockify_list_projects` | `nymeria/tools/time_hr_service_integrations.py` | List Clockify projects in a workspace. |
| `clockify_list_time_entries` | `nymeria/tools/time_hr_service_integrations.py` | List Clockify time entries for a workspace user. |
| `clockify_list_users` | `nymeria/tools/time_hr_service_integrations.py` | List Clockify users in a workspace. |
| `clockify_list_workspaces` | `nymeria/tools/time_hr_service_integrations.py` | List Clockify workspaces. |
| `clockify_update_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Update a Clockify time entry from a JSON object. |
| `cloudflare_create_dns_record` | `nymeria/tools/operations_monitoring_service_integrations.py` | Create a Cloudflare DNS record. |
| `cloudflare_delete_dns_record` | `nymeria/tools/operations_monitoring_service_integrations.py` | Delete a Cloudflare DNS record. |
| `cloudflare_delete_origin_certificate` | `nymeria/tools/operations_monitoring_service_integrations.py` | Delete a Cloudflare zone-level authenticated origin pull certificate. |
| `cloudflare_get_origin_certificate` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Cloudflare zone-level authenticated origin pull certificate. |
| `cloudflare_list_dns_records` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Cloudflare DNS records for a zone. |
| `cloudflare_list_origin_certificates` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Cloudflare zone-level authenticated origin pull certificates. |
| `cloudflare_list_zones` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Cloudflare zones. |
| `cloudflare_update_dns_record` | `nymeria/tools/operations_monitoring_service_integrations.py` | Patch a Cloudflare DNS record. |
| `cloudflare_upload_origin_certificate` | `nymeria/tools/operations_monitoring_service_integrations.py` | Upload a Cloudflare zone-level authenticated origin pull certificate. |
| `cockpit_get_singleton` | `nymeria/tools/data_table_service_integrations.py` | Get a Cockpit singleton. |
| `cockpit_list_collection_entries` | `nymeria/tools/data_table_service_integrations.py` | List Cockpit collection entries. |
| `cockpit_list_collections` | `nymeria/tools/data_table_service_integrations.py` | List Cockpit collection names. |
| `cockpit_list_singletons` | `nymeria/tools/data_table_service_integrations.py` | List Cockpit singleton names. |
| `cockpit_save_collection_entry` | `nymeria/tools/data_table_service_integrations.py` | Create or update a Cockpit collection entry. |
| `cockpit_submit_form` | `nymeria/tools/data_table_service_integrations.py` | Submit a Cockpit form. |
| `coda_create_table_row` | `nymeria/tools/data_table_service_integrations.py` | Create or upsert a Coda table row from a JSON column-value mapping. |
| `coda_delete_table_row` | `nymeria/tools/data_table_service_integrations.py` | Delete a Coda table row. |
| `coda_get_table_row` | `nymeria/tools/data_table_service_integrations.py` | Get a Coda table row. |
| `coda_list_controls` | `nymeria/tools/data_table_service_integrations.py` | List controls in a Coda doc. |
| `coda_list_docs` | `nymeria/tools/data_table_service_integrations.py` | List Coda docs visible to the credential. |
| `coda_list_formulas` | `nymeria/tools/data_table_service_integrations.py` | List formulas in a Coda doc. |
| `coda_list_table_rows` | `nymeria/tools/data_table_service_integrations.py` | List rows from a Coda table or view. |
| `coda_list_tables` | `nymeria/tools/data_table_service_integrations.py` | List Coda tables and views in a doc. |
| `coda_update_table_row` | `nymeria/tools/data_table_service_integrations.py` | Update a Coda table row from a JSON column-value mapping. |
| `coingecko_coin_markets` | `nymeria/tools/public_info_integrations.py` | List CoinGecko coin market data. |
| `coingecko_price` | `nymeria/tools/public_info_integrations.py` | Get current cryptocurrency prices from CoinGecko. |
| `compression_gunzip_text` | `nymeria/tools/transform_utility_integrations.py` | Decompress base64 gzip data into text. |
| `compression_gzip_text` | `nymeria/tools/transform_utility_integrations.py` | Compress text with gzip and return base64 data. |
| `compression_unzip_text_files` | `nymeria/tools/transform_utility_integrations.py` | Extract base64 zip data into a JSON object of filenames and text/base64 content. |
| `compression_zip_text_files` | `nymeria/tools/transform_utility_integrations.py` | Create a zip archive from a JSON object of filename to text. |
| `consult` | `nymeria/tools/think.py` | Ask another AI (Gemini) for a second opinion. Sends the question to a |
| `contentful_get_record` | `nymeria/tools/content_management_service_integrations.py` | Get a Contentful delivery or preview record. |
| `contentful_list_records` | `nymeria/tools/content_management_service_integrations.py` | List Contentful delivery or preview records. |
| `convertkit_add_subscriber_to_form` | `nymeria/tools/marketing_contact_service_integrations.py` | Subscribe an email address to a ConvertKit form. |
| `convertkit_add_subscriber_to_tag` | `nymeria/tools/marketing_contact_service_integrations.py` | Subscribe an email address to a ConvertKit tag. |
| `convertkit_get_account` | `nymeria/tools/marketing_contact_service_integrations.py` | Get ConvertKit account details. |
| `convertkit_list_forms` | `nymeria/tools/marketing_contact_service_integrations.py` | List ConvertKit forms. |
| `convertkit_list_subscribers` | `nymeria/tools/marketing_contact_service_integrations.py` | List ConvertKit subscribers, optionally filtered by email. |
| `convertkit_list_tags` | `nymeria/tools/marketing_contact_service_integrations.py` | List ConvertKit tags. |
| `copper_create_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Create a Copper CRM record from a JSON object. |
| `copper_delete_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Delete a Copper CRM record by ID. |
| `copper_get_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Get one Copper CRM record by ID. |
| `copper_list_records` | `nymeria/tools/relationship_crm_service_integrations.py` | List Copper CRM records for companies, people, leads, opportunities, projects, tasks, users, or customer sources. |
| `copper_update_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Update a Copper CRM record from a JSON object. |
| `crypto_generate_random` | `nymeria/tools/transform_utility_integrations.py` | Generate a random UUID or random string. |
| `crypto_hash_text` | `nymeria/tools/transform_utility_integrations.py` | Hash text with a selected digest algorithm. |
| `crypto_hmac_text` | `nymeria/tools/transform_utility_integrations.py` | Create an HMAC for text using the saved Crypto secret. |
| `crypto_sign_text` | `nymeria/tools/transform_utility_integrations.py` | Sign text with the saved Crypto private key. |
| `customerio_get_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Get a Customer.io campaign by ID. |
| `customerio_list_campaigns` | `nymeria/tools/marketing_contact_service_integrations.py` | List Customer.io campaigns. |
| `customerio_track_anonymous_event` | `nymeria/tools/marketing_contact_service_integrations.py` | Track a Customer.io event without a known customer ID. |
| `customerio_track_event` | `nymeria/tools/marketing_contact_service_integrations.py` | Track a Customer.io event for a known customer. |
| `customerio_update_segment` | `nymeria/tools/marketing_contact_service_integrations.py` | Add or remove customers from a Customer.io manual segment. |
| `customerio_upsert_customer` | `nymeria/tools/marketing_contact_service_integrations.py` | Create or update a Customer.io customer profile. |
| `datetime_add` | `nymeria/tools/transform_utility_integrations.py` | Add a duration to a date/time value. |
| `datetime_between` | `nymeria/tools/transform_utility_integrations.py` | Get the time difference between two date/time values. |
| `datetime_current` | `nymeria/tools/transform_utility_integrations.py` | Get the current date or time in a timezone. |
| `datetime_extract` | `nymeria/tools/transform_utility_integrations.py` | Extract one component from a date/time value. |
| `datetime_format` | `nymeria/tools/transform_utility_integrations.py` | Parse and format a date/time value. |
| `datetime_round` | `nymeria/tools/transform_utility_integrations.py` | Round a date/time down or up to a calendar boundary. |
| `datetime_subtract` | `nymeria/tools/transform_utility_integrations.py` | Subtract a duration from a date/time value. |
| `deepl_list_languages` | `nymeria/tools/business_service_integrations.py` | List DeepL source or target languages. |
| `deepl_translate_text` | `nymeria/tools/business_service_integrations.py` | Translate text with DeepL. |
| `demio_get_event` | `nymeria/tools/event_meeting_service_integrations.py` | Get a Demio event or a specific event date/session. |
| `demio_get_session_participants` | `nymeria/tools/event_meeting_service_integrations.py` | Get Demio participant report rows for an event date/session. |
| `demio_list_events` | `nymeria/tools/event_meeting_service_integrations.py` | List Demio events. |
| `demio_register_event` | `nymeria/tools/event_meeting_service_integrations.py` | Register an attendee for a Demio event. |
| `dhl_track_shipment` | `nymeria/tools/business_service_integrations.py` | Get DHL shipment tracking details. |
| `discord_delete_message` | `nymeria/tools/chat_platform_service_integrations.py` | Delete a Discord message. |
| `discord_get_channel` | `nymeria/tools/chat_platform_service_integrations.py` | Get Discord channel metadata. |
| `discord_get_channel_messages` | `nymeria/tools/chat_platform_service_integrations.py` | Get recent Discord channel messages. |
| `discord_list_guild_channels` | `nymeria/tools/chat_platform_service_integrations.py` | List channels in a Discord guild. |
| `discord_send_channel_message` | `nymeria/tools/chat_platform_service_integrations.py` | Send a Discord channel message. |
| `discourse_create_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create a reply post in a Discourse topic. |
| `discourse_create_topic` | `nymeria/tools/community_publishing_service_integrations.py` | Create a Discourse topic. |
| `discourse_get_post` | `nymeria/tools/community_publishing_service_integrations.py` | Get a Discourse post by ID. |
| `discourse_get_topic` | `nymeria/tools/community_publishing_service_integrations.py` | Get a Discourse topic and its first post stream page. |
| `discourse_list_latest_topics` | `nymeria/tools/community_publishing_service_integrations.py` | List latest topics from a Discourse forum. |
| `discourse_search` | `nymeria/tools/community_publishing_service_integrations.py` | Search a Discourse forum. |
| `discourse_update_post` | `nymeria/tools/community_publishing_service_integrations.py` | Update a Discourse post. |
| `drift_create_contact` | `nymeria/tools/support_service_integrations.py` | Create a Drift contact. |
| `drift_delete_contact` | `nymeria/tools/support_service_integrations.py` | Delete a Drift contact. |
| `drift_get_contact` | `nymeria/tools/support_service_integrations.py` | Get a Drift contact by ID. |
| `drift_list_contact_attributes` | `nymeria/tools/support_service_integrations.py` | List custom contact attributes configured in Drift. |
| `drift_update_contact` | `nymeria/tools/support_service_integrations.py` | Update a Drift contact. |
| `dropbox_copy_path` | `nymeria/tools/file_storage_service_integrations.py` | Copy a Dropbox file or folder. |
| `dropbox_create_folder` | `nymeria/tools/file_storage_service_integrations.py` | Create a Dropbox folder. |
| `dropbox_delete_path` | `nymeria/tools/file_storage_service_integrations.py` | Delete a Dropbox file or folder. |
| `dropbox_download_file` | `nymeria/tools/file_storage_service_integrations.py` | Download a Dropbox file and return a text/base64 preview. |
| `dropbox_get_current_account` | `nymeria/tools/file_storage_service_integrations.py` | Get Dropbox account metadata for the saved token. |
| `dropbox_get_metadata` | `nymeria/tools/file_storage_service_integrations.py` | Get Dropbox metadata for a file or folder path. |
| `dropbox_list_folder` | `nymeria/tools/file_storage_service_integrations.py` | List Dropbox folder entries. |
| `dropbox_move_path` | `nymeria/tools/file_storage_service_integrations.py` | Move or rename a Dropbox file or folder. |
| `dropbox_search` | `nymeria/tools/file_storage_service_integrations.py` | Search Dropbox files and folders. |
| `dropbox_upload_text_file` | `nymeria/tools/file_storage_service_integrations.py` | Upload text content to a Dropbox file path. |
| `dropcontact_fetch_request` | `nymeria/tools/lead_enrichment_service_integrations.py` | Fetch a completed Dropcontact request by request ID. |
| `dropcontact_submit_enrichment` | `nymeria/tools/lead_enrichment_service_integrations.py` | Submit one contact enrichment request to Dropcontact. |
| `egoi_create_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an E-goi contact. |
| `egoi_get_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Get an E-goi contact by contact ID or email. |
| `egoi_list_contacts` | `nymeria/tools/marketing_contact_service_integrations.py` | List contacts in an E-goi list. |
| `egoi_list_lists` | `nymeria/tools/marketing_contact_service_integrations.py` | List E-goi lists. |
| `egoi_update_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Update an E-goi contact. |
| `elastic_security_add_case_comment` | `nymeria/tools/enrichment_security_service_integrations.py` | Add a comment to an Elastic Security case. |
| `elastic_security_create_case` | `nymeria/tools/enrichment_security_service_integrations.py` | Create an Elastic Security case. |
| `elastic_security_get_case` | `nymeria/tools/enrichment_security_service_integrations.py` | Get an Elastic Security case by ID. |
| `elastic_security_list_case_tags` | `nymeria/tools/enrichment_security_service_integrations.py` | List Elastic Security case tags. |
| `elastic_security_list_cases` | `nymeria/tools/enrichment_security_service_integrations.py` | List Elastic Security cases. |
| `elasticsearch_delete_document` | `nymeria/tools/operations_monitoring_service_integrations.py` | Delete an Elasticsearch document by ID. |
| `elasticsearch_get_document` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get an Elasticsearch document by ID. |
| `elasticsearch_index_document` | `nymeria/tools/operations_monitoring_service_integrations.py` | Create or replace an Elasticsearch document. |
| `elasticsearch_list_indices` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Elasticsearch indices. |
| `elasticsearch_search` | `nymeria/tools/operations_monitoring_service_integrations.py` | Search Elasticsearch documents. |
| `emelia_add_contact_to_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Add a contact to an Emelia campaign. |
| `emelia_add_contact_to_list` | `nymeria/tools/marketing_contact_service_integrations.py` | Add a contact to an Emelia contact list. |
| `emelia_create_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Create an Emelia campaign. |
| `emelia_duplicate_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Duplicate an Emelia campaign. |
| `emelia_get_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Get an Emelia campaign. |
| `emelia_list_campaigns` | `nymeria/tools/marketing_contact_service_integrations.py` | List Emelia campaigns. |
| `emelia_list_contact_lists` | `nymeria/tools/marketing_contact_service_integrations.py` | List Emelia contact lists. |
| `emelia_update_campaign_status` | `nymeria/tools/marketing_contact_service_integrations.py` | Start or pause an Emelia campaign. |
| `erpnext_create_document` | `nymeria/tools/enterprise_business_service_integrations.py` | Create an ERPNext document. |
| `erpnext_delete_document` | `nymeria/tools/enterprise_business_service_integrations.py` | Delete an ERPNext document. |
| `erpnext_get_document` | `nymeria/tools/enterprise_business_service_integrations.py` | Get an ERPNext document by DocType and document name. |
| `erpnext_get_logged_user` | `nymeria/tools/enterprise_business_service_integrations.py` | Get the current ERPNext user for the configured credential. |
| `erpnext_list_documents` | `nymeria/tools/enterprise_business_service_integrations.py` | List ERPNext documents for a DocType. |
| `erpnext_update_document` | `nymeria/tools/enterprise_business_service_integrations.py` | Update an ERPNext document. |
| `facebook_graph_get_me` | `nymeria/tools/community_publishing_service_integrations.py` | Get the authenticated Facebook Graph profile. |
| `facebook_graph_get_node` | `nymeria/tools/community_publishing_service_integrations.py` | Get a Facebook Graph node or list one of its edges. |
| `facebook_page_create_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create a Facebook Page feed post. |
| `facebook_page_list_accounts` | `nymeria/tools/community_publishing_service_integrations.py` | List Facebook pages/accounts available to the authenticated user. |
| `fetch_url_nymeria` | `nymeria/tools/web_fetch.py` | Fetch a web page or PDF by URL and return its readable content. |
| `file_edit` | `nymeria/tools/file_edit.py` | Precisely edit an existing text file with exact, all-or-nothing operations. |
| `file_read` | `nymeria/tools/filesystem.py` | Read the contents of a file, including images. |
| `file_write` | `nymeria/tools/filesystem.py` | Write content to a file. |
| `freshdesk_create_contact` | `nymeria/tools/support_service_integrations.py` | Create a Freshdesk contact. |
| `freshdesk_create_ticket` | `nymeria/tools/support_service_integrations.py` | Create a Freshdesk ticket. |
| `freshdesk_delete_ticket` | `nymeria/tools/support_service_integrations.py` | Delete a Freshdesk ticket. |
| `freshdesk_get_contact` | `nymeria/tools/support_service_integrations.py` | Get a Freshdesk contact by ID. |
| `freshdesk_get_ticket` | `nymeria/tools/support_service_integrations.py` | Get a Freshdesk ticket by ID. |
| `freshdesk_list_contacts` | `nymeria/tools/support_service_integrations.py` | List Freshdesk contacts. |
| `freshdesk_list_tickets` | `nymeria/tools/support_service_integrations.py` | List Freshdesk tickets. |
| `freshdesk_search_tickets` | `nymeria/tools/support_service_integrations.py` | Search Freshdesk tickets with Freshdesk's ticket query syntax. |
| `freshdesk_update_contact` | `nymeria/tools/support_service_integrations.py` | Update a Freshdesk contact. |
| `freshdesk_update_ticket` | `nymeria/tools/support_service_integrations.py` | Update a Freshdesk ticket. |
| `freshservice_create_ticket` | `nymeria/tools/support_service_integrations.py` | Create a Freshservice ticket. |
| `freshservice_get_requester` | `nymeria/tools/support_service_integrations.py` | Get a Freshservice requester by ID. |
| `freshservice_get_ticket` | `nymeria/tools/support_service_integrations.py` | Get a Freshservice ticket by ID. |
| `freshservice_list_requesters` | `nymeria/tools/support_service_integrations.py` | List Freshservice requesters. |
| `freshservice_list_tickets` | `nymeria/tools/support_service_integrations.py` | List Freshservice tickets. |
| `freshservice_update_ticket` | `nymeria/tools/support_service_integrations.py` | Update a Freshservice ticket. |
| `freshworks_crm_create_record` | `nymeria/tools/sales_crm_service_integrations.py` | Create one Freshworks CRM record from a JSON object. |
| `freshworks_crm_delete_record` | `nymeria/tools/sales_crm_service_integrations.py` | Delete one Freshworks CRM record by ID. |
| `freshworks_crm_get_record` | `nymeria/tools/sales_crm_service_integrations.py` | Get one Freshworks CRM record by ID. |
| `freshworks_crm_list_records` | `nymeria/tools/sales_crm_service_integrations.py` | List Freshworks CRM records. |
| `freshworks_crm_search_records` | `nymeria/tools/sales_crm_service_integrations.py` | Search Freshworks CRM records. |
| `freshworks_crm_update_record` | `nymeria/tools/sales_crm_service_integrations.py` | Update one Freshworks CRM record from a JSON object. |
| `getresponse_create_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Create a GetResponse contact. |
| `getresponse_delete_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Delete a GetResponse contact. |
| `getresponse_get_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Get a GetResponse contact by ID. |
| `getresponse_list_campaigns` | `nymeria/tools/marketing_contact_service_integrations.py` | List GetResponse campaigns. |
| `getresponse_list_contacts` | `nymeria/tools/marketing_contact_service_integrations.py` | List GetResponse contacts with optional email and campaign filters. |
| `getresponse_update_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Update a GetResponse contact. |
| `ghost_create_post` | `nymeria/tools/content_management_service_integrations.py` | Create a Ghost post with the Admin API. |
| `ghost_delete_post` | `nymeria/tools/content_management_service_integrations.py` | Delete a Ghost post with the Admin API. |
| `ghost_get_post` | `nymeria/tools/content_management_service_integrations.py` | Get a Ghost post by ID or slug. |
| `ghost_list_posts` | `nymeria/tools/content_management_service_integrations.py` | List Ghost posts from the Content or Admin API. |
| `ghost_update_post` | `nymeria/tools/content_management_service_integrations.py` | Update a Ghost post with the Admin API. |
| `github_get_issue` | `nymeria/tools/developer_platform_integrations.py` | Get a single GitHub issue by repository issue number. |
| `github_get_release` | `nymeria/tools/developer_platform_integrations.py` | Get a GitHub release by tag name. |
| `github_get_repository` | `nymeria/tools/developer_platform_integrations.py` | Get GitHub repository metadata. |
| `github_list_issues` | `nymeria/tools/developer_platform_integrations.py` | List GitHub repository issues. |
| `github_list_pull_requests` | `nymeria/tools/developer_platform_integrations.py` | List GitHub repository pull requests. |
| `github_list_releases` | `nymeria/tools/developer_platform_integrations.py` | List GitHub repository releases. |
| `github_search_repositories` | `nymeria/tools/developer_platform_integrations.py` | Search GitHub repositories. |
| `gitlab_get_project` | `nymeria/tools/developer_platform_integrations.py` | Get GitLab project metadata by numeric ID or namespace path. |
| `gitlab_get_project_issue` | `nymeria/tools/developer_platform_integrations.py` | Get a single GitLab project issue by internal issue ID. |
| `gitlab_get_project_release` | `nymeria/tools/developer_platform_integrations.py` | Get a GitLab project release by tag name. |
| `gitlab_list_project_issues` | `nymeria/tools/developer_platform_integrations.py` | List GitLab project issues. |
| `gitlab_list_project_releases` | `nymeria/tools/developer_platform_integrations.py` | List GitLab project releases. |
| `gitlab_list_user_projects` | `nymeria/tools/developer_platform_integrations.py` | List GitLab projects owned by a user ID. |
| `gitlab_search_projects` | `nymeria/tools/developer_platform_integrations.py` | Search GitLab projects. |
| `google_analytics_get_metadata` | `nymeria/tools/google_analytics_service_integrations.py` | Get available dimensions and metrics for a GA4 property. |
| `google_analytics_list_account_summaries` | `nymeria/tools/google_analytics_service_integrations.py` | List Google Analytics accounts and GA4 properties visible to the account. |
| `google_analytics_run_realtime_report` | `nymeria/tools/google_analytics_service_integrations.py` | Run a GA4 realtime report for recent activity. |
| `google_analytics_run_report` | `nymeria/tools/google_analytics_service_integrations.py` | Run a GA4 report for selected metrics, dimensions, and date range. |
| `google_books_get_volume` | `nymeria/tools/media_discovery_service_integrations.py` | Get a Google Books volume by ID. |
| `google_books_search` | `nymeria/tools/media_discovery_service_integrations.py` | Search public Google Books volume metadata. |
| `google_business_profile_create_post` | `nymeria/tools/google_business_profile_service_integrations.py` | Create a Google Business Profile local post. |
| `google_business_profile_delete_post` | `nymeria/tools/google_business_profile_service_integrations.py` | Delete a Google Business Profile local post. |
| `google_business_profile_delete_review_reply` | `nymeria/tools/google_business_profile_service_integrations.py` | Delete the authenticated business reply on a Google Business Profile review. |
| `google_business_profile_get_post` | `nymeria/tools/google_business_profile_service_integrations.py` | Get one Google Business Profile local post. |
| `google_business_profile_get_review` | `nymeria/tools/google_business_profile_service_integrations.py` | Get one Google Business Profile review. |
| `google_business_profile_list_locations` | `nymeria/tools/google_business_profile_service_integrations.py` | List business locations for a Google Business Profile account. |
| `google_business_profile_list_posts` | `nymeria/tools/google_business_profile_service_integrations.py` | List local posts for a Google Business Profile location. |
| `google_business_profile_list_profile_accounts` | `nymeria/tools/google_business_profile_service_integrations.py` | List Google Business Profile accounts visible to the authenticated account. |
| `google_business_profile_list_reviews` | `nymeria/tools/google_business_profile_service_integrations.py` | List customer reviews for a Google Business Profile location. |
| `google_business_profile_reply_to_review` | `nymeria/tools/google_business_profile_service_integrations.py` | Reply to a Google Business Profile review. |
| `google_business_profile_update_post` | `nymeria/tools/google_business_profile_service_integrations.py` | Update a Google Business Profile local post. |
| `google_chat_delete_message` | `nymeria/tools/google_workspace_service_integrations.py` | Delete a Google Chat message. |
| `google_chat_get_member` | `nymeria/tools/google_workspace_service_integrations.py` | Get a Google Chat membership by resource name. |
| `google_chat_get_message` | `nymeria/tools/google_workspace_service_integrations.py` | Get a Google Chat message by resource name. |
| `google_chat_get_space` | `nymeria/tools/google_workspace_service_integrations.py` | Get a Google Chat space by resource name. |
| `google_chat_list_members` | `nymeria/tools/google_workspace_service_integrations.py` | List memberships in a Google Chat space. |
| `google_chat_list_messages` | `nymeria/tools/google_workspace_service_integrations.py` | List recent Google Chat messages in a space. |
| `google_chat_list_spaces` | `nymeria/tools/google_workspace_service_integrations.py` | List Google Chat spaces visible to the authenticated account. |
| `google_chat_send_message` | `nymeria/tools/google_workspace_service_integrations.py` | Send a Google Chat message to a space. |
| `google_chat_update_message` | `nymeria/tools/google_workspace_service_integrations.py` | Update a Google Chat message. |
| `google_contacts_create_contact` | `nymeria/tools/google_workspace_service_integrations.py` | Create a Google Contacts contact. |
| `google_contacts_delete_contact` | `nymeria/tools/google_workspace_service_integrations.py` | Delete a Google Contacts contact. |
| `google_contacts_get_contact` | `nymeria/tools/google_workspace_service_integrations.py` | Get one Google Contacts person by contact ID or people/* resource name. |
| `google_contacts_list_contacts` | `nymeria/tools/google_workspace_service_integrations.py` | List or search Google Contacts. |
| `google_contacts_update_contact` | `nymeria/tools/google_workspace_service_integrations.py` | Update a Google Contacts contact by replacing supplied top-level fields. |
| `google_docs_append_text` | `nymeria/tools/google_docs.py` | Append plain text to the end of a Google Docs document. |
| `google_docs_apply_text_style` | `nymeria/tools/google_docs.py` | Apply text styling to a range of text in a Google Docs document. |
| `google_docs_create` | `nymeria/tools/google_docs.py` | Create a new Google Docs document. |
| `google_docs_delete` | `nymeria/tools/google_docs.py` | Delete a Google Docs document from Google Drive. |
| `google_docs_delete_range` | `nymeria/tools/google_docs.py` | Delete a range of content from a Google Docs document. |
| `google_docs_find_index` | `nymeria/tools/google_docs.py` | Find the document indices of a text string in a Google Docs document. |
| `google_docs_insert_page_break` | `nymeria/tools/google_docs.py` | Insert a page break into a Google Docs document. |
| `google_docs_insert_table` | `nymeria/tools/google_docs.py` | Insert an empty table into a Google Docs document. |
| `google_docs_insert_text` | `nymeria/tools/google_docs.py` | Insert text at a specific position in a Google Docs document. |
| `google_docs_list` | `nymeria/tools/google_docs.py` | List or search Google Docs documents in your Drive. |
| `google_docs_read` | `nymeria/tools/google_docs.py` | Read a Google Docs document. |
| `google_docs_replace_text` | `nymeria/tools/google_docs.py` | Find and replace all occurrences of text in a Google Docs document. |
| `google_docs_table_append_row` | `nymeria/tools/google_docs.py` | Append a new row to the end of an existing table in a Google Docs document. |
| `google_docs_table_update_cell` | `nymeria/tools/google_docs.py` | Update the content of a specific cell in an existing table. |
| `google_docs_update_paragraph_style` | `nymeria/tools/google_docs.py` | Update paragraph styling for a range of text in a Google Docs document. |
| `google_docs_write` | `nymeria/tools/google_docs.py` | Write markdown-formatted content to a Google Docs document. |
| `google_docs_write_table` | `nymeria/tools/google_docs.py` | Create a populated table in a Google Docs document in one step. |
| `google_drive_create_folder` | `nymeria/tools/google_workspace_service_integrations.py` | Create a Google Drive folder. |
| `google_drive_download_text` | `nymeria/tools/google_workspace_service_integrations.py` | Download or export a Google Drive file as text. |
| `google_drive_get_file` | `nymeria/tools/google_workspace_service_integrations.py` | Get Google Drive file metadata. |
| `google_drive_search_files` | `nymeria/tools/google_workspace_service_integrations.py` | Search Google Drive files and folders. |
| `google_drive_trash_file` | `nymeria/tools/google_workspace_service_integrations.py` | Move a Google Drive file to or from trash. |
| `google_drive_upload_text_file` | `nymeria/tools/google_workspace_service_integrations.py` | Upload a UTF-8 text file to Google Drive. |
| `google_sheets_append` | `nymeria/tools/google_sheets.py` | Append one or more rows to a Google Sheet. |
| `google_sheets_search` | `nymeria/tools/google_sheets.py` | Search a Google Sheet for rows matching a query. |
| `google_sheets_update` | `nymeria/tools/google_sheets.py` | Update cells in an existing row by finding it first via a search value. |
| `google_slides_batch_update` | `nymeria/tools/google_workspace_service_integrations.py` | Run a Google Slides batchUpdate request for advanced presentation edits. |
| `google_slides_create_presentation` | `nymeria/tools/google_workspace_service_integrations.py` | Create a Google Slides presentation. |
| `google_slides_create_slide` | `nymeria/tools/google_workspace_service_integrations.py` | Create a slide in a Google Slides presentation. |
| `google_slides_get_page_thumbnail` | `nymeria/tools/google_workspace_service_integrations.py` | Get a temporary thumbnail URL for a Google Slides page. |
| `google_slides_get_presentation` | `nymeria/tools/google_workspace_service_integrations.py` | Get Google Slides presentation metadata and content. |
| `google_slides_list_slides` | `nymeria/tools/google_workspace_service_integrations.py` | List slides in a Google Slides presentation with optional text summaries. |
| `google_slides_replace_text` | `nymeria/tools/google_workspace_service_integrations.py` | Replace matching text across a Google Slides presentation or selected pages. |
| `google_tasks_complete_task` | `nymeria/tools/google_workspace_service_integrations.py` | Mark a Google Tasks task complete. |
| `google_tasks_create_task` | `nymeria/tools/google_workspace_service_integrations.py` | Create a Google Tasks task. |
| `google_tasks_delete_task` | `nymeria/tools/google_workspace_service_integrations.py` | Delete a Google Tasks task. |
| `google_tasks_get_task` | `nymeria/tools/google_workspace_service_integrations.py` | Get one Google Tasks task. |
| `google_tasks_list_tasklists` | `nymeria/tools/google_workspace_service_integrations.py` | List Google Tasks task lists. |
| `google_tasks_list_tasks` | `nymeria/tools/google_workspace_service_integrations.py` | List tasks from a Google Tasks task list. |
| `google_tasks_update_task` | `nymeria/tools/google_workspace_service_integrations.py` | Patch a Google Tasks task with an API-shaped JSON object. |
| `gotify_delete_message` | `nymeria/tools/notification_service_integrations.py` | Delete a Gotify message by ID with a client token. |
| `gotify_list_messages` | `nymeria/tools/notification_service_integrations.py` | List Gotify messages with a client token. |
| `gotify_send_message` | `nymeria/tools/notification_service_integrations.py` | Send a Gotify message with an application token. |
| `gotowebinar_create_registrant` | `nymeria/tools/event_meeting_service_integrations.py` | Create a GoToWebinar registrant. |
| `gotowebinar_create_webinar` | `nymeria/tools/event_meeting_service_integrations.py` | Create a GoToWebinar webinar. |
| `gotowebinar_delete_registrant` | `nymeria/tools/event_meeting_service_integrations.py` | Delete a GoToWebinar registrant. |
| `gotowebinar_get_registrant` | `nymeria/tools/event_meeting_service_integrations.py` | Get a GoToWebinar registrant by key. |
| `gotowebinar_get_session` | `nymeria/tools/event_meeting_service_integrations.py` | Get a GoToWebinar session by key. |
| `gotowebinar_get_webinar` | `nymeria/tools/event_meeting_service_integrations.py` | Get a GoToWebinar webinar by key. |
| `gotowebinar_list_registrants` | `nymeria/tools/event_meeting_service_integrations.py` | List registrants for a GoToWebinar webinar. |
| `gotowebinar_list_sessions` | `nymeria/tools/event_meeting_service_integrations.py` | List GoToWebinar sessions. |
| `gotowebinar_list_webinars` | `nymeria/tools/event_meeting_service_integrations.py` | List GoToWebinar webinars for the configured account. |
| `gotowebinar_update_webinar` | `nymeria/tools/event_meeting_service_integrations.py` | Update a GoToWebinar webinar. |
| `grafana_create_dashboard` | `nymeria/tools/operations_monitoring_service_integrations.py` | Create or update a Grafana dashboard. |
| `grafana_delete_dashboard` | `nymeria/tools/operations_monitoring_service_integrations.py` | Delete a Grafana dashboard by UID. |
| `grafana_get_dashboard` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Grafana dashboard by UID. |
| `grafana_list_teams` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Grafana teams. |
| `grafana_search_dashboards` | `nymeria/tools/operations_monitoring_service_integrations.py` | Search Grafana dashboards. |
| `graphql_execute_query` | `nymeria/tools/developer_platform_integrations.py` | Execute a GraphQL query or mutation over HTTP POST. |
| `grist_create_record` | `nymeria/tools/data_table_service_integrations.py` | Create a Grist record from a JSON field mapping. |
| `grist_delete_records` | `nymeria/tools/data_table_service_integrations.py` | Delete one or more Grist records. |
| `grist_list_columns` | `nymeria/tools/data_table_service_integrations.py` | List columns in a Grist table. |
| `grist_list_docs` | `nymeria/tools/data_table_service_integrations.py` | List Grist docs in a workspace. |
| `grist_list_orgs` | `nymeria/tools/data_table_service_integrations.py` | List Grist organizations available to the credential. |
| `grist_list_records` | `nymeria/tools/data_table_service_integrations.py` | List records from a Grist table. |
| `grist_list_tables` | `nymeria/tools/data_table_service_integrations.py` | List tables in a Grist doc. |
| `grist_list_workspaces` | `nymeria/tools/data_table_service_integrations.py` | List Grist workspaces in an organization. |
| `grist_update_record` | `nymeria/tools/data_table_service_integrations.py` | Update a Grist record from a JSON field mapping. |
| `hackernews_get_item` | `nymeria/tools/public_info_integrations.py` | Get a Hacker News item by ID. |
| `hackernews_get_user` | `nymeria/tools/public_info_integrations.py` | Get a Hacker News user profile. |
| `hackernews_search` | `nymeria/tools/public_info_integrations.py` | Search Hacker News via the Algolia HN API. |
| `harness_report` | `nymeria/tools/harness_report.py` | Report a Nymeria platform issue or pain point to the developer's backlog intake. |
| `harvest_create_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Create a Harvest time entry by duration. |
| `harvest_delete_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Delete a Harvest time entry. |
| `harvest_get_company` | `nymeria/tools/time_hr_service_integrations.py` | Get the Harvest account company profile. |
| `harvest_get_me` | `nymeria/tools/time_hr_service_integrations.py` | Get the authenticated Harvest user. |
| `harvest_list_clients` | `nymeria/tools/time_hr_service_integrations.py` | List Harvest clients. |
| `harvest_list_projects` | `nymeria/tools/time_hr_service_integrations.py` | List Harvest projects. |
| `harvest_list_tasks` | `nymeria/tools/time_hr_service_integrations.py` | List Harvest tasks. |
| `harvest_list_time_entries` | `nymeria/tools/time_hr_service_integrations.py` | List Harvest time entries. |
| `harvest_stop_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Stop a running Harvest time entry. |
| `harvest_update_time_entry` | `nymeria/tools/time_hr_service_integrations.py` | Update a Harvest time entry from a JSON object. |
| `hello_test` | `nymeria/tools/hello_test.py` | A basic test tool. Returns a greeting with some system info. |
| `helpscout_create_conversation` | `nymeria/tools/support_service_integrations.py` | Create a Help Scout conversation with an initial customer thread. |
| `helpscout_create_customer` | `nymeria/tools/support_service_integrations.py` | Create a Help Scout customer. |
| `helpscout_create_thread` | `nymeria/tools/support_service_integrations.py` | Create a Help Scout conversation thread. |
| `helpscout_get_conversation` | `nymeria/tools/support_service_integrations.py` | Get a Help Scout conversation by ID. |
| `helpscout_get_customer` | `nymeria/tools/support_service_integrations.py` | Get a Help Scout customer by ID. |
| `helpscout_get_mailbox` | `nymeria/tools/support_service_integrations.py` | Get a Help Scout mailbox by ID. |
| `helpscout_list_conversations` | `nymeria/tools/support_service_integrations.py` | List and filter Help Scout conversations. |
| `helpscout_list_customers` | `nymeria/tools/support_service_integrations.py` | List Help Scout customers. |
| `helpscout_list_mailboxes` | `nymeria/tools/support_service_integrations.py` | List Help Scout mailboxes. |
| `helpscout_update_customer` | `nymeria/tools/support_service_integrations.py` | Update a Help Scout customer. |
| `homeassistant_call_service` | `nymeria/tools/personal_device_service_integrations.py` | Call a Home Assistant service. |
| `homeassistant_check_config` | `nymeria/tools/personal_device_service_integrations.py` | Run Home Assistant core configuration checks. |
| `homeassistant_fire_event` | `nymeria/tools/personal_device_service_integrations.py` | Fire a Home Assistant event. |
| `homeassistant_get_config` | `nymeria/tools/personal_device_service_integrations.py` | Get Home Assistant configuration metadata. |
| `homeassistant_get_logbook` | `nymeria/tools/personal_device_service_integrations.py` | Get Home Assistant logbook entries. |
| `homeassistant_get_state` | `nymeria/tools/personal_device_service_integrations.py` | Get a Home Assistant entity state. |
| `homeassistant_list_events` | `nymeria/tools/personal_device_service_integrations.py` | List Home Assistant event types. |
| `homeassistant_list_services` | `nymeria/tools/personal_device_service_integrations.py` | List Home Assistant service domains. |
| `homeassistant_list_states` | `nymeria/tools/personal_device_service_integrations.py` | List Home Assistant entity states. |
| `homeassistant_render_template` | `nymeria/tools/personal_device_service_integrations.py` | Render a Home Assistant template. |
| `homeassistant_set_state` | `nymeria/tools/personal_device_service_integrations.py` | Create or update a Home Assistant entity state. |
| `hook_config` | `nymeria/tools/hooks.py` | Create, update, delete, or install a lifecycle hook. |
| `hook_info` | `nymeria/tools/hooks.py` | List, inspect, or debug lifecycle hooks. |
| `http_request` | `nymeria/tools/http_api.py` | Make a one-off HTTP request to a documented API endpoint. |
| `hubspot_archive_crm_object` | `nymeria/tools/customer_engagement_service_integrations.py` | Archive/delete a HubSpot CRM object. |
| `hubspot_create_crm_object` | `nymeria/tools/customer_engagement_service_integrations.py` | Create a HubSpot CRM object. |
| `hubspot_get_crm_object` | `nymeria/tools/customer_engagement_service_integrations.py` | Get a HubSpot CRM object by ID or a custom unique property. |
| `hubspot_list_crm_objects` | `nymeria/tools/customer_engagement_service_integrations.py` | List HubSpot CRM objects such as contacts, companies, deals, or tickets. |
| `hubspot_search_crm_objects` | `nymeria/tools/customer_engagement_service_integrations.py` | Search HubSpot CRM objects. |
| `hubspot_update_crm_object` | `nymeria/tools/customer_engagement_service_integrations.py` | Update a HubSpot CRM object. |
| `humantic_create_profile` | `nymeria/tools/lead_enrichment_service_integrations.py` | Create a Humantic AI profile from a LinkedIn URL, email, or unique user ID. |
| `humantic_get_profile` | `nymeria/tools/lead_enrichment_service_integrations.py` | Get a Humantic AI profile. |
| `humantic_update_profile_text` | `nymeria/tools/lead_enrichment_service_integrations.py` | Update a Humantic AI profile with additional text. |
| `hunter_domain_search` | `nymeria/tools/enrichment_security_service_integrations.py` | Find email addresses associated with a domain through Hunter. |
| `hunter_email_finder` | `nymeria/tools/enrichment_security_service_integrations.py` | Find a likely professional email address from name and domain. |
| `hunter_email_verifier` | `nymeria/tools/enrichment_security_service_integrations.py` | Verify deliverability details for an email address through Hunter. |
| `image_gen_fal` | `nymeria/tools/image_gen_integrations.py` | Generate an image cheaply via fal.ai (Z-Image Turbo or FLUX.1 schnell), a fast low-cost budget option. |
| `image_gen_flux` | `nymeria/tools/image_gen_integrations.py` | Generate an image with Black Forest Labs FLUX.2, a flagship photorealism and multi-reference model. |
| `image_gen_gemini` | `nymeria/tools/image_gen_integrations.py` | Generate an image with Google Gemini "Nano Banana Pro" (gemini-3-pro-image). |
| `image_gen_openai` | `nymeria/tools/image_gen_integrations.py` | Generate an image with OpenAI GPT Image (gpt-image-2), the current top-ranked text-to-image model. |
| `image_gen_replicate` | `nymeria/tools/image_gen_integrations.py` | Generate an image cheaply via Replicate (FLUX.1 schnell or Z-Image Turbo), a low-cost budget option. |
| `install_mcp_server` | `nymeria/tools/search_mcp.py` | Compatibility wrapper for installing one MCP server. |
| `install_skill` | `nymeria/tools/search_skills.py` | Install an Agent Skill from a marketplace onto disk. |
| `intercom_archive_contact` | `nymeria/tools/support_service_integrations.py` | Archive an Intercom contact. |
| `intercom_create_contact` | `nymeria/tools/support_service_integrations.py` | Create an Intercom contact. |
| `intercom_get_contact` | `nymeria/tools/support_service_integrations.py` | Get an Intercom contact by ID. |
| `intercom_get_conversation` | `nymeria/tools/support_service_integrations.py` | Get an Intercom conversation by ID. |
| `intercom_list_contacts` | `nymeria/tools/support_service_integrations.py` | List Intercom contacts. |
| `intercom_list_conversations` | `nymeria/tools/support_service_integrations.py` | List Intercom conversations. |
| `intercom_reply_conversation` | `nymeria/tools/support_service_integrations.py` | Reply to an Intercom conversation as an admin or contact. |
| `intercom_search_contacts` | `nymeria/tools/support_service_integrations.py` | Search Intercom contacts with Intercom's JSON query DSL. |
| `intercom_update_contact` | `nymeria/tools/support_service_integrations.py` | Update an Intercom contact. |
| `invoiceninja_create_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Create an Invoice Ninja record. |
| `invoiceninja_delete_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Delete an Invoice Ninja record. |
| `invoiceninja_email_invoice_or_quote` | `nymeria/tools/enterprise_business_service_integrations.py` | Email an Invoice Ninja invoice or quote. |
| `invoiceninja_get_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Get an Invoice Ninja record by ID. |
| `invoiceninja_list_records` | `nymeria/tools/enterprise_business_service_integrations.py` | List Invoice Ninja records. |
| `iterable_get_user` | `nymeria/tools/marketing_contact_service_integrations.py` | Get an Iterable user by email or user ID. |
| `iterable_list_lists` | `nymeria/tools/marketing_contact_service_integrations.py` | List Iterable static lists. |
| `iterable_track_event` | `nymeria/tools/marketing_contact_service_integrations.py` | Track an Iterable event. |
| `iterable_update_list_subscribers` | `nymeria/tools/marketing_contact_service_integrations.py` | Subscribe or unsubscribe Iterable users by email or user ID. |
| `iterable_upsert_user` | `nymeria/tools/marketing_contact_service_integrations.py` | Create or update an Iterable user. |
| `jenkins_cancel_quiet_down` | `nymeria/tools/build_ci_service_integrations.py` | Cancel Jenkins quiet-down mode. |
| `jenkins_copy_job` | `nymeria/tools/build_ci_service_integrations.py` | Copy a Jenkins job to a new job name. |
| `jenkins_create_job` | `nymeria/tools/build_ci_service_integrations.py` | Create a Jenkins job from XML config. |
| `jenkins_get_instance` | `nymeria/tools/build_ci_service_integrations.py` | Get Jenkins instance metadata. |
| `jenkins_list_job_builds` | `nymeria/tools/build_ci_service_integrations.py` | List builds for one Jenkins job. |
| `jenkins_list_jobs` | `nymeria/tools/build_ci_service_integrations.py` | List Jenkins jobs with basic build status. |
| `jenkins_quiet_down` | `nymeria/tools/build_ci_service_integrations.py` | Put Jenkins into quiet-down mode. |
| `jenkins_restart_instance` | `nymeria/tools/build_ci_service_integrations.py` | Restart a Jenkins instance. |
| `jenkins_shutdown_instance` | `nymeria/tools/build_ci_service_integrations.py` | Shut down a Jenkins instance. |
| `jenkins_trigger_job` | `nymeria/tools/build_ci_service_integrations.py` | Trigger a Jenkins job build. |
| `jenkins_trigger_job_with_parameters` | `nymeria/tools/build_ci_service_integrations.py` | Trigger a parameterized Jenkins job build. |
| `jina_deep_research` | `nymeria/tools/enrichment_security_service_integrations.py` | Run a Jina DeepSearch research query. |
| `jina_reader_fetch_url` | `nymeria/tools/enrichment_security_service_integrations.py` | Fetch a URL through Jina Reader and return LLM-friendly content. |
| `jina_search_web` | `nymeria/tools/enrichment_security_service_integrations.py` | Search the web through Jina Search and return LLM-friendly results. |
| `jira_add_issue_comment` | `nymeria/tools/project_management_service_integrations.py` | Add a plain-text comment to a Jira issue. |
| `jira_create_issue` | `nymeria/tools/project_management_service_integrations.py` | Create a Jira issue. |
| `jira_get_issue` | `nymeria/tools/project_management_service_integrations.py` | Get a Jira issue by key or ID. |
| `jira_get_myself` | `nymeria/tools/project_management_service_integrations.py` | Get the current Jira user for the configured credential. |
| `jira_list_issue_comments` | `nymeria/tools/project_management_service_integrations.py` | List comments on a Jira issue. |
| `jira_list_issue_transitions` | `nymeria/tools/project_management_service_integrations.py` | List available Jira transitions for an issue. |
| `jira_list_projects` | `nymeria/tools/project_management_service_integrations.py` | List Jira projects visible to the credential. |
| `jira_list_users` | `nymeria/tools/project_management_service_integrations.py` | Search Jira users. |
| `jira_search_issues` | `nymeria/tools/project_management_service_integrations.py` | Search Jira issues with JQL. |
| `jira_update_issue` | `nymeria/tools/project_management_service_integrations.py` | Update a Jira issue and optionally transition status. |
| `jwt_decode_token` | `nymeria/tools/transform_utility_integrations.py` | Decode a JWT without verifying its signature. |
| `jwt_sign_claims` | `nymeria/tools/transform_utility_integrations.py` | Sign JWT claims using saved JWT credentials. |
| `jwt_verify_token` | `nymeria/tools/transform_utility_integrations.py` | Verify a JWT with saved JWT credentials. |
| `keap_apply_tags` | `nymeria/tools/relationship_crm_service_integrations.py` | Apply one or more Keap tags to a contact. |
| `keap_create_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Create a Keap record from an API-shaped JSON object; contacts are upserted. |
| `keap_delete_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Delete a Keap record by ID. |
| `keap_get_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Get one Keap record by ID. |
| `keap_list_contact_tags` | `nymeria/tools/relationship_crm_service_integrations.py` | List tags applied to a Keap contact. |
| `keap_list_records` | `nymeria/tools/relationship_crm_service_integrations.py` | List Keap records such as companies, contacts, notes, orders, products, emails, files, tags, or users. |
| `keap_remove_tags` | `nymeria/tools/relationship_crm_service_integrations.py` | Remove one or more Keap tags from a contact. |
| `keap_send_email` | `nymeria/tools/relationship_crm_service_integrations.py` | Queue an email through Keap for one or more contacts. |
| `keap_update_note` | `nymeria/tools/relationship_crm_service_integrations.py` | Update a Keap contact note from a JSON object. |
| `kobotoolbox_create_file_from_url` | `nymeria/tools/data_table_service_integrations.py` | Create a KoBoToolbox form media file that redirects to a URL. |
| `kobotoolbox_delete_file` | `nymeria/tools/data_table_service_integrations.py` | Delete a KoBoToolbox form media file. |
| `kobotoolbox_delete_submission` | `nymeria/tools/data_table_service_integrations.py` | Delete a KoBoToolbox submission. |
| `kobotoolbox_get_file` | `nymeria/tools/data_table_service_integrations.py` | Get KoBoToolbox form media file metadata. |
| `kobotoolbox_get_form` | `nymeria/tools/data_table_service_integrations.py` | Get a KoBoToolbox form/asset by UID. |
| `kobotoolbox_get_hook` | `nymeria/tools/data_table_service_integrations.py` | Get a KoBoToolbox REST service hook. |
| `kobotoolbox_get_hook_logs` | `nymeria/tools/data_table_service_integrations.py` | Get KoBoToolbox REST service hook logs. |
| `kobotoolbox_get_submission` | `nymeria/tools/data_table_service_integrations.py` | Get a KoBoToolbox submission by ID. |
| `kobotoolbox_get_submission_validation` | `nymeria/tools/data_table_service_integrations.py` | Get validation status for a KoBoToolbox submission. |
| `kobotoolbox_list_files` | `nymeria/tools/data_table_service_integrations.py` | List KoBoToolbox form media files. |
| `kobotoolbox_list_forms` | `nymeria/tools/data_table_service_integrations.py` | List KoBoToolbox forms/assets. |
| `kobotoolbox_list_hooks` | `nymeria/tools/data_table_service_integrations.py` | List KoBoToolbox REST service hooks for a form. |
| `kobotoolbox_list_submissions` | `nymeria/tools/data_table_service_integrations.py` | List KoBoToolbox submissions for a form. |
| `kobotoolbox_redeploy_form` | `nymeria/tools/data_table_service_integrations.py` | Redeploy a KoBoToolbox form. |
| `kobotoolbox_retry_hook` | `nymeria/tools/data_table_service_integrations.py` | Retry all or one KoBoToolbox REST service hook delivery. |
| `kobotoolbox_set_submission_validation` | `nymeria/tools/data_table_service_integrations.py` | Set validation status for a KoBoToolbox submission. |
| `lemlist_create_lead` | `nymeria/tools/marketing_contact_service_integrations.py` | Create or update a Lemlist campaign lead. |
| `lemlist_get_campaign_stats` | `nymeria/tools/marketing_contact_service_integrations.py` | Get Lemlist campaign stats. |
| `lemlist_get_lead` | `nymeria/tools/marketing_contact_service_integrations.py` | Get a Lemlist lead by email. |
| `lemlist_get_team` | `nymeria/tools/marketing_contact_service_integrations.py` | Get Lemlist team metadata. |
| `lemlist_get_team_credits` | `nymeria/tools/marketing_contact_service_integrations.py` | Get Lemlist team credit balances. |
| `lemlist_list_activities` | `nymeria/tools/marketing_contact_service_integrations.py` | List Lemlist activities. |
| `lemlist_list_campaigns` | `nymeria/tools/marketing_contact_service_integrations.py` | List Lemlist campaigns. |
| `lemlist_list_unsubscribes` | `nymeria/tools/marketing_contact_service_integrations.py` | List Lemlist global unsubscribes. |
| `lemlist_remove_lead` | `nymeria/tools/marketing_contact_service_integrations.py` | Remove or unsubscribe a Lemlist lead from a campaign. |
| `lemlist_update_unsubscribe` | `nymeria/tools/marketing_contact_service_integrations.py` | Add or remove a Lemlist global unsubscribe. |
| `linear_add_issue_comment` | `nymeria/tools/work_tracking_service_integrations.py` | Add a comment to a Linear issue. |
| `linear_add_issue_link` | `nymeria/tools/work_tracking_service_integrations.py` | Attach a URL link to a Linear issue. |
| `linear_create_issue` | `nymeria/tools/work_tracking_service_integrations.py` | Create a Linear issue. |
| `linear_get_issue` | `nymeria/tools/work_tracking_service_integrations.py` | Get a Linear issue by ID or identifier. |
| `linear_list_issues` | `nymeria/tools/work_tracking_service_integrations.py` | List Linear issues with optional filters. |
| `linear_list_teams` | `nymeria/tools/work_tracking_service_integrations.py` | List Linear teams. |
| `linear_list_users` | `nymeria/tools/work_tracking_service_integrations.py` | List Linear users. |
| `linear_list_workflow_states` | `nymeria/tools/work_tracking_service_integrations.py` | List Linear workflow states, optionally filtered by team. |
| `linear_update_issue` | `nymeria/tools/work_tracking_service_integrations.py` | Update a Linear issue. |
| `lingvanex_list_languages` | `nymeria/tools/business_service_integrations.py` | List LingvaNex supported languages. |
| `lingvanex_translate_text` | `nymeria/tools/business_service_integrations.py` | Translate text with LingvaNex. |
| `linkedin_create_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create a LinkedIn text or article post. |
| `linkedin_get_me` | `nymeria/tools/community_publishing_service_integrations.py` | Get the authenticated LinkedIn member profile. |
| `list_installed_skills` | `nymeria/tools/search_skills.py` | List all Agent Skills currently installed and visible to the user. |
| `lonescale_add_company_item` | `nymeria/tools/lead_enrichment_service_integrations.py` | Add a company item to a LoneScale list. |
| `lonescale_add_people_item` | `nymeria/tools/lead_enrichment_service_integrations.py` | Add a person item to a LoneScale list. |
| `lonescale_create_list` | `nymeria/tools/lead_enrichment_service_integrations.py` | Create a LoneScale list. |
| `magento_cancel_order` | `nymeria/tools/commerce_billing_service_integrations.py` | Cancel a Magento order. |
| `magento_create_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Magento customer. |
| `magento_create_invoice` | `nymeria/tools/commerce_billing_service_integrations.py` | Create an invoice for a Magento order. |
| `magento_create_product` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Magento product. |
| `magento_delete_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Delete a Magento customer or product. |
| `magento_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get one Magento customer, order, or product by ID or SKU. |
| `magento_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List Magento customers, orders, or products with optional searchCriteria JSON. |
| `magento_ship_order` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a shipment for a Magento order. |
| `magento_update_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Magento customer. |
| `magento_update_product` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Magento product by SKU. |
| `mailcheck_check_email` | `nymeria/tools/enrichment_security_service_integrations.py` | Check an email address with Mailcheck. |
| `mailchimp_add_or_update_member` | `nymeria/tools/customer_engagement_service_integrations.py` | Add or update a Mailchimp audience member. |
| `mailchimp_get_member` | `nymeria/tools/customer_engagement_service_integrations.py` | Get a Mailchimp audience member by email or subscriber hash. |
| `mailchimp_list_audiences` | `nymeria/tools/customer_engagement_service_integrations.py` | List Mailchimp audiences/lists. |
| `mailchimp_list_campaigns` | `nymeria/tools/customer_engagement_service_integrations.py` | List Mailchimp campaigns. |
| `mailchimp_list_members` | `nymeria/tools/customer_engagement_service_integrations.py` | List Mailchimp audience members. |
| `mailchimp_update_member_tags` | `nymeria/tools/customer_engagement_service_integrations.py` | Add or remove Mailchimp member tags. |
| `mailerlite_create_subscriber` | `nymeria/tools/marketing_contact_service_integrations.py` | Create a MailerLite subscriber. |
| `mailerlite_get_subscriber` | `nymeria/tools/marketing_contact_service_integrations.py` | Get one MailerLite subscriber by ID or email. |
| `mailerlite_list_groups` | `nymeria/tools/marketing_contact_service_integrations.py` | List MailerLite groups. |
| `mailerlite_list_subscribers` | `nymeria/tools/marketing_contact_service_integrations.py` | List MailerLite subscribers. |
| `mailerlite_update_subscriber` | `nymeria/tools/marketing_contact_service_integrations.py` | Update a MailerLite subscriber with a JSON object of fields. |
| `mailgun_get_domain` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get Mailgun sending domain metadata for the configured domain. |
| `mailgun_list_events` | `nymeria/tools/messaging_delivery_service_integrations.py` | List Mailgun events for the configured sending domain. |
| `mailgun_send_email` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an email with Mailgun. |
| `mailjet_get_contact` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get a Mailjet contact by ID or email. |
| `mailjet_list_contacts` | `nymeria/tools/messaging_delivery_service_integrations.py` | List Mailjet contacts. |
| `mailjet_send_email` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an email with Mailjet. |
| `mailjet_send_sms` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS with Mailjet. |
| `mandrill_send_email` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an email with Mandrill / Mailchimp Transactional. |
| `mandrill_send_template` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an email with a Mandrill template. |
| `marketstack_get_eod` | `nymeria/tools/business_service_integrations.py` | Get Marketstack end-of-day stock market data. |
| `marketstack_get_exchange` | `nymeria/tools/business_service_integrations.py` | Get Marketstack exchange metadata. |
| `marketstack_get_ticker` | `nymeria/tools/business_service_integrations.py` | Get Marketstack ticker metadata. |
| `mautic_add_contact_to_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Add a Mautic contact to a campaign. |
| `mautic_add_contact_to_company` | `nymeria/tools/marketing_contact_service_integrations.py` | Add a Mautic contact to a company. |
| `mautic_add_contact_to_segment` | `nymeria/tools/marketing_contact_service_integrations.py` | Add a Mautic contact to a segment. |
| `mautic_create_company` | `nymeria/tools/marketing_contact_service_integrations.py` | Create a Mautic company. |
| `mautic_create_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Create a Mautic contact. |
| `mautic_delete_company` | `nymeria/tools/marketing_contact_service_integrations.py` | Delete a Mautic company by ID. |
| `mautic_delete_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Delete a Mautic contact by ID. |
| `mautic_get_company` | `nymeria/tools/marketing_contact_service_integrations.py` | Get a Mautic company by ID. |
| `mautic_get_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Get a Mautic contact by ID. |
| `mautic_list_companies` | `nymeria/tools/marketing_contact_service_integrations.py` | List Mautic companies. |
| `mautic_list_contacts` | `nymeria/tools/marketing_contact_service_integrations.py` | List Mautic contacts. |
| `mautic_remove_contact_from_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Remove a Mautic contact from a campaign. |
| `mautic_remove_contact_from_company` | `nymeria/tools/marketing_contact_service_integrations.py` | Remove a Mautic contact from a company. |
| `mautic_remove_contact_from_segment` | `nymeria/tools/marketing_contact_service_integrations.py` | Remove a Mautic contact from a segment. |
| `mautic_send_email_to_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Send a Mautic campaign/template email to a contact. |
| `mautic_send_segment_email` | `nymeria/tools/marketing_contact_service_integrations.py` | Send a Mautic segment/list email. |
| `mautic_update_company` | `nymeria/tools/marketing_contact_service_integrations.py` | Update a Mautic company with a JSON object of Mautic field aliases. |
| `mautic_update_contact` | `nymeria/tools/marketing_contact_service_integrations.py` | Update a Mautic contact with a JSON object of Mautic field aliases. |
| `mcp_manage` | `nymeria/tools/search_mcp.py` | Search, preview, install, and inspect MCP servers. |
| `medium_create_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create a Medium post on the authenticated user's profile. |
| `medium_create_publication_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create a Medium post under a publication. |
| `medium_get_me` | `nymeria/tools/community_publishing_service_integrations.py` | Get the authenticated Medium user profile. |
| `medium_list_publications` | `nymeria/tools/community_publishing_service_integrations.py` | List Medium publications associated with a user. |
| `memory_add` | `nymeria/tools/memory.py` | Add to memory: append to the thread notepad, or create/set one global key. |
| `memory_clear_all` | `nymeria/tools/memory.py` | Clear ALL global memories for this user. |
| `memory_edit` | `nymeria/tools/memory.py` | Edit existing memory: find/replace a substring (first exact match only); |
| `memory_read` | `nymeria/tools/memory.py` | Read memory. Get a specific entry, list everything, or substring-filter. |
| `messagebird_get_balance` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get MessageBird account balance. |
| `messagebird_send_sms` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS with MessageBird. |
| `metabase_get_dashboard` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Metabase dashboard by ID. |
| `metabase_get_question` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Metabase question/card by ID. |
| `metabase_list_dashboards` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Metabase dashboards. |
| `metabase_list_questions` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Metabase questions/cards. |
| `metabase_query_question` | `nymeria/tools/operations_monitoring_service_integrations.py` | Run a Metabase question/card and return JSON results. |
| `microsoft_excel_add_table_row` | `nymeria/tools/microsoft_graph_service_integrations.py` | Append rows to an Excel workbook table from a JSON two-dimensional values array. |
| `microsoft_excel_get_used_range` | `nymeria/tools/microsoft_graph_service_integrations.py` | Get the used range from an Excel worksheet. |
| `microsoft_excel_list_tables` | `nymeria/tools/microsoft_graph_service_integrations.py` | List tables in an Excel workbook. |
| `microsoft_excel_list_worksheets` | `nymeria/tools/microsoft_graph_service_integrations.py` | List worksheets in an Excel workbook stored in OneDrive or SharePoint. |
| `microsoft_excel_read_range` | `nymeria/tools/microsoft_graph_service_integrations.py` | Read an Excel worksheet range such as A1:D20. |
| `microsoft_excel_update_range` | `nymeria/tools/microsoft_graph_service_integrations.py` | Update an Excel worksheet range with a JSON two-dimensional values array. |
| `microsoft_onedrive_get_item` | `nymeria/tools/microsoft_graph_service_integrations.py` | Get OneDrive file or folder metadata by path, item ID, or drive item. |
| `microsoft_onedrive_list_children` | `nymeria/tools/microsoft_graph_service_integrations.py` | List OneDrive child files and folders by path, item ID, or drive item. |
| `microsoft_onedrive_search` | `nymeria/tools/microsoft_graph_service_integrations.py` | Search OneDrive files and folders. |
| `microsoft_onedrive_upload_text_file` | `nymeria/tools/microsoft_graph_service_integrations.py` | Upload or replace a UTF-8 text file in OneDrive. |
| `microsoft_sharepoint_create_item` | `nymeria/tools/microsoft_graph_service_integrations.py` | Create a SharePoint list item from a JSON object of fields. |
| `microsoft_sharepoint_delete_item` | `nymeria/tools/microsoft_graph_service_integrations.py` | Delete a SharePoint list item. |
| `microsoft_sharepoint_get_item` | `nymeria/tools/microsoft_graph_service_integrations.py` | Get one SharePoint list item. |
| `microsoft_sharepoint_get_site` | `nymeria/tools/microsoft_graph_service_integrations.py` | Get SharePoint site metadata by site ID or hostname plus site path. |
| `microsoft_sharepoint_list_items` | `nymeria/tools/microsoft_graph_service_integrations.py` | List SharePoint list items, optionally expanding fields. |
| `microsoft_sharepoint_list_lists` | `nymeria/tools/microsoft_graph_service_integrations.py` | List SharePoint lists in a site. |
| `microsoft_sharepoint_search_sites` | `nymeria/tools/microsoft_graph_service_integrations.py` | Search SharePoint sites visible to the signed-in user. |
| `microsoft_sharepoint_update_item_fields` | `nymeria/tools/microsoft_graph_service_integrations.py` | Update SharePoint list item fields from a JSON object. |
| `microsoft_teams_list_channel_messages` | `nymeria/tools/microsoft_graph_service_integrations.py` | List recent messages from a Microsoft Teams channel. |
| `microsoft_teams_list_channels` | `nymeria/tools/microsoft_graph_service_integrations.py` | List channels in a Microsoft Teams team. |
| `microsoft_teams_list_joined_teams` | `nymeria/tools/microsoft_graph_service_integrations.py` | List Microsoft Teams teams joined by the signed-in user. |
| `microsoft_teams_send_channel_message` | `nymeria/tools/microsoft_graph_service_integrations.py` | Send a Microsoft Teams channel message. |
| `microsoft_todo_create_task` | `nymeria/tools/microsoft_graph_service_integrations.py` | Create a Microsoft To Do task. |
| `microsoft_todo_list_task_lists` | `nymeria/tools/microsoft_graph_service_integrations.py` | List Microsoft To Do task lists for the signed-in user. |
| `microsoft_todo_list_tasks` | `nymeria/tools/microsoft_graph_service_integrations.py` | List tasks from a Microsoft To Do list. |
| `microsoft_todo_update_task` | `nymeria/tools/microsoft_graph_service_integrations.py` | Update a Microsoft To Do task with a JSON object of Graph task fields. |
| `misp_add_event_tag` | `nymeria/tools/enrichment_security_service_integrations.py` | Add a tag to a MISP event. |
| `misp_create_event` | `nymeria/tools/enrichment_security_service_integrations.py` | Create a MISP event. |
| `misp_get_event` | `nymeria/tools/enrichment_security_service_integrations.py` | Get a MISP event by ID. |
| `misp_list_tags` | `nymeria/tools/enrichment_security_service_integrations.py` | List MISP tags. |
| `misp_remove_event_tag` | `nymeria/tools/enrichment_security_service_integrations.py` | Remove a tag from a MISP event. |
| `misp_search_attributes` | `nymeria/tools/enrichment_security_service_integrations.py` | Search MISP attributes with restSearch. |
| `misp_search_events` | `nymeria/tools/enrichment_security_service_integrations.py` | Search MISP events with restSearch. |
| `mocean_get_balance` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get Mocean account balance. |
| `mocean_send_sms` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS with Mocean. |
| `mocean_send_voice` | `nymeria/tools/messaging_delivery_service_integrations.py` | Start a Mocean voice call that speaks text. |
| `monday_add_item_update` | `nymeria/tools/project_management_service_integrations.py` | Add an update/comment to a Monday item. |
| `monday_archive_board` | `nymeria/tools/project_management_service_integrations.py` | Archive a Monday board. |
| `monday_create_board` | `nymeria/tools/project_management_service_integrations.py` | Create a Monday board. |
| `monday_create_board_column` | `nymeria/tools/project_management_service_integrations.py` | Create a column on a Monday board. |
| `monday_create_board_group` | `nymeria/tools/project_management_service_integrations.py` | Create a group on a Monday board. |
| `monday_create_item` | `nymeria/tools/project_management_service_integrations.py` | Create an item on a Monday board. |
| `monday_delete_item` | `nymeria/tools/project_management_service_integrations.py` | Delete a Monday item. |
| `monday_get_board` | `nymeria/tools/project_management_service_integrations.py` | Get a Monday board by ID. |
| `monday_get_item` | `nymeria/tools/project_management_service_integrations.py` | Get one or more Monday items by ID. |
| `monday_get_me` | `nymeria/tools/project_management_service_integrations.py` | Get the current Monday user for the configured credential. |
| `monday_list_board_columns` | `nymeria/tools/project_management_service_integrations.py` | List columns on a Monday board. |
| `monday_list_board_groups` | `nymeria/tools/project_management_service_integrations.py` | List groups on a Monday board. |
| `monday_list_boards` | `nymeria/tools/project_management_service_integrations.py` | List Monday boards visible to the credential. |
| `monday_list_items` | `nymeria/tools/project_management_service_integrations.py` | List Monday items on a board or within a group. |
| `monday_move_item` | `nymeria/tools/project_management_service_integrations.py` | Move a Monday item to another group. |
| `monday_update_item_columns` | `nymeria/tools/project_management_service_integrations.py` | Update multiple Monday item column values. |
| `monica_create_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Create a Monica CRM record from a JSON object. |
| `monica_delete_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Delete a Monica CRM record by ID. |
| `monica_get_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Get one Monica CRM record by ID. |
| `monica_list_records` | `nymeria/tools/relationship_crm_service_integrations.py` | List Monica CRM records such as contacts, activities, calls, notes, reminders, tags, or tasks. |
| `monica_update_record` | `nymeria/tools/relationship_crm_service_integrations.py` | Update a Monica CRM record from a JSON object. |
| `msg91_send_sms` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS with MSG91. |
| `nasa_apod` | `nymeria/tools/public_info_integrations.py` | Get NASA Astronomy Picture of the Day metadata. |
| `netlify_cancel_deploy` | `nymeria/tools/operations_monitoring_service_integrations.py` | Cancel a Netlify deploy by deploy ID. |
| `netlify_delete_site` | `nymeria/tools/operations_monitoring_service_integrations.py` | Delete a Netlify site by ID, name, or domain. |
| `netlify_get_deploy` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Netlify deploy by site and deploy ID. |
| `netlify_get_site` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Netlify site by ID, name, or domain. |
| `netlify_list_deploys` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Netlify deploys for a site. |
| `netlify_list_sites` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Netlify sites available to the configured token. |
| `nextcloud_copy_path` | `nymeria/tools/file_storage_service_integrations.py` | Copy a Nextcloud file or folder. |
| `nextcloud_create_folder` | `nymeria/tools/file_storage_service_integrations.py` | Create a Nextcloud folder using WebDAV. |
| `nextcloud_delete_path` | `nymeria/tools/file_storage_service_integrations.py` | Delete a Nextcloud file or folder. |
| `nextcloud_download_file` | `nymeria/tools/file_storage_service_integrations.py` | Download a Nextcloud file and return a text/base64 preview. |
| `nextcloud_get_user` | `nymeria/tools/file_storage_service_integrations.py` | Get one Nextcloud user through the OCS API. |
| `nextcloud_list_folder` | `nymeria/tools/file_storage_service_integrations.py` | List Nextcloud folder contents using WebDAV. |
| `nextcloud_list_users` | `nymeria/tools/file_storage_service_integrations.py` | List Nextcloud users through the OCS API. |
| `nextcloud_move_path` | `nymeria/tools/file_storage_service_integrations.py` | Move or rename a Nextcloud file or folder. |
| `nextcloud_upload_text_file` | `nymeria/tools/file_storage_service_integrations.py` | Upload text content to a Nextcloud file path. |
| `nocodb_count_records` | `nymeria/tools/data_table_service_integrations.py` | Count NocoDB records, optionally filtered by formula. |
| `nocodb_create_record` | `nymeria/tools/data_table_service_integrations.py` | Create a NocoDB record from a JSON field mapping. |
| `nocodb_delete_record` | `nymeria/tools/data_table_service_integrations.py` | Delete a NocoDB record. |
| `nocodb_get_base` | `nymeria/tools/data_table_service_integrations.py` | Get NocoDB base metadata. |
| `nocodb_get_record` | `nymeria/tools/data_table_service_integrations.py` | Get a single NocoDB record. |
| `nocodb_list_bases` | `nymeria/tools/data_table_service_integrations.py` | List NocoDB bases, optionally within a workspace. |
| `nocodb_list_records` | `nymeria/tools/data_table_service_integrations.py` | List NocoDB records from a table. |
| `nocodb_update_record` | `nymeria/tools/data_table_service_integrations.py` | Update a NocoDB record from a JSON field mapping. |
| `notify` | `nymeria/tools/notify.py` | Send a notification to the user through their configured channels. |
| `notion_append_block_children` | `nymeria/tools/collaboration_data_service_integrations.py` | Append child blocks to a Notion page or block. |
| `notion_create_page` | `nymeria/tools/collaboration_data_service_integrations.py` | Create a Notion page under a page or data source. |
| `notion_get_block_children` | `nymeria/tools/collaboration_data_service_integrations.py` | Retrieve child blocks for a Notion page or block. |
| `notion_get_page` | `nymeria/tools/collaboration_data_service_integrations.py` | Retrieve Notion page properties. |
| `notion_query_data_source` | `nymeria/tools/collaboration_data_service_integrations.py` | Query a Notion data source. |
| `notion_search` | `nymeria/tools/collaboration_data_service_integrations.py` | Search Notion pages and data sources shared with the credential. |
| `notion_update_page` | `nymeria/tools/collaboration_data_service_integrations.py` | Update Notion page properties or archive/trash state. |
| `npm_package_info` | `nymeria/tools/public_info_integrations.py` | Get npm package metadata. |
| `npm_package_search` | `nymeria/tools/public_info_integrations.py` | Search the npm registry for packages. |
| `nym_todo` | `nymeria/tools/todo.py` | Create or update a TODO item. Omit todo_id to create, provide it to update. |
| `nym_todo_delete` | `nymeria/tools/todo.py` | Delete a TODO permanently (no archive). Cancels any scheduled execution. |
| `nym_todo_list` | `nymeria/tools/todo.py` | List TODO items. Shows active (non-done) by default. |
| `odoo_create_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Create an Odoo record. |
| `odoo_delete_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Delete an Odoo record. |
| `odoo_get_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Get an Odoo record by ID. |
| `odoo_get_server_version` | `nymeria/tools/enterprise_business_service_integrations.py` | Get the Odoo server version. |
| `odoo_list_records` | `nymeria/tools/enterprise_business_service_integrations.py` | List Odoo records from a model. |
| `odoo_update_record` | `nymeria/tools/enterprise_business_service_integrations.py` | Update an Odoo record. |
| `okta_create_user` | `nymeria/tools/enrichment_security_service_integrations.py` | Create an Okta user. |
| `okta_delete_user` | `nymeria/tools/enrichment_security_service_integrations.py` | Delete an Okta user, optionally deactivating the user first. |
| `okta_get_user` | `nymeria/tools/enrichment_security_service_integrations.py` | Get an Okta user by ID, login, or email. |
| `okta_list_users` | `nymeria/tools/enrichment_security_service_integrations.py` | List or search Okta users. |
| `okta_update_user` | `nymeria/tools/enrichment_security_service_integrations.py` | Update an Okta user profile. |
| `onesimple_create_pdf` | `nymeria/tools/business_service_integrations.py` | Create a PDF URL for a webpage with One Simple API. |
| `onesimple_create_qr_code` | `nymeria/tools/business_service_integrations.py` | Create a QR-code image URL with One Simple API. |
| `onesimple_create_screenshot` | `nymeria/tools/business_service_integrations.py` | Create a screenshot URL for a webpage with One Simple API. |
| `onesimple_expand_url` | `nymeria/tools/business_service_integrations.py` | Expand a shortened URL with One Simple API. |
| `onesimple_get_exchange_rate` | `nymeria/tools/business_service_integrations.py` | Convert a currency amount with One Simple API exchange-rate data. |
| `onesimple_get_image_metadata` | `nymeria/tools/business_service_integrations.py` | Get image metadata from an image URL with One Simple API. |
| `onesimple_get_page_info` | `nymeria/tools/business_service_integrations.py` | Get page SEO and metadata for a webpage with One Simple API. |
| `onesimple_validate_email` | `nymeria/tools/business_service_integrations.py` | Validate an email address with One Simple API. |
| `onfleet_complete_task` | `nymeria/tools/business_service_integrations.py` | Force-complete an Onfleet task. |
| `onfleet_get_task` | `nymeria/tools/business_service_integrations.py` | Get an Onfleet task by ID or short ID. |
| `onfleet_get_team` | `nymeria/tools/business_service_integrations.py` | Get an Onfleet team by ID. |
| `onfleet_get_worker` | `nymeria/tools/business_service_integrations.py` | Get an Onfleet worker by ID. |
| `onfleet_list_tasks` | `nymeria/tools/business_service_integrations.py` | List Onfleet tasks. |
| `onfleet_list_teams` | `nymeria/tools/business_service_integrations.py` | List Onfleet teams. |
| `onfleet_list_workers` | `nymeria/tools/business_service_integrations.py` | List Onfleet workers. |
| `onfleet_test_auth` | `nymeria/tools/business_service_integrations.py` | Validate the saved Onfleet API connection. |
| `open_thesaurus_synonyms` | `nymeria/tools/public_info_integrations.py` | Get German synonyms from OpenThesaurus. |
| `openweathermap_current` | `nymeria/tools/public_info_integrations.py` | Get current weather from OpenWeatherMap. |
| `openweathermap_forecast` | `nymeria/tools/public_info_integrations.py` | Get a 5-day weather forecast from OpenWeatherMap. |
| `oura_get_daily_activity` | `nymeria/tools/personal_device_service_integrations.py` | Get Oura daily activity summaries. |
| `oura_get_daily_readiness` | `nymeria/tools/personal_device_service_integrations.py` | Get Oura daily readiness summaries. |
| `oura_get_daily_sleep` | `nymeria/tools/personal_device_service_integrations.py` | Get Oura daily sleep summaries. |
| `oura_get_profile` | `nymeria/tools/personal_device_service_integrations.py` | Get the authenticated Oura personal profile. |
| `outlook_create_draft` | `nymeria/tools/outlook_email.py` | Create an email draft without sending it. |
| `outlook_delete_email` | `nymeria/tools/outlook_email.py` | Delete an email (moves to Deleted Items, or permanently deletes). |
| `outlook_draft_reply` | `nymeria/tools/outlook_email.py` | Create a draft reply to an email (does NOT send it). |
| `outlook_edit_draft` | `nymeria/tools/outlook_email.py` | Edit an existing email draft. Only provided fields are updated. |
| `outlook_forward_email` | `nymeria/tools/outlook_email.py` | Forward an email to another recipient. |
| `outlook_get_attachments` | `nymeria/tools/outlook_attachments.py` | Download and extract text content from all attachments on an email. |
| `outlook_get_email` | `nymeria/tools/outlook_email.py` | Get full details of email(s) by ID. |
| `outlook_list_emails` | `nymeria/tools/outlook_email.py` | List recent emails from Outlook. |
| `outlook_mark_email` | `nymeria/tools/outlook_email.py` | Mark an email as read or unread. |
| `outlook_move_email` | `nymeria/tools/outlook_email.py` | Move an email to a different folder. |
| `outlook_reply_email` | `nymeria/tools/outlook_email.py` | Reply to an email. |
| `outlook_search_emails` | `nymeria/tools/outlook_email.py` | Search emails in Outlook with optional filters. |
| `outlook_send_email` | `nymeria/tools/outlook_email.py` | Send a new email. |
| `outlook_set_category` | `nymeria/tools/outlook_email.py` | Add or remove a category tag on an email. |
| `paddle_create_coupon` | `nymeria/tools/commerce_billing_service_integrations.py` | Create Paddle coupon codes. |
| `paddle_get_order` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Paddle order by checkout ID. |
| `paddle_list_coupons` | `nymeria/tools/commerce_billing_service_integrations.py` | List Paddle coupons for a product. |
| `paddle_list_payments` | `nymeria/tools/commerce_billing_service_integrations.py` | List Paddle subscription payments. |
| `paddle_list_plans` | `nymeria/tools/commerce_billing_service_integrations.py` | List Paddle subscription plans. |
| `paddle_list_products` | `nymeria/tools/commerce_billing_service_integrations.py` | List Paddle products. |
| `paddle_list_subscription_users` | `nymeria/tools/commerce_billing_service_integrations.py` | List Paddle subscription users. |
| `paddle_reschedule_payment` | `nymeria/tools/commerce_billing_service_integrations.py` | Reschedule a Paddle subscription payment. |
| `paddle_update_coupon` | `nymeria/tools/commerce_billing_service_integrations.py` | Update Paddle coupon metadata by coupon code or group. |
| `pagerduty_add_incident_note` | `nymeria/tools/operations_monitoring_service_integrations.py` | Add a note to a PagerDuty incident. |
| `pagerduty_create_incident` | `nymeria/tools/operations_monitoring_service_integrations.py` | Create a PagerDuty incident. |
| `pagerduty_get_incident` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a PagerDuty incident by ID. |
| `pagerduty_get_user` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a PagerDuty user by ID. |
| `pagerduty_list_incidents` | `nymeria/tools/operations_monitoring_service_integrations.py` | List PagerDuty incidents. |
| `pagerduty_list_services` | `nymeria/tools/operations_monitoring_service_integrations.py` | List PagerDuty services. |
| `pagerduty_update_incident` | `nymeria/tools/operations_monitoring_service_integrations.py` | Update a PagerDuty incident. |
| `peekalink_check_availability` | `nymeria/tools/enrichment_security_service_integrations.py` | Check whether Peekalink can create a preview for a URL. |
| `peekalink_preview_url` | `nymeria/tools/enrichment_security_service_integrations.py` | Return link-preview metadata for a URL through Peekalink. |
| `personality_set` | `nymeria/tools/memory.py` | Set a communication preference. Auto-applied in future conversations. |
| `phantombuster_delete_agent` | `nymeria/tools/business_service_integrations.py` | Delete a Phantombuster agent. |
| `phantombuster_get_agent` | `nymeria/tools/business_service_integrations.py` | Get Phantombuster agent metadata. |
| `phantombuster_get_agent_output` | `nymeria/tools/business_service_integrations.py` | Get Phantombuster agent output, optionally resolving the result object. |
| `phantombuster_launch_agent` | `nymeria/tools/business_service_integrations.py` | Launch a Phantombuster agent. |
| `phantombuster_list_agents` | `nymeria/tools/business_service_integrations.py` | List Phantombuster agents. |
| `philips_hue_delete_light` | `nymeria/tools/personal_device_service_integrations.py` | Delete a Philips Hue light from the bridge. |
| `philips_hue_get_light` | `nymeria/tools/personal_device_service_integrations.py` | Get a Philips Hue light. |
| `philips_hue_list_lights` | `nymeria/tools/personal_device_service_integrations.py` | List Philips Hue lights. |
| `philips_hue_update_light_state` | `nymeria/tools/personal_device_service_integrations.py` | Update a Philips Hue light state. |
| `pipedrive_create_record` | `nymeria/tools/sales_crm_service_integrations.py` | Create a Pipedrive CRM record. |
| `pipedrive_delete_record` | `nymeria/tools/sales_crm_service_integrations.py` | Delete a Pipedrive CRM record. |
| `pipedrive_get_record` | `nymeria/tools/sales_crm_service_integrations.py` | Get a Pipedrive CRM record by ID. |
| `pipedrive_list_records` | `nymeria/tools/sales_crm_service_integrations.py` | List Pipedrive CRM records. |
| `pipedrive_list_users` | `nymeria/tools/sales_crm_service_integrations.py` | List Pipedrive users. |
| `pipedrive_search_records` | `nymeria/tools/sales_crm_service_integrations.py` | Search Pipedrive CRM records. |
| `pipedrive_update_record` | `nymeria/tools/sales_crm_service_integrations.py` | Update a Pipedrive CRM record. |
| `plivo_get_account` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get Plivo account metadata. |
| `plivo_send_message` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS or MMS message with Plivo. |
| `posthog_capture_event` | `nymeria/tools/marketing_contact_service_integrations.py` | Capture one PostHog event. |
| `posthog_create_alias` | `nymeria/tools/marketing_contact_service_integrations.py` | Create a PostHog alias for a distinct ID. |
| `posthog_identify` | `nymeria/tools/marketing_contact_service_integrations.py` | Identify a PostHog user and set properties. |
| `posthog_track_page_or_screen` | `nymeria/tools/marketing_contact_service_integrations.py` | Track a PostHog page or screen view. |
| `profitwell_get_metrics` | `nymeria/tools/commerce_billing_service_integrations.py` | Get ProfitWell daily or monthly metrics. |
| `profitwell_get_settings` | `nymeria/tools/commerce_billing_service_integrations.py` | Get ProfitWell account settings. |
| `pushbullet_delete_push` | `nymeria/tools/notification_service_integrations.py` | Delete a Pushbullet push by ID. |
| `pushbullet_list_pushes` | `nymeria/tools/notification_service_integrations.py` | List Pushbullet push history. |
| `pushbullet_send_push` | `nymeria/tools/notification_service_integrations.py` | Send a Pushbullet note or link push. |
| `pushbullet_update_push` | `nymeria/tools/notification_service_integrations.py` | Update a Pushbullet push, usually to dismiss it. |
| `pushcut_send_notification` | `nymeria/tools/notification_service_integrations.py` | Send a Pushcut notification. |
| `pushover_send_message` | `nymeria/tools/notification_service_integrations.py` | Send a Pushover message. |
| `quickbase_delete_records` | `nymeria/tools/data_table_service_integrations.py` | Delete Quickbase records matching a where clause. |
| `quickbase_list_fields` | `nymeria/tools/data_table_service_integrations.py` | List fields for a Quickbase table. |
| `quickbase_query_records` | `nymeria/tools/data_table_service_integrations.py` | Query Quickbase records. |
| `quickbase_upsert_records` | `nymeria/tools/data_table_service_integrations.py` | Create or update Quickbase records. |
| `quickbooks_create_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a QuickBooks Online customer. |
| `quickbooks_create_invoice` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a QuickBooks Online invoice. |
| `quickbooks_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a QuickBooks Online record by ID. |
| `quickbooks_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List QuickBooks Online records. |
| `quickbooks_query` | `nymeria/tools/commerce_billing_service_integrations.py` | Run a QuickBooks Online read-only SQL-style query. |
| `quickbooks_update_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a QuickBooks Online customer using sparse update fields. |
| `quickchart_create_url` | `nymeria/tools/public_info_integrations.py` | Create a QuickChart chart URL from labels and data arrays. |
| `rag_search` | `nymeria/tools/rag_search_tool.py` | Search your own memory: past conversations, saved memories, and completed TODOs. |
| `rag_settings` | `nymeria/tools/rag_search_tool.py` | Configure RAG settings. Returns current settings after changes. |
| `raindrop_create_bookmark` | `nymeria/tools/bookmark_link_service_integrations.py` | Create a Raindrop bookmark. |
| `raindrop_delete_bookmark` | `nymeria/tools/bookmark_link_service_integrations.py` | Delete a Raindrop bookmark by ID. |
| `raindrop_delete_tags` | `nymeria/tools/bookmark_link_service_integrations.py` | Delete one or more Raindrop tags. |
| `raindrop_get_bookmark` | `nymeria/tools/bookmark_link_service_integrations.py` | Get a Raindrop bookmark by ID. |
| `raindrop_get_collection` | `nymeria/tools/bookmark_link_service_integrations.py` | Get a Raindrop collection by ID. |
| `raindrop_get_user` | `nymeria/tools/bookmark_link_service_integrations.py` | Get Raindrop user metadata for the current user or a specific user ID. |
| `raindrop_list_bookmarks` | `nymeria/tools/bookmark_link_service_integrations.py` | List Raindrop bookmarks in a collection. |
| `raindrop_list_collections` | `nymeria/tools/bookmark_link_service_integrations.py` | List Raindrop collections. |
| `raindrop_list_tags` | `nymeria/tools/bookmark_link_service_integrations.py` | List Raindrop tags, optionally scoped to a collection. |
| `raindrop_update_bookmark` | `nymeria/tools/bookmark_link_service_integrations.py` | Update a Raindrop bookmark from a JSON object. |
| `react` | `nymeria/tools/react.py` | Post an emoji reaction to the chat-platform message behind this turn. |
| `reddit_create_comment` | `nymeria/tools/community_publishing_service_integrations.py` | Create a Reddit comment or reply. |
| `reddit_create_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create a Reddit post. |
| `reddit_delete_thing` | `nymeria/tools/community_publishing_service_integrations.py` | Delete a Reddit post or comment owned by the authenticated account. |
| `reddit_get_post` | `nymeria/tools/community_publishing_service_integrations.py` | Get a Reddit post and top-level comment listing. |
| `reddit_get_subreddit` | `nymeria/tools/community_publishing_service_integrations.py` | Get subreddit metadata. |
| `reddit_get_user` | `nymeria/tools/community_publishing_service_integrations.py` | Get Reddit user metadata. |
| `reddit_list_subreddit_posts` | `nymeria/tools/community_publishing_service_integrations.py` | List posts from a subreddit listing. |
| `reddit_search_posts` | `nymeria/tools/community_publishing_service_integrations.py` | Search Reddit posts. |
| `regression_echo` | `nymeria/tools/regression_echo.py` | Echo the supplied text back with a fixed prefix. |
| `reload_all` | `nymeria/tools/runtime_admin.py` | Reload all tools, skills, and trigger sources. |
| `request_credential` | `nymeria/tools/credential_prompt.py` | Open a secure in-chat prompt (modal / hosted form / OAuth dance) to |
| `rss_feed_read` | `nymeria/tools/public_info_integrations.py` | Read an RSS or Atom feed URL. |
| `run_tools_in_order` | `nymeria/tools/tool_order.py` | Run this whole batch of tool calls in the order listed, not concurrently. |
| `rundeck_execute_job` | `nymeria/tools/operations_monitoring_service_integrations.py` | Execute a Rundeck job. |
| `rundeck_get_job_metadata` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get Rundeck job metadata by job ID. |
| `s3_copy_object` | `nymeria/tools/file_storage_service_integrations.py` | Copy an S3 object. |
| `s3_create_bucket` | `nymeria/tools/file_storage_service_integrations.py` | Create an S3 bucket. |
| `s3_create_folder` | `nymeria/tools/file_storage_service_integrations.py` | Create an S3 folder marker object. |
| `s3_delete_bucket` | `nymeria/tools/file_storage_service_integrations.py` | Delete an empty S3 bucket. |
| `s3_delete_object` | `nymeria/tools/file_storage_service_integrations.py` | Delete an S3 object. |
| `s3_get_object_text` | `nymeria/tools/file_storage_service_integrations.py` | Get an S3 object and return a text/base64 preview. |
| `s3_list_buckets` | `nymeria/tools/file_storage_service_integrations.py` | List S3 buckets. |
| `s3_list_objects` | `nymeria/tools/file_storage_service_integrations.py` | List S3 objects in a bucket. |
| `s3_upload_text_object` | `nymeria/tools/file_storage_service_integrations.py` | Upload text content to an S3 object. |
| `salesforce_create_record` | `nymeria/tools/sales_crm_service_integrations.py` | Create a Salesforce object record from a JSON object. |
| `salesforce_delete_record` | `nymeria/tools/sales_crm_service_integrations.py` | Delete a Salesforce object record by ID. |
| `salesforce_get_record` | `nymeria/tools/sales_crm_service_integrations.py` | Get a Salesforce object record by ID. |
| `salesforce_query_records` | `nymeria/tools/sales_crm_service_integrations.py` | Run a Salesforce SOQL query. |
| `salesforce_update_record` | `nymeria/tools/sales_crm_service_integrations.py` | Update a Salesforce object record from a JSON object. |
| `salesmate_create_record` | `nymeria/tools/sales_crm_service_integrations.py` | Create one Salesmate record from a JSON object. |
| `salesmate_delete_record` | `nymeria/tools/sales_crm_service_integrations.py` | Delete one Salesmate record by ID. |
| `salesmate_get_record` | `nymeria/tools/sales_crm_service_integrations.py` | Get one Salesmate record by ID. |
| `salesmate_list_users` | `nymeria/tools/sales_crm_service_integrations.py` | List active Salesmate users. |
| `salesmate_search_records` | `nymeria/tools/sales_crm_service_integrations.py` | Search Salesmate records. |
| `salesmate_update_record` | `nymeria/tools/sales_crm_service_integrations.py` | Update one Salesmate record from a JSON object. |
| `search_mcp` | `nymeria/tools/search_mcp.py` | Search public MCP server registries for servers matching a query. |
| `search_skills` | `nymeria/tools/search_skills.py` | Search for Agent Skills by natural-language query. |
| `seatable_create_row` | `nymeria/tools/data_table_service_integrations.py` | Create a SeaTable row. |
| `seatable_delete_row` | `nymeria/tools/data_table_service_integrations.py` | Delete a SeaTable row. |
| `seatable_get_metadata` | `nymeria/tools/data_table_service_integrations.py` | Get SeaTable base metadata. |
| `seatable_get_row` | `nymeria/tools/data_table_service_integrations.py` | Get a SeaTable row by ID. |
| `seatable_list_rows` | `nymeria/tools/data_table_service_integrations.py` | List rows from a SeaTable table or view. |
| `seatable_update_row` | `nymeria/tools/data_table_service_integrations.py` | Update a SeaTable row. |
| `securityscorecard_add_portfolio_company` | `nymeria/tools/enrichment_security_service_integrations.py` | Add a company domain to a SecurityScorecard portfolio. |
| `securityscorecard_get_company_history` | `nymeria/tools/enrichment_security_service_integrations.py` | Get SecurityScorecard company historical factor scores. |
| `securityscorecard_get_company_scorecard` | `nymeria/tools/enrichment_security_service_integrations.py` | Get SecurityScorecard company information and scorecard summary. |
| `securityscorecard_list_company_factors` | `nymeria/tools/enrichment_security_service_integrations.py` | List SecurityScorecard company factor scores and issue counts. |
| `securityscorecard_list_portfolios` | `nymeria/tools/enrichment_security_service_integrations.py` | List SecurityScorecard portfolios. |
| `securityscorecard_remove_portfolio_company` | `nymeria/tools/enrichment_security_service_integrations.py` | Remove a company domain from a SecurityScorecard portfolio. |
| `segment_group` | `nymeria/tools/marketing_contact_service_integrations.py` | Send a Segment group call. |
| `segment_identify` | `nymeria/tools/marketing_contact_service_integrations.py` | Send a Segment identify call. |
| `segment_track` | `nymeria/tools/marketing_contact_service_integrations.py` | Send a Segment track event. |
| `self_modify_rollback` | `nymeria/tools/runtime_admin.py` | Rollback a file to its previous version if a self-modification broke something. |
| `sendgrid_get_contact` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get a SendGrid marketing contact by ID. |
| `sendgrid_list_contacts` | `nymeria/tools/messaging_delivery_service_integrations.py` | List or search SendGrid marketing contacts. |
| `sendgrid_list_lists` | `nymeria/tools/messaging_delivery_service_integrations.py` | List SendGrid marketing contact lists. |
| `sendgrid_send_email` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an email with SendGrid Mail Send. |
| `sendgrid_upsert_contacts` | `nymeria/tools/messaging_delivery_service_integrations.py` | Create or update SendGrid marketing contacts. |
| `sendy_add_subscriber` | `nymeria/tools/marketing_contact_service_integrations.py` | Add a Sendy subscriber to a list. |
| `sendy_count_active_subscribers` | `nymeria/tools/marketing_contact_service_integrations.py` | Count active Sendy subscribers in a list. |
| `sendy_create_campaign` | `nymeria/tools/marketing_contact_service_integrations.py` | Create a Sendy campaign. |
| `sendy_get_subscriber_status` | `nymeria/tools/marketing_contact_service_integrations.py` | Get a Sendy subscriber status. |
| `sendy_update_subscriber_subscription` | `nymeria/tools/marketing_contact_service_integrations.py` | Unsubscribe, remove, or delete a Sendy subscriber. |
| `sentry_get_event` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Sentry event by project and event ID. |
| `sentry_get_issue` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Sentry issue by issue/group ID. |
| `sentry_list_organizations` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Sentry organizations available to the token. |
| `sentry_list_project_events` | `nymeria/tools/operations_monitoring_service_integrations.py` | List events for a Sentry project. |
| `sentry_list_project_issues` | `nymeria/tools/operations_monitoring_service_integrations.py` | List issues for a Sentry project. |
| `sentry_list_projects` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Sentry projects, optionally scoped to an organization. |
| `sentry_update_issue` | `nymeria/tools/operations_monitoring_service_integrations.py` | Update a Sentry issue. |
| `servicenow_create_record` | `nymeria/tools/support_service_integrations.py` | Create a ServiceNow table record. |
| `servicenow_delete_record` | `nymeria/tools/support_service_integrations.py` | Delete a ServiceNow table record. |
| `servicenow_get_record` | `nymeria/tools/support_service_integrations.py` | Get a ServiceNow table record by sys_id. |
| `servicenow_list_records` | `nymeria/tools/support_service_integrations.py` | List ServiceNow table records. |
| `servicenow_update_record` | `nymeria/tools/support_service_integrations.py` | Update a ServiceNow table record. |
| `seven_get_balance` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get seven.io account balance. |
| `seven_send_sms` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS message with seven.io. |
| `shopify_create_product` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Shopify product. |
| `shopify_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Shopify record by ID. |
| `shopify_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List Shopify Admin REST records. |
| `shopify_update_product` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Shopify product. |
| `signl4_resolve_alert` | `nymeria/tools/notification_service_integrations.py` | Resolve a SIGNL4 alert by external ID. |
| `signl4_send_alert` | `nymeria/tools/notification_service_integrations.py` | Send a SIGNL4 alert event. |
| `skill_edit` | `nymeria/tools/skill_config.py` | Edit an existing generated Skill or Skill Kit. |
| `skill_manage` | `nymeria/tools/search_skills.py` | List, search, install, enable, disable, inspect, status, or prune Agent Skills. |
| `skill_write` | `nymeria/tools/skill_config.py` | Write a Skill or Skill Kit from full SKILL.md markdown. |
| `slack_add_reaction` | `nymeria/tools/collaboration_data_service_integrations.py` | Add a reaction to a Slack message. |
| `slack_get_channel_history` | `nymeria/tools/collaboration_data_service_integrations.py` | Get recent messages from a Slack conversation. |
| `slack_get_user` | `nymeria/tools/collaboration_data_service_integrations.py` | Get Slack user profile metadata. |
| `slack_list_channels` | `nymeria/tools/collaboration_data_service_integrations.py` | List Slack conversations visible to the credential. |
| `slack_list_users` | `nymeria/tools/collaboration_data_service_integrations.py` | List Slack users visible to the credential. |
| `slack_post_message` | `nymeria/tools/collaboration_data_service_integrations.py` | Post a message to Slack. |
| `slack_search_messages` | `nymeria/tools/collaboration_data_service_integrations.py` | Search Slack messages. |
| `slack_update_message` | `nymeria/tools/collaboration_data_service_integrations.py` | Update a Slack message. |
| `spawn_thread` | `nymeria/tools/spawn_thread.py` | Create or delete a conversation thread with scoped configuration. |
| `splunk_create_search_job` | `nymeria/tools/operations_monitoring_service_integrations.py` | Create a Splunk search job. |
| `splunk_get_search_job` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get a Splunk search job by SID. |
| `splunk_get_search_results` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get Splunk search job results. |
| `splunk_list_saved_searches` | `nymeria/tools/operations_monitoring_service_integrations.py` | List Splunk saved searches. |
| `spotify_get_album` | `nymeria/tools/media_discovery_service_integrations.py` | Get Spotify album metadata. |
| `spotify_get_artist` | `nymeria/tools/media_discovery_service_integrations.py` | Get Spotify artist metadata. |
| `spotify_get_playlist` | `nymeria/tools/media_discovery_service_integrations.py` | Get Spotify playlist metadata. |
| `spotify_get_track` | `nymeria/tools/media_discovery_service_integrations.py` | Get Spotify track metadata. |
| `spotify_search` | `nymeria/tools/media_discovery_service_integrations.py` | Search Spotify catalog metadata. |
| `stackby_create_rows` | `nymeria/tools/data_table_service_integrations.py` | Create one or more Stackby rows. |
| `stackby_delete_rows` | `nymeria/tools/data_table_service_integrations.py` | Delete one or more Stackby rows. |
| `stackby_get_row` | `nymeria/tools/data_table_service_integrations.py` | Get a Stackby row by ID. |
| `stackby_list_rows` | `nymeria/tools/data_table_service_integrations.py` | List Stackby rows. |
| `storyblok_delete_story` | `nymeria/tools/content_management_service_integrations.py` | Delete a Storyblok story through the Management API. |
| `storyblok_get_story` | `nymeria/tools/content_management_service_integrations.py` | Get a Storyblok story by slug/path or management story ID. |
| `storyblok_list_stories` | `nymeria/tools/content_management_service_integrations.py` | List Storyblok stories from the Content or Management API. |
| `storyblok_publish_story` | `nymeria/tools/content_management_service_integrations.py` | Publish a Storyblok story through the Management API. |
| `storyblok_unpublish_story` | `nymeria/tools/content_management_service_integrations.py` | Unpublish a Storyblok story through the Management API. |
| `strapi_create_entry` | `nymeria/tools/content_management_service_integrations.py` | Create a Strapi entry. |
| `strapi_delete_entry` | `nymeria/tools/content_management_service_integrations.py` | Delete a Strapi entry. |
| `strapi_get_entry` | `nymeria/tools/content_management_service_integrations.py` | Get a Strapi entry by ID. |
| `strapi_list_entries` | `nymeria/tools/content_management_service_integrations.py` | List Strapi collection entries. |
| `strapi_update_entry` | `nymeria/tools/content_management_service_integrations.py` | Update a Strapi entry. |
| `strava_create_activity` | `nymeria/tools/personal_device_service_integrations.py` | Create a manual Strava activity. |
| `strava_get_activity` | `nymeria/tools/personal_device_service_integrations.py` | Get a Strava activity by ID. |
| `strava_get_activity_streams` | `nymeria/tools/personal_device_service_integrations.py` | Get Strava activity streams for selected stream keys. |
| `strava_list_activities` | `nymeria/tools/personal_device_service_integrations.py` | List Strava activities for the authenticated athlete. |
| `strava_list_activity_comments` | `nymeria/tools/personal_device_service_integrations.py` | List comments on a Strava activity. |
| `strava_update_activity` | `nymeria/tools/personal_device_service_integrations.py` | Update a Strava activity from a JSON object. |
| `stripe_create_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Stripe customer. |
| `stripe_get_balance` | `nymeria/tools/commerce_billing_service_integrations.py` | Get the current Stripe account balance. |
| `stripe_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Stripe record by ID. |
| `stripe_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List Stripe records for safe inspection. |
| `stripe_search_records` | `nymeria/tools/commerce_billing_service_integrations.py` | Search Stripe records with Stripe Search query syntax. |
| `stripe_update_customer` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Stripe customer. |
| `supabase_delete_rows` | `nymeria/tools/data_table_service_integrations.py` | Delete Supabase rows matching a PostgREST filter. |
| `supabase_insert_rows` | `nymeria/tools/data_table_service_integrations.py` | Insert one or more rows into a Supabase table. |
| `supabase_list_rows` | `nymeria/tools/data_table_service_integrations.py` | List rows from a Supabase table through PostgREST. |
| `supabase_update_rows` | `nymeria/tools/data_table_service_integrations.py` | Update Supabase rows matching a PostgREST filter. |
| `taiga_create_record` | `nymeria/tools/project_management_service_integrations.py` | Create a Taiga epic, issue, task, or user story. |
| `taiga_delete_record` | `nymeria/tools/project_management_service_integrations.py` | Delete a Taiga epic, issue, task, or user story. |
| `taiga_get_record` | `nymeria/tools/project_management_service_integrations.py` | Get a Taiga epic, issue, task, or user story by ID. |
| `taiga_list_projects` | `nymeria/tools/project_management_service_integrations.py` | List Taiga projects visible to the credential. |
| `taiga_list_records` | `nymeria/tools/project_management_service_integrations.py` | List Taiga epics, issues, tasks, or user stories. |
| `taiga_update_record` | `nymeria/tools/project_management_service_integrations.py` | Update a Taiga epic, issue, task, or user story. |
| `tapfiliate_add_affiliate_metadata` | `nymeria/tools/commerce_billing_service_integrations.py` | Add metadata fields to a Tapfiliate affiliate. |
| `tapfiliate_add_program_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Add a Tapfiliate affiliate to a program. |
| `tapfiliate_approve_program_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Approve a Tapfiliate affiliate for a program. |
| `tapfiliate_create_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Tapfiliate affiliate. |
| `tapfiliate_delete_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Delete a Tapfiliate affiliate. |
| `tapfiliate_disapprove_program_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Disapprove a Tapfiliate affiliate for a program. |
| `tapfiliate_get_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Tapfiliate affiliate by ID. |
| `tapfiliate_get_program_affiliate` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Tapfiliate affiliate in a program. |
| `tapfiliate_list_affiliates` | `nymeria/tools/commerce_billing_service_integrations.py` | List Tapfiliate affiliates. |
| `tapfiliate_list_program_affiliates` | `nymeria/tools/commerce_billing_service_integrations.py` | List affiliates in a Tapfiliate program. |
| `tapfiliate_remove_affiliate_metadata` | `nymeria/tools/commerce_billing_service_integrations.py` | Remove a metadata field from a Tapfiliate affiliate. |
| `tapfiliate_update_affiliate_metadata` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a metadata field on a Tapfiliate affiliate. |
| `team_manage` | `nymeria/tools/teams.py` | Manage your callable-thread teams (isolated invocation bubbles). |
| `telegram_delete_message` | `nymeria/tools/chat_platform_service_integrations.py` | Delete a Telegram message. |
| `telegram_get_chat` | `nymeria/tools/chat_platform_service_integrations.py` | Get Telegram chat metadata. |
| `telegram_get_me` | `nymeria/tools/chat_platform_service_integrations.py` | Get the Telegram bot profile. |
| `telegram_send_message` | `nymeria/tools/chat_platform_service_integrations.py` | Send a Telegram text message. |
| `thehive_create_alert` | `nymeria/tools/enrichment_security_service_integrations.py` | Create a TheHive alert. |
| `thehive_create_case` | `nymeria/tools/enrichment_security_service_integrations.py` | Create a TheHive case. |
| `thehive_get_alert` | `nymeria/tools/enrichment_security_service_integrations.py` | Get a TheHive alert by ID. |
| `thehive_get_case` | `nymeria/tools/enrichment_security_service_integrations.py` | Get a TheHive case by ID. |
| `thehive_list_alerts` | `nymeria/tools/enrichment_security_service_integrations.py` | List or query TheHive alerts. |
| `thehive_list_cases` | `nymeria/tools/enrichment_security_service_integrations.py` | List or query TheHive cases. |
| `thread_instructions_set` | `nymeria/tools/dream_tools.py` | Overwrite the parent thread's per-thread instructions (appended to soul.md). |
| `todoist_close_task` | `nymeria/tools/productivity_service_integrations.py` | Close a Todoist task. |
| `todoist_create_project` | `nymeria/tools/productivity_service_integrations.py` | Create a Todoist project. |
| `todoist_create_task` | `nymeria/tools/productivity_service_integrations.py` | Create a Todoist task. |
| `todoist_get_project` | `nymeria/tools/productivity_service_integrations.py` | Get a Todoist project by ID. |
| `todoist_get_task` | `nymeria/tools/productivity_service_integrations.py` | Get a Todoist task by ID. |
| `todoist_list_projects` | `nymeria/tools/productivity_service_integrations.py` | List Todoist projects. |
| `todoist_list_tasks` | `nymeria/tools/productivity_service_integrations.py` | List Todoist tasks. |
| `todoist_update_task` | `nymeria/tools/productivity_service_integrations.py` | Update a Todoist task. |
| `tool_create` | `nymeria/tools/tool_create.py` | Draft, test, publish, list, or delete agent-created custom tools. |
| `tool_invoke` | `nymeria/tools/tool_invoke.py` | Run one tool by name WITHOUT binding it to this thread (cache-safe). |
| `tool_manage` | `nymeria/tools/tool_search.py` | Manage current-thread tool bindings: enable, disable, prune, list_categories, status. |
| `tool_search` | `nymeria/tools/tool_search.py` | Search available tools by keyword/category. |
| `totp_generate_code` | `nymeria/tools/transform_utility_integrations.py` | Generate a time-based one-time password from the saved TOTP secret. |
| `totp_verify_code` | `nymeria/tools/transform_utility_integrations.py` | Verify a time-based one-time password against the saved TOTP secret. |
| `travisci_cancel_build` | `nymeria/tools/build_ci_service_integrations.py` | Cancel a Travis CI build. |
| `travisci_get_build` | `nymeria/tools/build_ci_service_integrations.py` | Get one Travis CI build. |
| `travisci_list_builds` | `nymeria/tools/build_ci_service_integrations.py` | List Travis CI builds visible to the token. |
| `travisci_restart_build` | `nymeria/tools/build_ci_service_integrations.py` | Restart a Travis CI build. |
| `travisci_trigger_build` | `nymeria/tools/build_ci_service_integrations.py` | Trigger a Travis CI build request for a repository. |
| `trello_add_card_comment` | `nymeria/tools/productivity_service_integrations.py` | Add a comment to a Trello card. |
| `trello_create_card` | `nymeria/tools/productivity_service_integrations.py` | Create a Trello card. |
| `trello_get_board` | `nymeria/tools/productivity_service_integrations.py` | Get Trello board metadata. |
| `trello_get_card` | `nymeria/tools/productivity_service_integrations.py` | Get Trello card metadata. |
| `trello_list_board_lists` | `nymeria/tools/productivity_service_integrations.py` | List Trello lists on a board. |
| `trello_list_cards` | `nymeria/tools/productivity_service_integrations.py` | List Trello cards in a list. |
| `trello_search` | `nymeria/tools/productivity_service_integrations.py` | Search Trello boards and cards. |
| `trello_update_card` | `nymeria/tools/productivity_service_integrations.py` | Update a Trello card. |
| `trigger_config` | `nymeria/tools/triggers.py` | Create, update, or delete event triggers. |
| `trigger_info` | `nymeria/tools/triggers.py` | List or inspect event triggers and trigger source types. |
| `twilio_get_message` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get a Twilio message by SID. |
| `twilio_list_messages` | `nymeria/tools/messaging_delivery_service_integrations.py` | List Twilio messages. |
| `twilio_make_call` | `nymeria/tools/messaging_delivery_service_integrations.py` | Start an outbound Twilio voice call. |
| `twilio_send_message` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS/MMS/WhatsApp message with Twilio. |
| `twitch_announce` | `nymeria/tools/twitch.py` | Send a highlighted announcement to Twitch chat. Requires moderator permissions. |
| `twitch_automod_review` | `nymeria/tools/twitch.py` | Approve or deny a message held by AutoMod. |
| `twitch_ban` | `nymeria/tools/twitch.py` | Permanently ban a user from Twitch chat. Requires moderator permissions. |
| `twitch_clip` | `nymeria/tools/twitch.py` | Create a clip of the last ~30 seconds of the live stream. |
| `twitch_create_poll` | `nymeria/tools/twitch.py` | Create a poll in the channel. Requires broadcaster token. |
| `twitch_create_prediction` | `nymeria/tools/twitch.py` | Create a channel points prediction. Requires broadcaster token. |
| `twitch_delete_message` | `nymeria/tools/twitch.py` | Delete a specific chat message by ID, or clear all chat if no ID given. |
| `twitch_end_poll` | `nymeria/tools/twitch.py` | End an active poll. Requires broadcaster token. |
| `twitch_get_banned` | `nymeria/tools/twitch.py` | Get list of banned users in the channel with reasons. |
| `twitch_get_channel` | `nymeria/tools/twitch.py` | Get channel info: title, game, tags, language. |
| `twitch_get_chatters` | `nymeria/tools/twitch.py` | Get list of users currently in chat with total count. |
| `twitch_get_schedule` | `nymeria/tools/twitch.py` | Get the channel's upcoming stream schedule. |
| `twitch_get_stream` | `nymeria/tools/twitch.py` | Get the current live stream status: viewers, game, title, uptime. Returns 'offline' if not live. |
| `twitch_get_subs` | `nymeria/tools/twitch.py` | Check subscriber count, or check if a specific user is subscribed. Requires broadcaster token. |
| `twitch_resolve_prediction` | `nymeria/tools/twitch.py` | Resolve, cancel, or lock a prediction. Requires broadcaster token. |
| `twitch_send` | `nymeria/tools/twitch.py` | Send a message to the Twitch channel chat. |
| `twitch_set_channel_info` | `nymeria/tools/twitch.py` | Update channel title, game/category, and/or tags. Requires broadcaster token. |
| `twitch_shoutout` | `nymeria/tools/twitch.py` | Give a shoutout to another channel. Has a 2-minute cooldown per target. |
| `twitch_timeout` | `nymeria/tools/twitch.py` | Timeout a user in Twitch chat. Requires moderator permissions. |
| `twitch_unban` | `nymeria/tools/twitch.py` | Unban or untimeout a user in Twitch chat. Requires moderator permissions. |
| `twitch_warn` | `nymeria/tools/twitch.py` | Issue an official warning to a user. They see a popup in chat. |
| `twitter_create_post` | `nymeria/tools/community_publishing_service_integrations.py` | Create an X/Twitter post, reply, or quote post. |
| `twitter_delete_post` | `nymeria/tools/community_publishing_service_integrations.py` | Delete an X/Twitter post by ID or URL. |
| `twitter_get_me` | `nymeria/tools/community_publishing_service_integrations.py` | Get the authenticated X/Twitter user. |
| `twitter_get_user` | `nymeria/tools/community_publishing_service_integrations.py` | Get an X/Twitter user by ID or username. |
| `twitter_like_post` | `nymeria/tools/community_publishing_service_integrations.py` | Like an X/Twitter post as the authenticated user. |
| `twitter_repost` | `nymeria/tools/community_publishing_service_integrations.py` | Repost an X/Twitter post as the authenticated user. |
| `twitter_search_recent` | `nymeria/tools/community_publishing_service_integrations.py` | Search recent X/Twitter posts. |
| `twitter_send_direct_message` | `nymeria/tools/community_publishing_service_integrations.py` | Send an X/Twitter direct message to a user. |
| `ui_prompt` | `nymeria/tools/ui_prompt.py` | Show an interactive HTML form in the desktop app and return the user's answers. |
| `unleashed_get_stock_on_hand` | `nymeria/tools/commerce_billing_service_integrations.py` | Get Unleashed stock-on-hand for one product. |
| `unleashed_list_sales_orders` | `nymeria/tools/commerce_billing_service_integrations.py` | List Unleashed sales orders. |
| `unleashed_list_stock_on_hand` | `nymeria/tools/commerce_billing_service_integrations.py` | List Unleashed stock-on-hand records. |
| `uplead_enrich_company` | `nymeria/tools/lead_enrichment_service_integrations.py` | Enrich company data by domain or company name. |
| `uplead_enrich_person` | `nymeria/tools/lead_enrichment_service_integrations.py` | Enrich person data by email or first name, last name, and domain. |
| `uproc_get_profile` | `nymeria/tools/lead_enrichment_service_integrations.py` | Get the uProc account profile for the saved credential. |
| `uproc_process` | `nymeria/tools/lead_enrichment_service_integrations.py` | Run a uProc processor with explicit JSON parameters. |
| `uptimerobot_create_monitor` | `nymeria/tools/operations_monitoring_service_integrations.py` | Create an UptimeRobot monitor. |
| `uptimerobot_delete_monitor` | `nymeria/tools/operations_monitoring_service_integrations.py` | Delete an UptimeRobot monitor. |
| `uptimerobot_get_account` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get UptimeRobot account quota and monitor counts. |
| `uptimerobot_get_monitor` | `nymeria/tools/operations_monitoring_service_integrations.py` | Get an UptimeRobot monitor by ID. |
| `uptimerobot_list_monitors` | `nymeria/tools/operations_monitoring_service_integrations.py` | List UptimeRobot monitors. |
| `uptimerobot_reset_monitor` | `nymeria/tools/operations_monitoring_service_integrations.py` | Reset an UptimeRobot monitor's stats. |
| `uptimerobot_update_monitor` | `nymeria/tools/operations_monitoring_service_integrations.py` | Update an UptimeRobot monitor. |
| `urlscan_get_result` | `nymeria/tools/enrichment_security_service_integrations.py` | Get a urlscan.io scan result by UUID. |
| `urlscan_search_scans` | `nymeria/tools/enrichment_security_service_integrations.py` | Search archived urlscan.io scans. |
| `urlscan_submit_scan` | `nymeria/tools/enrichment_security_service_integrations.py` | Submit a URL to urlscan.io for scanning. |
| `vero_alias_user` | `nymeria/tools/marketing_contact_service_integrations.py` | Alias a Vero user ID to a new user ID. |
| `vero_identify_user` | `nymeria/tools/marketing_contact_service_integrations.py` | Create or update a Vero user profile. |
| `vero_track_event` | `nymeria/tools/marketing_contact_service_integrations.py` | Track a Vero event for a user. |
| `vero_update_user_subscription` | `nymeria/tools/marketing_contact_service_integrations.py` | Unsubscribe, resubscribe, or delete a Vero user. |
| `vero_update_user_tags` | `nymeria/tools/marketing_contact_service_integrations.py` | Add or remove Vero user tags. |
| `vonage_get_balance` | `nymeria/tools/messaging_delivery_service_integrations.py` | Get Vonage account balance. |
| `vonage_send_sms` | `nymeria/tools/messaging_delivery_service_integrations.py` | Send an SMS message with Vonage. |
| `watchdog_dispatch` | `nymeria/tools/watchdog_dispatch.py` | Dispatch a TODO to a target thread. You CANNOT target your own thread. |
| `watchdog_read_notepad` | `nymeria/tools/watchdog_dispatch.py` | Read another thread's notepad to understand what it's currently focused on. |
| `watchdog_todo_overview` | `nymeria/tools/watchdog_dispatch.py` | List all active TODOs across ALL threads, showing which thread each belongs to. |
| `web_search_brave` | `nymeria/tools/web_search_integrations.py` | Search the web for current information using Brave (independent search index). |
| `web_search_ddgs` | `nymeria/tools/web_search_integrations.py` | Search the web for current information using keyless in-process metasearch. |
| `web_search_exa_ai` | `nymeria/tools/web_search_integrations.py` | Search the web for current information using Exa (neural/semantic retrieval). |
| `web_search_firecrawl` | `nymeria/tools/web_search_integrations.py` | Search the web for current information using Firecrawl. |
| `web_search_perplexity` | `nymeria/tools/web.py` | Search the web for current information using Perplexity (Sonar). |
| `web_search_searxng` | `nymeria/tools/web_search_integrations.py` | Search the web for current information using a self-hosted SearXNG instance. |
| `web_search_tavily` | `nymeria/tools/web_search_integrations.py` | Search the web for current information using Tavily (agent-optimized retrieval). |
| `webflow_create_collection_item` | `nymeria/tools/content_management_service_integrations.py` | Create a Webflow CMS collection item. |
| `webflow_delete_collection_item` | `nymeria/tools/content_management_service_integrations.py` | Delete a Webflow CMS collection item. |
| `webflow_get_collection` | `nymeria/tools/content_management_service_integrations.py` | Get Webflow CMS collection metadata and fields. |
| `webflow_get_collection_item` | `nymeria/tools/content_management_service_integrations.py` | Get a Webflow CMS collection item. |
| `webflow_list_collection_items` | `nymeria/tools/content_management_service_integrations.py` | List items in a Webflow CMS collection. |
| `webflow_list_site_collections` | `nymeria/tools/content_management_service_integrations.py` | List Webflow CMS collections for a site. |
| `webflow_list_sites` | `nymeria/tools/content_management_service_integrations.py` | List Webflow sites available to the saved connection. |
| `webflow_update_collection_item` | `nymeria/tools/content_management_service_integrations.py` | Update a Webflow CMS collection item. |
| `wekan_add_card_comment` | `nymeria/tools/project_management_service_integrations.py` | Add a comment to a Wekan card. |
| `wekan_create_board` | `nymeria/tools/project_management_service_integrations.py` | Create a Wekan board. |
| `wekan_create_card` | `nymeria/tools/project_management_service_integrations.py` | Create a Wekan card. |
| `wekan_create_list` | `nymeria/tools/project_management_service_integrations.py` | Create a Wekan list on a board. |
| `wekan_delete_board` | `nymeria/tools/project_management_service_integrations.py` | Delete a Wekan board. |
| `wekan_delete_card` | `nymeria/tools/project_management_service_integrations.py` | Delete a Wekan card. |
| `wekan_delete_list` | `nymeria/tools/project_management_service_integrations.py` | Delete a Wekan list. |
| `wekan_get_board` | `nymeria/tools/project_management_service_integrations.py` | Get a Wekan board by ID. |
| `wekan_get_card` | `nymeria/tools/project_management_service_integrations.py` | Get a Wekan card by ID. |
| `wekan_get_current_user` | `nymeria/tools/project_management_service_integrations.py` | Get the current Wekan user for the configured credential. |
| `wekan_list_card_comments` | `nymeria/tools/project_management_service_integrations.py` | List comments on a Wekan card. |
| `wekan_list_cards` | `nymeria/tools/project_management_service_integrations.py` | List Wekan cards from a list or swimlane. |
| `wekan_list_lists` | `nymeria/tools/project_management_service_integrations.py` | List Wekan lists on a board. |
| `wekan_list_user_boards` | `nymeria/tools/project_management_service_integrations.py` | List Wekan boards for a user. |
| `wekan_list_users` | `nymeria/tools/project_management_service_integrations.py` | List Wekan users visible to the credential. |
| `wekan_update_card` | `nymeria/tools/project_management_service_integrations.py` | Update a Wekan card. |
| `whatsapp_delete_media` | `nymeria/tools/chat_platform_service_integrations.py` | Delete WhatsApp Business Cloud media. |
| `whatsapp_get_media_url` | `nymeria/tools/chat_platform_service_integrations.py` | Get a WhatsApp Business Cloud media download URL. |
| `whatsapp_list_phone_numbers` | `nymeria/tools/chat_platform_service_integrations.py` | List WhatsApp Business Cloud phone numbers. |
| `whatsapp_send_template_message` | `nymeria/tools/chat_platform_service_integrations.py` | Send a WhatsApp Business Cloud template message. |
| `whatsapp_send_text_message` | `nymeria/tools/chat_platform_service_integrations.py` | Send a WhatsApp Business Cloud text message. |
| `wikipedia_search` | `nymeria/tools/utility_integrations.py` | Search Wikipedia and return article summaries. |
| `wolfram_alpha_query` | `nymeria/tools/utility_integrations.py` | Query Wolfram|Alpha for computational facts and calculations. |
| `woocommerce_create_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a WooCommerce product, order, or customer. |
| `woocommerce_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a WooCommerce record by ID. |
| `woocommerce_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List WooCommerce records. |
| `woocommerce_update_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a WooCommerce product, order, or customer. |
| `wordpress_create_record` | `nymeria/tools/content_management_service_integrations.py` | Create a WordPress post, page, or user. |
| `wordpress_delete_record` | `nymeria/tools/content_management_service_integrations.py` | Delete a WordPress post, page, or user. |
| `wordpress_get_record` | `nymeria/tools/content_management_service_integrations.py` | Get a WordPress post, page, or user by ID. |
| `wordpress_list_records` | `nymeria/tools/content_management_service_integrations.py` | List WordPress posts, pages, or users. |
| `wordpress_update_record` | `nymeria/tools/content_management_service_integrations.py` | Update a WordPress post, page, or user. |
| `workflow_info` | `nymeria/tools/workflow_info.py` | Inspect nym-SDK workflow tools: definitions, approvals, and run logs. |
| `xero_create_contact` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Xero contact. |
| `xero_create_invoice` | `nymeria/tools/commerce_billing_service_integrations.py` | Create a Xero invoice. |
| `xero_get_record` | `nymeria/tools/commerce_billing_service_integrations.py` | Get a Xero contact or invoice by ID. |
| `xero_list_records` | `nymeria/tools/commerce_billing_service_integrations.py` | List Xero contacts or invoices. |
| `xero_list_tenants` | `nymeria/tools/commerce_billing_service_integrations.py` | List Xero tenants connected to the saved OAuth token. |
| `xero_update_contact` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Xero contact. |
| `xero_update_invoice` | `nymeria/tools/commerce_billing_service_integrations.py` | Update a Xero invoice. |
| `yourls_expand_url` | `nymeria/tools/bookmark_link_service_integrations.py` | Expand a short URL with YOURLS. |
| `yourls_get_db_stats` | `nymeria/tools/bookmark_link_service_integrations.py` | Get YOURLS database stats. |
| `yourls_get_url_stats` | `nymeria/tools/bookmark_link_service_integrations.py` | Get YOURLS stats for a short URL. |
| `yourls_shorten_url` | `nymeria/tools/bookmark_link_service_integrations.py` | Create a short URL with YOURLS. |
| `youtube_get_channels` | `nymeria/tools/media_discovery_service_integrations.py` | Get YouTube channel metadata by channel ID or legacy username. |
| `youtube_get_videos` | `nymeria/tools/media_discovery_service_integrations.py` | Get YouTube video metadata for one or more video IDs. |
| `youtube_list_playlist_items` | `nymeria/tools/media_discovery_service_integrations.py` | List items in a YouTube playlist. |
| `youtube_search` | `nymeria/tools/media_discovery_service_integrations.py` | Search YouTube videos, channels, or playlists with the Data API. |
| `zammad_create_record` | `nymeria/tools/support_service_integrations.py` | Create a Zammad record. |
| `zammad_get_record` | `nymeria/tools/support_service_integrations.py` | Get a Zammad record by ID. |
| `zammad_list_records` | `nymeria/tools/support_service_integrations.py` | List or search Zammad records. |
| `zammad_update_record` | `nymeria/tools/support_service_integrations.py` | Update a Zammad record. |
| `zendesk_create_ticket` | `nymeria/tools/customer_engagement_service_integrations.py` | Create a Zendesk ticket. |
| `zendesk_get_ticket` | `nymeria/tools/customer_engagement_service_integrations.py` | Get a Zendesk ticket by ID. |
| `zendesk_get_user` | `nymeria/tools/customer_engagement_service_integrations.py` | Get a Zendesk user by ID. |
| `zendesk_list_tickets` | `nymeria/tools/customer_engagement_service_integrations.py` | List Zendesk tickets, optionally filtered by status. |
| `zendesk_search` | `nymeria/tools/customer_engagement_service_integrations.py` | Search Zendesk tickets, users, organizations, and groups. |
| `zendesk_search_users` | `nymeria/tools/customer_engagement_service_integrations.py` | Search Zendesk users. |
| `zendesk_update_ticket` | `nymeria/tools/customer_engagement_service_integrations.py` | Update a Zendesk ticket. |
| `zoho_crm_create_records` | `nymeria/tools/sales_crm_service_integrations.py` | Create one or more Zoho CRM records from JSON object(s). |
| `zoho_crm_delete_record` | `nymeria/tools/sales_crm_service_integrations.py` | Delete one Zoho CRM record by ID. |
| `zoho_crm_get_record` | `nymeria/tools/sales_crm_service_integrations.py` | Get one Zoho CRM record by ID. |
| `zoho_crm_list_records` | `nymeria/tools/sales_crm_service_integrations.py` | List Zoho CRM records for a module. |
| `zoho_crm_search_records` | `nymeria/tools/sales_crm_service_integrations.py` | Search Zoho CRM records by criteria, email, phone, or word. |
| `zoho_crm_update_record` | `nymeria/tools/sales_crm_service_integrations.py` | Update one Zoho CRM record from a JSON object. |
| `zoom_create_meeting` | `nymeria/tools/event_meeting_service_integrations.py` | Create a Zoom meeting. |
| `zoom_delete_meeting` | `nymeria/tools/event_meeting_service_integrations.py` | Delete a Zoom meeting. |
| `zoom_get_meeting` | `nymeria/tools/event_meeting_service_integrations.py` | Get a Zoom meeting by ID. |
| `zoom_list_meetings` | `nymeria/tools/event_meeting_service_integrations.py` | List Zoom meetings for the authenticated user. |
| `zoom_update_meeting` | `nymeria/tools/event_meeting_service_integrations.py` | Update a Zoom meeting. |
