<script lang="ts">
  import { onMount } from 'svelte';
  import type { Thread, ThreadConfig, ThreadConfigUpdateRequest, UnifiedTool, AvailableModel } from '$lib/types';
  import { Icon } from '$lib/components/common';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { api } from '$lib/services/api.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import TriggerConfigTab from '$lib/components/triggers/TriggerConfigTab.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import { ToolCountWarning } from '$lib/components/tools';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { chatAppBindingsStore } from '$lib/stores/chatAppBindings.svelte';
  import ConnectTelegramWizard from './ConnectTelegramWizard.svelte';
  import ConnectMyTelegramBotWizard from './ConnectMyTelegramBotWizard.svelte';

  type ThreadSettingsTab = 'instructions' | 'system-prompt' | 'agent' | 'model' | 'tools' | 'mcp' | 'skills' | 'triggers' | 'chatapp';
  type TelegramAutonomousDelivery = ThreadConfig['telegramAutonomousDelivery'];
  type InAppNotificationLevel = ThreadConfig['inAppNotificationLevel'];

  interface Props {
    thread: Thread;
    threadConfig: ThreadConfig | null;
    initialTab?: ThreadSettingsTab;
    onClose: () => void;
    onSaved: (config: ThreadConfig) => void;
  }

  let { thread, threadConfig, initialTab = 'instructions', onClose, onSaved }: Props = $props();

  // Active tab
  let activeTab = $state<ThreadSettingsTab>('instructions');

  $effect(() => {
    activeTab = initialTab;
  });

  // Chat App tab state — binding count is reactive via chatAppBindingsStore
  let showChatAppWizard = $state(false);
  let showMyBotWizard = $state(false);
  let chatAppLoaded = $state(false);
  let chatAppLoadError = $state<string | null>(null);

  $effect(() => {
    // Load bindings the first time the user opens the Chat App tab. The
    // wizard refreshes on its own when it completes a bind, so we only
    // need to populate once for the initial render.
    if (activeTab === 'chatapp' && !chatAppLoaded) {
      chatAppLoaded = true;
      chatAppBindingsStore
        .loadBindings(thread.id)
        .catch((err) => {
          chatAppLoadError = err instanceof Error ? err.message : String(err);
        });
    }
  });

  let chatAppBindings = $derived(chatAppBindingsStore.getBindings(thread.id));

  async function handleUnbindChatApp(bindingId: number) {
    try {
      await chatAppBindingsStore.unbind(thread.id, bindingId);
    } catch (err) {
      chatAppLoadError = err instanceof Error ? err.message : String(err);
    }
  }

  // Per-thread skill overrides
  function getInitialThreadEnabledSkills(): Set<string> {
    return new Set(threadConfig?.enabledSkills ?? []);
  }

  function getInitialThreadDisabledSkills(): Set<string> {
    return new Set(threadConfig?.disabledSkills ?? []);
  }

  let threadEnabledSkills = $state<Set<string>>(getInitialThreadEnabledSkills());
  let threadDisabledSkills = $state<Set<string>>(getInitialThreadDisabledSkills());

  // Form state — initialized from threadConfig
  function getInitialInstructions(): string {
    return threadConfig?.instructions ?? '';
  }

  let instructions = $state(getInitialInstructions());

  // Derive initial tool state: if no per-thread config exists and global defaults
  // are customized, compute disabled/enabled from the default tool set so the UI
  // reflects what the agent will actually receive.
  function computeInitialToolState(): { disabled: Set<string>; enabled: Set<string> } {
    if (threadConfig?.hasCustomizations) {
      // Thread has its own customized config — use it directly
      return {
        disabled: new Set(threadConfig.disabledTools ?? []),
        enabled: new Set(threadConfig.enabledTools ?? []),
      };
    }
    // No per-thread overrides for uncustomized threads.
    // The default tool set already defines what's loaded.
    return { disabled: new Set(), enabled: new Set() };
  }

  const initialToolState = computeInitialToolState();
  let disabledTools = $state<Set<string>>(initialToolState.disabled);
  let enabledTools = $state<Set<string>>(initialToolState.enabled);

  function isMcpToolName(name: string): boolean {
    return name.startsWith('mcp__');
  }

  // Optional non-MCP tools (derived from defaultToolsStore — tools NOT in the user's core set)
  const optionalTools = $derived(() => {
    if (!defaultToolsStore.loaded) return [];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return defaultToolsStore.tools
      .filter(t => !coreSet.has(t.name) && !t.name.startsWith('mcp__'))
      .map(t => ({ name: t.name, description: t.description }));
  });

  // MCP tools grouped by server. Default MCP tools can be disabled for this
  // thread; non-default MCP tools can be enabled for this thread. Server
  // lifecycle controls live in the global MCP tab.
  const mcpServersForThread = $derived.by(() => {
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return mcpServersStore.servers.map(server => {
      const tools = server.discoveredTools
        .map(t => ({
          mcpName: `mcp__${server.id}__${t.name}`,
          shortName: t.name,
          description: t.description,
        }))
        .map(t => ({
          ...t,
          isDefault: coreSet.has(t.mcpName),
        }));
      return {
        id: server.id,
        name: server.name,
        enabled: server.enabled,
        discoveredCount: server.discoveredTools.length,
        tools,
      };
    });
  });

  let expandedMcpServer = $state<string | null>(null);

  // Display provider mapping for thread-level overrides
  // "" = Default (inherit global), "anthropic_proxy" = subscription, "anthropic_direct" = direct API,
  // "openai_custom" = openai provider pointed at a custom OpenAI-compatible endpoint
  // (local server, CLIProxy sidecar, etc.)
  type ThreadDisplayProvider = '' | 'anthropic_proxy' | 'anthropic_direct' | 'openai' | 'openrouter' | 'openai_custom';

  // Default placeholder for openai_custom — the GPT-5.5 CLIProxy sidecar is the
  // common target on this stack. Users can edit freely.
  const DEFAULT_CUSTOM_OPENAI_BASE_URL = 'http://cli-proxy-api-latest:8317/v1';

  function toThreadDisplayProvider(provider: string, baseUrl?: string | null): ThreadDisplayProvider {
    if (!provider) return '';
    if (provider === 'anthropic' && baseUrl === '') return 'anthropic_direct';
    if (provider === 'anthropic') return 'anthropic_proxy';
    if (provider === 'openai' && baseUrl) return 'openai_custom';
    return provider as ThreadDisplayProvider;
  }

  function fromThreadDisplayProvider(dp: ThreadDisplayProvider): { provider: string; baseUrl: string | null } {
    if (dp === '') return { provider: '', baseUrl: null };
    if (dp === 'anthropic_proxy') return { provider: 'anthropic', baseUrl: null };
    if (dp === 'anthropic_direct') return { provider: 'anthropic', baseUrl: '' };
    if (dp === 'openai_custom') return { provider: 'openai', baseUrl: DEFAULT_CUSTOM_OPENAI_BASE_URL };
    return { provider: dp, baseUrl: null };
  }

  function getInitialThreadDisplayProvider(): ThreadDisplayProvider {
    return toThreadDisplayProvider(
      threadConfig?.llmConfig?.provider ?? '',
      threadConfig?.llmConfig?.base_url
    );
  }

  function getInitialLlmProvider(): string {
    return threadConfig?.llmConfig?.provider ?? '';
  }

  function getInitialLlmModel(): string {
    return threadConfig?.llmConfig?.model ?? '';
  }

  function getInitialLlmBaseUrl(): string {
    return threadConfig?.llmConfig?.base_url ?? '';
  }

  function getInitialLlmApiKey(): string {
    return threadConfig?.llmConfig?.api_key ?? '';
  }

  function getInitialLlmTemperature(): string {
    return threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature)
      : '';
  }

  function getInitialLlmMaxTokens(): string {
    return threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens)
      : '';
  }

  function getInitialLlmExtendedThinking(): 'default' | 'true' | 'false' {
    return threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) as 'true' | 'false'
      : 'default';
  }

  function getInitialLlmReasoningEffort(): string {
    return threadConfig?.llmConfig?.reasoning_effort ?? '';
  }

  function getInitialLlmUseModelDefaults(): 'default' | 'true' | 'false' {
    return threadConfig?.llmConfig?.use_model_defaults != null
      ? String(threadConfig.llmConfig.use_model_defaults) as 'true' | 'false'
      : 'default';
  }

  function getInitialLlmOpenAiApiMode(): 'default' | 'chat_completions' | 'responses' {
    return threadConfig?.llmConfig?.openai_api_mode ?? 'default';
  }

  // LLM form state
  let threadDisplayProvider = $state<ThreadDisplayProvider>(getInitialThreadDisplayProvider());
  let llmProvider = $state(getInitialLlmProvider());
  let llmModel = $state(getInitialLlmModel());
  let llmBaseUrl = $state(getInitialLlmBaseUrl());
  let llmApiKey = $state(getInitialLlmApiKey());
  let llmTemperature = $state<string>(getInitialLlmTemperature());
  let llmMaxTokens = $state<string>(getInitialLlmMaxTokens());
  let llmExtendedThinking = $state<'default' | 'true' | 'false'>(getInitialLlmExtendedThinking());
  let llmReasoningEffort = $state(getInitialLlmReasoningEffort());
  let llmUseModelDefaults = $state<'default' | 'true' | 'false'>(getInitialLlmUseModelDefaults());
  let llmOpenAiApiMode = $state<'default' | 'chat_completions' | 'responses'>(getInitialLlmOpenAiApiMode());

  // Model metadata (reactive lookup)
  const threadModelMeta = $derived(modelsStore.getById(llmModel));

  // Dynamic model list for providers that support /v1/models
  let availableModels = $state<AvailableModel[]>([]);
  let loadingAvailableModels = $state(false);
  let availableModelsProvider = $state<string>('');

  // Effective provider: thread override or global default
  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

  function supportsApiMode(provider: string = getEffectiveProvider()): boolean {
    return provider === 'openai' || provider === 'openrouter';
  }

  async function fetchAvailableModels(provider: string) {
    if (provider !== 'anthropic' && provider !== 'openai') {
      availableModels = [];
      availableModelsProvider = '';
      return;
    }
    if (availableModelsProvider === provider && availableModels.length > 0) return;
    loadingAvailableModels = true;
    try {
      availableModels = await api.getAvailableModels(provider);
      availableModelsProvider = provider;
    } catch {
      availableModels = [];
    } finally {
      loadingAvailableModels = false;
    }
  }

  // Sync llmProvider from display provider and fetch models
  // (skip the model fetch for openai_custom — we don't want to call the real
  // OpenAI API through the global base URL, and the user types the model
  // name the sidecar/local server reports as free text.)
  $effect(() => {
    const { provider } = fromThreadDisplayProvider(threadDisplayProvider);
    llmProvider = provider;
    if (threadDisplayProvider === 'openai_custom') {
      availableModels = [];
      availableModelsProvider = '';
      return;
    }
    const ep = getEffectiveProvider();
    fetchAvailableModels(ep);
  });

  // Auto-populate the base URL field when the user picks "OpenAI (Custom base URL)"
  // if empty. Don't stomp an existing value.
  $effect(() => {
    if (threadDisplayProvider === 'openai_custom' && !llmBaseUrl) {
      llmBaseUrl = DEFAULT_CUSTOM_OPENAI_BASE_URL;
    }
  });

  // System prompt & agent fields
  function getInitialSystemPrompt(): string {
    return threadConfig?.systemPrompt ?? '';
  }

  function getInitialCallable(): boolean {
    return threadConfig?.callable ?? false;
  }

  function getInitialCallableName(): string {
    return threadConfig?.callableName ?? '';
  }

  function getInitialCallableDescription(): string {
    return threadConfig?.callableDescription ?? '';
  }

  let systemPrompt = $state(getInitialSystemPrompt());
  let isCallable = $state(getInitialCallable());
  let callableName = $state(getInitialCallableName());
  let callableDescription = $state(getInitialCallableDescription());

  // Visibility / advanced
  function getInitialInjectTodosInPrompt(): boolean {
    return threadConfig?.injectTodosInPrompt ?? false;
  }

  function getInitialShowAutonomousPrompts(): boolean {
    return threadConfig?.showAutonomousPrompts ?? false;
  }

  function getInitialShowPromptMetadata(): boolean {
    return threadConfig?.showPromptMetadata ?? false;
  }

  function getInitialTelegramAutonomousDelivery(): TelegramAutonomousDelivery {
    return threadConfig?.telegramAutonomousDelivery ?? 'full';
  }

  function getInitialInAppNotificationLevel(): InAppNotificationLevel {
    return threadConfig?.inAppNotificationLevel ?? 'notify_only';
  }

  let injectTodosInPrompt = $state(getInitialInjectTodosInPrompt());
  let showAutonomousPrompts = $state(getInitialShowAutonomousPrompts());
  let showPromptMetadata = $state(getInitialShowPromptMetadata());
  let telegramAutonomousDelivery = $state<TelegramAutonomousDelivery>(getInitialTelegramAutonomousDelivery());
  let inAppNotificationLevel = $state<InAppNotificationLevel>(getInitialInAppNotificationLevel());

  // Search
  let toolSearch = $state('');
  let saving = $state(false);
  let error = $state('');
  let showToolWarning = $state(false);

  // Effective tool count for this thread (default tools minus disabled, plus optional enabled)
  const effectiveToolCount = $derived.by(() => {
    const coreNames = defaultToolsStore.defaultToolNames;
    const activeCore = coreNames.filter(n => !disabledTools.has(n)).length;
    return activeCore + enabledTools.size;
  });

  // Ensure tools, triggers, and model metadata are loaded
  $effect(() => {
    if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) {
      unifiedToolsStore.loadTools();
    }
    if (!triggersStore.loaded && !triggersStore.loading) {
      triggersStore.loadTriggers();
    }
    if (!modelsStore.loaded && !modelsStore.loading) {
      modelsStore.loadModels();
    }
    if (!defaultToolsStore.loaded && !defaultToolsStore.loading) {
      defaultToolsStore.load();
    }
    if (!skillsStore.installedLoaded && !skillsStore.installedLoading) {
      skillsStore.loadInstalled();
    }
    if (!skillsStore.enabledGlobalLoaded && !skillsStore.enabledGlobalLoading) {
      skillsStore.loadGlobal();
    }
  });

  // Force-refresh MCP servers + default tools on panel mount so changes made
  // in the global Settings → MCP panel (new server installed, defaults
  // edited) are reflected here without a full app reload.
  onMount(() => {
    mcpServersStore.refresh();
    defaultToolsStore.resetLoaded();
    void defaultToolsStore.load();
  });

  // Per-thread skill resolution: (global ∪ enabled) − disabled
  const resolvedActiveSkillNames = $derived.by(() => {
    const seen = new Set<string>();
    for (const n of skillsStore.enabledGlobal) {
      if (!threadDisabledSkills.has(n)) seen.add(n);
    }
    for (const n of threadEnabledSkills) {
      if (!threadDisabledSkills.has(n)) seen.add(n);
    }
    return seen;
  });

  function toggleThreadSkillEnabled(name: string) {
    const next = new Set(threadEnabledSkills);
    if (next.has(name)) next.delete(name);
    else {
      next.add(name);
      // When explicitly enabling, clear any disable override for this skill.
      if (threadDisabledSkills.has(name)) {
        const d = new Set(threadDisabledSkills);
        d.delete(name);
        threadDisabledSkills = d;
      }
    }
    threadEnabledSkills = next;
  }

  function toggleThreadSkillDisabled(name: string) {
    const next = new Set(threadDisabledSkills);
    if (next.has(name)) next.delete(name);
    else {
      next.add(name);
      if (threadEnabledSkills.has(name)) {
        const e = new Set(threadEnabledSkills);
        e.delete(name);
        threadEnabledSkills = e;
      }
    }
    threadDisabledSkills = next;
  }

  const toolsLoadError = $derived(unifiedToolsStore.error || defaultToolsStore.error);
  const toolsReady = $derived(unifiedToolsStore.loaded && defaultToolsStore.loaded);
  const toolsLoading = $derived(unifiedToolsStore.loading || defaultToolsStore.loading || !toolsReady);

  const filteredTools = $derived.by(() => {
    if (!defaultToolsStore.loaded) return [];

    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    let allTools = unifiedToolsStore.tools.filter(t => coreSet.has(t.name) && !isMcpToolName(t.name) && t.category !== 'mcp_server');
    if (!toolSearch.trim()) return allTools;
    const q = toolSearch.toLowerCase();
    return allTools.filter(
      (t: UnifiedTool) =>
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q)
    );
  });

  const disabledToolCount = $derived([...disabledTools].filter((name) => !isMcpToolName(name)).length);

  const enabledToolCount = $derived([...enabledTools].filter((name) => !isMcpToolName(name)).length);

  const mcpOverrideCount = $derived(
    [...disabledTools].filter(isMcpToolName).length +
    [...enabledTools].filter(isMcpToolName).length
  );

  function isMcpThreadToolEnabled(tool: { mcpName: string; isDefault: boolean }): boolean {
    return tool.isDefault ? !disabledTools.has(tool.mcpName) : enabledTools.has(tool.mcpName);
  }

  function toggleMcpThreadTool(tool: { mcpName: string; isDefault: boolean }) {
    if (tool.isDefault) {
      toggleTool(tool.mcpName);
    } else {
      toggleOptionalTool(tool.mcpName);
    }
  }

  function toggleOptionalTool(toolName: string) {
    const next = new Set(enabledTools);
    if (next.has(toolName)) {
      next.delete(toolName);
    } else {
      next.add(toolName);
    }
    enabledTools = next;
  }

  function toggleTool(toolName: string) {
    const next = new Set(disabledTools);
    if (next.has(toolName)) {
      next.delete(toolName);
    } else {
      next.add(toolName);
    }
    disabledTools = next;
  }

  function hasChanges(): boolean {
    const origInstructions = threadConfig?.instructions ?? '';
    const origDisabled = new Set(threadConfig?.disabledTools ?? []);
    const origProvider = threadConfig?.llmConfig?.provider ?? '';
    const origModel = threadConfig?.llmConfig?.model ?? '';
    const origBaseUrl = threadConfig?.llmConfig?.base_url ?? '';
    const origApiKey = threadConfig?.llmConfig?.api_key ?? '';
    const origTemp = threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature) : '';
    const origMaxTokens = threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens) : '';
    const origExtThinking = threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) : 'default';
    const origReasoning = threadConfig?.llmConfig?.reasoning_effort ?? '';
    const origUseModelDefaults = threadConfig?.llmConfig?.use_model_defaults != null
      ? String(threadConfig.llmConfig.use_model_defaults) : 'default';
    const origOpenAiApiMode = threadConfig?.llmConfig?.openai_api_mode ?? 'default';

    const origEnabled = new Set(threadConfig?.enabledTools ?? []);
    const origSystemPrompt = threadConfig?.systemPrompt ?? '';
    const origCallable = threadConfig?.callable ?? false;
    const origCallableName = threadConfig?.callableName ?? '';
    const origCallableDescription = threadConfig?.callableDescription ?? '';

    if (instructions !== origInstructions) return true;
    if (disabledTools.size !== origDisabled.size) return true;
    for (const t of disabledTools) {
      if (!origDisabled.has(t)) return true;
    }
    if (enabledTools.size !== origEnabled.size) return true;
    for (const t of enabledTools) {
      if (!origEnabled.has(t)) return true;
    }
    const origEnabledSkills = new Set(threadConfig?.enabledSkills ?? []);
    const origDisabledSkills = new Set(threadConfig?.disabledSkills ?? []);
    if (threadEnabledSkills.size !== origEnabledSkills.size) return true;
    for (const s of threadEnabledSkills) if (!origEnabledSkills.has(s)) return true;
    if (threadDisabledSkills.size !== origDisabledSkills.size) return true;
    for (const s of threadDisabledSkills) if (!origDisabledSkills.has(s)) return true;
    if (llmProvider !== origProvider) return true;
    if (llmModel !== origModel) return true;
    if (llmBaseUrl !== origBaseUrl) return true;
    if (llmApiKey !== origApiKey) return true;
    if (llmTemperature !== origTemp) return true;
    if (llmMaxTokens !== origMaxTokens) return true;
    if (llmExtendedThinking !== origExtThinking) return true;
    if (llmReasoningEffort !== origReasoning) return true;
    if (llmUseModelDefaults !== origUseModelDefaults) return true;
    if (llmOpenAiApiMode !== origOpenAiApiMode) return true;
    if (systemPrompt !== origSystemPrompt) return true;
    if (isCallable !== origCallable) return true;
    if (callableName !== origCallableName) return true;
    if (callableDescription !== origCallableDescription) return true;

    const origInjectTodos = threadConfig?.injectTodosInPrompt ?? false;
    if (injectTodosInPrompt !== origInjectTodos) return true;

    const origShowAutonomous = threadConfig?.showAutonomousPrompts ?? false;
    if (showAutonomousPrompts !== origShowAutonomous) return true;

    const origShowPromptMeta = threadConfig?.showPromptMetadata ?? false;
    if (showPromptMetadata !== origShowPromptMeta) return true;

    const origTelegramDelivery = threadConfig?.telegramAutonomousDelivery ?? 'full';
    if (telegramAutonomousDelivery !== origTelegramDelivery) return true;

    const origNotificationLevel = threadConfig?.inAppNotificationLevel ?? 'notify_only';
    if (inAppNotificationLevel !== origNotificationLevel) return true;

    return false;
  }

  function checkToolCountAndSave() {
    const toolCount = effectiveToolCount;
    const callableCount = defaultToolsStore.callableThreadCount;
    if (toolCount + callableCount > 25 && !showToolWarning) {
      showToolWarning = true;
      return;
    }
    showToolWarning = false;
    handleSave();
  }

  async function handleSave() {
    saving = true;
    error = '';

    try {
      const updates: ThreadConfigUpdateRequest = {};

      // Instructions
      if (instructions.trim()) {
        updates.instructions = instructions.trim();
      } else {
        updates.clear_instructions = true;
      }

      // Disabled tools — only persist tools that are actually in the default set
      const coreSet = new Set(defaultToolsStore.defaultToolNames);
      const effectiveDisabled = [...disabledTools].filter(t => coreSet.has(t));
      if (effectiveDisabled.length > 0) {
        updates.disabled_tools = effectiveDisabled;
      } else {
        updates.clear_disabled_tools = true;
      }

      // Enabled optional tools
      if (enabledTools.size > 0) {
        updates.enabled_tools = Array.from(enabledTools);
      } else {
        updates.clear_enabled_tools = true;
      }

      // Per-thread skill overrides
      if (threadEnabledSkills.size > 0) {
        updates.enabled_skills = Array.from(threadEnabledSkills);
      } else {
        updates.clear_enabled_skills = true;
      }
      if (threadDisabledSkills.size > 0) {
        updates.disabled_skills = Array.from(threadDisabledSkills);
      } else {
        updates.clear_disabled_skills = true;
      }

      // LLM config
      const hasLlm = threadDisplayProvider || llmModel || llmTemperature || llmMaxTokens ||
        llmExtendedThinking !== 'default' || llmReasoningEffort ||
        llmUseModelDefaults !== 'default' || llmOpenAiApiMode !== 'default' || llmApiKey;

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        const mapped = fromThreadDisplayProvider(threadDisplayProvider);
        llm.provider = mapped.provider || null;
        // For openai_custom, persist the user-editable base URL (not the default
        // from fromThreadDisplayProvider, which is just a placeholder).
        if (threadDisplayProvider === 'openai_custom') {
          llm.base_url = llmBaseUrl || DEFAULT_CUSTOM_OPENAI_BASE_URL;
        } else {
          llm.base_url = mapped.baseUrl;
        }
        llm.api_key = llmApiKey || null;
        llm.model = llmModel || null;
        llm.temperature = llmTemperature ? parseFloat(llmTemperature) : null;
        llm.max_tokens = llmMaxTokens ? parseInt(llmMaxTokens, 10) : null;
        if (llmExtendedThinking !== 'default') {
          llm.extended_thinking = llmExtendedThinking === 'true';
        }
        llm.reasoning_effort = llmReasoningEffort || null;
        if (llmUseModelDefaults !== 'default') {
          llm.use_model_defaults = llmUseModelDefaults === 'true';
        } else {
          // Explicitly clear to remove stale per-thread override
          llm.use_model_defaults = null;
        }
        llm.openai_api_mode = supportsApiMode() && llmOpenAiApiMode !== 'default'
          ? llmOpenAiApiMode
          : null;
        updates.llm_config = llm;
      } else {
        updates.clear_llm_config = true;
      }

      // System prompt
      if (systemPrompt.trim()) {
        updates.system_prompt = systemPrompt.trim();
      } else {
        updates.clear_system_prompt = true;
      }

      // Callable fields
      updates.callable = isCallable;
      if (isCallable) {
        updates.callable_name = callableName.trim() || null;
        updates.callable_description = callableDescription.trim() || null;
      }

      // Visibility / advanced
      updates.inject_todos_in_prompt = injectTodosInPrompt;
      updates.show_autonomous_prompts = showAutonomousPrompts;
      updates.show_prompt_metadata = showPromptMetadata;
      updates.telegram_autonomous_delivery = telegramAutonomousDelivery;
      updates.in_app_notification_level = inAppNotificationLevel;

      const result = await threadConfigStore.updateConfig(thread.id, updates);

      // Sync sidebar title to callable name (backend already updated metadata,
      // so this is local-only to avoid stale title until next full sync)
      if (isCallable && callableName.trim()) {
        threadsStore.applyBackendTitle(thread.id, callableName.trim());
      }

      onSaved(result);

      // Refresh context stats so status bar shows new model + correct percentage
      const savedThreadId = thread.id;
      api.getThreadContextStats(savedThreadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === savedThreadId) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats.model);
        }
      });

      onClose();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      saving = false;
    }
  }

  async function handleReset() {
    saving = true;
    error = '';
    try {
      await threadConfigStore.deleteConfig(thread.id);
      // Reset form
      instructions = '';
      disabledTools = new Set();
      enabledTools = new Set();
      threadEnabledSkills = new Set();
      threadDisabledSkills = new Set();
      llmProvider = '';
      llmModel = '';
      llmTemperature = '';
      llmMaxTokens = '';
      llmExtendedThinking = 'default';
      llmReasoningEffort = '';
      systemPrompt = '';
      isCallable = false;
      callableName = '';
      callableDescription = '';
      showAutonomousPrompts = false;
      showPromptMetadata = false;
      telegramAutonomousDelivery = 'full';
      inAppNotificationLevel = 'notify_only';
      onSaved({
        threadId: thread.id,
        instructions: null,
        disabledTools: [],
        enabledTools: [],
        enabledSkills: [],
        disabledSkills: [],
        llmConfig: null,
        systemPrompt: null,
        callable: false,
        callableName: null,
        callableDescription: null,
        injectTodosInPrompt: false,
        showAutonomousPrompts: false,
        showPromptMetadata: false,
        telegramAutonomousDelivery: 'full',
        inAppNotificationLevel: 'notify_only',
        createdAt: null,
        updatedAt: null,
        hasCustomizations: false,
      });

      // Refresh context stats so status bar reverts to global model
      const resetThreadId = thread.id;
      api.getThreadContextStats(resetThreadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === resetThreadId) {
          chatStore.setContextStats(stats);
          chatStore.setActiveModel(stats.model);
        }
      });

      onClose();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to reset';
    } finally {
      saving = false;
    }
  }

  function handleBackdropClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('modal-backdrop')) {
      onClose();
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="modal-backdrop" onclick={handleBackdropClick} onkeydown={handleKeydown} role="dialog" aria-modal="true" tabindex="-1">
  <div class="modal-panel">
    <div class="modal-header">
      <h2>Thread Settings</h2>
      <span class="modal-subtitle">{thread.title}</span>
      <button class="close-btn" onclick={onClose} type="button">
        <Icon name="x" size={18} />
      </button>
    </div>

    <div class="tabs">
      <button
        class="tab"
        class:active={activeTab === 'instructions'}
        onclick={() => (activeTab = 'instructions')}
        type="button"
      >
        Instructions
      </button>
      <button
        class="tab"
        class:active={activeTab === 'system-prompt'}
        onclick={() => (activeTab = 'system-prompt')}
        type="button"
      >
        System Prompt
        {#if systemPrompt.trim()}
          <span class="tab-badge">1</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'agent'}
        onclick={() => (activeTab = 'agent')}
        type="button"
      >
        Agent
        {#if isCallable}
          <span class="tab-badge">1</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'model'}
        onclick={() => (activeTab = 'model')}
        type="button"
      >
        Model
      </button>
      <button
        class="tab"
        class:active={activeTab === 'tools'}
        onclick={() => (activeTab = 'tools')}
        type="button"
      >
        Tools
        {#if disabledToolCount > 0}
          <span class="tab-badge">{disabledToolCount}</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'mcp'}
        onclick={() => (activeTab = 'mcp')}
        type="button"
      >
        MCP
        {#if mcpOverrideCount > 0}
          <span class="tab-badge">{mcpOverrideCount}</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'skills'}
        onclick={() => (activeTab = 'skills')}
        type="button"
      >
        Skills
        {#if resolvedActiveSkillNames.size > 0}
          <span class="tab-badge">{resolvedActiveSkillNames.size}</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'triggers'}
        onclick={() => (activeTab = 'triggers')}
        type="button"
      >
        Triggers
        {#if triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length > 0}
          <span class="tab-badge">{triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length}</span>
        {/if}
      </button>
      <button
        class="tab"
        class:active={activeTab === 'chatapp'}
        onclick={() => (activeTab = 'chatapp')}
        type="button"
      >
        Chat App
        {#if chatAppBindings.length > 0}
          <span class="tab-badge">{chatAppBindings.length}</span>
        {/if}
      </button>
    </div>

    <div class="tab-content">
      {#if activeTab === 'instructions'}
        <div class="tab-panel">
          <label class="field-label" for="thread-instructions">
            Custom Instructions
          </label>
          <p class="field-hint">
            Appended to the base system prompt for this thread only.
          </p>
          <textarea
            id="thread-instructions"
            class="instructions-input"
            bind:value={instructions}
            placeholder="e.g. Focus on email management. Be concise. Always check the calendar before scheduling."
            maxlength={5000}
            rows={8}
          ></textarea>
          <span class="char-count">{instructions.length} / 5000</span>

          <div class="visibility-section">
            <h3 class="section-title">Advanced</h3>

            <label class="toggle-row">
              <input type="checkbox" bind:checked={injectTodosInPrompt} />
              <span class="toggle-label">Inject TODOs into system prompt</span>
            </label>
            <p class="field-hint">
              Include active TODOs directly in the system prompt so the LLM can
              see and act on them without tool calls. Uses extra context tokens.
            </p>

            <label class="toggle-row">
              <input type="checkbox" bind:checked={showAutonomousPrompts} />
              <span class="toggle-label">Show autonomous prompts</span>
            </label>
            <p class="field-hint">
              Show the prompts sent by the scheduler, watchdog, and triggers as
              messages in the chat. Useful for debugging autonomous behavior.
            </p>

            <label class="toggle-row">
              <input type="checkbox" bind:checked={showPromptMetadata} />
              <span class="toggle-label">Show prompt metadata</span>
            </label>
            <p class="field-hint">
              Show the time context and trigger type prepended to each message.
              Useful for debugging prompt flow and callable thread routing.
            </p>
          </div>
        </div>

      {:else if activeTab === 'system-prompt'}
        <div class="tab-panel">
          <label class="field-label" for="system-prompt-input">
            Custom System Prompt
          </label>
          <p class="field-hint">
            Replaces the base system prompt (soul.md) entirely for this thread.
            Leave empty to use the default.
          </p>
          <textarea
            id="system-prompt-input"
            class="instructions-input system-prompt-input"
            bind:value={systemPrompt}
            placeholder="You are a specialized assistant that..."
            maxlength={50000}
            rows={12}
          ></textarea>
          <span class="char-count">{systemPrompt.length} / 50000</span>
        </div>

      {:else if activeTab === 'agent'}
        <div class="tab-panel">
          <div class="agent-config-section">
            <h3 class="section-title">Agent Configuration</h3>
            <p class="field-hint">
              Mark this thread as a callable sub-agent. When enabled, Nymeria can delegate tasks to this thread.
            </p>

            <label class="toggle-row">
              <input type="checkbox" bind:checked={isCallable} />
              <span class="toggle-label">Make Callable</span>
            </label>

            {#if isCallable}
              <div class="field-group">
                <label class="field-label" for="callable-name-input">Callable Name</label>
                <p class="field-hint">The tool name Nymeria uses to call this thread (e.g., "BrowserAgent").</p>
                <input
                  id="callable-name-input"
                  class="field-input"
                  type="text"
                  bind:value={callableName}
                  placeholder="e.g. ResearchAgent"
                  maxlength={64}
                />
              </div>

              <div class="field-group">
                <label class="field-label" for="callable-desc-input">Callable Description</label>
                <p class="field-hint">What the LLM sees as the tool description. Describe when to use this thread.</p>
                <textarea
                  id="callable-desc-input"
                  class="instructions-input"
                  bind:value={callableDescription}
                  placeholder="e.g. Autonomous web research that finds information, summarizes articles, and compiles reports"
                  maxlength={500}
                  rows={3}
                ></textarea>
                <span class="char-count">{callableDescription.length} / 500</span>
              </div>
            {/if}
          </div>
        </div>

      {:else if activeTab === 'model'}
        <div class="tab-panel">
          <div class="field-group">
            <label class="field-label" for="llm-provider">Provider</label>
            <select id="llm-provider" class="field-select" bind:value={threadDisplayProvider}>
              <option value="">Default (inherit global)</option>
              <option value="anthropic_proxy">Anthropic (Subscription)</option>
              <option value="anthropic_direct">Anthropic (Direct API)</option>
              <option value="openai">OpenAI</option>
              <option value="openrouter">OpenRouter</option>
              <option value="openai_custom">OpenAI (Custom base URL)</option>
            </select>
          </div>

          {#if threadDisplayProvider === 'openai_custom'}
            <div class="field-group">
              <label class="field-label" for="llm-base-url">API Base URL</label>
              <input
                id="llm-base-url"
                class="field-input"
                type="text"
                bind:value={llmBaseUrl}
                placeholder="http://cli-proxy-api-latest:8317/v1"
              />
              <span class="field-hint">
                Any OpenAI-compatible endpoint reachable from inside the Nymeria api container, such as a CLIProxy sidecar (e.g. <code>cli-proxy-api-latest:8317/v1</code>) or a local inference server (<code>host.docker.internal:8080/v1</code>).
              </span>
            </div>

            <div class="field-group">
              <label class="field-label" for="llm-api-key">API Key (Optional)</label>
              <input
                id="llm-api-key"
                class="field-input"
                type="password"
                bind:value={llmApiKey}
                placeholder="Leave empty to inherit global provider key"
                autocomplete="off"
              />
              <span class="field-hint">
                Required when pointing at a CLIProxy sidecar with its own <code>api-keys</code> list (e.g. <code>cpx-latest-local-test</code> for the GPT-5.5 sidecar). Stored per-thread in the Nymeria data directory.
              </span>
            </div>
          {/if}

          {#if supportsApiMode()}
            <div class="field-group">
              <label class="field-label" for="llm-openai-api-mode">API Mode</label>
              <select id="llm-openai-api-mode" class="field-select" bind:value={llmOpenAiApiMode}>
                <option value="default">Default (inherit global)</option>
                <option value="chat_completions">Chat Completions (not recommended if thinking is enabled)</option>
                <option value="responses">Responses API</option>
              </select>
              <span class="field-hint">
                Responses API is the default for OpenAI-compatible reasoning models and OpenRouter beta. Chat Completions remains available as a compatibility override.
              </span>
            </div>
          {/if}

          <div class="field-group">
            <label class="field-label" for="llm-model">Model</label>
            {#if threadDisplayProvider === 'openai_custom'}
              <input
                id="llm-model"
                class="field-input"
                type="text"
                bind:value={llmModel}
                placeholder="Model name (e.g. gpt-5.5, local-llm)"
              />
              <span class="field-hint">
                The model name the endpoint reports (e.g. <code>gpt-5.5</code> for the CLIProxy sidecar, or the <code>--alias</code> flag value for a local server).
              </span>
            {:else if availableModels.length > 0}
              <select id="llm-model" class="field-select" bind:value={llmModel}>
                <option value="">Default (inherit global)</option>
                {#each availableModels as model}
                  <option value={model.id}>{model.name || model.id}</option>
                {/each}
              </select>
            {:else}
              <input
                id="llm-model"
                class="field-input"
                type="text"
                bind:value={llmModel}
                placeholder={loadingAvailableModels
                  ? 'Loading models… or type one (e.g. claude-opus-4-7)'
                  : 'Leave empty for global default'}
              />
              {#if loadingAvailableModels}
                <span class="field-hint">Fetching available models from {getEffectiveProvider()}…</span>
              {/if}
            {/if}
            {#if threadModelMeta && llmModel}
              <div class="model-meta-hint">
                <span class="meta-name">{threadModelMeta.name}</span>
                <span class="meta-details">
                  {modelsStore.formatContext(threadModelMeta.context_length)} ctx
                  {#if threadModelMeta.pricing_prompt != null}
                    &middot; In: {modelsStore.formatPrice(threadModelMeta.pricing_prompt)}
                  {/if}
                  {#if threadModelMeta.pricing_completion != null}
                    &middot; Out: {modelsStore.formatPrice(threadModelMeta.pricing_completion)}
                  {/if}
                  {#if threadModelMeta.input_modalities.includes('image')}
                    &middot; Vision
                  {/if}
                  {#if threadModelMeta.supported_parameters.includes('reasoning')}
                    &middot; Reasoning
                  {/if}
                </span>
              </div>
            {/if}
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-use-defaults">Use Model Defaults</label>
            <select id="llm-use-defaults" class="field-select" bind:value={llmUseModelDefaults}>
              <option value="default">Default (inherit global)</option>
              <option value="true">On</option>
              <option value="false">Off</option>
            </select>
            <span class="field-hint">
              Let the provider apply optimal defaults for temperature, top_p, frequency penalty
              {#if llmUseModelDefaults === 'true' && threadModelMeta?.default_temperature != null}
                (temp: {threadModelMeta.default_temperature})
              {/if}
            </span>
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-temp">Temperature</label>
            <input
              id="llm-temp"
              class="field-input"
              type="number"
              min="0"
              max="2"
              step="0.1"
              bind:value={llmTemperature}
              placeholder="Default"
              disabled={llmUseModelDefaults === 'true'}
            />
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-max-tokens">Max Output Tokens</label>
            <input
              id="llm-max-tokens"
              class="field-input"
              type="number"
              min="1"
              max="128000"
              step="1"
              bind:value={llmMaxTokens}
              placeholder="Default"
            />
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-ext-thinking">Extended Thinking</label>
            <select id="llm-ext-thinking" class="field-select" bind:value={llmExtendedThinking}>
              <option value="default">Default (inherit global)</option>
              <option value="true">Enabled</option>
              <option value="false">Disabled</option>
            </select>
          </div>

          <div class="field-group">
            <label class="field-label" for="llm-reasoning">Reasoning Effort</label>
            <select id="llm-reasoning" class="field-select" bind:value={llmReasoningEffort}>
              <option value="">Default (inherit global)</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </div>
        </div>

      {:else if activeTab === 'tools'}
        <div class="tab-panel tools-panel">
          <div class="tools-search">
            <input
              type="text"
              class="field-input"
              bind:value={toolSearch}
              placeholder="Search tools..."
            />
          </div>

          {#if toolsLoadError}
            <div class="tools-loading">{toolsLoadError}</div>
          {:else if toolsLoading}
            <div class="tools-loading">Loading tools...</div>
          {:else}
            <div class="tools-list">
              {#if filteredTools.length === 0}
                <div class="tools-loading">
                  {toolSearch.trim() ? 'No core tools match your search.' : 'No core tools enabled by default.'}
                </div>
              {:else}
                {#each filteredTools as tool (tool.id)}
                  <div
                    class="tool-row"
                    class:disabled={disabledTools.has(tool.name)}
                  >
                    <div class="tool-info">
                      <span class="tool-name">{tool.name}</span>
                      <span class="tool-desc">{tool.description}</span>
                    </div>
                    <button
                      class="tool-toggle"
                      class:off={disabledTools.has(tool.name)}
                      onclick={() => toggleTool(tool.name)}
                      type="button"
                      title={disabledTools.has(tool.name) ? 'Enable tool' : 'Disable tool'}
                    >
                      <span class="toggle-track">
                        <span class="toggle-thumb"></span>
                      </span>
                    </button>
                  </div>
                {/each}
              {/if}
            </div>

            {#if optionalTools().length > 0}
              <div class="optional-tools-section">
                <span class="field-label">
                  Optional Tools
                  {#if enabledToolCount > 0}
                    <span class="tab-badge">{enabledToolCount}</span>
                  {/if}
                </span>
                <p class="field-hint">
                  These tools are not in your core set. Enable them for this thread only.
                </p>
                <div class="tools-list">
                  {#each optionalTools() as tool (tool.name)}
                    <div
                      class="tool-row"
                      class:optional-enabled={enabledTools.has(tool.name)}
                    >
                      <div class="tool-info">
                        <span class="tool-name">{tool.name}</span>
                        <span class="tool-desc">{tool.description}</span>
                      </div>
                      <button
                        class="tool-toggle"
                        class:off={!enabledTools.has(tool.name)}
                        onclick={() => toggleOptionalTool(tool.name)}
                        type="button"
                        title={enabledTools.has(tool.name) ? 'Disable optional tool' : 'Enable optional tool'}
                      >
                        <span class="toggle-track">
                          <span class="toggle-thumb"></span>
                        </span>
                      </button>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}

          {/if}
        </div>

      {:else if activeTab === 'mcp'}
        <div class="tab-panel tools-panel">
          {#if toolsLoadError}
            <div class="tools-loading">{toolsLoadError}</div>
          {:else if toolsLoading}
            <div class="tools-loading">Loading MCP tools...</div>
          {:else}
            <div class="optional-tools-section">
              <span class="field-label">
                <Icon name="terminal" size={14} />
                MCP Servers
              </span>
              <p class="field-hint">
                Tools from installed MCP servers. Enable them for this thread,
                or tick them in <strong>Settings → MCP</strong> to make them
                core across every thread. Add, enable, or remove servers from
                that same panel.
              </p>

              {#if mcpServersForThread.length === 0}
                <div class="tools-loading">No MCP servers installed.</div>
              {:else}
                {#each mcpServersForThread as server (server.id)}
                  <div class="mcp-server-group" class:mcp-server-group-dormant={!server.enabled}>
                    <div class="mcp-server-header-row">
                      <button
                        class="mcp-server-header"
                        type="button"
                        onclick={() => expandedMcpServer = expandedMcpServer === server.id ? null : server.id}
                      >
                        <span
                          class="mcp-status-dot"
                          class:mcp-status-running={server.enabled && server.discoveredCount > 0}
                          class:mcp-status-warning={server.enabled && server.discoveredCount === 0}
                          class:mcp-status-stopped={!server.enabled}
                          title={server.enabled ? (server.discoveredCount > 0 ? 'Running' : 'Running, no tools discovered') : 'Stopped. Enable the server in Settings → MCP to make this tool available'}
                        ></span>
                        <span class="mcp-server-name">{server.name}</span>
                        <span class="mcp-tool-count">
                          {server.tools.filter(isMcpThreadToolEnabled).length}/{server.tools.length}
                        </span>
                      </button>
                    </div>
                    {#if expandedMcpServer === server.id}
                      <div class="mcp-tool-list">
                        {#if server.tools.length === 0}
                          <div class="mcp-empty-tools">
                            No tools discovered for this server. Rediscover it in Settings → MCP.
                          </div>
                        {:else}
                          {#each server.tools as tool (tool.mcpName)}
                            {@const isEnabled = isMcpThreadToolEnabled(tool)}
                            <div
                              class="tool-row"
                              class:optional-enabled={isEnabled}
                              class:disabled={!isEnabled && tool.isDefault}
                              class:tool-row-dormant={!server.enabled}
                              title={!server.enabled ? 'MCP server is not running. Enable it in Settings → MCP to make this tool available' : ''}
                            >
                              <div class="tool-info">
                                <span class="tool-name">{tool.shortName}</span>
                                <span class="tool-desc">{tool.description}</span>
                              </div>
                              <button
                                class="tool-toggle"
                                class:off={!isEnabled}
                                onclick={() => toggleMcpThreadTool(tool)}
                                type="button"
                                title={isEnabled ? 'Disable for this thread' : 'Enable for this thread'}
                              >
                                <span class="toggle-track">
                                  <span class="toggle-thumb"></span>
                                </span>
                              </button>
                            </div>
                          {/each}
                        {/if}
                      </div>
                    {/if}
                  </div>
                {/each}
              {/if}
            </div>
          {/if}
        </div>

      {:else if activeTab === 'skills'}
        <div class="tab-panel skills-thread-panel">
          {#if skillsStore.installed.length === 0}
            <div class="skills-empty">
              <p style="margin: 0;">No skills installed yet.</p>
              <p class="field-hint" style="margin-top: 0.5rem;">
                Install skills from Settings → Skills → Browse Marketplace, then return here to enable them for this thread.
              </p>
            </div>
          {:else}
            <p class="field-hint">
              Turn skills on or off for this thread. Globally-enabled skills are on by default and can be disabled here; other installed skills can be enabled for this thread only.
            </p>
            <div class="skills-list">
              {#each skillsStore.installed as skill (skill.name)}
                {@const defaultOn = skill.default_active}
                {@const globalOn = skillsStore.enabledGlobal.includes(skill.name)}
                {@const threadOn = threadEnabledSkills.has(skill.name)}
                {@const threadOff = threadDisabledSkills.has(skill.name)}
                {@const activeHere = (defaultOn || globalOn || threadOn) && !threadOff}
                <div class="skill-row" class:active={activeHere}>
                  <div class="skill-info">
                    <div class="skill-head">
                      <span class="skill-name">{skill.name}</span>
                      <span class="skill-scope">{skill.scope}</span>
                      {#if defaultOn}<span class="skill-chip">default</span>{/if}
                      {#if globalOn}<span class="skill-chip">global</span>{/if}
                      {#if skill.is_skill_kit}<span class="skill-chip">Skill Kit</span>{/if}
                      {#each skill.required_tools as toolName}
                        <span class="skill-chip skill-required" title={`Required tool: ${toolName} (${skill.tool_ttl})`}>
                          {toolName}
                        </span>
                      {/each}
                    </div>
                    <p class="skill-desc">{skill.description}</p>
                  </div>
                  <div class="skill-toggles">
                    {#if defaultOn || globalOn}
                      <button
                        class="skill-btn"
                        class:skill-btn-danger={threadOff}
                        onclick={() => toggleThreadSkillDisabled(skill.name)}
                        type="button"
                        title="Disable for this thread only"
                      >
                        {threadOff ? 'Disabled here' : 'Disable for thread'}
                      </button>
                    {:else}
                      <button
                        class="skill-btn"
                        class:skill-btn-active={threadOn}
                        onclick={() => toggleThreadSkillEnabled(skill.name)}
                        type="button"
                        title="Enable for this thread"
                      >
                        {threadOn ? 'Enabled here' : 'Enable for thread'}
                      </button>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>

      {:else if activeTab === 'triggers'}
        <div class="tab-panel">
          <div style="text-align: center; padding: 2rem; color: var(--text-muted);">
            <p style="margin: 0; font-size: var(--font-size-sm);">Triggers have moved to the Dashboard panel.</p>
            <p style="margin: 0.5rem 0 0; font-size: var(--font-size-xs);">Use the "Triggers" section in the right panel to manage automations.</p>
          </div>
        </div>

      {:else if activeTab === 'chatapp'}
        <div class="tab-panel">
          <p class="field-hint">
            Bind this thread to a chat-app conversation so messages flow both ways.
            You can keep using the desktop app for the same thread; nothing changes
            here when you chat from the bound chat instead.
          </p>

          <div class="visibility-section">
            <h3 class="section-title">Attention & Delivery</h3>

            <label class="field-label" for="telegram-autonomous-delivery">
              Telegram autonomous output
            </label>
            <select
              id="telegram-autonomous-delivery"
              class="field-input"
              bind:value={telegramAutonomousDelivery}
            >
              <option value="full">Full output</option>
              <option value="notify_only">Notify only</option>
              <option value="off">Off</option>
            </select>

            <label class="field-label" for="in-app-notification-level">
              Notification center
            </label>
            <select
              id="in-app-notification-level"
              class="field-input"
              bind:value={inAppNotificationLevel}
            >
              <option value="notify_only">Notify only</option>
              <option value="all_autonomous">All autonomous completions</option>
              <option value="off">Off</option>
            </select>
          </div>

          {#if chatAppLoadError}
            <div class="error-bar">{chatAppLoadError}</div>
          {/if}

          {#if chatAppBindings.length === 0}
            <div class="chatapp-cta">
              <p style="margin: 0;">No chats bound to this thread yet. Pick how you want to connect:</p>
              <div class="chatapp-cta-buttons">
                <button
                  class="btn btn-primary"
                  type="button"
                  onclick={() => (showChatAppWizard = true)}
                >Connect via shared bot</button>
                <button
                  class="btn btn-secondary"
                  type="button"
                  onclick={() => (showMyBotWizard = true)}
                >Use my own bot</button>
              </div>
              <p class="field-hint" style="margin: 0.25rem 0 0;">
                <strong>Shared:</strong> use the existing Nymeria bot for the
                fastest setup, with no BotFather required.
                <br />
                <strong>My own bot:</strong> paste a token from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> for a branded bot you control. Requires <code>NYMERIA_SECRETS_KEY</code> on the server.
              </p>
            </div>
          {:else}
            <ul class="binding-list">
              {#each chatAppBindings as binding (binding.id)}
                <li class="binding-row">
                  <div class="binding-meta">
                    <span class="binding-provider">{binding.provider}</span>
                    <code class="binding-chat">chat {binding.platform_chat_id}</code>
                    <span class="binding-when">
                      via {binding.user_telegram_bot_id ? 'your bot' : 'shared bot'}
                      &middot; since {new Date(binding.created_at).toLocaleString()}
                    </span>
                  </div>
                  <button
                    class="btn btn-ghost"
                    type="button"
                    onclick={() => handleUnbindChatApp(binding.id)}
                  >Unbind</button>
                </li>
              {/each}
            </ul>
            <p class="field-hint" style="margin-top: 0.5rem;">
              Only one chat per thread at a time. Unbind first to switch.
            </p>
          {/if}
        </div>

      {/if}
    </div>

    {#if error}
      <div class="error-bar">{error}</div>
    {/if}

    <div class="modal-footer">
      <button class="btn btn-ghost" onclick={handleReset} disabled={saving} type="button">
        Reset to Defaults
      </button>
      <div class="footer-right">
        <button class="btn btn-ghost" onclick={onClose} disabled={saving} type="button">
          Cancel
        </button>
        <button
          class="btn btn-primary"
          onclick={checkToolCountAndSave}
          disabled={saving || !hasChanges()}
          type="button"
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>
    </div>
  </div>
</div>

{#if showToolWarning}
  <ToolCountWarning
    toolCount={effectiveToolCount}
    callableCount={defaultToolsStore.callableThreadCount}
    onContinue={() => { showToolWarning = false; handleSave(); }}
    onGoBack={() => { showToolWarning = false; }}
  />
{/if}

{#if showChatAppWizard}
  <div class="chatapp-wizard-backdrop" role="dialog" aria-modal="true">
    <div class="chatapp-wizard-card">
      <ConnectTelegramWizard
        threadId={thread.id}
        onClose={() => (showChatAppWizard = false)}
        onBound={() => {
          // Refresh the panel's binding list so the row appears immediately.
          chatAppBindingsStore.loadBindings(thread.id).catch(() => {});
        }}
      />
    </div>
  </div>
{/if}

{#if showMyBotWizard}
  <div class="chatapp-wizard-backdrop" role="dialog" aria-modal="true">
    <div class="chatapp-wizard-card">
      <ConnectMyTelegramBotWizard
        threadId={thread.id}
        onClose={() => (showMyBotWizard = false)}
        onBound={() => {
          chatAppBindingsStore.loadBindings(thread.id).catch(() => {});
        }}
      />
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--spacing-xl);
    box-sizing: border-box;
    z-index: 1000;
  }

  .modal-panel {
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    width: fit-content;
    min-width: min(560px, 100%);
    max-width: min(920px, 100%);
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    box-sizing: border-box;
  }

  .modal-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-default);
  }

  .modal-header h2 {
    margin: 0;
    font-size: var(--font-size-base);
    font-weight: 600;
    color: var(--text-primary);
  }

  .modal-subtitle {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
  }

  .close-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    flex-shrink: 0;
    margin-left: auto;
  }

  .close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .tabs {
    display: flex;
    flex-wrap: nowrap;
    flex-shrink: 0;
    overflow-x: auto;
    overflow-y: hidden;
    border-bottom: 1px solid var(--border-default);
    padding: 0 var(--spacing-lg);
    scrollbar-width: thin;
  }

  .tab {
    display: flex;
    align-items: center;
    flex: 0 0 auto;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: all var(--transition-fast);
    white-space: nowrap;
  }

  .tab:hover {
    color: var(--text-primary);
  }

  .tab.active {
    color: var(--accent-primary);
    border-bottom-color: var(--accent-primary);
  }

  .tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 16px;
    height: 16px;
    padding: 0 4px;
    font-size: 10px;
    font-weight: 600;
    background: var(--accent-primary);
    color: var(--bg-base);
    border-radius: var(--radius-full);
  }

  .tab-content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }

  .tab-panel {
    padding: var(--spacing-lg);
  }

  .field-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 4px;
  }

  .field-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0 0 var(--spacing-sm) 0;
  }

  .model-meta-hint {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    padding: 5px 8px;
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
  }

  .meta-name {
    color: var(--text-primary);
    font-weight: 500;
  }

  .meta-details {
    color: var(--text-muted);
  }

  .field-group {
    margin-bottom: var(--spacing-md);
  }

  .field-input,
  .field-select {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition-fast);
  }

  .field-input:focus,
  .field-select:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .field-input::placeholder {
    color: var(--text-muted);
  }

  .field-select {
    cursor: pointer;
  }

  .instructions-input {
    width: 100%;
    padding: var(--spacing-sm);
    font-size: var(--font-size-sm);
    font-family: inherit;
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    outline: none;
    resize: vertical;
    min-height: 120px;
    transition: border-color var(--transition-fast);
  }

  .instructions-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .instructions-input::placeholder {
    color: var(--text-muted);
  }

  .system-prompt-input {
    min-height: 200px;
    font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
    font-size: calc(var(--font-size-sm) - 1px);
    line-height: 1.5;
  }

  .visibility-section {
    margin-top: var(--spacing-lg);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-default);
  }

  .agent-config-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .section-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 var(--spacing-xs) 0;
  }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: var(--spacing-sm) 0;
    cursor: pointer;
  }

  .toggle-row input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--accent-primary);
    cursor: pointer;
  }

  .toggle-label {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
  }

  .char-count {
    display: block;
    text-align: right;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-top: 4px;
  }

  /* Tools tab */
  .tools-panel {
    padding-bottom: var(--spacing-md);
  }

  .tools-search {
    padding: 0 0 var(--spacing-sm) 0;
  }

  .tools-list {
    max-height: 340px;
    overflow-y: auto;
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
  }

  .tools-loading {
    padding: var(--spacing-lg);
    text-align: center;
    color: var(--text-muted);
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--border-subtle, var(--border-default));
    transition: opacity var(--transition-fast);
  }

  .tool-row:last-child {
    border-bottom: none;
  }

  .tool-row.disabled {
    opacity: 0.5;
  }

  .tool-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .tool-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tool-row.disabled .tool-name {
    text-decoration: line-through;
  }

  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .optional-tools-section {
    margin-top: var(--spacing-lg);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-default);
  }

  .optional-tools-section > .field-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .tool-row.optional-enabled {
    background: color-mix(in srgb, var(--accent-primary) 5%, transparent);
  }

  /* Toggle switch */
  .tool-toggle {
    flex-shrink: 0;
    padding: 0;
    background: none;
    border: none;
    cursor: pointer;
  }

  .toggle-track {
    display: block;
    width: 32px;
    height: 18px;
    border-radius: 9px;
    background: var(--accent-primary);
    position: relative;
    transition: background var(--transition-fast);
  }

  .tool-toggle.off .toggle-track {
    background: var(--text-muted);
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 16px;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: white;
    transition: left var(--transition-fast);
  }

  .tool-toggle.off .toggle-thumb {
    left: 2px;
  }

  /* Footer */
  .modal-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-top: 1px solid var(--border-default);
  }

  .footer-right {
    display: flex;
    gap: var(--spacing-sm);
  }

  .btn {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-ghost {
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-default);
  }

  .btn-ghost:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .btn-primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .error-bar {
    padding: var(--spacing-sm) var(--spacing-lg);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
  }

  /* MCP Servers in tools tab */
  .mcp-server-group {
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    margin-bottom: var(--spacing-xs);
    overflow: hidden;
  }

  .mcp-server-group-dormant {
    border-style: dashed;
  }

  .mcp-server-header-row {
    display: flex;
    align-items: center;
    background: var(--bg-elevated-2);
  }

  .mcp-server-header {
    flex: 1;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .mcp-server-header:hover {
    background: var(--bg-hover);
  }

  .mcp-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--text-muted);
  }

  .mcp-status-dot.mcp-status-running {
    background: #22c55e;
  }

  .mcp-status-dot.mcp-status-warning {
    background: #f59e0b;
  }

  .mcp-status-dot.mcp-status-stopped {
    background: var(--text-muted);
    opacity: 0.5;
  }

  .mcp-server-name {
    flex: 1;
    text-align: left;
    font-weight: 500;
  }

  .mcp-tool-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .mcp-tool-list {
    border-top: 1px solid var(--border-subtle);
  }

  .mcp-empty-tools {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-style: italic;
  }

  .tool-row-dormant {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .skills-thread-panel .skills-empty {
    text-align: center;
    padding: var(--spacing-lg);
    color: var(--text-secondary);
  }

  .skills-thread-panel .skills-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-md);
  }

  .skills-thread-panel .skill-row {
    display: flex;
    gap: var(--spacing-md);
    padding: var(--spacing-sm);
    background: var(--bg-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }
  .skills-thread-panel .skill-row.active {
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 6%, var(--bg-base));
  }

  .skills-thread-panel .skill-info {
    flex: 1;
    min-width: 0;
  }

  .skills-thread-panel .skill-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    margin-bottom: 4px;
  }

  .skills-thread-panel .skill-name {
    font-weight: 600;
    color: var(--text-primary);
    font-family: var(--font-mono, monospace);
    font-size: var(--font-size-sm);
  }

  .skills-thread-panel .skill-scope,
  .skills-thread-panel .skill-chip {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    border-radius: var(--radius-full);
    background: var(--bg-elevated);
    color: var(--text-muted);
    border: 1px solid var(--border-subtle);
  }
  .skills-thread-panel .skill-chip {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }
  .skills-thread-panel .skill-required {
    color: var(--text-secondary);
    border-color: color-mix(in srgb, var(--accent-primary) 45%, var(--border-subtle));
  }

  .skills-thread-panel .skill-desc {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .skills-thread-panel .skill-toggles {
    flex-shrink: 0;
    display: flex;
    align-items: flex-start;
  }

  .skills-thread-panel .skill-btn {
    padding: 4px 10px;
    font-size: var(--font-size-xs);
    border-radius: var(--radius-sm);
    background: transparent;
    border: 1px solid var(--border-default);
    color: var(--text-secondary);
    cursor: pointer;
  }
  .skills-thread-panel .skill-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }
  .skills-thread-panel .skill-btn-active {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 10%, transparent);
  }
  .skills-thread-panel .skill-btn-danger {
    color: var(--danger, #ef4444);
    border-color: color-mix(in srgb, var(--danger, #ef4444) 40%, transparent);
    background: color-mix(in srgb, var(--danger, #ef4444) 8%, transparent);
  }

  /* Chat App tab + wizard overlay */
  .binding-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .binding-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }

  .binding-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .binding-provider {
    font-weight: 600;
    text-transform: capitalize;
    font-size: var(--font-size-sm);
  }

  .binding-chat {
    font-family: var(--font-family-mono);
    font-size: var(--font-size-sm);
  }

  .binding-when {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .chatapp-wizard-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
  }

  .chatapp-wizard-card {
    background: var(--glass-bg-strong);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.4);
    overflow: hidden;
  }

  .chatapp-cta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding: var(--spacing-lg);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-muted);
    text-align: left;
  }

  .chatapp-cta-buttons {
    display: flex;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }
</style>
