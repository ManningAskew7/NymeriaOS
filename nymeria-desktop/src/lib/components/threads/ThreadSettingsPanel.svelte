<script lang="ts">
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
  import MCPServerForm from '$lib/components/tools/MCPServerForm.svelte';
  import type { MCPServerCreateRequest } from '$lib/types';

  interface Props {
    thread: Thread;
    threadConfig: ThreadConfig | null;
    onClose: () => void;
    onSaved: (config: ThreadConfig) => void;
  }

  let { thread, threadConfig, onClose, onSaved }: Props = $props();

  // Active tab
  let activeTab = $state<'instructions' | 'system-prompt' | 'model' | 'tools' | 'triggers'>('instructions');

  // Form state — initialized from threadConfig
  let instructions = $state(threadConfig?.instructions ?? '');

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

  // Optional tools (derived from defaultToolsStore — tools NOT in the user's core set)
  // Split into non-MCP and MCP tools
  const optionalTools = $derived(() => {
    if (!defaultToolsStore.loaded) return [];
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    return defaultToolsStore.tools
      .filter(t => !coreSet.has(t.name) && !t.name.startsWith('mcp__'))
      .map(t => ({ name: t.name, description: t.description }));
  });

  // MCP tools grouped by server ID
  const mcpToolsByServer = $derived(() => {
    if (!defaultToolsStore.loaded) return {} as Record<string, { name: string; description: string }[]>;
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    const mcpTools = defaultToolsStore.tools.filter(t => t.name.startsWith('mcp__') && !coreSet.has(t.name));
    const grouped: Record<string, { name: string; description: string }[]> = {};
    for (const t of mcpTools) {
      const parts = t.name.split('__');
      const serverId = parts[1] ?? 'unknown';
      if (!grouped[serverId]) grouped[serverId] = [];
      grouped[serverId].push({ name: t.name, description: t.description });
    }
    return grouped;
  });

  // Also include MCP tools that ARE in the core set (for the core tools section display)
  const mcpCoreToolsByServer = $derived(() => {
    if (!defaultToolsStore.loaded) return {} as Record<string, { name: string; description: string }[]>;
    const coreSet = new Set(defaultToolsStore.defaultToolNames);
    const mcpTools = defaultToolsStore.tools.filter(t => t.name.startsWith('mcp__') && coreSet.has(t.name));
    const grouped: Record<string, { name: string; description: string }[]> = {};
    for (const t of mcpTools) {
      const parts = t.name.split('__');
      const serverId = parts[1] ?? 'unknown';
      if (!grouped[serverId]) grouped[serverId] = [];
      grouped[serverId].push({ name: t.name, description: t.description });
    }
    return grouped;
  });

  // MCP server add form state
  let showMcpAddForm = $state(false);
  let mcpAddLoading = $state(false);
  let mcpAddError = $state<string | null>(null);
  let expandedMcpServer = $state<string | null>(null);

  function getServerName(serverId: string): string {
    const server = mcpServersStore.servers.find(s => s.id === serverId);
    return server?.name ?? serverId;
  }

  function toggleAllMcpTools(serverId: string, tools: { name: string }[], enable: boolean) {
    const next = new Set(enabledTools);
    for (const t of tools) {
      if (enable) next.add(t.name);
      else next.delete(t.name);
    }
    enabledTools = next;
  }

  function areMcpToolsAllEnabled(tools: { name: string }[]): boolean {
    return tools.every(t => enabledTools.has(t.name));
  }

  async function handleMcpAdd(data: MCPServerCreateRequest) {
    mcpAddLoading = true;
    mcpAddError = null;
    try {
      const result = await mcpServersStore.create(data, thread.id);
      if (result.discoveryError) {
        mcpAddError = `Server added but discovery failed: ${result.discoveryError}`;
      } else {
        showMcpAddForm = false;
        mcpAddError = null;
        // Reload default tools to see new MCP tools
        defaultToolsStore.resetLoaded();
        await defaultToolsStore.load();
        // Auto-enable all discovered tools for this thread
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

  // Display provider mapping for thread-level overrides
  // "" = Default (inherit global), "anthropic_proxy" = subscription, "anthropic_direct" = direct API,
  // "local_openai" = openai provider pointed at a local OpenAI-compatible server
  type ThreadDisplayProvider = '' | 'anthropic_proxy' | 'anthropic_direct' | 'openai' | 'openrouter' | 'local_openai';

  // Hostnames that indicate a local OpenAI-compatible inference server.
  // host.docker.internal is how the Nymeria api container reaches the host.
  const LOCAL_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', 'host.docker.internal'];
  const DEFAULT_LOCAL_BASE_URL = 'http://host.docker.internal:8080/v1';

  function isLocalBaseUrl(baseUrl: string | null | undefined): boolean {
    if (!baseUrl) return false;
    return LOCAL_HOSTS.some(h => baseUrl.includes(h));
  }

  function toThreadDisplayProvider(provider: string, baseUrl?: string | null): ThreadDisplayProvider {
    if (!provider) return '';
    if (provider === 'anthropic' && baseUrl === '') return 'anthropic_direct';
    if (provider === 'anthropic') return 'anthropic_proxy';
    if (provider === 'openai' && isLocalBaseUrl(baseUrl)) return 'local_openai';
    return provider as ThreadDisplayProvider;
  }

  function fromThreadDisplayProvider(dp: ThreadDisplayProvider): { provider: string; baseUrl: string | null } {
    if (dp === '') return { provider: '', baseUrl: null };
    if (dp === 'anthropic_proxy') return { provider: 'anthropic', baseUrl: null };
    if (dp === 'anthropic_direct') return { provider: 'anthropic', baseUrl: '' };
    if (dp === 'local_openai') return { provider: 'openai', baseUrl: DEFAULT_LOCAL_BASE_URL };
    return { provider: dp, baseUrl: null };
  }

  // LLM form state
  let threadDisplayProvider = $state<ThreadDisplayProvider>(
    toThreadDisplayProvider(
      threadConfig?.llmConfig?.provider ?? '',
      threadConfig?.llmConfig?.base_url
    )
  );
  let llmProvider = $state(threadConfig?.llmConfig?.provider ?? '');
  let llmModel = $state(threadConfig?.llmConfig?.model ?? '');
  let llmBaseUrl = $state(threadConfig?.llmConfig?.base_url ?? '');
  let llmTemperature = $state<string>(
    threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature)
      : ''
  );
  let llmMaxTokens = $state<string>(
    threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens)
      : ''
  );
  let llmExtendedThinking = $state<'default' | 'true' | 'false'>(
    threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) as 'true' | 'false'
      : 'default'
  );
  let llmReasoningEffort = $state(threadConfig?.llmConfig?.reasoning_effort ?? '');
  let llmUseModelDefaults = $state<'default' | 'true' | 'false'>(
    threadConfig?.llmConfig?.use_model_defaults != null
      ? String(threadConfig.llmConfig.use_model_defaults) as 'true' | 'false'
      : 'default'
  );

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
  // (skip the model fetch for local_openai — we don't want to call the real
  // OpenAI API, and the user enters the local model alias as free text.)
  $effect(() => {
    const { provider } = fromThreadDisplayProvider(threadDisplayProvider);
    llmProvider = provider;
    if (threadDisplayProvider === 'local_openai') {
      availableModels = [];
      availableModelsProvider = '';
      return;
    }
    const ep = getEffectiveProvider();
    fetchAvailableModels(ep);
  });

  // Auto-populate the base URL field when the user picks Local LLM,
  // unless they already have a local URL in there.
  $effect(() => {
    if (threadDisplayProvider === 'local_openai' && !isLocalBaseUrl(llmBaseUrl)) {
      llmBaseUrl = DEFAULT_LOCAL_BASE_URL;
    }
  });

  // System prompt & agent fields
  let systemPrompt = $state(threadConfig?.systemPrompt ?? '');
  let isCallable = $state(threadConfig?.callable ?? false);
  let callableName = $state(threadConfig?.callableName ?? '');
  let callableDescription = $state(threadConfig?.callableDescription ?? '');

  // Visibility / advanced
  let injectTodosInPrompt = $state(threadConfig?.injectTodosInPrompt ?? false);
  let showAutonomousPrompts = $state(threadConfig?.showAutonomousPrompts ?? false);
  let showPromptMetadata = $state(threadConfig?.showPromptMetadata ?? false);

  // Search
  let toolSearch = $state('');
  let saving = $state(false);
  let error = $state('');
  let showToolWarning = $state(false);

  // Effective tool count for this thread (default tools minus disabled, plus optional enabled)
  const effectiveToolCount = $derived(() => {
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
    if (!mcpServersStore.loaded && !mcpServersStore.loading) {
      mcpServersStore.load();
    }
  });

  const filteredTools = $derived(() => {
    let allTools = unifiedToolsStore.tools;
    // Show only tools in the user's current default set (core tools for this thread)
    if (defaultToolsStore.loaded && defaultToolsStore.defaultToolNames.length > 0) {
      const coreSet = new Set(defaultToolsStore.defaultToolNames);
      allTools = allTools.filter(t => coreSet.has(t.name));
    }
    if (!toolSearch.trim()) return allTools;
    const q = toolSearch.toLowerCase();
    return allTools.filter(
      (t: UnifiedTool) =>
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.category.toLowerCase().includes(q)
    );
  });

  const disabledToolCount = $derived(disabledTools.size);

  const enabledToolCount = $derived(enabledTools.size);

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
    const origTemp = threadConfig?.llmConfig?.temperature != null
      ? String(threadConfig.llmConfig.temperature) : '';
    const origMaxTokens = threadConfig?.llmConfig?.max_tokens != null
      ? String(threadConfig.llmConfig.max_tokens) : '';
    const origExtThinking = threadConfig?.llmConfig?.extended_thinking != null
      ? String(threadConfig.llmConfig.extended_thinking) : 'default';
    const origReasoning = threadConfig?.llmConfig?.reasoning_effort ?? '';
    const origUseModelDefaults = threadConfig?.llmConfig?.use_model_defaults != null
      ? String(threadConfig.llmConfig.use_model_defaults) : 'default';

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
    if (llmProvider !== origProvider) return true;
    if (llmModel !== origModel) return true;
    if (llmTemperature !== origTemp) return true;
    if (llmMaxTokens !== origMaxTokens) return true;
    if (llmExtendedThinking !== origExtThinking) return true;
    if (llmReasoningEffort !== origReasoning) return true;
    if (llmUseModelDefaults !== origUseModelDefaults) return true;
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

    return false;
  }

  function checkToolCountAndSave() {
    const toolCount = effectiveToolCount();
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

      // LLM config
      const hasLlm = threadDisplayProvider || llmModel || llmTemperature || llmMaxTokens ||
        llmExtendedThinking !== 'default' || llmReasoningEffort ||
        llmUseModelDefaults !== 'default';

      if (hasLlm) {
        const llm: Record<string, unknown> = {};
        const mapped = fromThreadDisplayProvider(threadDisplayProvider);
        llm.provider = mapped.provider || null;
        // For local_openai, persist the user-editable base URL (not the default
        // from fromThreadDisplayProvider, which is just a placeholder).
        if (threadDisplayProvider === 'local_openai') {
          llm.base_url = llmBaseUrl || DEFAULT_LOCAL_BASE_URL;
        } else {
          llm.base_url = mapped.baseUrl;
        }
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
      onSaved({
        threadId: thread.id,
        instructions: null,
        disabledTools: [],
        enabledTools: [],
        llmConfig: null,
        systemPrompt: null,
        callable: false,
        callableName: null,
        callableDescription: null,
        showAutonomousPrompts: false,
        showPromptMetadata: false,
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
        class:active={activeTab === 'triggers'}
        onclick={() => (activeTab = 'triggers')}
        type="button"
      >
        Triggers
        {#if triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length > 0}
          <span class="tab-badge">{triggersStore.triggers.filter(t => t.enabled && t.thread_id === thread.id).length}</span>
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
            Leave empty to use the default. For agent threads, this defines the agent's personality and capabilities.
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
                <p class="field-hint">What the LLM sees as the tool description — describe when to use this thread.</p>
                <textarea
                  id="callable-desc-input"
                  class="instructions-input"
                  bind:value={callableDescription}
                  placeholder="e.g. Autonomous web research — finds information, summarizes articles, and compiles reports"
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
              <option value="local_openai">Local LLM (OpenAI-compatible)</option>
            </select>
          </div>

          {#if threadDisplayProvider === 'local_openai'}
            <div class="field-group">
              <label class="field-label" for="llm-base-url">API Base URL</label>
              <input
                id="llm-base-url"
                class="field-input"
                type="text"
                bind:value={llmBaseUrl}
                placeholder="http://host.docker.internal:8080/v1"
              />
              <span class="field-hint">
                URL of your local OpenAI-compatible server, reachable from inside the Nymeria api container. Use <code>host.docker.internal</code> when the server runs on the host.
              </span>
            </div>
          {/if}

          <div class="field-group">
            <label class="field-label" for="llm-model">Model</label>
            {#if threadDisplayProvider === 'local_openai'}
              <input
                id="llm-model"
                class="field-input"
                type="text"
                bind:value={llmModel}
                placeholder="Local model alias (e.g. local-llm)"
              />
              <span class="field-hint">
                The model alias your local server reports (the <code>--alias</code> flag value, or the model file basename).
              </span>
            {:else if availableModels.length > 0}
              <select id="llm-model" class="field-select" bind:value={llmModel}>
                <option value="">Default (inherit global)</option>
                {#each availableModels as model}
                  <option value={model.id}>{model.name || model.id}</option>
                {/each}
              </select>
            {:else if loadingAvailableModels}
              <select id="llm-model" class="field-select" disabled>
                <option>Loading models...</option>
              </select>
            {:else}
              <input
                id="llm-model"
                class="field-input"
                type="text"
                bind:value={llmModel}
                placeholder="Leave empty for global default"
              />
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

          {#if unifiedToolsStore.loading}
            <div class="tools-loading">Loading tools...</div>
          {:else}
            <div class="tools-list">
              {#each filteredTools() as tool (tool.id)}
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
            </div>
          {/if}

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

          <!-- MCP Servers subsection -->
          {#if Object.keys(mcpToolsByServer()).length > 0 || mcpServersStore.servers.length > 0}
            <div class="optional-tools-section">
              <span class="field-label">
                <Icon name="terminal" size={14} />
                MCP Servers
              </span>
              <p class="field-hint">
                Tools from MCP servers. Enable them for this thread.
              </p>

              {#each Object.entries(mcpToolsByServer()) as [serverId, tools]}
                <div class="mcp-server-group">
                  <div class="mcp-server-header-row">
                  <button
                    class="mcp-server-header"
                    onclick={() => expandedMcpServer = expandedMcpServer === serverId ? null : serverId}
                  >
                    <span class="mcp-server-name">{getServerName(serverId)}</span>
                    <span class="mcp-tool-count">{tools.filter(t => enabledTools.has(t.name)).length}/{tools.length}</span>
                  </button>
                  <button
                    class="mcp-bulk-toggle"
                    type="button"
                    onclick={() => toggleAllMcpTools(serverId, tools, !areMcpToolsAllEnabled(tools))}
                    title={areMcpToolsAllEnabled(tools) ? 'Disable all' : 'Enable all'}
                  >
                    {areMcpToolsAllEnabled(tools) ? 'Disable all' : 'Enable all'}
                  </button>
                </div>
                  {#if expandedMcpServer === serverId}
                    <div class="mcp-tool-list">
                      {#each tools as tool (tool.name)}
                        <div
                          class="tool-row"
                          class:optional-enabled={enabledTools.has(tool.name)}
                        >
                          <div class="tool-info">
                            <span class="tool-name">{tool.name.split('__').pop()}</span>
                            <span class="tool-desc">{tool.description}</span>
                          </div>
                          <button
                            class="tool-toggle"
                            class:off={!enabledTools.has(tool.name)}
                            onclick={() => toggleOptionalTool(tool.name)}
                            type="button"
                          >
                            <span class="toggle-track">
                              <span class="toggle-thumb"></span>
                            </span>
                          </button>
                        </div>
                      {/each}
                    </div>
                  {/if}
                </div>
              {/each}

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
            </div>
          {/if}
        </div>

      {:else if activeTab === 'triggers'}
        <div class="tab-panel">
          <TriggerConfigTab {thread} />
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
    toolCount={effectiveToolCount()}
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
    z-index: 1000;
  }

  .modal-panel {
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    width: min(560px, 90vw);
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
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
    border-bottom: 1px solid var(--border-default);
    padding: 0 var(--spacing-lg);
  }

  .tab {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: all var(--transition-fast);
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
    margin-top: var(--spacing-lg);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-default);
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

  .mcp-server-name {
    flex: 1;
    text-align: left;
    font-weight: 500;
  }

  .mcp-tool-count {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .mcp-bulk-toggle {
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.15rem 0.4rem;
    border-radius: var(--radius-sm);
  }

  .mcp-bulk-toggle:hover {
    background: var(--bg-hover);
  }

  .mcp-tool-list {
    border-top: 1px solid var(--border-subtle);
  }

  .mcp-add-btn {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-md);
    margin-top: var(--spacing-sm);
    border: 1px dashed var(--border-default);
    border-radius: var(--radius-sm);
    background: none;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    cursor: pointer;
    width: 100%;
    justify-content: center;
  }

  .mcp-add-btn:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }
</style>
