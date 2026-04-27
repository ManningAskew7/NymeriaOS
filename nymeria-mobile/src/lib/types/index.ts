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

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  steps?: MessageStep[];          // Ordered list of thinking/tool_call steps
  intermediateContent?: string;   // Legacy: concatenated thinking (computed from steps)
  timestamp: Date;
  status: MessageStatus;
  toolCalls?: ToolCall[];         // Legacy: all tool calls (computed from steps)
  attachments?: FileAttachment[]; // File attachments for multimodal messages
  contextSummary?: string;        // Context summary from /compact (collapsible in UI)
  autonomousSource?: string;      // Source of autonomous prompt: 'scheduler' | 'watchdog' | 'trigger'
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
export type ThreadPlatform = 'desktop' | 'callable' | 'discord' | 'telegram' | 'slack' | 'trigger';

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
  llm_config?: Partial<ThreadLLMConfig> | null;
  system_prompt?: string | null;
  callable?: boolean;
  callable_name?: string | null;
  callable_description?: string | null;
  callable_max_iterations?: number | null;
  inject_todos_in_prompt?: boolean;
  show_autonomous_prompts?: boolean;
  show_prompt_metadata?: boolean;
  telegram_autonomous_delivery?: 'full' | 'notify_only' | 'off';
  in_app_notification_level?: 'notify_only' | 'all_autonomous' | 'off';
  clear_instructions?: boolean;
  clear_disabled_tools?: boolean;
  clear_enabled_tools?: boolean;
  clear_llm_config?: boolean;
  clear_system_prompt?: boolean;
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


// Scheduled Task types
export type ScheduledTaskStatus = 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled';

export interface ScheduledTask {
  id: string;
  prompt: string;
  executeAt: Date;
  status: ScheduledTaskStatus;
  createdAt: Date;
  threadId: string;
}

export interface ScheduledTasksResponse {
  tasks: ScheduledTask[];
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
  | 'response'
  | 'error'
  | 'done'
  | 'queued'
  | 'compacting'
  | 'compact_result'
  | 'compacted'
  | 'context_attached'
  | 'iteration_limit';

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
export type LLMProvider = 'openrouter' | 'openai' | 'anthropic';
export type OpenAIApiMode = 'chat_completions' | 'responses';
export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';

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
  context_management: string;
  compact_threshold: number;
  compact_keep_messages: number;
  compact_model: string | null;
  sliding_window_cycles: number;
  max_self_invokes_per_hour: number;
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
  context_management?: string;
  compact_threshold?: number;
  compact_keep_messages?: number;
  compact_model?: string | null;
  sliding_window_cycles?: number;
  max_self_invokes_per_hour?: number;
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

// Built-in Tool Types

export type ToolSecurityLevel = 'safe' | 'moderate' | 'sensitive';
export type ToolCategory = 'core' | 'memory' | 'self_modify' | 'todo' | 'trigger' | 'email' | 'browser' | 'calendar' | 'google_docs' | 'custom' | 'mcp_server';
export type ToolType = 'builtin' | 'custom' | 'mcp_server';

export interface BuiltInTool {
  name: string;
  description: string;
  category: ToolCategory;
  securityLevel: ToolSecurityLevel;
  defaultEnabled: boolean;
  enabled: boolean;
  enabledReason: 'default' | 'user_override' | 'category_disabled' | 'globally_disabled';
  globallyDisabled: boolean;
  configSchema?: Record<string, unknown>;
  userConfig?: Record<string, unknown>;
}

export interface BuiltInToolsResponse {
  userId: string;
  tools: BuiltInTool[];
  byCategory: Record<ToolCategory, BuiltInTool[]>;
  total: number;
}

export interface ToolPreferences {
  enabledOverrides: Record<string, boolean>;
  disabledCategories: string[];
  toolConfigs: Record<string, Record<string, unknown>>;
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
