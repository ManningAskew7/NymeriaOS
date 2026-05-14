export interface ToolCategoryInfo {
  name: string;
  icon: string;
  description: string;
}

export const CATEGORY_INFO: Record<string, ToolCategoryInfo> = {
  core: {
    name: 'Core',
    icon: 'terminal',
    description: 'Essential system tools like bash, file operations, and web search',
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
    description: 'Native service integrations inspired by n8n agent tool nodes',
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
  'core',
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
