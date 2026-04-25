<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { connectionsStore } from '$lib/stores/connections.svelte';
  import { api } from '$lib/services/api.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import type { ServerSettings, LLMProvider, LogLevel, ThemeName, SavedConnection, AvailableModel } from '$lib/types';
  import { getThemeList, getThemePreviewColors } from '$lib/themes';
  import { modelOptions } from '$lib/utils/modelOptions';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import { ToolManagementPanel } from '../tools';
  import SkillsPanel from '../skills/SkillsPanel.svelte';
  import CLIProxyPanel from './CLIProxyPanel.svelte';
  import { backendProcessStore } from '$lib/stores/backendProcess.svelte';

  interface Props {
    initialTab?: string;
  }

  let { initialTab }: Props = $props();

  // Connection settings
  let apiUrl = $state(configStore.apiUrl);
  let apiKey = $state(configStore.apiKey);

  // Saved connections UI state
  let showSaveInput = $state(false);
  let saveConnectionName = $state('');
  let editingConnectionId = $state<string | null>(null);
  let editingName = $state('');

  // Display provider: splits "anthropic" into proxy vs direct based on base_url,
  // and "openai" into hosted vs local based on whether base_url points at a local host.
  type DisplayProvider = 'anthropic_proxy' | 'anthropic_direct' | 'openai' | 'openrouter' | 'local_openai';

  // Hostnames that indicate a local OpenAI-compatible inference server
  // (llama.cpp llama-server, LM Studio, Ollama, etc.). host.docker.internal is
  // how the Nymeria api container reaches the Windows/macOS host.
  const LOCAL_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', 'host.docker.internal'];
  const DEFAULT_LOCAL_BASE_URL = 'http://host.docker.internal:8080/v1';

  // Default CLIProxy URL inside the Nymeria docker network. Used when the user
  // picks Anthropic (Subscription) but llmBaseUrl is empty (e.g. they were
  // previously on Direct API which stores base_url as "").
  const DEFAULT_CLIPROXY_BASE_URL = 'http://cli-proxy-api:8317';

  function isLocalBaseUrl(baseUrl: string | null | undefined): boolean {
    if (!baseUrl) return false;
    return LOCAL_HOSTS.some(h => baseUrl.includes(h));
  }

  function toDisplayProvider(provider: LLMProvider, baseUrl: string): DisplayProvider {
    if (provider === 'anthropic' && !baseUrl) return 'anthropic_direct';
    if (provider === 'anthropic') return 'anthropic_proxy';
    if (provider === 'openai' && isLocalBaseUrl(baseUrl)) return 'local_openai';
    return provider as DisplayProvider;
  }

  function fromDisplayProvider(dp: DisplayProvider): { provider: LLMProvider; clearBaseUrl: boolean } {
    if (dp === 'anthropic_proxy') return { provider: 'anthropic', clearBaseUrl: false };
    if (dp === 'anthropic_direct') return { provider: 'anthropic', clearBaseUrl: true };
    if (dp === 'local_openai') return { provider: 'openai', clearBaseUrl: false };
    return { provider: dp as LLMProvider, clearBaseUrl: false };
  }

  // Server settings
  let serverSettings = $state<ServerSettings | null>(null);
  let displayProvider = $state<DisplayProvider>('anthropic_proxy');
  let llmProvider = $state<LLMProvider>('anthropic');
  let llmModel = $state('claude-sonnet-4-20250514');
  let llmTemperature = $state(1);
  let showModelHelp = $state(false);
  // Advanced LLM settings
  let llmMaxTokens = $state<number | null>(null);
  let llmTopP = $state<number | null>(null);
  let llmTopK = $state<number | null>(null);
  let llmFrequencyPenalty = $state<number | null>(null);
  let llmPresencePenalty = $state<number | null>(null);
  let llmReasoningEffort = $state<string | null>(null);
  let llmExtendedThinking = $state(false);
  let llmUseModelDefaults = $state(false);
  let llmBaseUrl = $state('');
  let showAdvancedLlm = $state(false);
  // Agent settings
  let contextManagement = $state<string>('auto_compact');
  let slidingWindowCycles = $state(5);
  let maxSelfInvokesPerHour = $state(50);
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

  // Theme settings
  let selectedTheme = $state<ThemeName>(configStore.theme);
  const themeList = getThemeList();

  // Model metadata (reactive lookup based on current model ID)
  const currentModelMeta = $derived(
    llmProvider === 'openrouter' ? modelsStore.getById(llmModel) : undefined
  );

  // Dynamic model list for providers that support /v1/models
  let availableModels = $state<AvailableModel[]>([]);
  let loadingAvailableModels = $state(false);
  let availableModelsProvider = $state<string>('');

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

  // Sync llmProvider from displayProvider and fetch models
  // (skip the model fetch for local_openai — we don't want to call the real
  // OpenAI API, and the user enters the local model alias as free text.)
  $effect(() => {
    const { provider } = fromDisplayProvider(displayProvider);
    llmProvider = provider;
    if (displayProvider === 'local_openai') {
      availableModels = [];
      availableModelsProvider = '';
      return;
    }
    fetchAvailableModels(provider);
  });

  // Auto-populate the base URL field when the user picks Local LLM,
  // unless they already have a local URL in there.
  $effect(() => {
    if (displayProvider === 'local_openai' && !isLocalBaseUrl(llmBaseUrl)) {
      llmBaseUrl = DEFAULT_LOCAL_BASE_URL;
    }
  });

  // Auto-populate the base URL field when the user picks Anthropic (Subscription)
  // and the field is empty. Without this, switching from Direct API → Subscription
  // saves an empty base_url, which gets interpreted as Direct on reload.
  $effect(() => {
    if (displayProvider === 'anthropic_proxy' && !llmBaseUrl) {
      llmBaseUrl = DEFAULT_CLIPROXY_BASE_URL;
    }
  });

  // UI state
  type SettingsTab = 'connection' | 'appearance' | 'llm' | 'agent' | 'tools' | 'skills' | 'voice' | 'proxy';
  let activeTab = $state<SettingsTab>((initialTab as SettingsTab) || 'connection');
  let showConnectionAdvanced = $state(!backendProcessStore.isTauri);
  let testStatus = $state<'idle' | 'testing' | 'success' | 'error'>('idle');
  let testMessage = $state('');
  let loadingSettings = $state(false);
  let savingSettings = $state(false);

  // Load server settings when connected
  async function loadServerSettings() {
    if (!configStore.isConfigured) return;

    loadingSettings = true;
    try {
      serverSettings = await api.getServerSettings();
      llmProvider = serverSettings.llm_provider;
      displayProvider = toDisplayProvider(serverSettings.llm_provider, serverSettings.llm_base_url || '');
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
      contextManagement = serverSettings.context_management;
      // Load model metadata for OpenRouter enrichment
      if (serverSettings.llm_provider === 'openrouter') {
        modelsStore.loadModels();
      }
      slidingWindowCycles = serverSettings.sliding_window_cycles;
      maxSelfInvokesPerHour = serverSettings.max_self_invokes_per_hour;
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
    } catch (e) {
      console.error('Failed to load server settings:', e);
    } finally {
      loadingSettings = false;
    }
  }

  // Load settings on mount if configured
  $effect(() => {
    if (configStore.isConfigured && !serverSettings && !loadingSettings) {
      loadServerSettings();
    }
  });

  // Load model metadata when provider switches to openrouter
  $effect(() => {
    if (displayProvider === 'openrouter') {
      modelsStore.loadModels();
    }
  });

  function handleSaveConnection() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;
    testStatus = 'idle';
    testMessage = 'Connection settings saved!';
    setTimeout(() => {
      testMessage = '';
    }, 2000);
  }

  // Saved connections handlers
  function handleSaveCurrentConnection() {
    if (!saveConnectionName.trim()) return;
    connectionsStore.saveCurrentAs(saveConnectionName.trim());
    saveConnectionName = '';
    showSaveInput = false;
  }

  function handleEditConnection(conn: SavedConnection) {
    editingConnectionId = conn.id;
    editingName = conn.name;
    apiUrl = conn.apiUrl;
    apiKey = conn.apiKey;
  }

  function handleUpdateConnection() {
    if (!editingConnectionId) return;
    connectionsStore.update(editingConnectionId, {
      name: editingName.trim() || undefined,
      apiUrl,
      apiKey,
    });
    // Also apply to configStore if this is the active connection
    if (connectionsStore.activeConnectionId === editingConnectionId) {
      configStore.apiUrl = apiUrl;
      configStore.apiKey = apiKey;
    }
    editingConnectionId = null;
    editingName = '';
    testMessage = 'Connection updated!';
    setTimeout(() => { testMessage = ''; }, 2000);
  }

  function handleCancelEdit() {
    editingConnectionId = null;
    editingName = '';
    apiUrl = configStore.apiUrl;
    apiKey = configStore.apiKey;
  }

  function handleDeleteConnection(id: string) {
    connectionsStore.delete(id);
  }

  async function handleConnectTo(id: string) {
    await connectionsStore.switchTo(id);
    // Sync local fields to new connection values
    apiUrl = configStore.apiUrl;
    apiKey = configStore.apiKey;
    // Reload server settings after switch
    serverSettings = null;
    await loadServerSettings();
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
        testMessage = 'Invalid API key. Check that it matches a token issued by the backend.';
        return;
      }
      if (!authResponse.ok) {
        testStatus = 'error';
        testMessage = `Auth check failed: ${authResponse.status}`;
        return;
      }

      testStatus = 'success';
      testMessage = 'Connection successful!';
      // Load server settings after successful connection
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
      const { provider: actualProvider, clearBaseUrl } = fromDisplayProvider(displayProvider);
      const effectiveBaseUrl = clearBaseUrl ? '' : llmBaseUrl;

      const result = await api.updateServerSettings({
        llm_provider: actualProvider,
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
        llm_base_url: effectiveBaseUrl,
        context_management: contextManagement,
        sliding_window_cycles: slidingWindowCycles,
        max_self_invokes_per_hour: maxSelfInvokesPerHour,
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
      });

      testStatus = 'success';
      testMessage = result.restart_required
        ? 'Settings saved! Restart the server for changes to take effect.'
        : 'Settings saved and applied!';
      serverSettingsStore.refresh();
    } catch (e) {
      testStatus = 'error';
      testMessage = e instanceof Error ? e.message : 'Failed to save settings';
    } finally {
      savingSettings = false;
    }
  }

  function handleThemeChange(theme: ThemeName) {
    selectedTheme = theme;
    configStore.setTheme(theme);
  }
</script>

<div class="settings-panel">
  <!-- Tabs -->
  <div class="tabs">
    <button
      class="tab"
      class:active={activeTab === 'connection'}
      onclick={() => (activeTab = 'connection')}
    >
      Connection
    </button>
    <button
      class="tab"
      class:active={activeTab === 'appearance'}
      onclick={() => (activeTab = 'appearance')}
    >
      Appearance
    </button>
    <button
      class="tab"
      class:active={activeTab === 'llm'}
      onclick={() => (activeTab = 'llm')}
      disabled={!serverSettings}
    >
      LLM
    </button>
    <button
      class="tab"
      class:active={activeTab === 'agent'}
      onclick={() => (activeTab = 'agent')}
      disabled={!serverSettings}
    >
      Agent
    </button>
    <button
      class="tab"
      class:active={activeTab === 'tools'}
      onclick={() => (activeTab = 'tools')}
      disabled={!serverSettings}
    >
      Tools
    </button>
    <button
      class="tab"
      class:active={activeTab === 'skills'}
      onclick={() => (activeTab = 'skills')}
      disabled={!serverSettings}
    >
      Skills
    </button>
    <button
      class="tab"
      class:active={activeTab === 'voice'}
      onclick={() => (activeTab = 'voice')}
      disabled={!serverSettings}
    >
      Voice
    </button>
    {#if backendProcessStore.isTauri}
      <button
        class="tab"
        class:active={activeTab === 'proxy'}
        onclick={() => (activeTab = 'proxy')}
      >
        Proxy
      </button>
    {/if}
  </div>

  <!-- Connection Tab -->
  {#if activeTab === 'connection'}
    <div class="tab-content">
      <!-- Saved Connections -->
      <div class="saved-connections">
        <div class="section-header">
          <span class="section-title">Saved Connections</span>
        </div>

        {#if connectionsStore.connections.length === 0}
          <p class="hint" style="margin-bottom: var(--spacing-md);">
            No saved connections. Save your current connection to quickly switch between backends.
          </p>
        {:else}
          <div class="connections-list">
            {#each connectionsStore.connections as conn}
              <div class="connection-row" class:active={connectionsStore.activeConnectionId === conn.id}>
                <span class="conn-status-dot" class:connected={connectionsStore.activeConnectionId === conn.id}></span>
                {#if editingConnectionId === conn.id}
                  <input
                    class="conn-name-input"
                    type="text"
                    bind:value={editingName}
                    placeholder="Connection name"
                  />
                {:else}
                  <div class="conn-info">
                    <span class="conn-name">{conn.name}</span>
                    <span class="conn-url">{conn.apiUrl}</span>
                  </div>
                {/if}
                <div class="conn-actions">
                  {#if editingConnectionId === conn.id}
                    <button class="conn-action-btn" onclick={handleUpdateConnection} title="Save">
                      <Icon name="check" size={14} />
                    </button>
                    <button class="conn-action-btn" onclick={handleCancelEdit} title="Cancel">
                      <Icon name="x" size={14} />
                    </button>
                  {:else}
                    {#if connectionsStore.activeConnectionId !== conn.id}
                      <Button
                        variant="secondary"
                        size="sm"
                        onclick={() => handleConnectTo(conn.id)}
                        disabled={connectionsStore.switching}
                      >
                        {connectionsStore.switching ? '...' : 'Connect'}
                      </Button>
                    {/if}
                    <button class="conn-action-btn" onclick={() => handleEditConnection(conn)} title="Edit">
                      <Icon name="edit" size={14} />
                    </button>
                    <button class="conn-action-btn danger" onclick={() => handleDeleteConnection(conn.id)} title="Delete">
                      <Icon name="trash" size={14} />
                    </button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}

        {#if showSaveInput}
          <div class="save-input-row">
            <input
              class="save-name-input"
              type="text"
              bind:value={saveConnectionName}
              placeholder="Connection name (e.g. Work, Personal)"
              onkeydown={(e) => e.key === 'Enter' && handleSaveCurrentConnection()}
            />
            <Button variant="primary" size="sm" onclick={handleSaveCurrentConnection} disabled={!saveConnectionName.trim()}>
              Save
            </Button>
            <Button variant="secondary" size="sm" onclick={() => { showSaveInput = false; saveConnectionName = ''; }}>
              Cancel
            </Button>
          </div>
        {:else}
          <Button variant="secondary" size="sm" onclick={() => (showSaveInput = true)}>
            <Icon name="plus" size={14} />
            Save Current Connection
          </Button>
        {/if}
      </div>

      <div class="section-divider"></div>

      {#if backendProcessStore.isTauri}
        <div class="auto-config-notice">
          <span class="status-dot connected"></span>
          <span>Backend is auto-managed. Connection is configured automatically.</span>
        </div>
        <button class="advanced-toggle" onclick={() => (showConnectionAdvanced = !showConnectionAdvanced)}>
          {showConnectionAdvanced ? 'Hide' : 'Show'} Advanced Connection Settings
        </button>
      {/if}

      {#if showConnectionAdvanced}
        {#if connectionsStore.activeConnection}
          <div class="active-connection-notice">
            <Icon name="server" size={14} />
            <span>Connected to: <strong>{connectionsStore.activeConnection.name ?? 'Unknown'}</strong></span>
          </div>
        {/if}

        <div class="field">
          <label for="api-url">API URL</label>
          <input
            id="api-url"
            type="text"
            bind:value={apiUrl}
            placeholder="http://localhost:8000"
          />
          <p class="hint">The URL of your Nymeria API server</p>
        </div>

        <div class="field">
          <label for="api-key">API Key</label>
          <input
            id="api-key"
            type="password"
            bind:value={apiKey}
            placeholder="Enter your API key"
          />
          <p class="hint">Per-user account token (<code>nym_...</code>) minted via <code>python run.py users add</code>, or the bootstrap admin token from <code>BOOTSTRAP_TOKEN.txt</code></p>
        </div>

        <div class="actions">
          <Button variant="secondary" onclick={handleTestConnection} disabled={testStatus === 'testing'}>
            {testStatus === 'testing' ? 'Testing...' : 'Test Connection'}
          </Button>
          {#if editingConnectionId}
            <Button variant="primary" onclick={handleUpdateConnection}>
              Update Connection
            </Button>
            <Button variant="secondary" onclick={handleCancelEdit}>
              Cancel Edit
            </Button>
          {:else}
            <Button variant="primary" onclick={handleSaveConnection}>
              Save
            </Button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Proxy Tab (CLIProxy management, Tauri only) -->
  {#if activeTab === 'proxy'}
    <CLIProxyPanel />
  {/if}

  <!-- Appearance Tab -->
  {#if activeTab === 'appearance'}
    <div class="tab-content">
      <div class="field">
        <span class="field-label">Theme</span>
        <p class="hint">Choose a color scheme for the interface</p>
        <div class="theme-grid">
          {#each themeList as theme}
            {@const colors = getThemePreviewColors(theme.id)}
            <button
              class="theme-card"
              class:selected={selectedTheme === theme.id}
              onclick={() => handleThemeChange(theme.id)}
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
      </div>
    </div>
  {/if}

  <!-- LLM Tab -->
  {#if activeTab === 'llm'}
    <div class="tab-content">
      {#if loadingSettings}
        <p class="loading">Loading settings...</p>
      {:else}
        <div class="field">
          <label for="llm-provider">Provider</label>
          <select id="llm-provider" bind:value={displayProvider}>
            <option value="anthropic_proxy">Anthropic (Subscription)</option>
            <option value="anthropic_direct">Anthropic (Direct API)</option>
            <option value="openai">OpenAI</option>
            <option value="openrouter">OpenRouter</option>
            <option value="local_openai">Local LLM (OpenAI-compatible)</option>
          </select>
          <p class="hint">
            {#if displayProvider === 'anthropic_proxy'}
              Routes through CLIProxy using your Claude subscription
            {:else if displayProvider === 'anthropic_direct'}
              Direct Anthropic API — pay-per-token (requires ANTHROPIC_API_KEY)
            {:else if displayProvider === 'local_openai'}
              Local OpenAI-compatible server (e.g. llama.cpp llama-server, LM Studio). Edit the API Base URL under Advanced Settings.
            {:else}
              LLM provider (requires API key in server .env)
            {/if}
          </p>
        </div>

        <div class="field">
          <label for="llm-model">Model</label>
          {#if displayProvider === 'local_openai'}
            <!-- Local LLM model names are whatever the user --alias'd llama-server with;
                 no dropdown — use the free-text input below. -->
          {:else if availableModels.length > 0 && (llmProvider === 'anthropic' || llmProvider === 'openai')}
            <select id="llm-model" bind:value={llmModel}>
              {#each availableModels as model}
                <option value={model.id}>{model.name || model.id}</option>
              {/each}
            </select>
            <p class="hint">{availableModels.length} models available</p>
          {:else if loadingAvailableModels}
            <select id="llm-model" disabled>
              <option>Loading models...</option>
            </select>
            <p class="hint">Fetching available models...</p>
          {:else}
            <select id="llm-model" bind:value={llmModel}>
              {#each modelOptions[llmProvider] as model}
                <option value={model.value}>{model.label}</option>
              {/each}
            </select>
          {/if}
          <div class="model-custom-row">
            <input
              type="text"
              bind:value={llmModel}
              placeholder={
                displayProvider === 'local_openai' ? 'Local model alias (e.g. local-llm)' :
                llmProvider === 'openrouter' ? 'e.g. meta-llama/llama-4-scout' :
                'Custom model ID'
              }
              class="model-custom"
            />
            <button
              type="button"
              class="info-btn"
              title="How to use a custom model"
              onclick={() => showModelHelp = !showModelHelp}
            >
              <Icon name="info" size={14} />
            </button>
          </div>
          <p class="hint">
            {#if displayProvider === 'local_openai'}
              Enter the model alias your local server reports (the <code>--alias</code> flag value, or the model file basename)
            {:else if llmProvider === 'openrouter'}
              Use the dropdown or paste a model ID from <a href="https://openrouter.ai/models" target="_blank" rel="noopener">openrouter.ai/models</a>
            {:else if llmProvider === 'openai'}
              Use the dropdown or enter an OpenAI model name
            {:else}
              Use the dropdown or enter an Anthropic model name
            {/if}
          </p>
          {#if showModelHelp}
            <div class="model-help-box">
              <strong>Using a custom model</strong>
              {#if llmProvider === 'openrouter'}
                <ol>
                  <li>Go to <a href="https://openrouter.ai/models" target="_blank" rel="noopener">openrouter.ai/models</a></li>
                  <li>Find the model you want and open its page</li>
                  <li>Copy the model ID (e.g. <code>meta-llama/llama-4-scout</code>)</li>
                  <li>Paste it into the text field above</li>
                </ol>
                <p>The model ID is shown at the top of every model page on OpenRouter, in the format <code>provider/model-name</code>.</p>
              {:else}
                <ol>
                  <li>Find the model name in your provider's documentation</li>
                  <li>Enter the exact model identifier in the text field above</li>
                </ol>
              {/if}
            </div>
          {/if}
          {#if currentModelMeta}
            <div class="model-meta-hint">
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

        <div class="field">
          <label class="toggle-label" for="llm-use-model-defaults">
            <input type="checkbox" id="llm-use-model-defaults" bind:checked={llmUseModelDefaults} />
            Use model defaults
          </label>
          <p class="hint">
            Let the provider apply model-specific optimal defaults for temperature, top_p, and frequency penalty
            {#if llmUseModelDefaults && currentModelMeta}
              {#if currentModelMeta.default_temperature != null}
                &mdash; temp: {currentModelMeta.default_temperature}
              {/if}
              {#if currentModelMeta.default_top_p != null}
                , top_p: {currentModelMeta.default_top_p}
              {/if}
            {/if}
          </p>
        </div>

        <div class="field" class:field-disabled={llmUseModelDefaults}>
          <label for="llm-temperature">Temperature: {llmTemperature}</label>
          <input
            id="llm-temperature"
            type="range"
            min="0"
            max="2"
            step="0.1"
            bind:value={llmTemperature}
            disabled={llmUseModelDefaults}
          />
          <p class="hint">0 = deterministic, 2 = creative</p>
        </div>

        <div class="field">
          <label class="toggle-label" for="llm-extended-thinking">
            <input type="checkbox" id="llm-extended-thinking" bind:checked={llmExtendedThinking} />
            Enable Reasoning
          </label>
          <p class="hint">Enable/disable thinking tokens for compatible models</p>
        </div>

        <!-- Advanced Settings Collapsible -->
        <div class="advanced-section">
          <button
            class="advanced-toggle"
            onclick={() => (showAdvancedLlm = !showAdvancedLlm)}
          >
            <span class="toggle-icon">{showAdvancedLlm ? '▼' : '▶'}</span>
            Advanced Settings
          </button>

          {#if showAdvancedLlm}
            <div class="advanced-content">
              <div class="field">
                <label for="llm-max-tokens">Max Tokens (optional)</label>
                <input
                  id="llm-max-tokens"
                  type="number"
                  min="1"
                  max="32000"
                  placeholder="Default (model limit)"
                  bind:value={llmMaxTokens}
                />
                <p class="hint">Maximum output tokens (1-32000)</p>
              </div>

              <div class="field" class:field-disabled={llmUseModelDefaults}>
                <label for="llm-top-p">Top P: {llmTopP !== null ? llmTopP.toFixed(2) : 'Default'}</label>
                <div class="slider-with-clear">
                  <input
                    id="llm-top-p"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={llmTopP ?? 1}
                    oninput={(e) => (llmTopP = parseFloat(e.currentTarget.value))}
                    disabled={llmUseModelDefaults}
                  />
                  <button class="clear-btn" onclick={() => (llmTopP = null)} title="Reset to default" disabled={llmUseModelDefaults}>×</button>
                </div>
                <p class="hint">
                  Nucleus sampling threshold (0-1)
                  {#if llmUseModelDefaults && currentModelMeta?.default_top_p != null}
                    &mdash; Model default: {currentModelMeta.default_top_p}
                  {/if}
                </p>
              </div>

              <div class="field">
                <label for="llm-top-k">Top K (optional)</label>
                <input
                  id="llm-top-k"
                  type="number"
                  min="1"
                  max="100"
                  placeholder="Default"
                  bind:value={llmTopK}
                />
                <p class="hint">Top-k sampling (1-100)</p>
              </div>

              <div class="field" class:field-disabled={llmUseModelDefaults}>
                <label for="llm-freq-penalty">Frequency Penalty: {llmFrequencyPenalty !== null ? llmFrequencyPenalty.toFixed(1) : 'Default'}</label>
                <div class="slider-with-clear">
                  <input
                    id="llm-freq-penalty"
                    type="range"
                    min="-2"
                    max="2"
                    step="0.1"
                    value={llmFrequencyPenalty ?? 0}
                    oninput={(e) => (llmFrequencyPenalty = parseFloat(e.currentTarget.value))}
                    disabled={llmUseModelDefaults}
                  />
                  <button class="clear-btn" onclick={() => (llmFrequencyPenalty = null)} title="Reset to default" disabled={llmUseModelDefaults}>×</button>
                </div>
                <p class="hint">
                  Reduce repetition of token sequences (-2 to 2)
                  {#if llmUseModelDefaults && currentModelMeta?.default_frequency_penalty != null}
                    &mdash; Model default: {currentModelMeta.default_frequency_penalty}
                  {/if}
                </p>
              </div>

              <div class="field" class:field-disabled={llmUseModelDefaults}>
                <label for="llm-pres-penalty">Presence Penalty: {llmPresencePenalty !== null ? llmPresencePenalty.toFixed(1) : 'Default'}</label>
                <div class="slider-with-clear">
                  <input
                    id="llm-pres-penalty"
                    type="range"
                    min="-2"
                    max="2"
                    step="0.1"
                    value={llmPresencePenalty ?? 0}
                    oninput={(e) => (llmPresencePenalty = parseFloat(e.currentTarget.value))}
                    disabled={llmUseModelDefaults}
                  />
                  <button class="clear-btn" onclick={() => (llmPresencePenalty = null)} title="Reset to default" disabled={llmUseModelDefaults}>×</button>
                </div>
                <p class="hint">Encourage new topics (-2 to 2)</p>
              </div>

              <div class="field">
                <label for="llm-reasoning">Reasoning Effort</label>
                <select id="llm-reasoning" bind:value={llmReasoningEffort}>
                  <option value={null}>Default</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
                <p class="hint">For reasoning models (o1, Claude with thinking)</p>
              </div>

              <div class="field">
                <label for="llm-base-url">API Base URL (optional)</label>
                <input
                  id="llm-base-url"
                  type="text"
                  placeholder="Default (provider's standard URL)"
                  bind:value={llmBaseUrl}
                />
                <p class="hint">
                  Override the API endpoint (e.g., <code>http://localhost:8317/v1</code> for a local proxy).
                  Leave empty to use the provider's default URL.
                </p>
              </div>

            </div>
          {/if}
        </div>

        <div class="actions">
          <Button variant="primary" onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save LLM Settings'}
          </Button>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Agent Tab -->
  {#if activeTab === 'agent'}
    <div class="tab-content">
      {#if loadingSettings}
        <p class="loading">Loading settings...</p>
      {:else}
        <div class="field">
          <label for="context-management">Context Management</label>
          <select id="context-management" bind:value={contextManagement}>
            <option value="auto_compact">Auto-Compact (default)</option>
            <option value="sliding_window">Sliding Window</option>
            <option value="none">None</option>
          </select>
          <p class="hint">
            {#if contextManagement === 'auto_compact'}
              Automatically summarizes old messages when context gets large
            {:else if contextManagement === 'sliding_window'}
              Keeps only the N most recent conversation cycles
            {:else}
              No context management — conversation history grows unbounded
            {/if}
          </p>
        </div>

        {#if contextManagement === 'sliding_window'}
          <div class="field">
            <label for="context-cycles">Context Window Cycles: {slidingWindowCycles}</label>
            <input
              id="context-cycles"
              type="range"
              min="1"
              max="20"
              step="1"
              bind:value={slidingWindowCycles}
            />
            <p class="hint">Number of conversation cycles to keep in context</p>
          </div>
        {/if}

        <div class="field">
          <label for="max-invokes">Max Self-Invokes/Hour: {maxSelfInvokesPerHour}</label>
          <input
            id="max-invokes"
            type="range"
            min="1"
            max="100"
            step="1"
            bind:value={maxSelfInvokesPerHour}
          />
          <p class="hint">Rate limit for autonomous operations</p>
        </div>

        <div class="field">
          <label for="log-level">Log Level</label>
          <select id="log-level" bind:value={logLevel}>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
        </div>

        <div class="field checkbox-field">
          <input
            id="watchdog-enabled"
            type="checkbox"
            bind:checked={watchdogEnabled}
          />
          <label for="watchdog-enabled">Enable Watchdog</label>
          <p class="hint">Monitor TODO staleness</p>
        </div>

        {#if watchdogEnabled}
          <div class="field">
            <label for="watchdog-interval">Watchdog Interval: {watchdogIntervalMinutes} min</label>
            <input
              id="watchdog-interval"
              type="range"
              min="5"
              max="60"
              step="5"
              bind:value={watchdogIntervalMinutes}
            />
          </div>

          <div class="field">
            <label for="staleness-minutes">Staleness Threshold: {todoStalenessMinutes} minutes</label>
            <input
              id="staleness-minutes"
              type="range"
              min="5"
              max="240"
              step="5"
              bind:value={todoStalenessMinutes}
            />
          </div>
        {/if}

        <div class="actions">
          <Button variant="primary" onclick={handleSaveServerSettings} disabled={savingSettings}>
            {savingSettings ? 'Saving...' : 'Save Agent Settings'}
          </Button>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Tools Tab -->
  {#if activeTab === 'tools'}
    <div class="tab-content tab-tools-flex">
      <ToolManagementPanel />
    </div>
  {/if}

  <!-- Skills Tab -->
  {#if activeTab === 'skills'}
    <div class="tab-content tab-content-full">
      <SkillsPanel />
    </div>
  {/if}

  <!-- Voice Tab -->
  {#if activeTab === 'voice'}
    <div class="tab-content">
      {#if loadingSettings}
        <p class="loading">Loading settings...</p>
      {:else}
        <h3 class="section-heading">Text-to-Speech (TTS)</h3>

        <div class="field">
          <label for="tts-provider">TTS Provider</label>
          <select id="tts-provider" bind:value={ttsProvider}>
            <option value="none">None (disabled)</option>
            <option value="cartesia">Cartesia Sonic</option>
            <option value="gemini">Gemini TTS</option>
            <option value="openai">OpenAI</option>
            <option value="qwen3">Qwen3-TTS (Local)</option>
          </select>
          <p class="hint">
            {#if ttsProvider === 'cartesia'}
              Cartesia Sonic-3 — ultra-low latency (~90ms), speed/emotion controls
            {:else if ttsProvider === 'gemini'}
              Google Gemini 3.1 Flash TTS — 30 voices, 70+ languages, audio tags supported
            {:else if ttsProvider === 'openai'}
              Uses OpenAI TTS API (tts-1, tts-1-hd)
            {:else if ttsProvider === 'qwen3'}
              Local Qwen3-TTS via OpenAI-compatible server
            {:else}
              TTS disabled — voice endpoints will not return audio
            {/if}
          </p>
        </div>

        {#if ttsProvider !== 'none'}
          {#if ttsProvider !== 'gemini' && ttsProvider !== 'cartesia'}
            <div class="field">
              <label for="tts-base-url">Base URL</label>
              <input
                id="tts-base-url"
                type="text"
                bind:value={ttsBaseUrl}
                placeholder={ttsProvider === 'openai' ? 'https://api.openai.com/v1' : 'http://localhost:8880/v1'}
              />
              <p class="hint">Leave empty for default ({ttsProvider === 'openai' ? 'api.openai.com' : 'localhost:8880'})</p>
            </div>
          {/if}

          <div class="field">
            <label for="tts-model">Model</label>
            <input
              id="tts-model"
              type="text"
              bind:value={ttsModel}
              placeholder={ttsProvider === 'cartesia' ? 'sonic-3' : ttsProvider === 'gemini' ? 'gemini-3.1-flash-tts-preview' : ttsProvider === 'openai' ? 'tts-1-hd' : 'Qwen3-TTS-0.6B'}
            />
          </div>

          <div class="field">
            <label for="tts-voice">Voice</label>
            <input
              id="tts-voice"
              type="text"
              bind:value={ttsVoice}
              placeholder={ttsProvider === 'cartesia' ? 'Voice ID from play.cartesia.ai' : ttsProvider === 'gemini' ? 'Kore' : ttsProvider === 'openai' ? 'nova' : 'default'}
            />
            <p class="hint">
              {#if ttsProvider === 'cartesia'}
                Voice UUID from <a href="https://play.cartesia.ai/voices" target="_blank">play.cartesia.ai/voices</a>
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
              <div class="field">
                <label for="tts-format">Output Format</label>
                <select id="tts-format" bind:value={ttsOutputFormat}>
                  <option value="mp3">MP3</option>
                  <option value="wav">WAV</option>
                  <option value="opus">Opus</option>
                  <option value="aac">AAC</option>
                </select>
              </div>
            {/if}

            <div class="field">
              <label for="tts-speed">Speed: {ttsSpeed.toFixed(2)}x</label>
              <input
                id="tts-speed"
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

        <div class="field">
          <label for="stt-provider">STT Provider</label>
          <select id="stt-provider" bind:value={sttProvider}>
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
              STT disabled — voice endpoints will not accept audio
            {/if}
          </p>
        </div>

        {#if sttProvider !== 'none'}
          <div class="field">
            <label for="stt-base-url">Base URL</label>
            <input
              id="stt-base-url"
              type="text"
              bind:value={sttBaseUrl}
              placeholder={sttProvider === 'openai' ? 'https://api.openai.com/v1' : 'http://localhost:8003/v1'}
            />
            <p class="hint">Leave empty for default ({sttProvider === 'openai' ? 'api.openai.com' : 'localhost:8003'})</p>
          </div>

          <div class="field">
            <label for="stt-model">Model</label>
            <input
              id="stt-model"
              type="text"
              bind:value={sttModel}
              placeholder={sttProvider === 'openai' ? 'gpt-4o-mini-transcribe' : 'large-v3-turbo'}
            />
          </div>

          <div class="field">
            <label for="stt-language">Language Hint</label>
            <input
              id="stt-language"
              type="text"
              bind:value={sttLanguage}
              placeholder="en"
              style="max-width: 120px;"
            />
            <p class="hint">Optional ISO 639-1 code (e.g., en, es, de). Improves accuracy.</p>
          </div>
        {/if}

        <h3 class="section-heading">Watch / Default Thread</h3>

        <div class="field">
          <label for="voice-thread">Voice Thread</label>
          <select id="voice-thread" bind:value={voiceDefaultThreadId}>
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
    </div>
  {/if}

  <!-- Status message -->
  {#if testMessage}
    <div class="message" class:success={testStatus === 'success'} class:error={testStatus === 'error'}>
      {#if testStatus === 'success'}
        <Icon name="success" size={16} />
      {:else if testStatus === 'error'}
        <Icon name="error" size={16} />
      {/if}
      {testMessage}
    </div>
  {/if}
</div>

<style>
  .settings-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-width: 400px;
  }

  .tabs {
    display: flex;
    gap: var(--spacing-xs);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-sm);
  }

  .tab {
    padding: var(--spacing-sm) var(--spacing-md);
    background: none;
    border: none;
    border-radius: var(--radius-md) var(--radius-md) 0 0;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: var(--font-size-sm);
    font-weight: 500;
    transition: all 0.15s ease;
  }

  .tab:hover:not(:disabled) {
    color: var(--text-primary);
    background: var(--bg-elevated-2);
  }

  .tab.active {
    color: var(--accent-primary);
    border-bottom: 2px solid var(--accent-primary);
  }

  .tab:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .tab-content {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .tab-content-full {
    min-height: 400px;
    max-height: 60vh;
    overflow-y: auto;
  }

  /* Tools tab — pins its own footer + lets scrollbar sit flush with the Modal's right border */
  .tab-tools-flex {
    min-height: 0;
    max-height: 60vh;
    height: 60vh;
    /* Keep a stable width so collapsing the Core/Available cards doesn't
       cascade into the parent Modal auto-sizing down (Modal only has a
       400px floor, so narrow content otherwise shrinks the whole dialog). */
    min-width: 640px;
    display: flex;
    flex-direction: column;
    /* Pull flush-right by defeating the parent Modal's right padding */
    margin-right: calc(-1 * var(--spacing-lg));
  }

  .field {
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

  .checkbox-field {
    flex-direction: row;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .checkbox-field label {
    margin: 0;
  }

  .checkbox-field .hint {
    margin-left: auto;
  }

  label,
  .field-label {
    font-weight: 500;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  input[type='text'],
  input[type='password'],
  select {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
  }

  input[type='text']:focus,
  input[type='password']:focus,
  select:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
  }

  input[type='range'] {
    width: 100%;
    accent-color: var(--accent-primary);
  }

  input[type='checkbox'] {
    width: 18px;
    height: 18px;
    accent-color: var(--accent-primary);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    cursor: pointer;
  }

  select {
    cursor: pointer;
  }

  .model-custom-row {
    display: flex;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    align-items: center;
  }

  .model-custom {
    flex: 1;
    font-size: var(--font-size-sm);
  }

  .info-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    flex-shrink: 0;
    background: transparent;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-full);
    color: var(--text-muted);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .info-btn:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .model-help-box {
    margin-top: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.6;
  }

  .model-help-box strong {
    display: block;
    margin-bottom: var(--spacing-xs);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .model-help-box ol {
    margin: 0;
    padding-left: var(--spacing-lg);
  }

  .model-help-box li {
    margin-bottom: 2px;
  }

  .model-help-box p {
    margin: var(--spacing-xs) 0 0;
  }

  .model-help-box code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    background: var(--bg-elevated);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
  }

  .model-help-box a,
  .hint a {
    color: var(--accent-secondary);
    text-decoration: none;
  }

  .model-help-box a:hover,
  .hint a:hover {
    text-decoration: underline;
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .model-meta-hint {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
    padding: 6px 10px;
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

  .field-disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .loading {
    color: var(--text-secondary);
    font-style: italic;
  }

  .message {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .message.success {
    background: rgba(52, 211, 153, 0.15);
    color: var(--success);
  }

  .message.error {
    background: rgba(248, 113, 113, 0.15);
    color: var(--error);
  }

  .actions {
    display: flex;
    gap: var(--spacing-sm);
    justify-content: flex-end;
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  /* Theme selector styles */
  .theme-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: var(--spacing-sm);
    margin-top: var(--spacing-sm);
  }

  .theme-card {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm);
    background: var(--bg-elevated-2);
    border: 2px solid var(--border-subtle);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all 0.15s ease;
    text-align: left;
  }

  .theme-card:hover {
    border-color: var(--border-default);
    background: var(--bg-hover);
  }

  .theme-card.selected {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
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

  /* Advanced settings section */
  .advanced-section {
    margin-top: var(--spacing-sm);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .advanced-toggle {
    width: 100%;
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: none;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .advanced-toggle:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .toggle-icon {
    font-size: 10px;
    color: var(--text-muted);
  }

  .advanced-content {
    padding: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .slider-with-clear {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .slider-with-clear input[type='range'] {
    flex: 1;
  }

  .clear-btn {
    width: 24px;
    height: 24px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font-size: 14px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .clear-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--border-default);
  }

  input[type='number'] {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
  }

  input[type='number']:focus {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15);
  }

  input[type='number']::placeholder {
    color: var(--text-muted);
  }

  .auto-config-notice {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    background: var(--bg-elevated-2);
    border-radius: 6px;
    border: 1px solid var(--border-default);
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }

  .status-dot.connected {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--success, #22c55e);
    box-shadow: 0 0 6px var(--success, #22c55e);
    flex-shrink: 0;
  }

  .advanced-toggle {
    background: none;
    border: none;
    color: var(--accent-primary);
    font-size: 0.8125rem;
    cursor: pointer;
    padding: 0;
    margin-bottom: 1rem;
  }

  .advanced-toggle:hover {
    text-decoration: underline;
  }

  /* Saved Connections */
  .saved-connections {
    margin-bottom: var(--spacing-sm);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--spacing-sm);
  }

  .section-title {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .section-divider {
    height: 1px;
    background: var(--glass-border);
    margin: var(--spacing-md) 0;
  }

  .connections-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-sm);
  }

  .connection-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    background: var(--bg-tertiary, rgba(255, 255, 255, 0.03));
    border: 1px solid transparent;
    transition: all var(--transition-fast);
  }

  .connection-row.active {
    border-color: var(--accent-primary);
    background: rgba(34, 211, 238, 0.05);
  }

  .connection-row:hover {
    background: var(--bg-hover);
  }

  .conn-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
    flex-shrink: 0;
  }

  .conn-status-dot.connected {
    background: var(--success, #22c55e);
    box-shadow: 0 0 6px rgba(34, 197, 94, 0.4);
  }

  .conn-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .conn-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conn-url {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conn-name-input {
    flex: 1;
    min-width: 0;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    border: 1px solid var(--glass-border);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .conn-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-shrink: 0;
  }

  .conn-action-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    transition: all var(--transition-fast);
  }

  .conn-action-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .conn-action-btn.danger:hover {
    color: var(--error, #ef4444);
  }

  .save-input-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-xs);
  }

  .save-name-input {
    flex: 1;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    border: 1px solid var(--glass-border);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
  }

  .active-connection-notice {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    margin-bottom: var(--spacing-md);
    border-radius: var(--radius-md);
    background: rgba(34, 211, 238, 0.08);
    border: 1px solid rgba(34, 211, 238, 0.2);
    color: var(--accent-primary);
    font-size: var(--font-size-sm);
  }
</style>
