"""Two-level functional taxonomy for integration tools.

GENERATED artifact (agent swarm + deterministic validation): the source of
truth for how an integration tool maps to a functional GROUP (level 1) and a
SERVICE/provider (level 2). category stays ``integrations`` everywhere else; this
module only adds the sub-grouping the tool menus render.

Regenerate when the integration tool modules change. ``get_integration_grouping``
falls back to a first-segment service in the ``other`` group for any tool added
before a regeneration, so a new tool is never hidden.
"""

from __future__ import annotations

from typing import Dict, Optional, TypedDict


class IntegrationGrouping(TypedDict):
    group: str
    group_label: str
    service: str
    service_label: str


_OTHER_GROUP = "other"

# Functional groups under the Integrations category, in display order. The
# frontend owns icons; this owns the label + order used across clients.
GROUP_META: Dict[str, Dict[str, object]] = {
    "utilities": {"label": "Utilities & Reference", "order": 10},
    "reference_data": {"label": "Reference & Public Data", "order": 20},
    "developer_tools": {"label": "Developer Tools", "order": 30},
    "cloud_infrastructure": {"label": "Cloud Infrastructure & Storage", "order": 40},
    "monitoring_ops": {"label": "Monitoring & Operations", "order": 50},
    "messaging_chat": {"label": "Messaging & Chat", "order": 60},
    "messaging_delivery": {"label": "Messaging & Email Delivery", "order": 70},
    "notifications": {"label": "Notifications", "order": 80},
    "productivity": {"label": "Productivity & Collaboration", "order": 90},
    "project_management": {"label": "Project Management", "order": 100},
    "content_cms": {"label": "Content & CMS", "order": 110},
    "social_publishing": {"label": "Social & Publishing", "order": 120},
    "media_entertainment": {"label": "Media & Entertainment", "order": 130},
    "crm_sales": {"label": "CRM, Sales & Lead Enrichment", "order": 140},
    "marketing_email": {"label": "Marketing & Email Automation", "order": 150},
    "support_helpdesk": {"label": "Support & Helpdesk", "order": 160},
    "ecommerce_billing": {"label": "E-commerce & Billing", "order": 170},
    "databases_nocode": {"label": "Databases & No-Code", "order": 180},
    "events_webinars": {"label": "Events & Webinars", "order": 190},
    "time_tracking_hr": {"label": "Time Tracking & HR", "order": 200},
    "personal_health": {"label": "Personal Devices & Health", "order": 210},
    "security_intel": {"label": "Security & Intelligence", "order": 220},
    _OTHER_GROUP: {"label": "Other Integrations", "order": 999},
}

# Backend module constant -> functional group key. Build-time provenance; the
# runtime path resolves a group from the matched service, not from this map.
MODULE_TO_GROUP: Dict[str, str] = {
    "AWS_SERVICE_TOOLS": "cloud_infrastructure",
    "BOOKMARK_LINK_SERVICE_TOOLS": "productivity",
    "BUILD_CI_SERVICE_TOOLS": "developer_tools",
    "BUSINESS_SERVICE_TOOLS": "utilities",
    "CHAT_PLATFORM_SERVICE_TOOLS": "messaging_chat",
    "COLLABORATION_DATA_SERVICE_TOOLS": "productivity",
    "COMMERCE_BILLING_SERVICE_TOOLS": "ecommerce_billing",
    "COMMUNITY_PUBLISHING_SERVICE_TOOLS": "social_publishing",
    "CONTENT_MANAGEMENT_SERVICE_TOOLS": "content_cms",
    "CUSTOMER_ENGAGEMENT_SERVICE_TOOLS": "crm_sales",
    "DATA_TABLE_SERVICE_TOOLS": "databases_nocode",
    "DEVELOPER_PLATFORM_TOOLS": "developer_tools",
    "ENRICHMENT_SECURITY_SERVICE_TOOLS": "security_intel",
    "ENTERPRISE_BUSINESS_SERVICE_TOOLS": "ecommerce_billing",
    "EVENT_MEETING_SERVICE_TOOLS": "events_webinars",
    "FILE_STORAGE_SERVICE_TOOLS": "cloud_infrastructure",
    "GOOGLE_BUSINESS_PROFILE_SERVICE_TOOLS": "social_publishing",
    "LEAD_ENRICHMENT_SERVICE_TOOLS": "crm_sales",
    "MARKETING_CONTACT_SERVICE_TOOLS": "marketing_email",
    "MEDIA_DISCOVERY_SERVICE_TOOLS": "media_entertainment",
    "MESSAGING_DELIVERY_SERVICE_TOOLS": "messaging_delivery",
    "MICROSOFT_GRAPH_SERVICE_TOOLS": "productivity",
    "NOTIFICATION_SERVICE_TOOLS": "notifications",
    "OPERATIONS_MONITORING_SERVICE_TOOLS": "monitoring_ops",
    "PERSONAL_DEVICE_SERVICE_TOOLS": "personal_health",
    "PRODUCTIVITY_SERVICE_TOOLS": "project_management",
    "PROJECT_MANAGEMENT_SERVICE_TOOLS": "project_management",
    "PUBLIC_INFO_TOOLS": "reference_data",
    "RELATIONSHIP_CRM_SERVICE_TOOLS": "crm_sales",
    "SALES_CRM_SERVICE_TOOLS": "crm_sales",
    "SUPPORT_SERVICE_TOOLS": "support_helpdesk",
    "TIME_HR_SERVICE_TOOLS": "time_tracking_hr",
    "TRANSFORM_UTILITY_TOOLS": "utilities",
    "UTILITY_INTEGRATION_TOOLS": "utilities",
    "WORK_TRACKING_SERVICE_TOOLS": "project_management",
}

# service_key (tool-name prefix, no trailing underscore) -> {label, group}.
# Matched against a tool name by longest boundary-prefix, so product-level keys
# (e.g. "aws_ses") win over shorter ones. A given service lives in exactly one
# group.
SERVICE_REGISTRY: Dict[str, Dict[str, str]] = {
    "apitemplate": {"label": "APITemplate.io", "group": "utilities"},
    "bitly": {"label": "Bitly", "group": "utilities"},
    "brandfetch": {"label": "Brandfetch", "group": "utilities"},
    "calculator": {"label": "Calculator", "group": "utilities"},
    "compression": {"label": "Compression", "group": "utilities"},
    "crypto": {"label": "Cryptography", "group": "utilities"},
    "datetime": {"label": "Date & Time", "group": "utilities"},
    "deepl": {"label": "DeepL", "group": "utilities"},
    "dhl": {"label": "DHL", "group": "utilities"},
    "jwt": {"label": "JWT", "group": "utilities"},
    "lingvanex": {"label": "Lingvanex", "group": "utilities"},
    "marketstack": {"label": "Marketstack", "group": "utilities"},
    "onesimple": {"label": "OneSimpleAPI", "group": "utilities"},
    "onfleet": {"label": "Onfleet", "group": "utilities"},
    "phantombuster": {"label": "PhantomBuster", "group": "utilities"},
    "totp": {"label": "TOTP", "group": "utilities"},
    "wikipedia": {"label": "Wikipedia", "group": "utilities"},
    "wolfram_alpha": {"label": "Wolfram Alpha", "group": "utilities"},
    "coingecko": {"label": "CoinGecko", "group": "reference_data"},
    "hackernews": {"label": "Hacker News", "group": "reference_data"},
    "nasa": {"label": "NASA", "group": "reference_data"},
    "npm": {"label": "npm Registry", "group": "reference_data"},
    "open_thesaurus": {"label": "Open Thesaurus", "group": "reference_data"},
    "openweathermap": {"label": "OpenWeatherMap", "group": "reference_data"},
    "quickchart": {"label": "QuickChart", "group": "reference_data"},
    "rss": {"label": "RSS", "group": "reference_data"},
    "circleci": {"label": "CircleCI", "group": "developer_tools"},
    "github": {"label": "GitHub", "group": "developer_tools"},
    "gitlab": {"label": "GitLab", "group": "developer_tools"},
    "graphql": {"label": "GraphQL", "group": "developer_tools"},
    "jenkins": {"label": "Jenkins", "group": "developer_tools"},
    "travisci": {"label": "Travis CI", "group": "developer_tools"},
    "aws_lambda": {"label": "AWS Lambda", "group": "cloud_infrastructure"},
    "aws_ses": {"label": "Amazon SES", "group": "cloud_infrastructure"},
    "aws_sns": {"label": "Amazon SNS", "group": "cloud_infrastructure"},
    "aws_textract": {"label": "Amazon Textract", "group": "cloud_infrastructure"},
    "aws_transcribe": {"label": "Amazon Transcribe", "group": "cloud_infrastructure"},
    "dropbox": {"label": "Dropbox", "group": "cloud_infrastructure"},
    "nextcloud": {"label": "Nextcloud", "group": "cloud_infrastructure"},
    "s3": {"label": "Amazon S3", "group": "cloud_infrastructure"},
    "cloudflare": {"label": "Cloudflare", "group": "monitoring_ops"},
    "elasticsearch": {"label": "Elasticsearch", "group": "monitoring_ops"},
    "grafana": {"label": "Grafana", "group": "monitoring_ops"},
    "metabase": {"label": "Metabase", "group": "monitoring_ops"},
    "netlify": {"label": "Netlify", "group": "monitoring_ops"},
    "pagerduty": {"label": "PagerDuty", "group": "monitoring_ops"},
    "rundeck": {"label": "Rundeck", "group": "monitoring_ops"},
    "sentry": {"label": "Sentry", "group": "monitoring_ops"},
    "splunk": {"label": "Splunk", "group": "monitoring_ops"},
    "uptimerobot": {"label": "UptimeRobot", "group": "monitoring_ops"},
    "discord": {"label": "Discord", "group": "messaging_chat"},
    "matrix": {"label": "Matrix", "group": "messaging_chat"},
    "mattermost": {"label": "Mattermost", "group": "messaging_chat"},
    "rocketchat": {"label": "Rocket.Chat", "group": "messaging_chat"},
    "telegram": {"label": "Telegram", "group": "messaging_chat"},
    "webex": {"label": "Webex", "group": "messaging_chat"},
    "whatsapp": {"label": "WhatsApp", "group": "messaging_chat"},
    "zulip": {"label": "Zulip", "group": "messaging_chat"},
    "brevo": {"label": "Brevo", "group": "messaging_delivery"},
    "mailgun": {"label": "Mailgun", "group": "messaging_delivery"},
    "mailjet": {"label": "Mailjet", "group": "messaging_delivery"},
    "mandrill": {"label": "Mandrill", "group": "messaging_delivery"},
    "messagebird": {"label": "MessageBird", "group": "messaging_delivery"},
    "mocean": {"label": "MoceanAPI", "group": "messaging_delivery"},
    "msg91": {"label": "MSG91", "group": "messaging_delivery"},
    "plivo": {"label": "Plivo", "group": "messaging_delivery"},
    "sendgrid": {"label": "SendGrid", "group": "messaging_delivery"},
    "seven": {"label": "seven.io", "group": "messaging_delivery"},
    "twilio": {"label": "Twilio", "group": "messaging_delivery"},
    "vonage": {"label": "Vonage", "group": "messaging_delivery"},
    "gotify": {"label": "Gotify", "group": "notifications"},
    "pushbullet": {"label": "Pushbullet", "group": "notifications"},
    "pushcut": {"label": "Pushcut", "group": "notifications"},
    "pushover": {"label": "Pushover", "group": "notifications"},
    "signl4": {"label": "SIGNL4", "group": "notifications"},
    "airtable": {"label": "Airtable", "group": "productivity"},
    "microsoft_excel": {"label": "Microsoft Excel", "group": "productivity"},
    "microsoft_onedrive": {"label": "Microsoft OneDrive", "group": "productivity"},
    "microsoft_sharepoint": {"label": "Microsoft SharePoint", "group": "productivity"},
    "microsoft_teams": {"label": "Microsoft Teams", "group": "productivity"},
    "microsoft_todo": {"label": "Microsoft To Do", "group": "productivity"},
    "notion": {"label": "Notion", "group": "productivity"},
    "raindrop": {"label": "Raindrop.io", "group": "productivity"},
    "slack": {"label": "Slack", "group": "productivity"},
    "yourls": {"label": "YOURLS", "group": "productivity"},
    "asana": {"label": "Asana", "group": "project_management"},
    "clickup": {"label": "ClickUp", "group": "project_management"},
    "jira": {"label": "Jira", "group": "project_management"},
    "linear": {"label": "Linear", "group": "project_management"},
    "monday": {"label": "monday.com", "group": "project_management"},
    "taiga": {"label": "Taiga", "group": "project_management"},
    "todoist": {"label": "Todoist", "group": "project_management"},
    "trello": {"label": "Trello", "group": "project_management"},
    "wekan": {"label": "Wekan", "group": "project_management"},
    "contentful": {"label": "Contentful", "group": "content_cms"},
    "ghost": {"label": "Ghost", "group": "content_cms"},
    "storyblok": {"label": "Storyblok", "group": "content_cms"},
    "strapi": {"label": "Strapi", "group": "content_cms"},
    "webflow": {"label": "Webflow", "group": "content_cms"},
    "wordpress": {"label": "WordPress", "group": "content_cms"},
    "discourse": {"label": "Discourse", "group": "social_publishing"},
    "facebook": {"label": "Facebook", "group": "social_publishing"},
    "google_business_profile": {"label": "Google Business Profile", "group": "social_publishing"},
    "linkedin": {"label": "LinkedIn", "group": "social_publishing"},
    "medium": {"label": "Medium", "group": "social_publishing"},
    "reddit": {"label": "Reddit", "group": "social_publishing"},
    "twitter": {"label": "Twitter", "group": "social_publishing"},
    "google_books": {"label": "Google Books", "group": "media_entertainment"},
    "spotify": {"label": "Spotify", "group": "media_entertainment"},
    "youtube": {"label": "YouTube", "group": "media_entertainment"},
    "affinity": {"label": "Affinity", "group": "crm_sales"},
    "agilecrm": {"label": "Agile CRM", "group": "crm_sales"},
    "clearbit": {"label": "Clearbit", "group": "crm_sales"},
    "copper": {"label": "Copper", "group": "crm_sales"},
    "dropcontact": {"label": "Dropcontact", "group": "crm_sales"},
    "freshworks_crm": {"label": "Freshworks CRM", "group": "crm_sales"},
    "hubspot": {"label": "HubSpot", "group": "crm_sales"},
    "humantic": {"label": "Humantic AI", "group": "crm_sales"},
    "hunter": {"label": "Hunter.io", "group": "crm_sales"},
    "keap": {"label": "Keap", "group": "crm_sales"},
    "lonescale": {"label": "LoneScale", "group": "crm_sales"},
    "monica": {"label": "Monica", "group": "crm_sales"},
    "pipedrive": {"label": "Pipedrive", "group": "crm_sales"},
    "salesforce": {"label": "Salesforce", "group": "crm_sales"},
    "salesmate": {"label": "Salesmate", "group": "crm_sales"},
    "uplead": {"label": "UpLead", "group": "crm_sales"},
    "uproc": {"label": "uProc", "group": "crm_sales"},
    "zoho_crm": {"label": "Zoho CRM", "group": "crm_sales"},
    "actionnetwork": {"label": "Action Network", "group": "marketing_email"},
    "activecampaign": {"label": "ActiveCampaign", "group": "marketing_email"},
    "autopilot": {"label": "Autopilot", "group": "marketing_email"},
    "convertkit": {"label": "ConvertKit", "group": "marketing_email"},
    "customerio": {"label": "Customer.io", "group": "marketing_email"},
    "egoi": {"label": "E-goi", "group": "marketing_email"},
    "emelia": {"label": "Emelia", "group": "marketing_email"},
    "getresponse": {"label": "GetResponse", "group": "marketing_email"},
    "iterable": {"label": "Iterable", "group": "marketing_email"},
    "lemlist": {"label": "Lemlist", "group": "marketing_email"},
    "mailchimp": {"label": "Mailchimp", "group": "marketing_email"},
    "mailerlite": {"label": "MailerLite", "group": "marketing_email"},
    "mautic": {"label": "Mautic", "group": "marketing_email"},
    "posthog": {"label": "PostHog", "group": "marketing_email"},
    "segment": {"label": "Segment", "group": "marketing_email"},
    "sendy": {"label": "Sendy", "group": "marketing_email"},
    "vero": {"label": "Vero", "group": "marketing_email"},
    "drift": {"label": "Drift", "group": "support_helpdesk"},
    "freshdesk": {"label": "Freshdesk", "group": "support_helpdesk"},
    "freshservice": {"label": "Freshservice", "group": "support_helpdesk"},
    "helpscout": {"label": "Help Scout", "group": "support_helpdesk"},
    "intercom": {"label": "Intercom", "group": "support_helpdesk"},
    "servicenow": {"label": "ServiceNow", "group": "support_helpdesk"},
    "zammad": {"label": "Zammad", "group": "support_helpdesk"},
    "zendesk": {"label": "Zendesk", "group": "support_helpdesk"},
    "chargebee": {"label": "Chargebee", "group": "ecommerce_billing"},
    "erpnext": {"label": "ERPNext", "group": "ecommerce_billing"},
    "invoiceninja": {"label": "Invoice Ninja", "group": "ecommerce_billing"},
    "magento": {"label": "Magento", "group": "ecommerce_billing"},
    "odoo": {"label": "Odoo", "group": "ecommerce_billing"},
    "paddle": {"label": "Paddle", "group": "ecommerce_billing"},
    "profitwell": {"label": "ProfitWell", "group": "ecommerce_billing"},
    "quickbooks": {"label": "QuickBooks", "group": "ecommerce_billing"},
    "shopify": {"label": "Shopify", "group": "ecommerce_billing"},
    "stripe": {"label": "Stripe", "group": "ecommerce_billing"},
    "tapfiliate": {"label": "Tapfiliate", "group": "ecommerce_billing"},
    "unleashed": {"label": "Unleashed", "group": "ecommerce_billing"},
    "woocommerce": {"label": "WooCommerce", "group": "ecommerce_billing"},
    "xero": {"label": "Xero", "group": "ecommerce_billing"},
    "adalo": {"label": "Adalo", "group": "databases_nocode"},
    "baserow": {"label": "Baserow", "group": "databases_nocode"},
    "bubble": {"label": "Bubble", "group": "databases_nocode"},
    "cockpit": {"label": "Cockpit CMS", "group": "databases_nocode"},
    "coda": {"label": "Coda", "group": "databases_nocode"},
    "grist": {"label": "Grist", "group": "databases_nocode"},
    "kobotoolbox": {"label": "KoboToolbox", "group": "databases_nocode"},
    "nocodb": {"label": "NocoDB", "group": "databases_nocode"},
    "quickbase": {"label": "Quickbase", "group": "databases_nocode"},
    "seatable": {"label": "SeaTable", "group": "databases_nocode"},
    "stackby": {"label": "Stackby", "group": "databases_nocode"},
    "supabase": {"label": "Supabase", "group": "databases_nocode"},
    "demio": {"label": "Demio", "group": "events_webinars"},
    "gotowebinar": {"label": "GoToWebinar", "group": "events_webinars"},
    "zoom": {"label": "Zoom", "group": "events_webinars"},
    "bamboohr": {"label": "BambooHR", "group": "time_tracking_hr"},
    "beeminder": {"label": "Beeminder", "group": "time_tracking_hr"},
    "clockify": {"label": "Clockify", "group": "time_tracking_hr"},
    "harvest": {"label": "Harvest", "group": "time_tracking_hr"},
    "homeassistant": {"label": "Home Assistant", "group": "personal_health"},
    "oura": {"label": "Oura Ring", "group": "personal_health"},
    "philips_hue": {"label": "Philips Hue", "group": "personal_health"},
    "strava": {"label": "Strava", "group": "personal_health"},
    "elastic_security": {"label": "Elastic Security", "group": "security_intel"},
    "jina": {"label": "Jina AI", "group": "security_intel"},
    "mailcheck": {"label": "MailCheck", "group": "security_intel"},
    "misp": {"label": "MISP", "group": "security_intel"},
    "okta": {"label": "Okta", "group": "security_intel"},
    "peekalink": {"label": "Peekalink", "group": "security_intel"},
    "securityscorecard": {"label": "SecurityScorecard", "group": "security_intel"},
    "thehive": {"label": "TheHive", "group": "security_intel"},
    "urlscan": {"label": "urlscan.io", "group": "security_intel"},
}

# Longest-first so prefix matching is deterministic.
_SERVICE_KEYS_BY_LEN = sorted(SERVICE_REGISTRY, key=len, reverse=True)


def get_integration_grouping(tool_name: str) -> Optional[IntegrationGrouping]:
    """Return the functional group + service for an integration tool name.

    Callers gate on ``category == integrations`` before calling this; the
    first-segment fallback keeps an unrecognized (newly added) integration tool
    visible under the ``other`` group rather than dropping it.
    """
    if not tool_name:
        return None
    for key in _SERVICE_KEYS_BY_LEN:
        if tool_name == key or tool_name.startswith(key + "_"):
            svc = SERVICE_REGISTRY[key]
            group_key = svc["group"]
            return {
                "group": group_key,
                "group_label": str(GROUP_META.get(group_key, {}).get("label", group_key)),
                "service": key,
                "service_label": svc["label"],
            }
    segment = tool_name.split("_", 1)[0]
    if not segment:
        return None
    return {
        "group": _OTHER_GROUP,
        "group_label": str(GROUP_META[_OTHER_GROUP]["label"]),
        "service": segment,
        "service_label": segment[:1].upper() + segment[1:],
    }
