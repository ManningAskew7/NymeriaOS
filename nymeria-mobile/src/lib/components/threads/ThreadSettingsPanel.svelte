<script lang="ts">
  import type { ThreadConfig, ThreadConfigUpdateRequest, UnifiedTool } from '$lib/types';
  import { threadConfigStore } from '$lib/stores/threadConfig.svelte';
  import { unifiedToolsStore } from '$lib/stores/unifiedTools.svelte';
  import { defaultToolsStore } from '$lib/stores/defaultTools.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { chatStore } from '$lib/stores/chat.svelte';
  import { triggersStore } from '$lib/stores/triggers.svelte';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { api } from '$lib/services/api.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import Button from '$lib/components/common/Button.svelte';
  import TriggerConfigTab from '$lib/components/triggers/TriggerConfigTab.svelte';
  import { ToolCountWarning } from '$lib/components/tools';
  import { mcpServersStore } from '$lib/stores/mcpServers.svelte';
  import MCPServerForm from '$lib/components/tools/MCPServerForm.svelte';
  import type { MCPServerCreateRequest } from '$lib/types';
  import { untrack } from 'svelte';

  interface Props {
    threadId: string;
    open: boolean;
    onClose: () => void;
  }

  let { threadId, open, onClose }: Props = $props();

  type Tab = 'instructions' | 'system' | 'agent' | 'model' | 'tools' | 'mcp' | 'triggers';
  type TelegramAutonomousDelivery = ThreadConfig['telegramAutonomousDelivery'];
  type InAppNotificationLevel = ThreadConfig['inAppNotificationLevel'];
  let activeTab = $state<Tab>('instructions');

  // Config loaded from API
  let threadConfig = $state<ThreadConfig | null>(null);
  let loading = $state(true);

  // Form state — Instructions
  let instructions = $state('');
  let injectTodosInPrompt = $state(false);
  let showAutonomousPrompts = $state(false);
  let showPromptMetadata = $state(false);
  let telegramAutonomousDelivery = $state<TelegramAutonomousDelivery>('full');
  let inAppNotificationLevel = $state<InAppNotificationLevel>('notify_only');

  // Form state — System Prompt & Agent
  let systemPrompt = $state('');
  let isCallable = $state(false);
  let callableName = $state('');
  let callableDescription = $state('');

  // Form state — Model
  let llmProvider = $state('');
  let llmModel = $state('');
  let llmBaseUrl = $state('');
  let llmApiKey = $state('');
  let llmTemperature = $state('');
  let llmMaxTokens = $state('');
  let llmExtendedThinking = $state<'default' | 'true' | 'false'>('default');
  let llmReasoningEffort = $state('');
  let llmUseModelDefaults = $state<'default' | 'true' | 'false'>('default');
  let llmOpenAiApiMode = $state<'default' | 'chat_completions' | 'responses'>('default');

  // Form state — Tools
  let disabledTools = $state<Set<string>>(new Set());
  let enabledTools = $state<Set<string>>(new Set());
  let toolSearch = $state('');
  let showToolWarning = $state(false);

  function isMcpToolName(name: string): boolean {
    return name.startsWith('mcp__');
  }

  // UI state
  let saving = $state(false);
  let error = $state('');

  // Model metadata (reactive)
  const threadModelMeta = $derived(modelsStore.getById(llmModel));

  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

  function supportsApiMode(provider: string = getEffectiveProvider()): boolean {
    return provider === 'openai' || provider === 'openrouter';
  }

  // Effective tool count
  const effectiveToolCount = $derived.by(() => {
    const coreNames = defaultToolsStore.defaultToolNames;
    const activeCore = coreNames.filter(n => !disabledTools.has(n)).length;
    return activeCore + enabledTools.size;
  });

  // Optional tools (not in core set), excluding MCP tools
  const optionalTools = $derived(() => {
    if (!defaultToolsStore.loaded) return [];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return defaultToolsStore.tools
      .filter(t => !coreSet.has(t.name) && !t.name.startsWith('mcp__'))
      .map(t => ({ name: t.name, description: t.description }));
  });

  // MCP tools grouped by server. Default MCP tools can be disabled for this
  // thread; non-default MCP tools can be enabled for this thread.
  const mcpServersForThread = $derived(() => {
    if (!defaultToolsStore.loaded) return [] as {
      id: string;
      name: string;
      enabled: boolean;
      discoveredCount: number;
      tools: { name: string; shortName: string; description: string; isDefault: boolean }[];
    }[];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return mcpServersStore.servers.map(server => ({
      id: server.id,
      name: server.name,
      enabled: server.enabled,
      discoveredCount: server.discoveredTools.length,
      tools: server.discoveredTools.map(tool => {
        const name = `mcp__${server.id}__${tool.name}`;
        return {
          name,
          shortName: tool.name,
          description: tool.description,
          isDefault: coreSet.has(name),
        };
      }),
    }));
  });

  // MCP UI state
  let showMcpAddForm = $state(false);
  let mcpAddLoading = $state(false);
  let mcpAddError = $state<string | null>(null);
  let expandedMcpServer = $state<string | null>(null);

  function toggleAllMcpTools(tools: { name: string; isDefault?: boolean }[], enable: boolean) {
    const nextEnabled = new Set(enabledTools);
    const nextDisabled = new Set(disabledTools);
    for (const t of tools) {
      if (t.isDefault) {
        if (enable) nextDisabled.delete(t.name);
        else nextDisabled.add(t.name);
      } else {
        if (enable) nextEnabled.add(t.name);
        else nextEnabled.delete(t.name);
      }
    }
    enabledTools = nextEnabled;
    disabledTools = nextDisabled;
  }

  function areMcpToolsAllEnabled(tools: { name: string; isDefault?: boolean }[]): boolean {
    return tools.every(t => isMcpThreadToolEnabled(t));
  }

  function isMcpThreadToolEnabled(tool: { name: string; isDefault?: boolean }): boolean {
    return tool.isDefault ? !disabledTools.has(tool.name) : enabledTools.has(tool.name);
  }

  function toggleMcpThreadTool(tool: { name: string; isDefault?: boolean }) {
    if (tool.isDefault) {
      toggleTool(tool.name);
    } else {
      toggleOptionalTool(tool.name);
    }
  }

  async function handleMcpAdd(data: MCPServerCreateRequest) {
    mcpAddLoading = true;
    mcpAddError = null;
    try {
      const result = await mcpServersStore.create(data, threadId);
      if (result.discoveryError) {
        mcpAddError = `Server added but discovery failed: ${result.discoveryError}`;
      } else {
        showMcpAddForm = false;
        mcpAddError = null;
        defaultToolsStore.resetLoaded();
        await defaultToolsStore.load();
        const next = new Set(enabledTools);
        for (const tool of result.server.discoveredTools) {
          next.add(`mcp__${result.server.id}__${tool.name}`);
        }
        enabledTools = next;
      }
    } catch (e) {
      mcpAddError = e instanceof Error ? e.message : 'Failed to add server';
    } finally {
      mcpAddLoading = false;
    }
  }

  // Filtered core tools
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

  // Trigger count for badge
  const activeTriggerCount = $derived(
    triggersStore.triggers.filter(t => t.enabled && t.thread_id === threadId).length
  );

  // Load config and deps
  $effect(() => {
    if (open && threadId) {
      untrack(() => loadConfig());
    }
  });

  $effect(() => {
    if (open) {
      untrack(() => {
        if (!unifiedToolsStore.loaded && !unifiedToolsStore.loading) unifiedToolsStore.loadTools();
        if (!triggersStore.loaded && !triggersStore.loading) triggersStore.loadTriggers();
        if (!modelsStore.loaded && !modelsStore.loading) modelsStore.loadModels();
        if (!serverSettingsStore.loaded && !serverSettingsStore.loading) serverSettingsStore.load();
        if (!defaultToolsStore.loaded && !defaultToolsStore.loading) defaultToolsStore.load();
        if (!mcpServersStore.loaded && !mcpServersStore.loading) mcpServersStore.load();
        if (Object.keys(triggersStore.sources).length === 0) triggersStore.loadSources();
      });
    }
  });

  async function loadConfig() {
    loading = true;
    try {
      const cfg = await threadConfigStore.loadConfig(threadId);
      threadConfig = cfg;
      initFormFromConfig(cfg);
    } catch {
      threadConfig = null;
      initFormFromConfig(null);
    } finally {
      loading = false;
    }
  }

  function initFormFromConfig(cfg: ThreadConfig | null) {
    instructions = cfg?.instructions ?? '';
    injectTodosInPrompt = cfg?.injectTodosInPrompt ?? false;
    showAutonomousPrompts = cfg?.showAutonomousPrompts ?? false;
    showPromptMetadata = cfg?.showPromptMetadata ?? false;
    telegramAutonomousDelivery = cfg?.telegramAutonomousDelivery ?? 'full';
    inAppNotificationLevel = cfg?.inAppNotificationLevel ?? 'notify_only';
    systemPrompt = cfg?.systemPrompt ?? '';
    isCallable = cfg?.callable ?? false;
    callableName = cfg?.callableName ?? '';
    callableDescription = cfg?.callableDescription ?? '';
    llmProvider = cfg?.llmConfig?.provider ?? '';
    llmModel = cfg?.llmConfig?.model ?? '';
    llmBaseUrl = cfg?.llmConfig?.base_url ?? '';
    llmApiKey = cfg?.llmConfig?.api_key ?? '';
    llmTemperature = cfg?.llmConfig?.temperature != null ? String(cfg.llmConfig.temperature) : '';
    llmMaxTokens = cfg?.llmConfig?.max_tokens != null ? String(cfg.llmConfig.max_tokens) : '';
    llmExtendedThinking = cfg?.llmConfig?.extended_thinking != null
      ? (String(cfg.llmConfig.extended_thinking) as 'true' | 'false')
      : 'default';
    llmReasoningEffort = cfg?.llmConfig?.reasoning_effort ?? '';
    llmUseModelDefaults = cfg?.llmConfig?.use_model_defaults != null
      ? (String(cfg.llmConfig.use_model_defaults) as 'true' | 'false')
      : 'default';
    llmOpenAiApiMode = cfg?.llmConfig?.openai_api_mode ?? 'default';

    // Tools
    if (cfg?.hasCustomizations) {
      disabledTools = new Set(cfg.disabledTools ?? []);
      enabledTools = new Set(cfg.enabledTools ?? []);
    } else {
      disabledTools = new Set();
      enabledTools = new Set();
    }
  }

  function toggleTool(toolName: string) {
    const next = new Set(disabledTools);
    if (next.has(toolName)) { next.delete(toolName); } else { next.add(toolName); }
    disabledTools = next;
  }

  function toggleOptionalTool(toolName: string) {
    const next = new Set(enabledTools);
    if (next.has(toolName)) { next.delete(toolName); } else { next.add(toolName); }
    enabledTools = next;
  }

  function hasChanges(): boolean {
    const orig = threadConfig;
    const origInstructions = orig?.instructions ?? '';
    const origDisabled = new Set(orig?.disabledTools ?? []);
    const origEnabled = new Set(orig?.enabledTools ?? []);
    const origProvider = orig?.llmConfig?.provider ?? '';
    const origModel = orig?.llmConfig?.model ?? '';
    const origBaseUrl = orig?.llmConfig?.base_url ?? '';
    const origApiKey = orig?.llmConfig?.api_key ?? '';
    const origTemp = orig?.llmConfig?.temperature != null ? String(orig.llmConfig.temperature) : '';
    const origMaxTokens = orig?.llmConfig?.max_tokens != null ? String(orig.llmConfig.max_tokens) : '';
    const origExtThinking = orig?.llmConfig?.extended_thinking != null
      ? String(orig.llmConfig.extended_thinking) : 'default';
    const origReasoning = orig?.llmConfig?.reasoning_effort ?? '';
    const origUseDefaults = orig?.llmConfig?.use_model_defaults != null
      ? String(orig.llmConfig.use_model_defaults) : 'default';
    const origOpenAiApiMode = orig?.llmConfig?.openai_api_mode ?? 'default';
    const origSystemPrompt = orig?.systemPrompt ?? '';
    const origCallable = orig?.callable ?? false;
    const origCallableName = orig?.callableName ?? '';
    const origCallableDesc = orig?.callableDescription ?? '';
    const origInjectTodos = orig?.injectTodosInPrompt ?? false;
    const origShowAuto = orig?.showAutonomousPrompts ?? false;
    const origShowMeta = orig?.showPromptMetadata ?? false;
    const origTelegramDelivery = orig?.telegramAutonomousDelivery ?? 'full';
    const origNotificationLevel = orig?.inAppNotificationLevel ?? 'notify_only';

    if (instructions !== origInstructions) return true;
    if (injectTodosInPrompt !== origInjectTodos) return true;
    if (showAutonomousPrompts !== origShowAuto) return true;
    if (showPromptMetadata !== origShowMeta) return true;
    if (telegramAutonomousDelivery !== origTelegramDelivery) return true;
    if (inAppNotificationLevel !== origNotificationLevel) return true;
    if (systemPrompt !== origSystemPrompt) return true;
    if (isCallable !== origCallable) return true;
    if (callableName !== origCallableName) return true;
    if (callableDescription !== origCallableDesc) return true;
    if (llmProvider !== origProvider) return true;
    if (llmModel !== origModel) return true;
    if (llmBaseUrl !== origBaseUrl) return true;
    if (llmApiKey !== origApiKey) return true;
    if (llmTemperature !== origTemp) return true;
    if (llmMaxTokens !== origMaxTokens) return true;
    if (llmExtendedThinking !== origExtThinking) return true;
    if (llmReasoningEffort !== origReasoning) return true;
    if (llmUseModelDefaults !== origUseDefaults) return true;
    if (llmOpenAiApiMode !== origOpenAiApiMode) return true;
    if (disabledTools.size !== origDisabled.size) return true;
    for (const t of disabledTools) { if (!origDisabled.has(t)) return true; }
    if (enabledTools.size !== origEnabled.size) return true;
    for (const t of enabledTools) { if (!origEnabled.has(t)) return true; }
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

      // Disabled tools
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

      // LLM config
      const hasLlm = llmProvider || llmModel || llmTemperature || llmMaxTokens ||
        llmBaseUrl || llmApiKey ||
        llmExtendedThinking !== 'default' || llmReasoningEffort ||
        llmUseModelDefaults !== 'default' || llmOpenAiApiMode !== 'default';

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        llm.provider = llmProvider || null;
        llm.base_url = getEffectiveProvider() === 'openai' ? (llmBaseUrl || null) : null;
        llm.api_key = getEffectiveProvider() === 'openai' ? (llmApiKey || null) : null;
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

      // Callable
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

      const result = await threadConfigStore.updateConfig(threadId, updates);

      // Sync sidebar title to callable name
      if (isCallable && callableName.trim()) {
        threadsStore.applyBackendTitle(threadId, callableName.trim());
      }
      const thread = threadsStore.threads.find((item) => item.id === threadId);
      threadsStore.updateThread(threadId, {
        callable: result.callable,
        platform: result.callable
          ? 'callable'
          : thread?.platform === 'callable'
            ? 'desktop'
            : thread?.platform,
      });

      // Refresh context stats
      api.getThreadContextStats(threadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === threadId) {
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
      await threadConfigStore.deleteConfig(threadId);
      initFormFromConfig(null);
      const thread = threadsStore.threads.find((item) => item.id === threadId);
      threadsStore.updateThread(threadId, {
        callable: false,
        platform: thread?.platform === 'callable' ? 'desktop' : thread?.platform,
      });

      api.getThreadContextStats(threadId).then((stats) => {
        if (stats && threadsStore.currentThreadId === threadId) {
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
</script>

{#if open}
  <div class="thread-settings-modal">
    <div class="settings-header">
      <button class="back-btn" onclick={onClose}>
        <Icon name="chevronLeft" size={22} />
      </button>
      <h2>Thread Settings</h2>
    </div>

    <div class="tab-bar">
      <button class="tab-btn" class:active={activeTab === 'instructions'} onclick={() => (activeTab = 'instructions')}>
        Instructions
      </button>
      <button class="tab-btn" class:active={activeTab === 'system'} onclick={() => (activeTab = 'system')}>
        System
        {#if systemPrompt.trim()}<span class="tab-badge">1</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'agent'} onclick={() => (activeTab = 'agent')}>
        Agent
        {#if isCallable}<span class="tab-badge">1</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'model'} onclick={() => (activeTab = 'model')}>
        Model
      </button>
      <button class="tab-btn" class:active={activeTab === 'tools'} onclick={() => (activeTab = 'tools')}>
        Tools
        {#if disabledToolCount > 0}<span class="tab-badge">{disabledToolCount}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'mcp'} onclick={() => (activeTab = 'mcp')}>
        MCP
        {#if mcpOverrideCount > 0}<span class="tab-badge">{mcpOverrideCount}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'triggers'} onclick={() => (activeTab = 'triggers')}>
        Triggers
        {#if activeTriggerCount > 0}<span class="tab-badge">{activeTriggerCount}</span>{/if}
      </button>
    </div>

    <div class="settings-body">
      {#if loading}
        <div class="loading-state">Loading...</div>

      {:else if activeTab === 'instructions'}
        <div class="setting-group">
          <label class="setting-label">Custom Instructions</label>
          <p class="hint">Appended to the system prompt for this thread only.</p>
          <textarea
            class="setting-textarea"
            bind:value={instructions}
            placeholder="e.g. Focus on email management. Be concise."
            maxlength={5000}
            rows={6}
          ></textarea>
          <span class="char-count">{instructions.length} / 5,000</span>
        </div>

        <div class="section-divider">
          <span class="section-title">Advanced</span>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={injectTodosInPrompt} />
            <span>Inject TODOs into system prompt</span>
          </label>
          <p class="hint">Include active TODOs in the system prompt so the LLM sees them without tool calls.</p>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={showAutonomousPrompts} />
            <span>Show autonomous prompts</span>
          </label>
          <p class="hint">Show scheduler, watchdog, and trigger prompts in the chat.</p>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={showPromptMetadata} />
            <span>Show prompt metadata</span>
          </label>
          <p class="hint">Show time context and trigger type prepended to each message.</p>
        </div>
        <div class="setting-group">
          <label class="setting-label" for="telegram-autonomous-delivery">
            Telegram autonomous output
          </label>
          <select
            id="telegram-autonomous-delivery"
            class="setting-input"
            bind:value={telegramAutonomousDelivery}
          >
            <option value="full">Full output</option>
            <option value="notify_only">Notify only</option>
            <option value="off">Off</option>
          </select>
        </div>
        <div class="setting-group">
          <label class="setting-label" for="in-app-notification-level">
            Notification center
          </label>
          <select
            id="in-app-notification-level"
            class="setting-input"
            bind:value={inAppNotificationLevel}
          >
            <option value="notify_only">Notify only</option>
            <option value="all_autonomous">All autonomous completions</option>
            <option value="off">Off</option>
          </select>
        </div>

      {:else if activeTab === 'system'}
        <div class="setting-group">
          <label class="setting-label">Custom System Prompt</label>
          <p class="hint">Replaces the base system prompt for this thread. Leave empty for default.</p>
          <textarea
            class="setting-textarea mono"
            bind:value={systemPrompt}
            placeholder="You are a specialized assistant that..."
            maxlength={50000}
            rows={10}
          ></textarea>
          <span class="char-count">{systemPrompt.length} / 50,000</span>
        </div>

      {:else if activeTab === 'agent'}
        <div class="section-divider">
          <span class="section-title">Agent Configuration</span>
        </div>
        <div class="setting-group">
          <label class="setting-toggle">
            <input type="checkbox" bind:checked={isCallable} />
            <span>Make Callable</span>
          </label>
          <p class="hint">Mark this thread as a callable sub-agent.</p>
        </div>
        {#if isCallable}
          <div class="setting-group">
            <label class="setting-label">Callable Name</label>
            <input
              type="text"
              class="setting-input"
              bind:value={callableName}
              placeholder="e.g. ResearchAgent"
              maxlength={64}
            />
            <span class="char-count">{callableName.length} / 64</span>
          </div>
          <div class="setting-group">
            <label class="setting-label">Callable Description</label>
            <textarea
              class="setting-textarea"
              bind:value={callableDescription}
              placeholder="Describe what this agent does..."
              maxlength={500}
              rows={3}
            ></textarea>
            <span class="char-count">{callableDescription.length} / 500</span>
          </div>
        {/if}

      {:else if activeTab === 'model'}
        <div class="setting-group">
          <label class="setting-label">Provider</label>
          <select class="setting-input" bind:value={llmProvider}>
            <option value="">Default (inherit global)</option>
            <option value="openrouter">OpenRouter</option>
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
          </select>
        </div>

        {#if supportsApiMode()}
          <div class="setting-group">
            <label class="setting-label" for="llm-openai-api-mode">API Mode</label>
            <select id="llm-openai-api-mode" class="setting-input" bind:value={llmOpenAiApiMode}>
              <option value="default">Default (inherit global)</option>
              <option value="chat_completions">Chat Completions (not recommended if thinking is enabled)</option>
              <option value="responses">Responses API</option>
            </select>
            <p class="hint">Responses API is the default for OpenAI-compatible reasoning models and OpenRouter beta. Chat Completions remains available as a compatibility override.</p>
          </div>
        {/if}

        {#if getEffectiveProvider() === 'openai'}
          <div class="setting-group">
            <label class="setting-label" for="llm-base-url">API Base URL</label>
            <input
              id="llm-base-url"
              type="text"
              class="setting-input"
              bind:value={llmBaseUrl}
              placeholder="http://cli-proxy-api-latest:8317/v1"
            />
            <p class="hint">OpenAI-compatible endpoint reachable by the Nymeria backend. Leave empty to inherit global settings.</p>
          </div>

          <div class="setting-group">
            <label class="setting-label" for="llm-api-key">API Key</label>
            <input
              id="llm-api-key"
              type="password"
              class="setting-input"
              bind:value={llmApiKey}
              placeholder="Leave empty to inherit global key"
              autocomplete="off"
            />
          </div>
        {/if}

        <div class="setting-group">
          <label class="setting-label">Model</label>
          <input
            type="text"
            class="setting-input"
            bind:value={llmModel}
            placeholder="Leave empty for global default"
          />
          {#if threadModelMeta && llmModel}
            <div class="model-meta">
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
              </span>
            </div>
          {/if}
        </div>

        <div class="setting-group">
          <label class="setting-label">Use Model Defaults</label>
          <select class="setting-input" bind:value={llmUseModelDefaults}>
            <option value="default">Default (inherit global)</option>
            <option value="true">On</option>
            <option value="false">Off</option>
          </select>
          <p class="hint">Let provider apply optimal defaults for temp, top_p, etc.</p>
        </div>

        <div class="setting-group">
          <label class="setting-label">Temperature</label>
          <input
            type="number"
            class="setting-input"
            min="0"
            max="2"
            step="0.1"
            bind:value={llmTemperature}
            placeholder="Default"
            disabled={llmUseModelDefaults === 'true'}
          />
        </div>

        <div class="setting-group">
          <label class="setting-label">Max Output Tokens</label>
          <input
            type="number"
            class="setting-input"
            min="1"
            max="128000"
            step="1"
            bind:value={llmMaxTokens}
            placeholder="Default"
          />
        </div>

        <div class="setting-group">
          <label class="setting-label">Extended Thinking</label>
          <select class="setting-input" bind:value={llmExtendedThinking}>
            <option value="default">Default (inherit global)</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </select>
        </div>

        <div class="setting-group">
          <label class="setting-label">Reasoning Effort</label>
          <select class="setting-input" bind:value={llmReasoningEffort}>
            <option value="">Default (inherit global)</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </div>

      {:else if activeTab === 'tools'}
        <div class="tools-search">
          <input
            type="text"
            class="setting-input"
            bind:value={toolSearch}
            placeholder="Search tools..."
          />
        </div>

        {#if toolsLoadError}
          <div class="loading-state">{toolsLoadError}</div>
        {:else if toolsLoading}
          <div class="loading-state">Loading tools...</div>
        {:else}
          <div class="tools-list">
            {#if filteredTools.length === 0}
              <div class="loading-state">
                {toolSearch.trim() ? 'No core tools match your search.' : 'No core tools enabled by default.'}
              </div>
            {:else}
              {#each filteredTools as tool (tool.id)}
                <div class="tool-row" class:tool-disabled={disabledTools.has(tool.name)}>
                  <div class="tool-info">
                    <span class="tool-name">{tool.name}</span>
                    <span class="tool-desc">{tool.description}</span>
                  </div>
                  <button
                    class="toggle-btn"
                    class:off={disabledTools.has(tool.name)}
                    onclick={() => toggleTool(tool.name)}
                    type="button"
                  >
                    <span class="toggle-track"><span class="toggle-thumb"></span></span>
                  </button>
                </div>
              {/each}
            {/if}
          </div>

          {#if optionalTools().length > 0}
            <div class="section-divider">
              <span class="section-title">
                Optional Tools
                {#if enabledToolCount > 0}<span class="tab-badge">{enabledToolCount}</span>{/if}
              </span>
              <p class="hint">Not in your core set. Enable for this thread only.</p>
            </div>
            <div class="tools-list">
              {#each optionalTools() as tool (tool.name)}
                <div class="tool-row" class:tool-enabled={enabledTools.has(tool.name)}>
                  <div class="tool-info">
                    <span class="tool-name">{tool.name}</span>
                    <span class="tool-desc">{tool.description}</span>
                  </div>
                  <button
                    class="toggle-btn"
                    class:off={!enabledTools.has(tool.name)}
                    onclick={() => toggleOptionalTool(tool.name)}
                    type="button"
                  >
                    <span class="toggle-track"><span class="toggle-thumb"></span></span>
                  </button>
                </div>
              {/each}
            </div>
          {/if}

        {/if}

      {:else if activeTab === 'mcp'}
        {#if toolsLoadError}
          <div class="loading-state">{toolsLoadError}</div>
        {:else if toolsLoading}
          <div class="loading-state">Loading MCP tools...</div>
        {:else}
          <div class="section-divider">
            <span class="section-title">
              <Icon name="terminal" size={14} />
              MCP Servers
            </span>
            <p class="hint">Tools from MCP servers. Enable for this thread.</p>
          </div>

          {#if mcpServersForThread().length === 0}
            <div class="loading-state">No MCP servers installed.</div>
          {:else}
            {#each mcpServersForThread() as server (server.id)}
              <div class="mcp-server-group">
                <div class="mcp-server-header-row">
                  <button
                    class="mcp-server-header"
                    onclick={() => expandedMcpServer = expandedMcpServer === server.id ? null : server.id}
                  >
                    <span class="mcp-server-name">{server.name}</span>
                    <span class="mcp-tool-count">{server.tools.filter(isMcpThreadToolEnabled).length}/{server.tools.length}</span>
                  </button>
                  <button
                    class="mcp-bulk-btn"
                    type="button"
                    onclick={() => toggleAllMcpTools(server.tools, !areMcpToolsAllEnabled(server.tools))}
                  >
                    {areMcpToolsAllEnabled(server.tools) ? 'Disable all' : 'Enable all'}
                  </button>
                </div>
                {#if expandedMcpServer === server.id}
                  <div class="mcp-tool-list">
                    {#if server.tools.length === 0}
                      <div class="loading-state">No tools discovered for this server.</div>
                    {:else}
                      {#each server.tools as tool (tool.name)}
                        {@const isEnabled = isMcpThreadToolEnabled(tool)}
                        <div class="tool-row" class:tool-enabled={isEnabled} class:tool-disabled={!isEnabled && tool.isDefault}>
                          <div class="tool-info">
                            <span class="tool-name">{tool.shortName}</span>
                            <span class="tool-desc">{tool.description}</span>
                          </div>
                          <button
                            class="toggle-btn"
                            class:off={!isEnabled}
                            onclick={() => toggleMcpThreadTool(tool)}
                            type="button"
                            title={isEnabled ? 'Disable for this thread' : 'Enable for this thread'}
                          >
                            <span class="toggle-track"><span class="toggle-thumb"></span></span>
                          </button>
                        </div>
                      {/each}
                    {/if}
                  </div>
                {/if}
              </div>
            {/each}
          {/if}

          {#if showMcpAddForm}
            <MCPServerForm
              mode="add"
              loading={mcpAddLoading}
              error={mcpAddError}
              onSubmit={handleMcpAdd}
              onCancel={() => { showMcpAddForm = false; mcpAddError = null; }}
            />
          {:else}
            <button class="mcp-add-btn" onclick={() => showMcpAddForm = true}>
              <Icon name="plus" size={14} /> Add MCP Server
            </button>
          {/if}
        {/if}

      {:else if activeTab === 'triggers'}
        <TriggerConfigTab {threadId} />
      {/if}
    </div>

    {#if error}
      <div class="error-bar">{error}</div>
    {/if}

    <div class="settings-footer">
      <button class="footer-btn reset" onclick={handleReset} disabled={saving} type="button">
        Reset
      </button>
      <div class="footer-right">
        <button class="footer-btn secondary" onclick={onClose} disabled={saving} type="button">
          Cancel
        </button>
        <button
          class="footer-btn primary"
          onclick={checkToolCountAndSave}
          disabled={saving || !hasChanges()}
          type="button"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
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
{/if}

<style>
  .thread-settings-modal {
    position: fixed;
    inset: 0;
    z-index: 300;
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
  }

  .settings-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-md);
    height: var(--header-height);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-top: env(safe-area-inset-top);
  }

  .back-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
  }

  .back-btn:active {
    background: var(--bg-hover);
  }

  .settings-header h2 {
    flex: 1;
    font-size: var(--font-size-lg);
    font-weight: 600;
  }

  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  .tab-btn {
    flex: 1;
    min-width: 0;
    padding: var(--spacing-sm) var(--spacing-xs);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    white-space: nowrap;
    min-height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
  }

  .tab-btn.active {
    color: var(--accent-primary);
    border-bottom-color: var(--accent-primary);
  }

  .tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 4px;
    font-size: 10px;
    font-weight: 600;
    background: var(--accent-primary);
    color: white;
    border-radius: 9px;
  }

  .settings-body {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: var(--spacing-lg);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .loading-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-xl);
  }

  .setting-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .setting-label {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .setting-input {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: 16px;
    min-height: 44px;
  }

  .setting-input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .setting-input:disabled {
    opacity: 0.4;
  }

  select.setting-input {
    appearance: none;
    -webkit-appearance: none;
  }

  .setting-textarea {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: 16px;
    resize: vertical;
    font-family: inherit;
    line-height: 1.5;
  }

  .setting-textarea:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .setting-textarea.mono {
    font-family: monospace;
    font-size: 14px;
  }

  .setting-toggle {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    min-height: 44px;
  }

  .setting-toggle input {
    width: 22px;
    height: 22px;
    accent-color: var(--accent-primary);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .char-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-align: right;
  }

  .section-divider {
    padding-top: var(--spacing-md);
    margin-top: var(--spacing-sm);
    border-top: 1px solid var(--border-subtle);
  }

  .section-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  /* Model metadata */
  .model-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--spacing-xs) var(--spacing-sm);
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    margin-top: var(--spacing-xs);
  }

  .meta-name {
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    font-weight: 500;
  }

  .meta-details {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* Tools tab */
  .tools-search {
    margin-bottom: var(--spacing-sm);
  }

  .tools-list {
    display: flex;
    flex-direction: column;
  }

  .tool-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-subtle);
    min-height: 48px;
  }

  .tool-row:last-child {
    border-bottom: none;
  }

  .tool-row.tool-disabled {
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
  }

  .tool-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Toggle switch */
  .toggle-btn {
    flex-shrink: 0;
    padding: 0;
    background: none;
    border: none;
  }

  .toggle-track {
    display: block;
    width: 40px;
    height: 22px;
    border-radius: 11px;
    background: var(--accent-primary);
    position: relative;
    transition: background 0.2s;
  }

  .toggle-btn.off .toggle-track {
    background: var(--text-muted);
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 20px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: white;
    transition: left 0.2s;
  }

  .toggle-btn.off .toggle-thumb {
    left: 2px;
  }

  /* Error bar */
  .error-bar {
    padding: var(--spacing-sm) var(--spacing-md);
    background: color-mix(in srgb, var(--error) 15%, transparent);
    color: var(--error);
    font-size: var(--font-size-sm);
    flex-shrink: 0;
  }

  /* Footer */
  .settings-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-bottom: calc(var(--spacing-sm) + env(safe-area-inset-bottom));
    gap: var(--spacing-sm);
  }

  .footer-right {
    display: flex;
    gap: var(--spacing-sm);
  }

  .footer-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    font-size: var(--font-size-sm);
    font-weight: 500;
    border-radius: var(--radius-md);
    min-height: 44px;
  }

  .footer-btn:disabled {
    opacity: 0.4;
  }

  .footer-btn.reset {
    color: var(--text-muted);
  }

  .footer-btn.reset:active {
    color: var(--error);
  }

  .footer-btn.secondary {
    color: var(--text-secondary);
  }

  .footer-btn.secondary:active {
    background: var(--bg-hover);
  }

  .footer-btn.primary {
    color: white;
    background: var(--accent-primary);
    border: 1px solid var(--accent-primary);
  }

  .footer-btn.primary:active {
    filter: brightness(0.9);
  }

  /* Range inputs (for model tab if needed) */
  input[type="range"] {
    width: 100%;
    height: 4px;
    -webkit-appearance: none;
    appearance: none;
    background: var(--border-subtle);
    border-radius: 2px;
  }

  input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--accent-primary);
  }

  input[type="number"] {
    -moz-appearance: textfield;
  }

  /* MCP Servers in tools tab */
  .mcp-server-group {
    border: 1px solid var(--border-subtle, var(--border-color));
    border-radius: 6px;
    margin-bottom: 0.3rem;
    overflow: hidden;
  }

  .mcp-server-header-row {
    display: flex;
    align-items: center;
    background: var(--bg-elevated, var(--surface-secondary));
  }

  .mcp-server-header {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.5rem;
    background: none;
    border: none;
    color: var(--text-primary);
    font-size: 0.85rem;
    min-height: 44px;
  }

  .mcp-server-name {
    flex: 1;
    text-align: left;
    font-weight: 500;
  }

  .mcp-tool-count {
    font-size: 0.75rem;
    color: var(--text-secondary, var(--text-muted));
  }

  .mcp-bulk-btn {
    font-size: 0.75rem;
    color: var(--accent-primary, var(--accent));
    background: none;
    border: none;
    padding: 0.3rem 0.5rem;
    min-height: 44px;
  }

  .mcp-tool-list {
    border-top: 1px solid var(--border-subtle, var(--border-color));
  }

  .mcp-add-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    padding: 0.5rem;
    margin-top: 0.4rem;
    border: 1px dashed var(--border-color);
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    font-size: 0.85rem;
    width: 100%;
    min-height: 44px;
  }
</style>
