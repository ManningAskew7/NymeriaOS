export interface ToolCategoryInfo {
  name: string;
  icon: string;
  description: string;
}

export const CATEGORY_INFO: Record<string, ToolCategoryInfo> = {
  general: {
    name: 'General',
    icon: 'terminal',
    description: 'General-purpose tools like bash, file operations, and web search',
  },
  profile: {
    name: 'Profile',
    icon: 'brain',
    description: 'Tools for saving and retrieving user memories and preferences',
  },
  notepad: {
    name: 'Notepad',
    icon: 'sticky-note',
    description: 'Per-thread persistent notes that survive context compaction',
  },
  self_modify: {
    name: 'Self-Modify',
    icon: 'code',
    description: 'Tools that allow Nymeria to modify her own code (sensitive)',
  },
  todo: {
    name: 'TODOs',
    icon: 'list',
    description: 'Task management and scheduling tools',
  },
  subagent: {
    name: 'Utilities',
    icon: 'refresh',
    description: 'Utility tools for delegated or supporting work',
  },
  trigger: {
    name: 'Triggers',
    icon: 'zap',
    description: 'Event-driven trigger management tools',
  },
  email: {
    name: 'Outlook Email',
    icon: 'mail',
    description: 'Outlook email tools for reading, sending, and managing mail',
  },
  browser: {
    name: 'Browser',
    icon: 'globe',
    description: 'Playwright browser automation tools for web interaction',
  },
  image: {
    name: 'Image Generation',
    icon: 'image',
    description: 'Image generation tools for creating workspace image artifacts',
  },
  calendar: {
    name: 'Google Calendar',
    icon: 'calendar',
    description: 'Google Calendar tools for managing events and schedules',
  },
  google_docs: {
    name: 'Google Docs',
    icon: 'file-text',
    description: 'Google Docs tools for reading, writing, and formatting documents',
  },
  integrations: {
    name: 'Integrations',
    icon: 'plug',
    description: 'Native service integrations and public data utilities',
  },
  skills: {
    name: 'Agent Skills',
    icon: 'bolt',
    description: 'Skill installation and execution tools',
  },
  custom: {
    name: 'Custom',
    icon: 'puzzle',
    description: 'User-created custom tools (HTTP, MCP, etc.)',
  },
  mcp_server: {
    name: 'MCP Servers',
    icon: 'server',
    description: 'Tools auto-discovered from MCP servers',
  },
};

export const CATEGORY_ORDER = [
  'general',
  'profile',
  'notepad',
  'todo',
  'trigger',
  'email',
  'browser',
  'image',
  'calendar',
  'integrations',
  'skills',
  'self_modify',
  'subagent',
  'custom',
];

export function getCategoryInfo(category: string): ToolCategoryInfo {
  return (
    CATEGORY_INFO[category] || {
      name: category,
      icon: 'tool',
      description: '',
    }
  );
}

// ── Integration functional sub-groups ────────────────────────────────
// The Integrations category is the only one that nests a second level
// (functional group -> service -> tools). The backend taxonomy
// (Nymeria/nymeria/tools/integration_taxonomy.py) is the source of truth for
// which group/service a tool belongs to and for its labels; the frontend owns
// only the icon and display order, so they stay tied to the local Icon set.
// `icon` values must be names defined in components/common/Icon.svelte.

export interface ToolGroupInfo {
  label: string;
  icon: string;
  order: number;
}

// Keyed by the backend group key. Generated to mirror integration_taxonomy.py;
// keep keys in sync when the taxonomy is regenerated. A backend group key with
// no entry here falls back to the backend-provided label, a generic icon, and a
// trailing sort order, so a newly added group is never hidden.
export const GROUP_INFO: Record<string, ToolGroupInfo> = {
  utilities: { label: "Utilities & Reference", icon: "tool", order: 10 },
  reference_data: { label: "Reference & Public Data", icon: "info", order: 20 },
  developer_tools: { label: "Developer Tools", icon: "terminal", order: 30 },
  cloud_infrastructure: { label: "Cloud Infrastructure & Storage", icon: "server", order: 40 },
  monitoring_ops: { label: "Monitoring & Operations", icon: "bolt", order: 50 },
  messaging_chat: { label: "Messaging & Chat", icon: "chat", order: 60 },
  messaging_delivery: { label: "Messaging & Email Delivery", icon: "send", order: 70 },
  notifications: { label: "Notifications", icon: "bell", order: 80 },
  productivity: { label: "Productivity & Collaboration", icon: "folderOpen", order: 90 },
  project_management: { label: "Project Management", icon: "check", order: 100 },
  content_cms: { label: "Content & CMS", icon: "fileText", order: 110 },
  social_publishing: { label: "Social & Publishing", icon: "users", order: 120 },
  media_entertainment: { label: "Media & Entertainment", icon: "image", order: 130 },
  crm_sales: { label: "CRM, Sales & Lead Enrichment", icon: "user", order: 140 },
  marketing_email: { label: "Marketing & Email Automation", icon: "send", order: 150 },
  support_helpdesk: { label: "Support & Helpdesk", icon: "info", order: 160 },
  ecommerce_billing: { label: "E-commerce & Billing", icon: "copy", order: 170 },
  databases_nocode: { label: "Databases & No-Code", icon: "folder", order: 180 },
  events_webinars: { label: "Events & Webinars", icon: "calendar", order: 190 },
  time_tracking_hr: { label: "Time Tracking & HR", icon: "clock", order: 200 },
  personal_health: { label: "Personal Devices & Health", icon: "pin", order: 210 },
  security_intel: { label: "Security & Intelligence", icon: "warning", order: 220 },
  other: { label: "Other Integrations", icon: "tool", order: 999 },
};

export function getGroupInfo(group: string, fallbackLabel?: string): ToolGroupInfo {
  return (
    GROUP_INFO[group] || {
      label: fallbackLabel || group,
      icon: 'tool',
      order: 999,
    }
  );
}
