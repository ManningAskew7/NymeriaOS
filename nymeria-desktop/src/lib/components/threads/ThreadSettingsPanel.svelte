<script lang="ts">
  import { onMount } from 'svelte';
  import type { Thread, ThreadConfig, ThreadConfigUpdateRequest, ThreadPlatform, UnifiedTool } from '$lib/types';
  import { Icon, ToggleSwitch } from '$lib/components/common';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { api } from '$lib/services/api.svelte';
  import { trapFocus } from '$lib/actions/focus';
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
  import { filterToolSearch } from '$lib/utils/toolSearch';
  import {
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    fromThreadDisplayProvider,
    supportsOpenAiApiMode,
    toThreadDisplayProvider,
    type ThreadDisplayProvider,
  } from '$lib/utils/providerMapping';
  import { detectThreadPlatform, isNativeDisplayPlatform } from '$lib/utils/platform';
  import ModelConfigTab from './ModelConfigTab.svelte';
  import SkillsConfigTab from './SkillsConfigTab.svelte';
  import ChatAppConfigTab from './ChatAppConfigTab.svelte';

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

  function platformAfterCallableChange(target: Thread, callable: boolean): ThreadPlatform {
    const detected = detectThreadPlatform(target.id);
    if (isNativeDisplayPlatform(target.platform)) return target.platform;
    if (isNativeDisplayPlatform(detected)) return detected;
    if (callable) return 'callable';
    return target.platform === 'callable' ? 'desktop' : (target.platform ?? detected);
  }

  // Active tab
  let activeTab = $state<ThreadSettingsTab>('instructions');

  $effect(() => {
    activeTab = initialTab;
  });

  let chatAppBindings = $derived(chatAppBindingsStore.getBindings(thread.id));

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
  const optionalTools = $derived.by(() => {
    if (!defaultToolsStore.loaded) return [];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return defaultToolsStore.tools
      .filter(t => !coreSet.has(t.name) && !t.name.startsWith('mcp__'))
      .map(t => ({
        name: t.name,
        description: t.description,
        category: t.category,
        securityLevel: t.security_level,
      }));
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

  function getInitialCompactThresholdMode(): 'default' | 'percentage' | 'tokens' {
    return threadConfig?.llmConfig?.compact_threshold_mode ?? 'default';
  }

  function getInitialCompactThreshold(): string {
    return threadConfig?.llmConfig?.compact_threshold != null
      ? String(threadConfig.llmConfig.compact_threshold)
      : '';
  }

  function getInitialCompactThresholdTokens(): string {
    return threadConfig?.llmConfig?.compact_threshold_tokens != null
      ? String(threadConfig.llmConfig.compact_threshold_tokens)
      : '';
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
  let compactThresholdMode = $state<'default' | 'percentage' | 'tokens'>(getInitialCompactThresholdMode());
  let compactThresholdPct = $state<string>(getInitialCompactThreshold());
  let compactThresholdTokens = $state<string>(getInitialCompactThresholdTokens());

  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

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

  const toolsLoadError = $derived(unifiedToolsStore.error || defaultToolsStore.error);
  const toolsReady = $derived(unifiedToolsStore.loaded && defaultToolsStore.loaded);
  const toolsLoading = $derived(unifiedToolsStore.loading || defaultToolsStore.loading || !toolsReady);

  const filteredTools = $derived.by(() => {
    if (!defaultToolsStore.loaded) return [];

    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    let allTools = unifiedToolsStore.tools.filter(t => coreSet.has(t.name) && !isMcpToolName(t.name) && t.category !== 'mcp_server');
    return filterToolSearch(allTools, toolSearch, (tool: UnifiedTool) => ({
      id: tool.id,
      name: tool.name,
      description: tool.description,
      category: tool.category,
      tags: tool.tags,
      toolType: tool.toolType,
      implementationType: tool.implementationType,
    }));
  });

  const filteredOptionalTools = $derived.by(() =>
    filterToolSearch(optionalTools, toolSearch, (tool) => ({
      name: tool.name,
      description: tool.description,
      category: tool.category,
      tags: [tool.securityLevel, 'optional'],
      toolType: 'optional',
    }))
  );

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
    const origCompactMode = threadConfig?.llmConfig?.compact_threshold_mode ?? 'default';
    const origCompactPct = threadConfig?.llmConfig?.compact_threshold != null
      ? String(threadConfig.llmConfig.compact_threshold) : '';
    const origCompactTokens = threadConfig?.llmConfig?.compact_threshold_tokens != null
      ? String(threadConfig.llmConfig.compact_threshold_tokens) : '';

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
    if (compactThresholdMode !== origCompactMode) return true;
    if (compactThresholdPct !== origCompactPct) return true;
    if (compactThresholdTokens !== origCompactTokens) return true;
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
        llmUseModelDefaults !== 'default' || llmOpenAiApiMode !== 'default' || llmBaseUrl || llmApiKey ||
        compactThresholdMode !== 'default' || compactThresholdPct || compactThresholdTokens;

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        const mapped = fromThreadDisplayProvider(threadDisplayProvider);
        llm.provider = mapped.provider || null;
        const effectiveProviderForSave = mapped.provider || getEffectiveProvider();
        // For openai_custom, persist the user-editable base URL (not the default
        // from fromThreadDisplayProvider, which is just a placeholder). Other
        // OpenAI-compatible providers may also carry an optional per-thread URL.
        if (threadDisplayProvider === 'openai_custom') {
          llm.base_url = llmBaseUrl || DEFAULT_CUSTOM_OPENAI_BASE_URL;
        } else if (supportsOpenAiApiMode(effectiveProviderForSave)) {
          llm.base_url = llmBaseUrl || mapped.baseUrl;
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
        llm.openai_api_mode = supportsOpenAiApiMode(getEffectiveProvider()) && llmOpenAiApiMode !== 'default'
          ? llmOpenAiApiMode
          : null;
        llm.compact_threshold_mode = compactThresholdMode === 'default' ? null : compactThresholdMode;
        llm.compact_threshold = compactThresholdPct ? parseFloat(compactThresholdPct) : null;
        llm.compact_threshold_tokens = compactThresholdTokens ? parseInt(compactThresholdTokens, 10) : null;
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
      threadsStore.updateThread(thread.id, {
        callable: result.callable,
        platform: platformAfterCallableChange(thread, result.callable),
      });

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
      compactThresholdMode = 'default';
      compactThresholdPct = '';
      compactThresholdTokens = '';
      systemPrompt = '';
      isCallable = false;
      callableName = '';
      callableDescription = '';
      showAutonomousPrompts = false;
      showPromptMetadata = false;
      telegramAutonomousDelivery = 'full';
      inAppNotificationLevel = 'notify_only';
      threadsStore.updateThread(thread.id, {
        callable: false,
        platform: platformAfterCallableChange(thread, false),
      });
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

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div class="modal-backdrop">
  <button
    class="modal-backdrop-button"
    type="button"
    tabindex="-1"
    aria-label="Close thread settings"
    onclick={onClose}
  ></button>
  <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="thread-settings-title" tabindex="-1" use:trapFocus>
    <div class="modal-header">
      <h2 id="thread-settings-title">Thread Settings</h2>
      <span class="modal-subtitle">{thread.title}</span>
      <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
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
        <ModelConfigTab
          bind:threadDisplayProvider
          bind:llmProvider
          bind:llmModel
          bind:llmBaseUrl
          bind:llmApiKey
          bind:llmTemperature
          bind:llmMaxTokens
          bind:llmExtendedThinking
          bind:llmReasoningEffort
          bind:llmUseModelDefaults
          bind:llmOpenAiApiMode
          bind:compactThresholdMode
          bind:compactThresholdPct
          bind:compactThresholdTokens
        />

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
                    <ToggleSwitch
                      checked={!disabledTools.has(tool.name)}
                      onclick={() => toggleTool(tool.name)}
                      title={disabledTools.has(tool.name) ? 'Enable tool' : 'Disable tool'}
                      ariaLabel={`${disabledTools.has(tool.name) ? 'Enable' : 'Disable'} ${tool.name}`}
                    />
                  </div>
                {/each}
              {/if}
            </div>

            {#if optionalTools.length > 0}
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
                  {#if filteredOptionalTools.length === 0}
                    <div class="tools-loading">
                      {toolSearch.trim() ? 'No optional tools match your search.' : 'No optional tools available.'}
                    </div>
                  {:else}
                    {#each filteredOptionalTools as tool (tool.name)}
                      <div
                        class="tool-row"
                        class:optional-enabled={enabledTools.has(tool.name)}
                      >
                        <div class="tool-info">
                          <span class="tool-name">{tool.name}</span>
                          <span class="tool-desc">{tool.description}</span>
                        </div>
                        <ToggleSwitch
                          checked={enabledTools.has(tool.name)}
                          onclick={() => toggleOptionalTool(tool.name)}
                          title={enabledTools.has(tool.name) ? 'Disable optional tool' : 'Enable optional tool'}
                          ariaLabel={`${enabledTools.has(tool.name) ? 'Disable' : 'Enable'} optional tool ${tool.name}`}
                        />
                      </div>
                    {/each}
                  {/if}
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
                              <ToggleSwitch
                                checked={isEnabled}
                                onclick={() => toggleMcpThreadTool(tool)}
                                title={isEnabled ? 'Disable for this thread' : 'Enable for this thread'}
                                ariaLabel={`${isEnabled ? 'Disable' : 'Enable'} MCP tool ${tool.shortName} for this thread`}
                              />
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
        <SkillsConfigTab
          bind:threadEnabledSkills
          bind:threadDisabledSkills
        />

      {:else if activeTab === 'triggers'}
        <div class="tab-panel">
          <div style="text-align: center; padding: 2rem; color: var(--text-muted);">
            <p style="margin: 0; font-size: var(--font-size-sm);">Triggers have moved to the Dashboard panel.</p>
            <p style="margin: 0.5rem 0 0; font-size: var(--font-size-xs);">Use the "Triggers" section in the right panel to manage automations.</p>
          </div>
        </div>

      {:else if activeTab === 'chatapp'}
        <ChatAppConfigTab
          {thread}
          bind:telegramAutonomousDelivery
          bind:inAppNotificationLevel
        />

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

  .modal-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .modal-panel {
    position: relative;
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

  .field-group {
    margin-bottom: var(--spacing-md);
  }

  .field-input {
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

  .field-input:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-primary-alpha, rgba(99, 102, 241, 0.15));
  }

  .field-input::placeholder {
    color: var(--text-muted);
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

</style>
