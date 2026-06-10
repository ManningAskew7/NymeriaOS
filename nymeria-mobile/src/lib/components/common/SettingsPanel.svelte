<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import { healthStore } from '$lib/stores/health.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { modelOptions } from '$lib/utils/modelOptions';
  import { buildMobileProviderGroups } from '$lib/utils/providerGroups';
  import { loadAvailableModels, type AvailableModelsState } from '$lib/utils/models';
  import { getThemeList, getThemePreviewColors, type ThemeName } from '$lib/themes';
  import type { ServerSettings, LLMProvider, OpenAIApiMode, LogLevel, LLMProviderSpec, ProviderRoute } from '$lib/types';
  import Icon from './Icon.svelte';
  import Button from './Button.svelte';
  import ProviderSelect from './ProviderSelect.svelte';
  import { CredentialManagerPanel } from '../credentials';
  import { MCPManagementPanel, ToolManagementPanel } from '../tools';
  import { AccountTab, UsersTab } from '../account';
  import {
    coerceProviderRoute,
    hasRouteChoice,
    providerRouteLabel,
    supportedRoutesForProvider,
  } from '$lib/utils/providerRoutes';

  interface Props {
    open: boolean;
    onClose: () => void;
    initialTab?: string;
  }

  let { open, onClose, initialTab }: Props = $props();

  type Tab = 'connection' | 'appearance' | 'llm' | 'agent' | 'rag' | 'tools' | 'mcp' | 'credentials' | 'voice' | 'account' | 'users';
  type TabConfig = { id: Tab; label: string; disabled: boolean };
  const adminServerTabs: Tab[] = ['llm', 'agent', 'voice', 'users'];
  // Tier-grouped picker options sourced from the live provider catalog.
  // Mobile has no synthetic display providers (no anthropic_proxy /
  // anthropic_direct / openai_custom variants like desktop), so the helper
  // can just bucket catalog specs by tier. See utils/providerGroups.ts.
  let activeTab = $state<Tab>('connection');
  let isAdmin = $derived(configStore.identity?.role === 'admin');

  function isAdminServerTab(tab: Tab): boolean {
    return adminServerTabs.includes(tab);
  }

  function visibleTabs(): TabConfig[] {
    const tabs: TabConfig[] = [
      { id: 'connection', label: 'Connection', disabled: false },
      { id: 'appearance', label: 'Theme', disabled: false },
    ];

    if (isAdmin) {
      tabs.push(
        { id: 'llm', label: 'Provider', disabled: !serverSettings },
        { id: 'agent', label: 'Agent', disabled: !serverSettings }
      );
    }

    tabs.push(
      { id: 'tools', label: 'Tools', disabled: !serverSettings },
      { id: 'rag', label: 'RAG', disabled: false },
      { id: 'mcp', label: 'MCP', disabled: !serverSettings },
      { id: 'credentials', label: 'Connections', disabled: !serverSettings }
    );

    if (isAdmin) {
      tabs.push({ id: 'voice', label: 'Voice', disabled: !serverSettings });
    }

    tabs.push({ id: 'account', label: 'Account', disabled: false });

    if (isAdmin) {
      tabs.push({ id: 'users', label: 'Users', disabled: false });
    }

    return tabs;
  }

  // When the panel re-opens with a requested tab, jump to it.
  $effect(() => {
    if (open && initialTab) {
      activeTab = initialTab as Tab;
    }
  });

  $effect(() => {
    if (!isAdmin && isAdminServerTab(activeTab)) {
      activeTab = 'connection';
    }
  });

  // Connection settings
  let apiUrl = $state(configStore.apiUrl);
  let apiKey = $state(configStore.apiKey);
  let testStatus = $state<'idle' | 'testing' | 'success' | 'error'>('idle');
  let testMessage = $state('');

  // Server settings
  let serverSettings = $state<ServerSettings | null>(null);
  let loadingSettings = $state(false);
  let savingSettings = $state(false);

  // LLM settings
  let llmProvider = $state<LLMProvider>('anthropic');
  let llmProviderRoute = $state<ProviderRoute | null>(null);
  let llmModel = $state('claude-sonnet-4-20250514');
  let llmTemperature = $state(1);
  let llmMaxTokens = $state<number | null>(null);
  let llmTopP = $state<number | null>(null);
  let llmTopK = $state<number | null>(null);
  let llmFrequencyPenalty = $state<number | null>(null);
  let llmPresencePenalty = $state<number | null>(null);
  let llmReasoningEffort = $state<string | null>(null);
  let llmExtendedThinking = $state(false);
  let llmUseModelDefaults = $state(false);
  let llmBaseUrl = $state('');
  let llmContextLength = $state<number | null | undefined>(null);
  let llmOllamaNumCtx = $state<number | null | undefined>(null);
  let openaiApiMode = $state<OpenAIApiMode>('responses');
  let showAdvancedLlm = $state(false);
  let providerCatalog = $state<LLMProviderSpec[]>([]);
  let mobileProviderGroups = $derived(buildMobileProviderGroups(providerCatalog));

  // Agent settings
  let contextManagement = $state<string>('auto_compact');
  let compactThreshold = $state(0.8);
  let compactThresholdMode = $state<'percentage' | 'tokens'>('percentage');
  let compactThresholdTokens = $state(100000);
  let slidingWindowCycles = $state(5);
  let logLevel = $state<LogLevel>('INFO');
  let watchdogEnabled = $state(true);
  let watchdogIntervalMinutes = $state(5);
  let todoStalenessMinutes = $state(20);

  // Voice settings
  let ttsProvider = $state<string>('none');
  let ttsBaseUrl = $state('');
  let ttsModel = $state('tts-1-hd');
  let ttsVoice = $state('nova');
  let ttsOutputFormat = $state('mp3');
  let ttsSpeed = $state(1.0);
  let sttProvider = $state<string>('none');
  let sttBaseUrl = $state('');
  let sttModel = $state('gpt-4o-mini-transcribe');
  let sttLanguage = $state('');
  let voiceDefaultThreadId = $state('');

  // RAG engine (server-wide, admin)
  let embeddingProvider = $state('openai');
  let embeddingModel = $state('text-embedding-3-small');
  let embeddingDimensions = $state<number | null>(null);
  let ragEmbedToolResults = $state(true);
  let ragRerankProvider = $state('llm');
  let ragRerankModel = $state('');
  // RAG (per-user) — via the per-user /users/{id}/rag/settings API
  let ragEnabled = $state(true);
  let ragMaxChunks = $state(5);
  let ragIncludeConversations = $state(true);
  let ragIncludeMemories = $state(false);
  let ragIncludeTodos = $state(true);
  let ragIncludeTools = $state(true);
  let ragAutoFlush = $state(true);
  let ragRetrievalMode = $state('hybrid');
  let ragRerankEnabled = $state(false);
  let ragUserLoading = $state(false);
  let ragUserSaving = $state(false);
  let ragUserMessage = $state('');
  let ragUserLoaded = $state(false);

  // Theme
  let selectedTheme = $state<ThemeName>(configStore.theme);
  const themeList = getThemeList();

  // Model metadata (reactive)
  const currentModelMeta = $derived(
    llmProvider === 'openrouter' ? modelsStore.getById(llmModel) : undefined
  );

  let availableModelsState = $state<AvailableModelsState>({
    models: [],
    provider: '',
    loading: false,
  });

  $effect(() => {
    void loadAvailableModels(llmProvider, availableModelsState, llmBaseUrl);
  });

  $effect(() => {
    if (hasRouteChoice(llmProvider, providerCatalog)) {
      llmProviderRoute = coerceProviderRoute(llmProvider, providerCatalog, llmProviderRoute);
    } else {
      llmProviderRoute = null;
    }
  });

  function showProviderRouteSelect(provider: string = llmProvider): boolean {
    return hasRouteChoice(provider, providerCatalog);
  }

  function showOpenAiApiMode(provider: string = llmProvider): boolean {
    if (!provider || provider === 'anthropic' || provider === 'bedrock') return false;
    if (hasRouteChoice(provider, providerCatalog)) {
      return coerceProviderRoute(provider, providerCatalog, llmProviderRoute) === 'openai_compat';
    }
    return provider !== 'google' && provider !== 'ollama';
  }

  // Load server settings
  async function loadServerSettings() {
    if (!configStore.isConfigured) return;
    loadingSettings = true;
    try {
      const [settings, catalog] = await Promise.all([
        api.getServerSettings(),
        api.getLLMProviderCatalog(),
      ]);
      serverSettings = settings;
      providerCatalog = catalog;
      llmProvider = serverSettings.llm_provider;
      llmProviderRoute = serverSettings.llm_provider_route;
      llmModel = serverSettings.llm_model;
      llmTemperature = serverSettings.llm_temperature;
      llmMaxTokens = serverSettings.llm_max_tokens;
      llmTopP = serverSettings.llm_top_p;
      llmTopK = serverSettings.llm_top_k;
      llmFrequencyPenalty = serverSettings.llm_frequency_penalty;
      llmPresencePenalty = serverSettings.llm_presence_penalty;
      llmReasoningEffort = serverSettings.llm_reasoning_effort;
      llmExtendedThinking = serverSettings.llm_extended_thinking;
      llmUseModelDefaults = serverSettings.llm_use_model_defaults;
      llmBaseUrl = serverSettings.llm_base_url || '';
      llmContextLength = serverSettings.llm_context_length;
      llmOllamaNumCtx = serverSettings.llm_ollama_num_ctx;
      openaiApiMode = serverSettings.openai_api_mode ?? 'responses';
      contextManagement = serverSettings.context_management;
      compactThreshold = serverSettings.compact_threshold ?? 0.8;
      compactThresholdMode = serverSettings.compact_threshold_mode ?? 'percentage';
      compactThresholdTokens = serverSettings.compact_threshold_tokens ?? 100000;
      slidingWindowCycles = serverSettings.sliding_window_cycles;
      logLevel = serverSettings.log_level;
      watchdogEnabled = serverSettings.watchdog_enabled;
      watchdogIntervalMinutes = serverSettings.watchdog_interval_minutes;
      todoStalenessMinutes = serverSettings.todo_staleness_minutes;
      // Voice
      ttsProvider = serverSettings.tts_provider ?? 'none';
      ttsBaseUrl = serverSettings.tts_base_url ?? '';
      ttsModel = serverSettings.tts_model ?? 'tts-1-hd';
      ttsVoice = serverSettings.tts_voice ?? 'nova';
      ttsOutputFormat = serverSettings.tts_output_format ?? 'mp3';
      ttsSpeed = serverSettings.tts_speed ?? 1.0;
      sttProvider = serverSettings.stt_provider ?? 'none';
      sttBaseUrl = serverSettings.stt_base_url ?? '';
      sttModel = serverSettings.stt_model ?? 'gpt-4o-mini-transcribe';
      sttLanguage = serverSettings.stt_language ?? '';
      voiceDefaultThreadId = serverSettings.voice_default_thread_id ?? '';
      embeddingProvider = serverSettings.embedding_provider ?? 'openai';
      embeddingModel = serverSettings.embedding_model ?? 'text-embedding-3-small';
      embeddingDimensions = serverSettings.embedding_dimensions;
      ragEmbedToolResults = serverSettings.rag_embed_tool_results ?? true;
      ragRerankProvider = serverSettings.rag_rerank_provider ?? 'llm';
      ragRerankModel = serverSettings.rag_rerank_model ?? '';
      if (serverSettings.llm_provider === 'openrouter') {
        modelsStore.loadModels();
      }
    } catch (e) {
      console.error('Failed to load settings:', e);
    } finally {
      loadingSettings = false;
    }
  }

  $effect(() => {
    if (open && configStore.isConfigured && !serverSettings && !loadingSettings) {
      loadServerSettings();
    }
  });

  async function loadRagUserSettings() {
    const uid = configStore.identity?.id;
    if (!uid || !configStore.isConfigured) return;
    ragUserLoading = true;
    try {
      const s = await api.getRagSettings(uid);
      ragEnabled = s.enabled;
      ragMaxChunks = s.max_chunks;
      ragIncludeConversations = s.include_conversations;
      ragIncludeMemories = s.include_memories;
      ragIncludeTodos = s.include_todos;
      ragIncludeTools = s.include_tools;
      ragAutoFlush = s.auto_flush;
      ragRetrievalMode = s.retrieval_mode;
      ragRerankEnabled = s.rerank_enabled;
      ragUserLoaded = true;
    } catch (e) {
      console.error('Failed to load RAG settings:', e);
    } finally {
      ragUserLoading = false;
    }
  }

  $effect(() => {
    if (open && activeTab === 'rag' && !ragUserLoaded && !ragUserLoading) {
      loadRagUserSettings();
    }
  });

  $effect(() => {
    if (llmProvider === 'openrouter') {
      modelsStore.loadModels();
    }
  });

  function handleSaveConnection() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;
    configStore.setupCompleted = true;
    healthStore.check();
    testStatus = 'success';
    testMessage = 'Connection saved!';
    setTimeout(() => { testMessage = ''; }, 2000);
  }

  async function handleTestConnection() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;
    testStatus = 'testing';
    testMessage = '';
    try {
      const isHealthy = await api.healthCheck();
      if (!isHealthy) {
        testStatus = 'error';
        testMessage = 'Cannot connect to server. Is the backend running?';
        return;
      }

      const authResponse = await api.verifyAuth();
      if (authResponse.status === 401 || authResponse.status === 403) {
        testStatus = 'error';
        testMessage = 'Invalid account token. Check that it matches a token issued by the backend.';
        return;
      }
      if (!authResponse.ok) {
        testStatus = 'error';
        testMessage = `Auth check failed: ${authResponse.status}`;
        return;
      }

      testStatus = 'success';
      testMessage = 'Connection successful!';
      await loadServerSettings();
    } catch (e) {
      testStatus = 'error';
      testMessage = e instanceof Error ? e.message : 'Connection failed';
    }
  }

  async function handleSaveServerSettings() {
    savingSettings = true;
    testMessage = '';
    try {
      const optionalNumberUpdate = (
        value: number | null | undefined,
        original: number | null | undefined
      ): number | null | undefined => value == null ? (original != null ? null : undefined) : value;
      const result = await api.updateServerSettings({
        llm_provider: llmProvider,
        llm_model: llmModel,
        llm_temperature: llmTemperature,
        llm_max_tokens: llmMaxTokens,
        llm_top_p: llmTopP,
        llm_top_k: llmTopK,
        llm_frequency_penalty: llmFrequencyPenalty,
        llm_presence_penalty: llmPresencePenalty,
        llm_reasoning_effort: llmReasoningEffort,
        llm_extended_thinking: llmExtendedThinking,
        llm_use_model_defaults: llmUseModelDefaults,
        llm_base_url: llmBaseUrl || null,
        llm_context_length: optionalNumberUpdate(llmContextLength, serverSettings?.llm_context_length),
        llm_ollama_num_ctx: optionalNumberUpdate(llmOllamaNumCtx, serverSettings?.llm_ollama_num_ctx),
        llm_provider_route: showProviderRouteSelect() ? llmProviderRoute : null,
        openai_api_mode: openaiApiMode,
        context_management: contextManagement,
        compact_threshold: compactThreshold,
        compact_threshold_mode: compactThresholdMode,
        compact_threshold_tokens: compactThresholdTokens,
        sliding_window_cycles: slidingWindowCycles,
        log_level: logLevel,
        watchdog_enabled: watchdogEnabled,
        watchdog_interval_minutes: watchdogIntervalMinutes,
        todo_staleness_minutes: todoStalenessMinutes,
        // Voice
        tts_provider: ttsProvider,
        tts_base_url: ttsBaseUrl || null,
        tts_model: ttsModel,
        tts_voice: ttsVoice,
        tts_output_format: ttsOutputFormat,
        tts_speed: ttsSpeed,
        stt_provider: sttProvider,
        stt_base_url: sttBaseUrl || null,
        stt_model: sttModel,
        stt_language: sttLanguage || null,
        voice_default_thread_id: voiceDefaultThreadId || null,
        embedding_provider: embeddingProvider,
        embedding_model: embeddingModel,
        embedding_dimensions: embeddingDimensions ?? null,
        rag_embed_tool_results: ragEmbedToolResults,
        rag_rerank_provider: ragRerankProvider,
        rag_rerank_model: ragRerankModel.trim() || null,
      });
      testStatus = 'success';
      testMessage = result.restart_required
        ? 'Saved! Restart server for some changes.'
        : 'Settings saved and applied!';
      serverSettingsStore.refresh();
    } catch (e) {
      testStatus = 'error';
      testMessage = e instanceof Error ? e.message : 'Failed to save';
    } finally {
      savingSettings = false;
    }
  }

  async function handleSaveRagUserSettings() {
    const uid = configStore.identity?.id;
    if (!uid) return;
    ragUserSaving = true;
    ragUserMessage = '';
    try {
      await api.updateRagSettings(uid, {
        enabled: ragEnabled,
        max_chunks: ragMaxChunks,
        include_conversations: ragIncludeConversations,
        include_memories: ragIncludeMemories,
        include_todos: ragIncludeTodos,
        include_tools: ragIncludeTools,
        auto_flush: ragAutoFlush,
        retrieval_mode: ragRetrievalMode,
        rerank_enabled: ragRerankEnabled,
      });
      ragUserMessage = 'RAG settings saved!';
    } catch (e) {
      ragUserMessage = e instanceof Error ? e.message : 'Failed to save RAG settings';
    } finally {
      ragUserSaving = false;
    }
  }

  function handleThemeSelect(theme: ThemeName) {
    selectedTheme = theme;
    configStore.setTheme(theme);
  }
</script>

{#if open}
  <div class="settings-modal">
    <div class="settings-header">
      <button type="button" class="back-btn" onclick={onClose}>
        <Icon name="chevronLeft" size={22} />
      </button>
      <h2>Settings</h2>
    </div>

    <div class="tab-bar">
      {#each visibleTabs() as tab}
        <button
          class="tab-btn"
          class:active={activeTab === tab.id}
          disabled={tab.disabled}
          onclick={() => (activeTab = tab.id as Tab)}
        >
          {tab.label}
        </button>
      {/each}
    </div>

    <div class="settings-body">
      <!-- Connection Tab -->
      {#if activeTab === 'connection'}
        <div class="setting-group">
          <label class="setting-label">API URL</label>
          <input
            type="url"
            class="setting-input"
            bind:value={apiUrl}
            placeholder="http://192.168.1.100:8000"
          />
          <p class="hint">The URL of your Nymeria API server</p>
        </div>
        <div class="setting-group">
          <label class="setting-label">Account Token</label>
          <input
            type="password"
            class="setting-input"
            bind:value={apiKey}
            placeholder="nym_..."
          />
          <p class="hint">Per-user account token (<code>nym_...</code>) or bootstrap token from <code>BOOTSTRAP_TOKEN.txt</code></p>
        </div>
        <div class="setting-actions">
          <Button variant="secondary" onclick={handleTestConnection}>
            {testStatus === 'testing' ? 'Testing...' : 'Test Connection'}
          </Button>
          <Button onclick={handleSaveConnection}>Save Connection</Button>
        </div>

      <!-- Theme Tab -->
      {:else if activeTab === 'appearance'}
        <div class="theme-grid">
          {#each themeList as theme}
            {@const colors = getThemePreviewColors(theme.id)}
            <button
              class="theme-card"
              class:active={selectedTheme === theme.id}
              onclick={() => handleThemeSelect(theme.id)}
            >
              <div class="theme-preview" style="background: {colors.bg};">
                <div class="preview-accent" style="background: {colors.accent};"></div>
                <div class="preview-text" style="color: {colors.text};">Aa</div>
              </div>
              <div class="theme-info">
                <span class="theme-name">{theme.name}</span>
                <span class="theme-desc">{theme.description}</span>
              </div>
            </button>
          {/each}
        </div>

        <div class="setting-group">
          <label class="setting-toggle">
            <input
              type="checkbox"
              checked={configStore.showAutonomousPrompts}
              onchange={(e) => (configStore.showAutonomousPrompts = e.currentTarget.checked)}
            />
            <span>Show autonomous prompts</span>
          </label>
          <p class="hint">
            Show the prompts sent by the scheduler, watchdog, and triggers as
            messages in chat (both live and in history). You can still force
            this on for a specific thread from its Thread Settings.
          </p>
        </div>

        <div class="setting-group">
          <label class="setting-toggle">
            <input
              type="checkbox"
              checked={configStore.describeToolCalls}
              onchange={(e) => (configStore.describeToolCalls = e.currentTarget.checked)}
            />
            <span>Describe tool calls</span>
          </label>
          <p class="hint">
            Show a short plain-English summary next to each tool name on the
            collapsed tool cards, so you can follow what the agent is doing
            without expanding them.
          </p>
        </div>

      <!-- Provider Tab -->
      {:else if activeTab === 'llm' && isAdmin}
        {#if loadingSettings}
          <div class="loading-state">Loading settings...</div>
        {:else}
          <div class="setting-group">
            <label class="setting-label" for="mobile-llm-provider">Provider</label>
            <ProviderSelect
              id="mobile-llm-provider"
              bind:value={llmProvider}
              groups={mobileProviderGroups}
            />
            <p class="hint">Save credentials in Connections or set the provider key in the server environment.</p>
          </div>

          {#if showProviderRouteSelect()}
            <div class="setting-group">
              <label class="setting-label" for="mobile-llm-provider-route">Provider Route</label>
              <select id="mobile-llm-provider-route" class="setting-input" bind:value={llmProviderRoute}>
                {#each supportedRoutesForProvider(llmProvider, providerCatalog) as route}
                  <option value={route}>{providerRouteLabel(route)}</option>
                {/each}
              </select>
              <p class="hint">
                {#if llmProviderRoute === 'openai_compat'}
                  Uses the provider's OpenAI-compatible API surface.
                {:else}
                  Uses the dedicated LangChain provider package when available.
                {/if}
              </p>
            </div>
          {/if}

          <div class="setting-group">
            <label class="setting-label">Model</label>
            {#if availableModelsState.models.length > 0}
              <select class="setting-input" bind:value={llmModel}>
                {#each availableModelsState.models as model}
                  <option value={model.id}>{model.name || model.id}</option>
                {/each}
              </select>
              <p class="hint">{availableModelsState.models.length} models available from provider</p>
            {:else if availableModelsState.loading}
              <select class="setting-input" disabled>
                <option>Loading models...</option>
              </select>
              <p class="hint">Fetching available models from provider...</p>
            {:else}
              <select class="setting-input" bind:value={llmModel}>
                {#if (modelOptions[llmProvider] ?? []).length > 0}
                  {#each modelOptions[llmProvider] ?? [] as opt}
                    <option value={opt.value}>{opt.label}</option>
                  {/each}
                {:else}
                  <option value={llmModel}>{llmModel || 'Type a model ID below'}</option>
                {/if}
              </select>
            {/if}
            <input
              type="text"
              class="setting-input setting-input-sm"
              bind:value={llmModel}
              placeholder={llmProvider === 'openrouter' ? 'e.g. meta-llama/llama-4-scout' : 'Custom model ID'}
            />
            <p class="hint">
              {#if llmProvider === 'openrouter'}
                Select above or paste a model ID from openrouter.ai/models
              {:else}
                Use the live model list when available, or type an exact model ID
              {/if}
            </p>
            {#if currentModelMeta}
              <div class="model-meta">
                <span class="meta-name">{currentModelMeta.name}</span>
                <span class="meta-details">
                  {modelsStore.formatContext(currentModelMeta.context_length)} ctx
                  {#if currentModelMeta.pricing_prompt != null}
                    &middot; In: {modelsStore.formatPrice(currentModelMeta.pricing_prompt)}
                  {/if}
                  {#if currentModelMeta.pricing_completion != null}
                    &middot; Out: {modelsStore.formatPrice(currentModelMeta.pricing_completion)}
                  {/if}
                  {#if currentModelMeta.input_modalities.includes('image')}
                    &middot; Vision
                  {/if}
                  {#if currentModelMeta.supported_parameters.includes('reasoning')}
                    &middot; Reasoning
                  {/if}
                </span>
              </div>
            {/if}
          </div>

          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={llmUseModelDefaults} />
              <span>Use model defaults</span>
            </label>
            <p class="hint">
              Let the provider apply model-specific optimal defaults
              {#if llmUseModelDefaults && currentModelMeta}
                {#if currentModelMeta.default_temperature != null}
                  (temp: {currentModelMeta.default_temperature})
                {/if}
              {/if}
            </p>
          </div>

          <div class="setting-group" class:field-disabled={llmUseModelDefaults}>
            <label class="setting-label">Temperature: {llmTemperature}</label>
            <input
              type="range"
              min="0"
              max="2"
              step="0.1"
              bind:value={llmTemperature}
              disabled={llmUseModelDefaults}
            />
            <p class="hint">0 = deterministic, 2 = creative</p>
          </div>

          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={llmExtendedThinking} />
              <span>Enable Reasoning</span>
            </label>
            <p class="hint">Enable thinking tokens for compatible models</p>
          </div>

          {#if showOpenAiApiMode()}
            <div class="setting-group">
              <label class="setting-label" for="openai-api-mode">API Mode</label>
              <select id="openai-api-mode" class="setting-input" bind:value={openaiApiMode}>
                <option value="responses">Responses API</option>
                <option value="chat_completions">Chat Completions (not recommended if thinking is enabled)</option>
              </select>
              <p class="hint">Responses API is the default path for OpenAI reasoning models, CLIProxy Codex OAuth, and OpenRouter beta.</p>
            </div>
          {/if}

          <!-- Advanced Settings -->
          <div class="advanced-section">
            <button
              class="advanced-toggle"
              onclick={() => (showAdvancedLlm = !showAdvancedLlm)}
            >
              <Icon name={showAdvancedLlm ? 'chevronDown' : 'chevronRight'} size={16} />
              <span>Advanced Settings</span>
            </button>

            {#if showAdvancedLlm}
              <div class="advanced-content">
                <div class="setting-group">
                  <label class="setting-label">Max Tokens</label>
                  <input
                    type="number"
                    class="setting-input"
                    min="1"
                    max="32000"
                    placeholder="Default (model limit)"
                    bind:value={llmMaxTokens}
                  />
                  <p class="hint">Maximum output tokens (1-32000)</p>
                </div>

                <div class="setting-group" class:field-disabled={llmUseModelDefaults}>
                  <label class="setting-label">
                    Top P: {llmTopP !== null ? llmTopP.toFixed(2) : 'Default'}
                  </label>
                  <div class="slider-row">
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.05"
                      value={llmTopP ?? 1}
                      oninput={(e) => (llmTopP = parseFloat((e.target as HTMLInputElement).value))}
                      disabled={llmUseModelDefaults}
                    />
                    <button class="clear-btn" onclick={() => (llmTopP = null)} disabled={llmUseModelDefaults}>×</button>
                  </div>
                  <p class="hint">Nucleus sampling threshold (0-1)</p>
                </div>

                <div class="setting-group">
                  <label class="setting-label">Top K</label>
                  <input
                    type="number"
                    class="setting-input"
                    min="1"
                    max="100"
                    placeholder="Default"
                    bind:value={llmTopK}
                  />
                  <p class="hint">Top-k sampling (1-100)</p>
                </div>

                <div class="setting-group" class:field-disabled={llmUseModelDefaults}>
                  <label class="setting-label">
                    Frequency Penalty: {llmFrequencyPenalty !== null ? llmFrequencyPenalty.toFixed(1) : 'Default'}
                  </label>
                  <div class="slider-row">
                    <input
                      type="range"
                      min="-2"
                      max="2"
                      step="0.1"
                      value={llmFrequencyPenalty ?? 0}
                      oninput={(e) => (llmFrequencyPenalty = parseFloat((e.target as HTMLInputElement).value))}
                      disabled={llmUseModelDefaults}
                    />
                    <button class="clear-btn" onclick={() => (llmFrequencyPenalty = null)} disabled={llmUseModelDefaults}>×</button>
                  </div>
                  <p class="hint">Reduce repetition of token sequences (-2 to 2)</p>
                </div>

                <div class="setting-group" class:field-disabled={llmUseModelDefaults}>
                  <label class="setting-label">
                    Presence Penalty: {llmPresencePenalty !== null ? llmPresencePenalty.toFixed(1) : 'Default'}
                  </label>
                  <div class="slider-row">
                    <input
                      type="range"
                      min="-2"
                      max="2"
                      step="0.1"
                      value={llmPresencePenalty ?? 0}
                      oninput={(e) => (llmPresencePenalty = parseFloat((e.target as HTMLInputElement).value))}
                      disabled={llmUseModelDefaults}
                    />
                    <button class="clear-btn" onclick={() => (llmPresencePenalty = null)} disabled={llmUseModelDefaults}>×</button>
                  </div>
                  <p class="hint">Encourage new topics (-2 to 2)</p>
                </div>

                <div class="setting-group">
                  <label class="setting-label">Reasoning Effort</label>
                  <select class="setting-input" bind:value={llmReasoningEffort}>
                    <option value={null}>Default</option>
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High</option>
                  </select>
                  <p class="hint">For reasoning models (o1, Claude with thinking)</p>
                </div>

                <div class="setting-group">
                  <label class="setting-label">API Base URL</label>
                  <input
                    type="text"
                    class="setting-input"
                    placeholder="Default (provider's standard URL)"
                    bind:value={llmBaseUrl}
                  />
                  <p class="hint">Override the API endpoint for proxies</p>
                </div>

                <div class="setting-group">
                  <label class="setting-label" for="llm-context-length">Context Window Tokens</label>
                  <input
                    id="llm-context-length"
                    type="number"
                    class="setting-input"
                    min="1000"
                    max="2000000"
                    step="1"
                    placeholder="Auto-detect"
                    bind:value={llmContextLength}
                  />
                  <p class="hint">Manual context window override for local servers or proxies that do not report it</p>
                </div>

                <div class="setting-group">
                  <label class="setting-label" for="llm-ollama-num-ctx">Ollama num_ctx</label>
                  <input
                    id="llm-ollama-num-ctx"
                    type="number"
                    class="setting-input"
                    min="1000"
                    max="2000000"
                    step="1"
                    placeholder="Auto-detect"
                    bind:value={llmOllamaNumCtx}
                  />
                  <p class="hint">Passed to Ollama as options.num_ctx. Leave empty unless you need a VRAM cap.</p>
                </div>
              </div>
            {/if}
          </div>

          <Button onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save LLM Settings'}
          </Button>
        {/if}

      <!-- Agent Tab -->
      {:else if activeTab === 'agent' && isAdmin}
        {#if loadingSettings}
          <div class="loading-state">Loading settings...</div>
        {:else}
          <div class="setting-group">
            <label class="setting-label">Context Management</label>
            <select class="setting-input" bind:value={contextManagement}>
              <option value="auto_compact">Auto-Compact (default)</option>
              <option value="sliding_window">Sliding Window</option>
              <option value="none">None</option>
            </select>
            <p class="hint">
              {#if contextManagement === 'auto_compact'}
                Summarizes old messages when context gets large
              {:else if contextManagement === 'sliding_window'}
                Keeps only the N most recent conversation cycles
              {:else}
                No context management. History grows unbounded
              {/if}
            </p>
          </div>

          {#if contextManagement === 'auto_compact'}
            <div class="setting-group">
              <label class="setting-label" for="compact-threshold-mode">Auto-Compact Trigger</label>
              <select id="compact-threshold-mode" bind:value={compactThresholdMode}>
                <option value="percentage">Percentage of context window</option>
                <option value="tokens">Absolute input-token count</option>
              </select>
              <p class="hint">
                {#if compactThresholdMode === 'percentage'}
                  Triggers at a fraction of the model's context window.
                {:else}
                  Triggers at a fixed input-token count regardless of model. Clamped to the model's context window.
                {/if}
              </p>
            </div>

            {#if compactThresholdMode === 'percentage'}
              <div class="setting-group">
                <label class="setting-label" for="compact-threshold">Auto-Compact Threshold: {Math.round(compactThreshold * 100)}%</label>
                <input
                  id="compact-threshold"
                  type="range"
                  min="0.05"
                  max="0.95"
                  step="0.01"
                  bind:value={compactThreshold}
                />
                <p class="hint">Context usage percentage that triggers summarization</p>
              </div>
            {:else}
              <div class="setting-group">
                <label class="setting-label" for="compact-threshold-tokens">Auto-Compact Token Threshold: {compactThresholdTokens.toLocaleString()} tokens</label>
                <input
                  id="compact-threshold-tokens"
                  type="number"
                  min="1000"
                  max="2000000"
                  step="1000"
                  bind:value={compactThresholdTokens}
                />
                <p class="hint">Token count from the most recent provider response that triggers summarization (1,000-2,000,000)</p>
              </div>
            {/if}
          {/if}

          {#if contextManagement === 'sliding_window'}
            <div class="setting-group">
              <label class="setting-label">Window Cycles: {slidingWindowCycles}</label>
              <input
                type="range"
                min="1"
                max="20"
                step="1"
                bind:value={slidingWindowCycles}
              />
              <p class="hint">Number of conversation cycles to keep</p>
            </div>
          {/if}

          <div class="setting-group">
            <label class="setting-label">Log Level</label>
            <select class="setting-input" bind:value={logLevel}>
              <option value="DEBUG">DEBUG</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
            </select>
          </div>

          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={watchdogEnabled} />
              <span>Enable Watchdog</span>
            </label>
            <p class="hint">Monitor task staleness and nudge agent</p>
          </div>

          {#if watchdogEnabled}
            <div class="setting-group">
              <label class="setting-label">Watchdog Interval: {watchdogIntervalMinutes} min</label>
              <input
                type="range"
                min="5"
                max="60"
                step="5"
                bind:value={watchdogIntervalMinutes}
              />
            </div>

            <div class="setting-group">
              <label class="setting-label">Staleness Threshold: {todoStalenessMinutes} min</label>
              <input
                type="range"
                min="5"
                max="240"
                step="5"
                bind:value={todoStalenessMinutes}
              />
            </div>
          {/if}

          <Button onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save Agent Settings'}
          </Button>
        {/if}

      {:else if activeTab === 'rag'}
        <h3 class="section-heading">My RAG (this account)</h3>
        {#if ragUserLoading}
          <p class="loading">Loading RAG settings...</p>
        {:else}
          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragEnabled} />
              <span>Enable semantic memory (RAG)</span>
            </label>
            <p class="hint">Let the agent search your own past threads, tool results, and notes</p>
          </div>

          <div class="setting-group">
            <label class="setting-label">Retrieval mode</label>
            <select class="setting-input" bind:value={ragRetrievalMode}>
              <option value="hybrid">Hybrid (BM25 + vector)</option>
              <option value="vector">Vector-only</option>
            </select>
            <p class="hint">
              {#if ragRetrievalMode === 'hybrid'}
                Robust default. If the embedder underperforms, BM25 still salvages the ranking so RAG stays useful.
              {:else}
                Vector-only: typically higher scores with a strong embedder, but nothing if embeddings fail.
              {/if}
            </p>
          </div>

          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragRerankEnabled} />
              <span>Use reranker</span>
            </label>
            <p class="hint">Reorder results for accuracy (adds latency; uses the server's configured reranker)</p>
          </div>

          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragIncludeConversations} />
              <span>Search threads</span>
            </label>
          </div>
          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragIncludeMemories} />
              <span>Search saved memories</span>
            </label>
          </div>
          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragIncludeTodos} />
              <span>Search completed tasks</span>
            </label>
          </div>
          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragIncludeTools} />
              <span>Search tool results</span>
            </label>
          </div>
          <div class="setting-group">
            <label class="setting-label">Max results per search: {ragMaxChunks}</label>
            <input type="range" min="1" max="10" step="1" bind:value={ragMaxChunks} />
          </div>
          <div class="setting-group">
            <label class="setting-toggle">
              <input type="checkbox" bind:checked={ragAutoFlush} />
              <span>Auto-flush before context trims</span>
            </label>
          </div>

          <Button onclick={handleSaveRagUserSettings} disabled={ragUserSaving}>
            {ragUserSaving ? 'Saving...' : 'Save My RAG Settings'}
          </Button>
          {#if ragUserMessage}
            <p class="hint">{ragUserMessage}</p>
          {/if}
        {/if}

        {#if isAdmin}
          <h3 class="section-heading">RAG engine (server-wide)</h3>
          {#if loadingSettings}
            <p class="loading">Loading settings...</p>
          {:else}
            <div class="setting-group">
              <label class="setting-toggle">
                <input type="checkbox" bind:checked={ragEmbedToolResults} />
                <span>Embed tool results</span>
              </label>
              <p class="hint">Index tool output as retrievable chunks (deduped at ingest)</p>
            </div>
            <div class="setting-group">
              <label class="setting-label">Embedding provider</label>
              <select class="setting-input" bind:value={embeddingProvider}>
                <option value="openai">OpenAI-compatible (incl. Voyage)</option>
                <option value="cohere">Cohere (native)</option>
                <option value="gemini">Gemini (native)</option>
                <option value="local">Local (on-device)</option>
              </select>
            </div>
            <div class="setting-group">
              <label class="setting-label">Embedding model</label>
              <input class="setting-input" type="text" bind:value={embeddingModel} />
            </div>
            <div class="setting-group">
              <label class="setting-label">Embedding dimensions</label>
              <input class="setting-input" type="number" step="1" bind:value={embeddingDimensions} />
              <p class="hint">Vector width. Blank keeps 1536. Changing the embedder needs a restart, and a dimension change needs `nymeria reembed`.</p>
            </div>
            <div class="setting-group">
              <label class="setting-label">Reranker provider</label>
              <select class="setting-input" bind:value={ragRerankProvider}>
                <option value="llm">LLM (thread model)</option>
                <option value="voyage">Voyage</option>
                <option value="cohere">Cohere</option>
                <option value="zeroentropy">ZeroEntropy</option>
                <option value="local">Local cross-encoder</option>
              </select>
              <p class="hint">The engine each user's "Use reranker" toggle drives. Managed providers need a key; 'local' needs the local-rag extra.</p>
            </div>
            {#if ragRerankProvider !== 'llm'}
              <div class="setting-group">
                <label class="setting-label">Reranker model</label>
                <input class="setting-input" type="text" bind:value={ragRerankModel} placeholder="e.g. rerank-2.5-lite" />
              </div>
            {/if}
            <Button onclick={handleSaveServerSettings} disabled={savingSettings}>
              {savingSettings ? 'Saving...' : 'Save RAG Engine'}
            </Button>
          {/if}
        {/if}

      <!-- Tools Tab -->
      {:else if activeTab === 'tools'}
        <ToolManagementPanel open={true} onClose={onClose} />

      {:else if activeTab === 'mcp'}
        <MCPManagementPanel open={true} onClose={onClose} />

      {:else if activeTab === 'credentials'}
        <CredentialManagerPanel />

      {:else if activeTab === 'voice' && isAdmin}
        {#if loadingSettings}
          <div class="loading-state">Loading settings...</div>
        {:else}
          <h3 class="section-heading">Text-to-Speech (TTS)</h3>

          <div class="setting-group">
            <label class="setting-label">TTS Provider</label>
            <select class="setting-input" bind:value={ttsProvider}>
              <option value="none">None (disabled)</option>
              <option value="cartesia">Cartesia Sonic</option>
              <option value="gemini">Gemini TTS</option>
              <option value="openai">OpenAI</option>
              <option value="qwen3">Qwen3-TTS (Local)</option>
            </select>
            <p class="hint">
              {#if ttsProvider === 'cartesia'}
                Cartesia Sonic-3 with ultra-low latency (~90ms) and speed/emotion controls
              {:else if ttsProvider === 'gemini'}
                Google Gemini 3.1 Flash TTS with 30 voices, 70+ languages, and audio tag support
              {:else if ttsProvider === 'openai'}
                Uses OpenAI TTS API (tts-1, tts-1-hd)
              {:else if ttsProvider === 'qwen3'}
                Local Qwen3-TTS via OpenAI-compatible server
              {:else}
                TTS disabled. Voice endpoints will not return audio
              {/if}
            </p>
          </div>

          {#if ttsProvider !== 'none'}
            {#if ttsProvider !== 'gemini' && ttsProvider !== 'cartesia'}
              <div class="setting-group">
                <label class="setting-label">Base URL</label>
                <input
                  type="text"
                  class="setting-input"
                  bind:value={ttsBaseUrl}
                  placeholder={ttsProvider === 'openai' ? 'https://api.openai.com/v1' : 'http://localhost:8880/v1'}
                />
                <p class="hint">Leave empty for default ({ttsProvider === 'openai' ? 'api.openai.com' : 'localhost:8880'})</p>
              </div>
            {/if}

            <div class="setting-group">
              <label class="setting-label">Model</label>
              <input
                type="text"
                class="setting-input"
                bind:value={ttsModel}
                placeholder={ttsProvider === 'cartesia' ? 'sonic-3' : ttsProvider === 'gemini' ? 'gemini-3.1-flash-tts-preview' : ttsProvider === 'openai' ? 'tts-1-hd' : 'Qwen3-TTS-0.6B'}
              />
            </div>

            <div class="setting-group">
              <label class="setting-label">Voice</label>
              <input
                type="text"
                class="setting-input"
                bind:value={ttsVoice}
                placeholder={ttsProvider === 'cartesia' ? 'Voice ID from play.cartesia.ai' : ttsProvider === 'gemini' ? 'Kore' : ttsProvider === 'openai' ? 'nova' : 'default'}
              />
              <p class="hint">
                {#if ttsProvider === 'cartesia'}
                  Voice UUID from play.cartesia.ai/voices
                {:else if ttsProvider === 'gemini'}
                  Options: Kore, Puck, Charon, Algenib, Leda, Orus, Zephyr, and more
                {:else if ttsProvider === 'openai'}
                  Options: alloy, echo, fable, onyx, nova, shimmer
                {:else}
                  Voice ID or reference audio path for Qwen3-TTS
                {/if}
              </p>
            </div>

            {#if ttsProvider !== 'gemini'}
              {#if ttsProvider !== 'cartesia'}
                <div class="setting-group">
                  <label class="setting-label">Output Format</label>
                  <select class="setting-input" bind:value={ttsOutputFormat}>
                    <option value="mp3">MP3</option>
                    <option value="wav">WAV</option>
                    <option value="opus">Opus</option>
                    <option value="aac">AAC</option>
                  </select>
                </div>
              {/if}

              <div class="setting-group">
                <label class="setting-label">Speed: {ttsSpeed.toFixed(2)}x</label>
                <input
                  type="range"
                  min="0.25"
                  max="4.0"
                  step="0.25"
                  bind:value={ttsSpeed}
                />
              </div>
            {/if}
          {/if}

          <h3 class="section-heading">Speech-to-Text (STT)</h3>

          <div class="setting-group">
            <label class="setting-label">STT Provider</label>
            <select class="setting-input" bind:value={sttProvider}>
              <option value="none">None (disabled)</option>
              <option value="openai">OpenAI</option>
              <option value="faster-whisper">Faster-Whisper (Local)</option>
            </select>
            <p class="hint">
              {#if sttProvider === 'openai'}
                Uses OpenAI transcription API (gpt-4o-mini-transcribe)
              {:else if sttProvider === 'faster-whisper'}
                Local faster-whisper via OpenAI-compatible server
              {:else}
                STT disabled. Voice endpoints will not accept audio
              {/if}
            </p>
          </div>

          {#if sttProvider !== 'none'}
            <div class="setting-group">
              <label class="setting-label">Base URL</label>
              <input
                type="text"
                class="setting-input"
                bind:value={sttBaseUrl}
                placeholder={sttProvider === 'openai' ? 'https://api.openai.com/v1' : 'http://localhost:8003/v1'}
              />
              <p class="hint">Leave empty for default ({sttProvider === 'openai' ? 'api.openai.com' : 'localhost:8003'})</p>
            </div>

            <div class="setting-group">
              <label class="setting-label">Model</label>
              <input
                type="text"
                class="setting-input"
                bind:value={sttModel}
                placeholder={sttProvider === 'openai' ? 'gpt-4o-mini-transcribe' : 'large-v3-turbo'}
              />
            </div>

            <div class="setting-group">
              <label class="setting-label">Language Hint</label>
              <input
                type="text"
                class="setting-input setting-input-sm"
                bind:value={sttLanguage}
                placeholder="en"
              />
              <p class="hint">Optional ISO 639-1 code (e.g., en, es, de). Improves accuracy.</p>
            </div>
          {/if}

          <h3 class="section-heading">Watch / Default Thread</h3>

          <div class="setting-group">
            <label class="setting-label">Voice Thread</label>
            <select class="setting-input" bind:value={voiceDefaultThreadId}>
              <option value="">watch-default (auto-created)</option>
              {#each threadsStore.threads as thread}
                <option value={thread.id}>{thread.title || thread.id}</option>
              {/each}
            </select>
            <p class="hint">Thread used by the watch app and /voice/chat endpoint when no thread is specified</p>
          </div>

          <Button onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save Voice Settings'}
          </Button>
        {/if}

      <!-- Account Tab (current user identity + sign out) -->
      {:else if activeTab === 'account'}
        <AccountTab />

      <!-- Users Tab (admin only) -->
      {:else if activeTab === 'users' && isAdmin}
        <UsersTab />
      {/if}
    </div>

    <!-- Status message -->
    {#if testMessage}
      <div class="status-bar" class:success={testStatus === 'success'} class:error={testStatus === 'error'}>
        {#if testStatus === 'success'}
          <Icon name="success" size={16} />
        {:else if testStatus === 'error'}
          <Icon name="error" size={16} />
        {/if}
        {testMessage}
      </div>
    {/if}
  </div>
{/if}

<style>
  .settings-modal {
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
    padding-top: var(--safe-area-top);
  }

  .back-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
  }

  .back-btn:active {
    background: var(--bg-hover);
  }

  .settings-header h2 {
    font-size: var(--font-size-lg);
    font-weight: 600;
  }

  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    overflow-x: auto;
  }

  .tab-btn {
    flex: 0 0 auto;
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    white-space: nowrap;
    min-height: var(--touch-target-min);
  }

  .users-placeholder {
    padding: var(--spacing-md);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }
  .users-placeholder h3 {
    margin: 0 0 var(--spacing-sm);
    font-size: var(--font-size-md);
    color: var(--text-primary);
  }
  .users-placeholder p {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    line-height: 1.55;
  }
  .users-placeholder code {
    background: var(--bg-base);
    padding: 1px 6px;
    border-radius: var(--radius-sm);
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .tab-btn.active {
    color: var(--accent-primary);
    border-bottom-color: var(--accent-primary);
  }

  .tab-btn:disabled {
    opacity: 0.4;
  }

  .settings-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-lg);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .setting-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .section-heading {
    font-size: var(--font-size-md);
    font-weight: 600;
    color: var(--text-primary);
    margin: var(--spacing-sm) 0 0 0;
    padding-bottom: var(--spacing-xs);
    border-bottom: 1px solid var(--border-subtle);
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
    min-height: var(--touch-target-min);
  }

  .setting-input:focus {
    outline: none;
    border-color: var(--accent-primary);
  }

  .setting-input-sm {
    margin-top: var(--spacing-xs);
  }

  select.setting-input {
    appearance: none;
    -webkit-appearance: none;
  }

  .setting-toggle {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: var(--font-size-sm);
    color: var(--text-primary);
    min-height: var(--touch-target-min);
  }

  .setting-toggle input {
    width: 22px;
    height: 22px;
    accent-color: var(--accent-primary);
  }

  .setting-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    flex-wrap: wrap;
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .loading-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-xl);
  }

  .field-disabled {
    opacity: 0.4;
    pointer-events: none;
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

  /* Advanced section */
  .advanced-section {
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .advanced-toggle {
    width: 100%;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    min-height: var(--touch-target-min);
  }

  .advanced-toggle:active {
    background: var(--bg-hover);
  }

  .advanced-content {
    padding: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .slider-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .slider-row input[type="range"] {
    flex: 1;
  }

  .clear-btn {
    width: var(--touch-target-min);
    height: var(--touch-target-min);
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-muted);
    font-size: 18px;
    flex-shrink: 0;
  }

  .clear-btn:active {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .clear-btn:disabled {
    opacity: 0.3;
  }

  /* Theme grid */
  .theme-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: var(--spacing-md);
  }

  .theme-card {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm);
    border: 2px solid var(--border-subtle);
    border-radius: var(--radius-lg);
    text-align: left;
  }

  .theme-card.active {
    border-color: var(--accent-primary);
  }

  .theme-card:active {
    background: var(--bg-hover);
  }

  .theme-preview {
    position: relative;
    width: 100%;
    height: 48px;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }

  .preview-accent {
    position: absolute;
    bottom: 0;
    left: 0;
    width: 100%;
    height: 4px;
  }

  .preview-text {
    font-size: 18px;
    font-weight: 600;
    opacity: 0.8;
  }

  .theme-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .theme-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .theme-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.3;
  }

  /* Status bar */
  .status-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-sm);
    flex-shrink: 0;
    padding-bottom: calc(var(--spacing-sm) + var(--safe-area-bottom));
  }

  .status-bar.success {
    background: rgba(var(--success-rgb), 0.15);
    color: var(--success);
  }

  .status-bar.error {
    background: rgba(248, 113, 113, 0.15);
    color: var(--error);
  }

  /* Range inputs */
  input[type="range"] {
    width: 100%;
    height: 4px;
    -webkit-appearance: none;
    appearance: none;
    background: var(--border-subtle);
    border-radius: 2px;
    outline: none;
  }

  input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--accent-primary);
    cursor: pointer;
    transition: box-shadow 120ms ease;
  }

  /* §6 — slider thumb glow on keyboard focus. Mobile sliders are usually
     touched, not focused, so :focus-visible avoids triggering on iOS's
     sticky-hover after touch — the ring only appears for an external
     keyboard user. Desktop SettingsPanel has the same pattern. */
  input[type="range"]:focus-visible::-webkit-slider-thumb {
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-primary) 18%, transparent);
  }

  input[type="range"]:focus-visible::-moz-range-thumb {
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-primary) 18%, transparent);
  }

  input[type="number"] {
    -moz-appearance: textfield;
  }
</style>
