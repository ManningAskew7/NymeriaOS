<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import { healthStore } from '$lib/stores/health.svelte';
  import { threadsStore } from '$lib/stores/threads.svelte';
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { modelOptions } from '$lib/utils/modelOptions';
  import { getThemeList, getThemePreviewColors, type ThemeName } from '$lib/themes';
  import type { ServerSettings, LLMProvider, OpenAIApiMode, LogLevel } from '$lib/types';
  import Icon from './Icon.svelte';
  import Button from './Button.svelte';
  import { MCPManagementPanel, ToolManagementPanel } from '../tools';
  import { AccountTab, UsersTab } from '../account';

  interface Props {
    open: boolean;
    onClose: () => void;
    initialTab?: string;
  }

  let { open, onClose, initialTab }: Props = $props();

  type Tab = 'connection' | 'appearance' | 'llm' | 'agent' | 'tools' | 'mcp' | 'voice' | 'account' | 'users';
  type TabConfig = { id: Tab; label: string; disabled: boolean };
  const adminServerTabs: Tab[] = ['llm', 'agent', 'voice', 'users'];
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
      { id: 'mcp', label: 'MCP', disabled: !serverSettings }
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
  let openaiApiMode = $state<OpenAIApiMode>('responses');
  let showAdvancedLlm = $state(false);

  // Agent settings
  let contextManagement = $state<string>('auto_compact');
  let compactThreshold = $state(0.8);
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

  // Theme
  let selectedTheme = $state<ThemeName>(configStore.theme);
  const themeList = getThemeList();

  // Model metadata (reactive)
  const currentModelMeta = $derived(
    llmProvider === 'openrouter' ? modelsStore.getById(llmModel) : undefined
  );

  // Load server settings
  async function loadServerSettings() {
    if (!configStore.isConfigured) return;
    loadingSettings = true;
    try {
      serverSettings = await api.getServerSettings();
      llmProvider = serverSettings.llm_provider;
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
      openaiApiMode = serverSettings.openai_api_mode ?? 'responses';
      contextManagement = serverSettings.context_management;
      compactThreshold = serverSettings.compact_threshold ?? 0.8;
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
        openai_api_mode: openaiApiMode,
        context_management: contextManagement,
        compact_threshold: compactThreshold,
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

  function handleThemeSelect(theme: ThemeName) {
    selectedTheme = theme;
    configStore.setTheme(theme);
  }
</script>

{#if open}
  <div class="settings-modal">
    <div class="settings-header">
      <button class="back-btn" onclick={onClose}>
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

      <!-- Provider Tab -->
      {:else if activeTab === 'llm' && isAdmin}
        {#if loadingSettings}
          <div class="loading-state">Loading settings...</div>
        {:else}
          <div class="setting-group">
            <label class="setting-label">Provider</label>
            <select class="setting-input" bind:value={llmProvider}>
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            <p class="hint">Requires API key in server .env</p>
          </div>

          <div class="setting-group">
            <label class="setting-label">Model</label>
            <select class="setting-input" bind:value={llmModel}>
              {#each modelOptions[llmProvider] as opt}
                <option value={opt.value}>{opt.label}</option>
              {/each}
            </select>
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
                Select above or type a custom model name
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

          {#if llmProvider === 'openai' || llmProvider === 'openrouter'}
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
            <p class="hint">Monitor TODO staleness and nudge agent</p>
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

      <!-- Tools Tab -->
      {:else if activeTab === 'tools'}
        <ToolManagementPanel open={true} onClose={onClose} />

      {:else if activeTab === 'mcp'}
        <MCPManagementPanel open={true} onClose={onClose} />

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
    font-family: var(--font-mono, ui-monospace, 'SF Mono', monospace);
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
    background: rgba(52, 211, 153, 0.15);
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
  }

  input[type="number"] {
    -moz-appearance: textfield;
  }
</style>
