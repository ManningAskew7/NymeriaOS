<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { api } from '$lib/services/api.svelte';
  import type { ServerSettings, LLMProvider, LogLevel, ThemeName } from '$lib/types';
  import { getThemeList, getThemePreviewColors } from '$lib/themes';
  import { modelOptions } from '$lib/utils/modelOptions';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import { ToolManagementPanel } from '../tools';

  // Connection settings
  let apiUrl = $state(configStore.apiUrl);
  let apiKey = $state(configStore.apiKey);

  // Server settings
  let serverSettings = $state<ServerSettings | null>(null);
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
  let showAdvancedLlm = $state(false);
  // Agent settings
  let contextManagement = $state<string>('auto_compact');
  let slidingWindowCycles = $state(5);
  let maxSelfInvokesPerHour = $state(50);
  let logLevel = $state<LogLevel>('INFO');
  let watchdogEnabled = $state(true);
  let watchdogIntervalMinutes = $state(5);
  let todoStalenessMinutes = $state(20);

  // Theme settings
  let selectedTheme = $state<ThemeName>(configStore.theme);
  const themeList = getThemeList();

  // UI state
  let activeTab = $state<'connection' | 'appearance' | 'llm' | 'agent' | 'tools'>('connection');
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
      llmModel = serverSettings.llm_model;
      llmTemperature = serverSettings.llm_temperature;
      llmMaxTokens = serverSettings.llm_max_tokens;
      llmTopP = serverSettings.llm_top_p;
      llmTopK = serverSettings.llm_top_k;
      llmFrequencyPenalty = serverSettings.llm_frequency_penalty;
      llmPresencePenalty = serverSettings.llm_presence_penalty;
      llmReasoningEffort = serverSettings.llm_reasoning_effort;
      llmExtendedThinking = serverSettings.llm_extended_thinking;
      contextManagement = serverSettings.context_management;
      slidingWindowCycles = serverSettings.sliding_window_cycles;
      maxSelfInvokesPerHour = serverSettings.max_self_invokes_per_hour;
      logLevel = serverSettings.log_level;
      watchdogEnabled = serverSettings.watchdog_enabled;
      watchdogIntervalMinutes = serverSettings.watchdog_interval_minutes;
      todoStalenessMinutes = serverSettings.todo_staleness_minutes;
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

  function handleSaveConnection() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;
    testStatus = 'idle';
    testMessage = 'Connection settings saved!';
    setTimeout(() => {
      testMessage = '';
    }, 2000);
  }

  async function handleTestConnection() {
    configStore.apiUrl = apiUrl;
    configStore.apiKey = apiKey;

    testStatus = 'testing';
    testMessage = '';

    try {
      const isHealthy = await api.healthCheck();
      if (isHealthy) {
        testStatus = 'success';
        testMessage = 'Connection successful!';
        // Load server settings after successful connection
        await loadServerSettings();
      } else {
        testStatus = 'error';
        testMessage = 'API returned unhealthy status';
      }
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
        context_management: contextManagement,
        sliding_window_cycles: slidingWindowCycles,
        max_self_invokes_per_hour: maxSelfInvokesPerHour,
        log_level: logLevel,
        watchdog_enabled: watchdogEnabled,
        watchdog_interval_minutes: watchdogIntervalMinutes,
        todo_staleness_minutes: todoStalenessMinutes
      });

      testStatus = 'success';
      testMessage = result.restart_required
        ? 'Settings saved! Restart the server for changes to take effect.'
        : 'Settings saved and applied!';
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
  </div>

  <!-- Connection Tab -->
  {#if activeTab === 'connection'}
    <div class="tab-content">
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
        <p class="hint">Found in your Nymeria .env file as NYMERIA_API_KEY</p>
      </div>

      <div class="actions">
        <Button variant="secondary" onclick={handleTestConnection} disabled={testStatus === 'testing'}>
          {testStatus === 'testing' ? 'Testing...' : 'Test Connection'}
        </Button>
        <Button variant="primary" onclick={handleSaveConnection}>
          Save
        </Button>
      </div>
    </div>
  {/if}

  <!-- Appearance Tab -->
  {#if activeTab === 'appearance'}
    <div class="tab-content">
      <div class="field">
        <label>Theme</label>
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
          <select id="llm-provider" bind:value={llmProvider}>
            <option value="anthropic">Anthropic</option>
            <option value="openai">OpenAI</option>
            <option value="openrouter">OpenRouter</option>
          </select>
          <p class="hint">LLM provider (requires API key in server .env)</p>
        </div>

        <div class="field">
          <label for="llm-model">Model</label>
          <select id="llm-model" bind:value={llmModel}>
            {#each modelOptions[llmProvider] as model}
              <option value={model.value}>{model.label}</option>
            {/each}
          </select>
          <div class="model-custom-row">
            <input
              type="text"
              bind:value={llmModel}
              placeholder={llmProvider === 'openrouter' ? 'e.g. meta-llama/llama-4-scout' : 'Custom model ID'}
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
            {#if llmProvider === 'openrouter'}
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
        </div>

        <div class="field">
          <label for="llm-temperature">Temperature: {llmTemperature}</label>
          <input
            id="llm-temperature"
            type="range"
            min="0"
            max="2"
            step="0.1"
            bind:value={llmTemperature}
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

              <div class="field">
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
                  />
                  <button class="clear-btn" onclick={() => (llmTopP = null)} title="Reset to default">×</button>
                </div>
                <p class="hint">Nucleus sampling threshold (0-1)</p>
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

              <div class="field">
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
                  />
                  <button class="clear-btn" onclick={() => (llmFrequencyPenalty = null)} title="Reset to default">×</button>
                </div>
                <p class="hint">Reduce repetition of token sequences (-2 to 2)</p>
              </div>

              <div class="field">
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
                  />
                  <button class="clear-btn" onclick={() => (llmPresencePenalty = null)} title="Reset to default">×</button>
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
    <div class="tab-content tab-content-full">
      <ToolManagementPanel />
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

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
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

  label {
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
</style>
