// Message types
export type MessageRole = 'user' | 'assistant' | 'system';
export type MessageStatus = 'pending' | 'streaming' | 'complete' | 'error';

// File type discriminator for attachments
export type FileType = 'image' | 'document';

// Generic file attachment for multimodal messages (images and documents)
export interface FileAttachment {
  id: string;              // Unique ID for the attachment
  type: FileType;          // 'image' or 'document'
  dataUrl: string;         // Base64 data URL (data:mime/type;base64,...)
  mimeType: string;        // MIME type (image/jpeg, application/pdf, etc.)
  name: string;            // Original filename
  size: number;            // File size in bytes
}

export interface WorkspaceArtifact {
  path: string;
  name: string;
  mimeType: string;
  sizeBytes: number;
}


// Tool call types (defined early so MessageStep can reference ToolCallStatus)
export type ToolCallStatus = 'pending' | 'running' | 'success' | 'error' | 'cancelled';

// Step in a message - either thinking content or a tool call
// Steps are ordered by arrival time to preserve interleaving
export interface MessageStep {
  type: 'thinking' | 'tool_call' | 'response';
  // For thinking:
  content?: string;
  // For tool_call:
  id?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  result?: string;
  artifacts?: WorkspaceArtifact[];
  status?: ToolCallStatus;
  startTime?: Date;
  endTime?: Date;
}

export interface ToolReloadInfo {
  tools: string[];
  ttl: string;
  ttlSeconds?: number | null;
  source?: string;
  skillName?: string | null;
  reason?: string | null;
  resumePrompt?: string;
}

export interface DispatchInfo {
  threadId: string;
  title: string;
  originalThreadId?: string;
  matchedRef?: string;
}

export interface Message {
  id: string;
  role: MessageRole;
  kind?: 'compaction_notice';
  content: string;
  steps?: MessageStep[];          // Ordered list of thinking/tool_call/response steps
  intermediateContent?: string;   // Legacy history fallback for messages without steps
  timestamp: Date;
  status: MessageStatus;
  toolCalls?: ToolCall[];         // Legacy history fallback for messages without steps
  attachments?: FileAttachment[]; // File attachments for multimodal messages
  contextSummary?: string;        // Context summary from /compact (collapsible in UI)
  messagesRemoved?: number;       // Number of messages summarized by compaction
  autoResumed?: boolean;          // True when assistant output resumed after compaction
  autonomousSource?: string;      // Source of autonomous prompt: 'scheduler' | 'watchdog' | 'trigger'
  toolReloadInfo?: ToolReloadInfo; // Present on messages that follow a tool hot-reload
  dispatchInfo?: DispatchInfo;    // Present on responses routed to another thread
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  result?: string;
  artifacts?: WorkspaceArtifact[];
  status: ToolCallStatus;
  startTime?: Date;
  endTime?: Date;
}

// Thread types
export type ThreadPlatform = 'desktop' | 'callable' | 'discord' | 'telegram' | 'slack' | 'twitch' | 'trigger';

export interface Thread {
  id: string;
  title: string;
  createdAt: Date;
  updatedAt: Date;
  messageCount: number;
  preview?: string;
  platform?: ThreadPlatform;
  platformMeta?: {
    guildName?: string;
    channelName?: string;
    guildId?: string;
    channelId?: string;
  };
  hasCustomConfig?: boolean;
  callable?: boolean;
  pinned?: boolean;
  recovered?: boolean;
  recoverySources?: string[];
}

// Thread organization types
export type SortMode = 'recent' | 'oldest' | 'alphabetical' | 'tasks' | 'active';

export interface ThreadFolder {
  id: string;
  name: string;
  createdAt: Date;
  order: number;          // Manual ordering of folders in sidebar
  threadIds: string[];    // Threads in this folder (ordered)
  collapsed: boolean;     // UI collapse state
  pinned?: boolean;
}

// Per-thread configuration types
export interface ThreadLLMConfig {
  provider?: LLMProvider | null;
  model?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  extended_thinking?: boolean | null;
  reasoning_effort?: string | null;
  use_model_defaults?: boolean | null;
  openai_api_mode?: 'chat_completions' | 'responses' | null;
}

export interface ThreadConfig {
  threadId: string;
  instructions?: string | null;
  disabledTools: string[];
  enabledTools: string[];
  llmConfig?: ThreadLLMConfig | null;
  systemPrompt?: string | null;
  callable: boolean;
  callableName?: string | null;
  callableDescription?: string | null;
  callableMaxIterations?: number | null;
  callableTeamId?: string | null;
  callableTeamName?: string | null;
  enabledSkills: string[];
  disabledSkills: string[];
  injectTodosInPrompt: boolean;
  showAutonomousPrompts: boolean;
  showPromptMetadata?: boolean;
  telegramAutonomousDelivery: 'full' | 'notify_only' | 'off';
  inAppNotificationLevel: 'notify_only' | 'all_autonomous' | 'off';
  createdAt?: string | null;
  updatedAt?: string | null;
  hasCustomizations: boolean;
}

export interface ThreadConfigUpdateRequest {
  instructions?: string | null;
  disabled_tools?: string[] | null;
  enabled_tools?: string[] | null;
  enabled_skills?: string[] | null;
  disabled_skills?: string[] | null;
  llm_config?: Partial<ThreadLLMConfig> | null;
  system_prompt?: string | null;
  callable?: boolean;
  callable_name?: string | null;
  callable_description?: string | null;
  callable_max_iterations?: number | null;
  callable_team_id?: string | null;
  callable_team_name?: string | null;
  inject_todos_in_prompt?: boolean;
  show_autonomous_prompts?: boolean;
  show_prompt_metadata?: boolean;
  telegram_autonomous_delivery?: 'full' | 'notify_only' | 'off';
  in_app_notification_level?: 'notify_only' | 'all_autonomous' | 'off';
  clear_instructions?: boolean;
  clear_disabled_tools?: boolean;
  clear_enabled_tools?: boolean;
  clear_enabled_skills?: boolean;
  clear_disabled_skills?: boolean;
  clear_llm_config?: boolean;
  clear_system_prompt?: boolean;
}

// =========================================================================
// Agent Skills (SKILL.md progressive-disclosure bundles)
// =========================================================================

export type SkillScope = 'bundled' | 'global' | 'user';
export type SkillMarketplaceSource = 'anthropic' | 'clawhub' | 'git';

export interface SkillMetadata {
  name: string;
  description: string;
  scope: SkillScope;
  allowed_tools: string[];
  required_tools: string[];
  tool_ttl: string;
  is_skill_kit: boolean;
  default_active: boolean;
  has_scripts: boolean;
  has_references: boolean;
  has_assets: boolean;
}

export interface SkillDetail extends SkillMetadata {
  body: string;
  path: string;
  license?: string | null;
  scripts: string[];
  references: string[];
}

export interface ThreadActiveSkillsResponse {
  thread_id: string;
  default_enabled: string[];
  enabled_global: string[];
  thread_enabled: string[];
  thread_disabled: string[];
  skills: SkillMetadata[];
}

export interface ThreadCallableToolInfo {
  thread_id: string;
  name: string;
  description?: string | null;
  team_id?: string | null;
  team_name?: string | null;
}

export interface ThreadCallableToolsResponse {
  thread_id: string;
  callable_thread_count: number;
  callable_threads: ThreadCallableToolInfo[];
}

export interface AgentTemplate {
  name: string;
  description: string;
  systemPrompt: string;
  tools: string[];
  allowedTools: string[];
  requiredEnvVars: string[];
  llmProvider?: string | null;
  llmModel?: string | null;
  llmTemperature?: number | null;
  llmMaxTokens?: number | null;
}

export interface AgentThreadCreateRequest {
  callable_name: string;
  callable_description?: string;
  system_prompt?: string;
  llm_provider?: string;
  llm_model?: string;
  llm_temperature?: number;
  llm_max_tokens?: number;
}

export interface OptionalTool {
  name: string;
  description: string;
}

export interface DefaultToolInfo {
  name: string;
  description: string;
  category: string;
  security_level: string;
  is_optional: boolean;
  is_default: boolean;
}

export interface DefaultToolsResponse {
  mode: 'legacy' | 'custom';
  default_tools: string[];
  available_tools: DefaultToolInfo[];
  callable_thread_count: number;
}

export interface ThreadHistory {
  threadId: string;
  messages: Message[];
}

export interface ThreadStatus {
  threadId: string;
  revision: string | null;
  processing: boolean;
}

// TODO types (from backend)
export type TodoStatus = 'pending' | 'in_progress' | 'done';
export type TodoRecurrence = '5min' | '10min' | '15min' | '30min' | 'hourly' | 'daily' | 'weekly' | 'monthly';
export type TodoCreatedBy = 'agent' | 'user';

export interface TodoItem {
  id: string;
  task: string;
  status: TodoStatus;
  createdAt: Date;
  updatedAt: Date;
  notes?: string;
  // Scheduling fields
  scheduledFor?: Date;
  threadId?: string;
  lastExecution?: Date;
  // User management & recurrence fields
  createdBy: TodoCreatedBy;
  recurrence?: TodoRecurrence;
}

export interface TodoCreateRequest {
  task: string;
  notes?: string;
  scheduledFor?: string; // Relative ("2h") or absolute
  recurrence?: TodoRecurrence;
  threadId?: string; // Thread ID for scheduled execution output
}

export interface TodoUpdateRequest {
  task?: string;
  status?: TodoStatus;
  notes?: string;
  scheduledFor?: string; // Relative ("2h") or absolute
  recurrence?: TodoRecurrence;
  threadId?: string; // Thread ID for scheduled execution output
  clearSchedule?: boolean;
  clearRecurrence?: boolean;
}

export interface TodoListResponse {
  userId: string;
  items: TodoItem[];
  total: number;
}


// Activity Log types
export type ActivityType =
  | 'self_invoke'
  | 'watchdog_nudge'
  | 'task_completed'
  | 'task_failed'
  | 'todo_added'
  | 'todo_updated'
  | 'todo_completed'
  | 'todo_deleted'
  | 'trigger_completed';

export interface ActivityEntry {
  id: string;
  timestamp: Date;
  type: ActivityType;
  message: string;
  threadId?: string;
  metadata?: Record<string, unknown>;
}

export interface ActivityLogResponse {
  entries: ActivityEntry[];
  total: number;
}

// Notification types
export interface Notification {
  id: string;
  summary: string;
  threadId?: string;
  taskId?: string;
  createdAt: Date;
  read: boolean;
}

export interface NotificationsResponse {
  notifications: Notification[];
  unreadCount: number;
}

// API types
export interface Tool {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
}

export interface ChatRequest {
  message: string;
  threadId?: string;
  attachments?: FileAttachment[]; // Optional file attachments for multimodal models
  forceUnsupportedAttachments?: boolean;
}

export interface ChatResponse {
  threadId: string;
  response: string;
  toolCalls?: ToolCall[];
}

export interface AttachmentValidationResult {
  compatible: boolean;
  effective_provider: string;
  effective_model: string;
  model_input_modalities: string[];
  required_modalities: string[];
  unsupported_modalities: string[];
  warnings: string[];
  can_force_send: boolean;
}

// SSE Event types
export type SSEEventType =
  | 'thinking'
  | 'tool_call'
  | 'tool_result'
  | 'workspace_artifact'
  | 'dispatched'
  | 'response'
  | 'error'
  | 'done'
  | 'queued'
  | 'compacting'
  | 'compact_result'
  | 'compacted'
  | 'context_attached'
  | 'iteration_limit'
  | 'tool_reload';

export interface SSEEvent {
  type: SSEEventType;
  data: unknown;
  timestamp: Date;
  threadId?: string;  // Backend sends thread_id with every event
}

export interface ThinkingEvent {
  type: 'thinking';
  data: { message: string };
}

export interface ToolCallEvent {
  type: 'tool_call';
  data: {
    id: string;
    name: string;
    arguments: Record<string, unknown>;
  };
}

export interface ToolResultEvent {
  type: 'tool_result';
  data: {
    id?: string;
    name: string;
    result: string;
    status: 'success' | 'error';
  };
}

export interface WorkspaceArtifactEvent {
  type: 'workspace_artifact';
  data: {
    toolCallId?: string;
    toolName: string;
    artifact: WorkspaceArtifact;
  };
}

export interface DispatchedEvent {
  type: 'dispatched';
  data: DispatchInfo;
}

export interface ResponseEvent {
  type: 'response';
  data: {
    content: string;
    isComplete: boolean;
  };
}

export interface ErrorEvent {
  type: 'error';
  data: {
    message: string;
    code?: string;
    details?: Record<string, unknown>;
  };
}

export interface ContextStats {
  threadId: string;
  model: string;
  totalTokens: number;
  inputTokens: number;
  outputTokens: number;
  contextLimit: number;
  usagePercentage: number;
  compactionCount: number;
  lastCompaction: string | null;
  contextManagement: string;
}

export interface DoneEvent {
  type: 'done';
  data: {
    threadId: string;
    contextStats?: ContextStats;
    model?: string;
    title?: string;
    title_source?: string;
    dispatchedTo?: DispatchInfo;
  };
}

// Theme type
export type ThemeName = 'midnight' | 'monokai' | 'dracula' | 'light' | 'high-contrast' | 'platinum';

// Identity of the currently connected Nymeria account, returned by GET /me.
// Drives per-user namespacing of localStorage keys.
export interface AccountIdentity {
  id: string;
  email: string;
  display_name: string;
  role: 'user' | 'admin';
}

// Account types — mirror of Pydantic models in Nymeria/nymeria/triggers/api.py.
// Returned by /admin/users and /me/tokens endpoints.
export type UserRole = 'user' | 'admin';

export interface AdminUser {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  disabled: boolean;
  created_at: string;
  updated_at: string;
  token_count: number;
  last_token_use: string | null;
  thread_count?: number;
  todo_count?: number;
  platform_count?: number;
}

export interface TokenInfo {
  // First 8 hex chars of the token's sha256 — stable revoke handle.
  token_hash_prefix: string;
  label: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface IssuedTokenResponse {
  // Raw token shown ONCE — UI must surface in a copy-once dialog and drop.
  raw_token: string;
  metadata: TokenInfo;
}

export interface RotatedTokensResponse {
  raw_token: string;
  metadata: TokenInfo;
  revoked_count: number;
}

export interface PlatformIdentity {
  provider: 'discord' | 'telegram' | 'twitch';
  provider_user_id: string;
  created_at: string;
}

// Per-thread chat-app binding (e.g. mobile thread <-> Telegram chat).
// Returned by GET /threads/{id}/chatapp/bindings. ``user_telegram_bot_id``
// is null for bindings served by the shared Nymeria bot, and an integer
// row id for bindings served by the user's own (BYO) Telegram bot.
export interface ChatAppBinding {
  id: number;
  thread_id: string;
  provider: 'discord' | 'telegram' | 'twitch';
  platform_chat_id: string;
  created_at: string;
  user_telegram_bot_id?: number | null;
}

// User-owned Telegram bot registered via BotFather token paste. The token
// itself is never exposed by the API after registration -- only the
// public-facing metadata. ``last_seen_at`` is set the first time the
// supervisor process successfully starts a polling loop for this bot.
export interface MyTelegramBot {
  id: number;
  bot_username: string;
  enabled: boolean;
  created_at: string;
  last_seen_at: string | null;
}

// Response for the bind-code and platform-link-code endpoints. The UI shows
// `code` to the user; if `bot_username` is configured server-side, `deep_link`
// is a one-tap Telegram URL that pre-fills the right command.
export interface ChatAppBindCodeResponse {
  code: string;
  expires_at: string;
  bot_username?: string | null;
  deep_link?: string | null;
}

// Config types
export interface AppConfig {
  apiUrl: string;
  apiKey: string;
  setupCompleted?: boolean;
  theme?: ThemeName;
  suppressAttachmentWarnings?: boolean;
  identity?: AccountIdentity | null;
}

// Server settings types
export type LLMProvider = string;
export type OpenAIApiMode = 'chat_completions' | 'responses';
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';

export interface LLMProviderSpec {
  id: string;
  label: string;
  api_format: string;
  default_base_url: string | null;
  api_key_env_vars: string[];
  base_url_env_vars: string[];
  default_model: string | null;
  default_api_mode: OpenAIApiMode | string;
  supports_chat_completions: boolean;
  supports_responses: boolean;
  requires_api_key: boolean;
  requires_base_url: boolean;
  docs_url: string | null;
  notes: string;
  aliases: string[];
}

// Available model from provider (from GET /models/available)
export interface AvailableModel {
  id: string;
  name: string;
  owned_by: string;
  created: number | null;
  context_length?: number | null;
  max_completion_tokens?: number | null;
  supported_parameters?: string[];
  input_modalities?: string[];
  tokenizer?: string | null;
  default_temperature?: number | null;
  default_top_p?: number | null;
  default_frequency_penalty?: number | null;
  pricing_prompt?: number | null;
  pricing_completion?: number | null;
}

// OpenRouter model metadata (from GET /models)
export interface ModelMetadata {
  id: string;
  name: string;
  context_length: number;
  max_completion_tokens: number | null;
  pricing_prompt: number | null;
  pricing_completion: number | null;
  supported_parameters: string[];
  input_modalities: string[];
  tokenizer: string | null;
  default_temperature: number | null;
  default_top_p: number | null;
  default_frequency_penalty: number | null;
}

export interface ServerSettings {
  llm_provider: LLMProvider;
  llm_model: string;
  llm_fallback_models: string[];
  llm_temperature: number;
  llm_max_tokens: number | null;
  llm_top_p: number | null;
  llm_top_k: number | null;
  llm_frequency_penalty: number | null;
  llm_presence_penalty: number | null;
  llm_reasoning_effort: string | null;
  llm_extended_thinking: boolean;
  llm_use_model_defaults: boolean;
  llm_base_url: string | null;
  openai_api_mode: OpenAIApiMode | null;
  llm_stream_max_retries: number;
  llm_stream_retry_initial_delay: number;
  llm_stream_retry_max_delay: number;
  context_management: string;
  compact_threshold: number;
  compact_keep_messages: number;
  compact_model: string | null;
  sliding_window_cycles: number;
  tool_output_max_chars: number;
  log_level: LogLevel;
  watchdog_enabled: boolean;
  watchdog_interval_minutes: number;
  todo_staleness_minutes: number;
  activity_retention_hours: number;
  // Voice settings
  tts_provider: string;
  tts_base_url: string | null;
  tts_model: string;
  tts_voice: string;
  tts_output_format: string;
  tts_speed: number;
  stt_provider: string;
  stt_base_url: string | null;
  stt_model: string;
  stt_language: string | null;
  voice_default_thread_id: string | null;
}

export interface ServerSettingsUpdate {
  llm_provider?: LLMProvider;
  llm_model?: string;
  llm_fallback_models?: string;
  llm_temperature?: number;
  llm_max_tokens?: number | null;
  llm_top_p?: number | null;
  llm_top_k?: number | null;
  llm_frequency_penalty?: number | null;
  llm_presence_penalty?: number | null;
  llm_reasoning_effort?: string | null;
  llm_extended_thinking?: boolean;
  llm_use_model_defaults?: boolean;
  llm_base_url?: string | null;
  openai_api_mode?: OpenAIApiMode | null;
  // Provider/capability credentials are write-only through PATCH /settings.
  anthropic_api_key?: string | null;
  anthropic_direct_api_key?: string | null;
  openai_api_key?: string | null;
  openrouter_api_key?: string | null;
  embedding_api_key?: string | null;
  gemini_api_key?: string | null;
  perplexity_api_key?: string | null;
  wolfram_alpha_app_id?: string | null;
  searxng_base_url?: string | null;
  nasa_api_key?: string | null;
  openweathermap_api_key?: string | null;
  npm_registry_url?: string | null;
  github_token?: string | null;
  github_api_base_url?: string | null;
  gitlab_token?: string | null;
  gitlab_base_url?: string | null;
  bitly_token?: string | null;
  bitly_base_url?: string | null;
  brandfetch_api_key?: string | null;
  brandfetch_base_url?: string | null;
  marketstack_api_key?: string | null;
  marketstack_base_url?: string | null;
  deepl_api_key?: string | null;
  deepl_api_plan?: string | null;
  deepl_base_url?: string | null;
  todoist_api_key?: string | null;
  todoist_base_url?: string | null;
  trello_api_key?: string | null;
  trello_api_token?: string | null;
  trello_base_url?: string | null;
  asana_access_token?: string | null;
  asana_base_url?: string | null;
  linear_api_key?: string | null;
  linear_api_url?: string | null;
  jira_email?: string | null;
  jira_api_token?: string | null;
  jira_access_token?: string | null;
  jira_base_url?: string | null;
  clickup_access_token?: string | null;
  clickup_base_url?: string | null;
  slack_bot_token?: string | null;
  slack_access_token?: string | null;
  slack_base_url?: string | null;
  notion_api_key?: string | null;
  notion_version?: string | null;
  notion_base_url?: string | null;
  airtable_access_token?: string | null;
  airtable_api_key?: string | null;
  airtable_base_url?: string | null;
  hubspot_access_token?: string | null;
  hubspot_base_url?: string | null;
  zendesk_email?: string | null;
  zendesk_api_token?: string | null;
  zendesk_access_token?: string | null;
  zendesk_subdomain?: string | null;
  zendesk_base_url?: string | null;
  mailchimp_api_key?: string | null;
  mailchimp_access_token?: string | null;
  mailchimp_server_prefix?: string | null;
  mailchimp_base_url?: string | null;
  freshdesk_api_key?: string | null;
  freshdesk_domain?: string | null;
  freshdesk_base_url?: string | null;
  helpscout_access_token?: string | null;
  helpscout_base_url?: string | null;
  intercom_access_token?: string | null;
  intercom_base_url?: string | null;
  intercom_version?: string | null;
  pipedrive_api_token?: string | null;
  pipedrive_access_token?: string | null;
  pipedrive_base_url?: string | null;
  twilio_account_sid?: string | null;
  twilio_auth_token?: string | null;
  twilio_api_key_sid?: string | null;
  twilio_base_url?: string | null;
  sendgrid_api_key?: string | null;
  sendgrid_base_url?: string | null;
  mailgun_api_key?: string | null;
  mailgun_domain?: string | null;
  mailgun_base_url?: string | null;
  brevo_api_key?: string | null;
  brevo_base_url?: string | null;
  mailjet_api_key?: string | null;
  mailjet_secret_key?: string | null;
  mailjet_sms_token?: string | null;
  mailjet_base_url?: string | null;
  mandrill_api_key?: string | null;
  mandrill_base_url?: string | null;
  messagebird_access_key?: string | null;
  messagebird_base_url?: string | null;
  mocean_api_key?: string | null;
  mocean_api_secret?: string | null;
  mocean_base_url?: string | null;
  msg91_auth_key?: string | null;
  msg91_base_url?: string | null;
  stripe_secret_key?: string | null;
  stripe_base_url?: string | null;
  shopify_shop?: string | null;
  shopify_access_token?: string | null;
  shopify_api_key?: string | null;
  shopify_password?: string | null;
  shopify_api_version?: string | null;
  shopify_base_url?: string | null;
  woocommerce_url?: string | null;
  woocommerce_base_url?: string | null;
  woocommerce_consumer_key?: string | null;
  woocommerce_consumer_secret?: string | null;
  chargebee_api_key?: string | null;
  chargebee_site?: string | null;
  chargebee_base_url?: string | null;
  pushbullet_access_token?: string | null;
  pushbullet_base_url?: string | null;
  pushcut_api_key?: string | null;
  pushcut_base_url?: string | null;
  gotify_base_url?: string | null;
  gotify_app_token?: string | null;
  gotify_client_token?: string | null;
  pushover_api_token?: string | null;
  pushover_user_key?: string | null;
  pushover_base_url?: string | null;
  signl4_team_secret?: string | null;
  signl4_webhook_url?: string | null;
  signl4_base_url?: string | null;
  wordpress_url?: string | null;
  wordpress_username?: string | null;
  wordpress_password?: string | null;
  strapi_url?: string | null;
  strapi_api_token?: string | null;
  strapi_email?: string | null;
  strapi_password?: string | null;
  strapi_api_version?: string | null;
  contentful_space_id?: string | null;
  contentful_delivery_token?: string | null;
  contentful_preview_token?: string | null;
  contentful_base_url?: string | null;
  contentful_preview_base_url?: string | null;
  ghost_url?: string | null;
  ghost_content_api_key?: string | null;
  ghost_admin_api_key?: string | null;
  ghost_api_version?: string | null;
  storyblok_content_token?: string | null;
  storyblok_management_token?: string | null;
  storyblok_space_id?: string | null;
  storyblok_content_base_url?: string | null;
  storyblok_management_base_url?: string | null;
  netlify_access_token?: string | null;
  netlify_base_url?: string | null;
  uptimerobot_api_key?: string | null;
  uptimerobot_base_url?: string | null;
  pagerduty_api_token?: string | null;
  pagerduty_from_email?: string | null;
  pagerduty_base_url?: string | null;
  sentry_auth_token?: string | null;
  sentry_base_url?: string | null;
  cloudflare_api_token?: string | null;
  cloudflare_base_url?: string | null;
  urlscan_api_key?: string | null;
  urlscan_base_url?: string | null;
  hunter_api_key?: string | null;
  hunter_base_url?: string | null;
  mailcheck_api_key?: string | null;
  mailcheck_base_url?: string | null;
  peekalink_api_key?: string | null;
  peekalink_base_url?: string | null;
  jina_api_key?: string | null;
  jina_reader_base_url?: string | null;
  jina_search_base_url?: string | null;
  jina_deepsearch_base_url?: string | null;
  baserow_api_token?: string | null;
  baserow_base_url?: string | null;
  nocodb_api_token?: string | null;
  nocodb_base_url?: string | null;
  nocodb_auth_header?: string | null;
  coda_api_token?: string | null;
  coda_base_url?: string | null;
  grist_api_key?: string | null;
  grist_base_url?: string | null;
  discord_base_url?: string | null;
  mattermost_access_token?: string | null;
  mattermost_base_url?: string | null;
  matrix_access_token?: string | null;
  matrix_base_url?: string | null;
  rocketchat_auth_token?: string | null;
  rocketchat_user_id?: string | null;
  rocketchat_base_url?: string | null;
  zulip_api_key?: string | null;
  zulip_email?: string | null;
  zulip_base_url?: string | null;
  google_books_api_key?: string | null;
  google_books_base_url?: string | null;
  youtube_api_key?: string | null;
  youtube_base_url?: string | null;
  spotify_access_token?: string | null;
  spotify_client_id?: string | null;
  spotify_client_secret?: string | null;
  spotify_base_url?: string | null;
  spotify_accounts_base_url?: string | null;
  reddit_access_token?: string | null;
  reddit_refresh_token?: string | null;
  reddit_client_id?: string | null;
  reddit_client_secret?: string | null;
  reddit_base_url?: string | null;
  reddit_public_base_url?: string | null;
  reddit_token_url?: string | null;
  discourse_api_key?: string | null;
  discourse_api_username?: string | null;
  discourse_base_url?: string | null;
  medium_access_token?: string | null;
  medium_base_url?: string | null;
  llm_stream_max_retries?: number;
  llm_stream_retry_initial_delay?: number;
  llm_stream_retry_max_delay?: number;
  context_management?: string;
  compact_threshold?: number;
  compact_keep_messages?: number;
  compact_model?: string | null;
  sliding_window_cycles?: number;
  tool_output_max_chars?: number;
  log_level?: LogLevel;
  watchdog_enabled?: boolean;
  watchdog_interval_minutes?: number;
  todo_staleness_minutes?: number;
  activity_retention_hours?: number;
  // Voice settings
  tts_provider?: string;
  tts_base_url?: string | null;
  tts_model?: string;
  tts_voice?: string;
  tts_output_format?: string;
  tts_speed?: number;
  stt_provider?: string;
  stt_base_url?: string | null;
  stt_model?: string;
  stt_language?: string | null;
  voice_default_thread_id?: string | null;
}

export interface LLMProviderTestRequest {
  llm_provider: LLMProvider;
  llm_model: string;
  api_key: string;
  llm_base_url?: string | null;
  openai_api_mode?: OpenAIApiMode | null;
}

export interface LLMProviderTestResponse {
  ok: boolean;
  provider: LLMProvider;
  model: string;
  message: string;
  openai_api_mode: OpenAIApiMode | null;
  status_code: number | null;
  error_type: string | null;
}

export interface LLMProviderTestSuiteRequest {
  llm_provider: LLMProvider;
  llm_model?: string | null;
  api_key?: string | null;
  llm_base_url?: string | null;
  openai_api_mode?: OpenAIApiMode | null;
  run_model_list?: boolean;
  run_chat_completion?: boolean;
  run_tool_call?: boolean;
  allow_billable?: boolean;
  prefer_free_model?: boolean;
  timeout_seconds?: number;
}

export interface LLMProviderTestSuiteStep {
  name: string;
  status: 'passed' | 'failed' | 'warning' | 'skipped';
  ok: boolean;
  message: string;
  url: string | null;
  status_code: number | null;
  latency_ms: number | null;
  error_type: string | null;
  metadata: Record<string, unknown>;
}

export interface LLMProviderTestSuiteResponse {
  ok: boolean;
  provider: LLMProvider;
  requested_provider: LLMProvider;
  model: string | null;
  effective_base_url: string | null;
  effective_api_mode: OpenAIApiMode | null;
  credential_source: string;
  models_count: number | null;
  message: string;
  steps: LLMProviderTestSuiteStep[];
}

// Custom Tool Types

export type ToolParameterType = 'string' | 'integer' | 'number' | 'boolean' | 'array' | 'object';
export type ToolImplementationType = 'http' | 'mcp';
export type HTTPMethod = 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
export type ResponseFormat = 'json' | 'text' | 'auto';

export interface ToolParameter {
  type: ToolParameterType;
  description: string;
  required: boolean;
  default?: string;
  enum?: string[];
}

export interface HTTPToolConfig {
  method: HTTPMethod;
  url: string;
  headers: Record<string, string>;
  bodyTemplate?: string;
  queryParams: Record<string, string>;
  timeoutSeconds: number;
  responsePath?: string;
  responseFormat: ResponseFormat;
}

export interface MCPToolConfig {
  serverCommand: string;
  serverArgs: string[];
  toolName: string;
  envVars: Record<string, string>;
  workingDirectory?: string;
  idleTimeoutSeconds: number;
  startupTimeoutSeconds: number;
}

export interface CustomTool {
  id: string;
  name: string;
  description: string;
  parameters: Record<string, ToolParameter>;
  implementationType: ToolImplementationType;
  httpConfig?: HTTPToolConfig;
  mcpConfig?: MCPToolConfig;
  enabled: boolean;
  tags: string[];
  createdAt: Date;
  updatedAt: Date;
}

export interface CustomToolCreateRequest {
  id: string;
  name: string;
  description: string;
  parameters?: Record<string, ToolParameter>;
  implementationType: ToolImplementationType;
  httpConfig?: {
    method: HTTPMethod;
    url: string;
    headers?: Record<string, string>;
    body_template?: string;
    query_params?: Record<string, string>;
    timeout_seconds?: number;
    response_path?: string;
    response_format?: ResponseFormat;
  };
  mcpConfig?: {
    server_command: string;
    server_args?: string[];
    tool_name: string;
    env_vars?: Record<string, string>;
    working_directory?: string;
    idle_timeout_seconds?: number;
    startup_timeout_seconds?: number;
  };
  enabled?: boolean;
  tags?: string[];
}

export interface CustomToolUpdateRequest {
  name?: string;
  description?: string;
  parameters?: Record<string, ToolParameter>;
  httpConfig?: CustomToolCreateRequest['httpConfig'];
  mcpConfig?: CustomToolCreateRequest['mcpConfig'];
  enabled?: boolean;
  tags?: string[];
}

export interface CustomToolListResponse {
  tools: CustomTool[];
  total: number;
}

export interface CustomToolTestRequest {
  params: Record<string, unknown>;
}

export interface CustomToolTestResponse {
  status: 'ok' | 'error';
  toolId: string;
  result?: string;
  error?: string;
  success: boolean;
  executionTimeMs: number;
}

// Tool Types

export type ToolSecurityLevel = 'safe' | 'moderate' | 'sensitive';
export type ToolCategory = 'core' | 'memory' | 'profile' | 'notepad' | 'self_modify' | 'todo' | 'trigger' | 'email' | 'browser' | 'image' | 'calendar' | 'google_docs' | 'integrations' | 'custom' | 'mcp_server';
export type ToolType = 'builtin' | 'custom' | 'mcp_server' | 'callable_thread';

export interface ToolSearchResult {
  name: string;
  description: string;
  category: string;
  securityLevel: ToolSecurityLevel | string;
  toolType: ToolType | string;
  isDefault: boolean;
  status: string | null;
  score: number;
  enableHint: string;
}

export interface ToolSearchResponse {
  query: string;
  mode: 'semantic' | 'bm25' | 'fuzzy' | 'substring';
  warning?: string | null;
  results: ToolSearchResult[];
}

export interface ToolCategoriesResponse {
  categories: Record<ToolCategory, string[]>;
}

// Unified Tool Types

export interface UnifiedTool {
  id: string;
  name: string;
  description: string;  // Effective description (custom if set, else default)
  defaultDescription: string;  // Original tool description
  customDescription?: string | null;  // User's custom description override
  category: ToolCategory | string;
  securityLevel: ToolSecurityLevel;
  enabled: boolean;
  enabledReason: 'default' | 'user_override' | 'category_disabled' | 'globally_disabled';
  toolType: ToolType;
  implementationType: ToolImplementationType | null;
  configSchema?: Record<string, unknown>;
  userConfig: Record<string, unknown>;
  configurable: boolean;  // True if tool has config_schema
  parameters?: Record<string, unknown>;
  httpConfig?: {
    url: string;
    method: string;
    headers?: Record<string, string>;
    bodyTemplate?: string;
    timeout?: number;
    retries?: number;
  };
  mcpConfig?: {
    server: string;
    tool: string;
  };
  tags: string[];
  editable: boolean;
  createdAt?: string;
  updatedAt?: string;
}

export interface UnifiedToolListResponse {
  tools: UnifiedTool[];
  total: number;
  builtinCount: number;
  customCount: number;
}

// Credential Vault Types

export type CredentialOwnerType = 'user' | 'system';
export type CredentialStatus = 'active' | 'pending_setup' | 'invalid' | 'disabled' | string;

export interface Credential {
  id: string;
  ownerType: CredentialOwnerType;
  ownerUserId: string | null;
  name: string;
  provider: string;
  kind: string;
  accountLabel: string | null;
  status: CredentialStatus;
  metadata: Record<string, unknown>;
  scopes: string[];
  allowedTargets: string[];
  expiresAt: string | null;
  lastUsedAt: string | null;
  lastTestedAt: string | null;
  disabledAt: string | null;
  createdByUserId: string | null;
  createdAt: string;
  updatedAt: string;
  secretFields: string[];
  hasSecret: boolean;
}

export interface CredentialListResponse {
  credentials: Credential[];
  total: number;
}

export interface CredentialCreateRequest {
  owner_type?: CredentialOwnerType;
  owner_user_id?: string | null;
  name: string;
  provider: string;
  kind?: string;
  account_label?: string | null;
  metadata?: Record<string, unknown>;
  scopes?: string[];
  allowed_targets?: string[];
  expires_at?: string | null;
  status?: CredentialStatus;
  secret_fields?: Record<string, string>;
}

export interface CredentialUpdateRequest {
  name?: string;
  account_label?: string | null;
  metadata?: Record<string, unknown>;
  scopes?: string[];
  allowed_targets?: string[];
  expires_at?: string | null;
  status?: CredentialStatus;
  secret_fields?: Record<string, string>;
}

export interface CredentialSetupSessionRequest {
  provider: string;
  kind?: string;
  name: string;
  target_type?: string | null;
  target_id?: string | null;
  required_fields?: string[];
  metadata?: Record<string, unknown>;
}

export interface CredentialBinding {
  id: string;
  credentialId: string;
  targetType: string;
  targetId: string;
  bindingName: string | null;
  createdByUserId: string | null;
  createdAt: string;
}

export interface CredentialBindingRequest {
  target_type: string;
  target_id: string;
  binding_name?: string | null;
}

// MCP Server Types

export interface MCPDiscoveredTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface MCPServer {
  id: string;
  name: string;
  description: string;
  serverCommand: string;
  serverArgs: string[];
  envVars: Record<string, string>;
  workingDirectory?: string;
  idleTimeoutSeconds: number;
  startupTimeoutSeconds: number;
  enabled: boolean;
  discoveredTools: MCPDiscoveredTool[];
  createdAt: string;
  updatedAt: string;
}

export interface MCPServerCreateRequest {
  id: string;
  name: string;
  description?: string;
  server_command: string;
  server_args?: string[];
  env_vars?: Record<string, string>;
  working_directory?: string;
  idle_timeout_seconds?: number;
  startup_timeout_seconds?: number;
  enabled?: boolean;
}

export interface MCPServerUpdateRequest {
  name?: string;
  description?: string;
  server_command?: string;
  server_args?: string[];
  env_vars?: Record<string, string>;
  working_directory?: string;
  idle_timeout_seconds?: number;
  startup_timeout_seconds?: number;
  enabled?: boolean;
}

export interface MCPServerListResponse {
  servers: MCPServer[];
  total: number;
}

// Trigger Types

export type TriggerActionType = 'agent_prompt' | 'notify' | 'create_todo';
export type TriggerCreatedBy = 'user' | 'agent';

export interface TriggerSourceSchemaField {
  type: 'string' | 'boolean' | 'integer' | 'number' | 'object' | 'array';
  description: string;
  required: boolean;
  default?: unknown;
  placeholder?: string;
  enum?: string[];
  group?: string;
  order?: number;
  secret?: boolean;
}

export interface TriggerSourceInfo {
  name: string;
  description: string;
  config_schema: Record<string, TriggerSourceSchemaField>;
  category: string;
  icon: string;
  setup_guide: string;
  template_variables: string[];
  example_config: Record<string, unknown>;
  requires_auth: string | null;
}

export interface TriggerCondition {
  field: string;
  operator: 'equals' | 'contains' | 'starts_with' | 'matches_regex' | 'not_equals';
  value: string;
  case_sensitive?: boolean;
}

export interface TriggerAction {
  type: TriggerActionType;
  config: Record<string, unknown>;
}

export type TriggerHealthStatus = 'healthy' | 'degraded' | 'failing';

export interface Trigger {
  id: string;
  name: string;
  source_type: string;
  source_config: Record<string, unknown>;
  action: TriggerAction;
  conditions: TriggerCondition[];
  enabled: boolean;
  cooldown_seconds: number;
  last_fired: string | null;
  fire_count: number;
  thread_id: string;
  created_at: string;
  created_by: TriggerCreatedBy;
  consecutive_errors: number;
  last_error: string | null;
  health_status: TriggerHealthStatus;
}

export interface TriggerCreateRequest {
  name: string;
  source_type: string;
  source_config: Record<string, unknown>;
  action_type: TriggerActionType;
  action_config: Record<string, unknown>;
  conditions?: TriggerCondition[];
  cooldown_seconds?: number;
  enabled?: boolean;
  thread_id?: string;
}

export interface TriggerUpdateRequest {
  name?: string;
  enabled?: boolean;
  source_config?: Record<string, unknown>;
  action_type?: TriggerActionType;
  action_config?: Record<string, unknown>;
  conditions?: TriggerCondition[];
  cooldown_seconds?: number;
}

export interface TriggerExecution {
  id: string;
  trigger_id: string;
  trigger_name: string;
  timestamp: string;
  status: 'success' | 'error' | 'partial' | 'deferred';
  event_count: number;
  events_summary: string;
  response_summary: string;
  error_message: string | null;
  duration_seconds: number;
  action_type: string;
}

export interface TriggerTestResult {
  sample_event: Record<string, unknown>;
  rendered_output: string;
  action_type: string;
  template_variables_used: string[];
  conditions_pass: boolean;
}
