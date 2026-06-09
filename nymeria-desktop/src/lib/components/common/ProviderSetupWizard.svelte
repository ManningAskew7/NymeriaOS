<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type {
    LLMProvider,
    LLMProviderTestResponse,
    OpenAIApiMode,
    ServerSettings,
    ServerSettingsUpdate,
  } from '$lib/types';
  import { modelOptions } from '$lib/utils/modelOptions';
  import {
    DEFAULT_CLIPROXY_BASE_URL,
    DEFAULT_OPENAI_CLIPROXY_BASE_URL,
    normalizeBaseUrl,
  } from '$lib/utils/providerMapping';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import Modal from './Modal.svelte';

  interface Props {
    isOpen: boolean;
    currentSettings: ServerSettings | null;
    onClose: () => void;
    onSaved?: () => void | Promise<void>;
  }

  type AuthMethod = 'api_key' | 'cliproxy_claude_oauth' | 'cliproxy_codex_oauth';
  type WizardStep = 1 | 2 | 3;
  type TestStatus = 'idle' | 'testing' | 'success' | 'error';
  type SaveStatus = 'idle' | 'saving' | 'success' | 'error';

  let { isOpen, currentSettings, onClose, onSaved = () => {} }: Props = $props();

  const directProviders: { value: LLMProvider; label: string }[] = [
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'openai', label: 'OpenAI' },
    { value: 'openrouter', label: 'OpenRouter' },
  ];

  const methodLabels: Record<AuthMethod, string> = {
    api_key: 'Direct API key',
    cliproxy_claude_oauth: 'CLIProxy Claude OAuth',
    cliproxy_codex_oauth: 'CLIProxy Codex OAuth',
  };

  const providerLabels: Record<string, string> = {
    anthropic: 'Anthropic',
    openai: 'OpenAI',
    openrouter: 'OpenRouter',
  };

  let step = $state<WizardStep>(1);
  let authMethod = $state<AuthMethod>('api_key');
  let directProvider = $state<LLMProvider>('anthropic');
  let model = $state('claude-sonnet-4-20250514');
  let apiKey = $state('');
  let baseUrl = $state('');
  let openaiApiMode = $state<OpenAIApiMode>('responses');
  let testStatus = $state<TestStatus>('idle');
  let testMessage = $state('');
  let saveStatus = $state<SaveStatus>('idle');
  let saveMessage = $state('');
  let testResponse = $state<LLMProviderTestResponse | null>(null);
  let lastSuccessfulTestSignature = $state('');
  let wasOpen = $state(false);
  let lastProvider = $state<LLMProvider>('anthropic');
  let lastAuthMethod = $state<AuthMethod>('api_key');

  const effectiveProvider = $derived<LLMProvider>(
    authMethod === 'cliproxy_claude_oauth'
      ? 'anthropic'
      : authMethod === 'cliproxy_codex_oauth'
        ? 'openai'
        : directProvider
  );

  const normalizedProviderBaseUrl = $derived(getNormalizedBaseUrl());
  const testCanRun = $derived(
    model.trim().length > 0
      && apiKey.trim().length > 0
      && (authMethod === 'api_key' || normalizedProviderBaseUrl.length > 0)
  );
  const currentTestSignature = $derived(getTestSignature());
  const testIsCurrent = $derived(
    testStatus === 'success'
      && lastSuccessfulTestSignature.length > 0
      && lastSuccessfulTestSignature === currentTestSignature
  );

  $effect(() => {
    if (isOpen && !wasOpen) {
      initializeFromSettings();
      wasOpen = true;
    } else if (!isOpen) {
      wasOpen = false;
    }
  });

  $effect(() => {
    if (effectiveProvider !== lastProvider) {
      model = defaultModelFor(effectiveProvider);
      lastProvider = effectiveProvider;
      resetVerification();
    }
  });

  $effect(() => {
    if (authMethod !== lastAuthMethod) {
      if (authMethod === 'cliproxy_claude_oauth') {
        baseUrl = currentSettings?.llm_provider === 'anthropic' && currentSettings.llm_base_url
          ? currentSettings.llm_base_url
          : DEFAULT_CLIPROXY_BASE_URL;
      } else if (authMethod === 'cliproxy_codex_oauth') {
        baseUrl = currentSettings?.llm_provider === 'openai' && currentSettings.llm_base_url
          ? currentSettings.llm_base_url
          : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
        openaiApiMode = 'responses';
      } else {
        baseUrl = '';
      }
      apiKey = '';
      lastAuthMethod = authMethod;
      resetVerification();
    }
  });

  function initializeFromSettings() {
    step = 1;
    apiKey = '';
    testStatus = 'idle';
    testMessage = '';
    saveStatus = 'idle';
    saveMessage = '';
    testResponse = null;
    lastSuccessfulTestSignature = '';

    if (currentSettings) {
      model = currentSettings.llm_model || defaultModelFor(currentSettings.llm_provider);
      openaiApiMode = currentSettings.openai_api_mode ?? 'responses';
      if (currentSettings.llm_provider === 'anthropic' && currentSettings.llm_base_url) {
        authMethod = 'cliproxy_claude_oauth';
        baseUrl = currentSettings.llm_base_url;
      } else if (currentSettings.llm_provider === 'openai' && currentSettings.llm_base_url) {
        authMethod = 'cliproxy_codex_oauth';
        baseUrl = currentSettings.llm_base_url;
      } else {
        authMethod = 'api_key';
        directProvider = currentSettings.llm_provider;
        baseUrl = '';
      }
    } else {
      authMethod = 'api_key';
      directProvider = 'anthropic';
      model = defaultModelFor('anthropic');
      baseUrl = '';
      openaiApiMode = 'responses';
    }

    lastProvider = effectiveProvider;
    lastAuthMethod = authMethod;
  }

  function defaultModelFor(provider: LLMProvider): string {
    if (provider === 'anthropic') return 'claude-sonnet-4-20250514';
    return modelOptions[provider]?.[0]?.value ?? '';
  }

  function providerLabel(provider: LLMProvider): string {
    return providerLabels[provider] ?? provider;
  }

  function getNormalizedBaseUrl(): string {
    if (authMethod === 'api_key') return '';

    const fallback = authMethod === 'cliproxy_claude_oauth'
      ? DEFAULT_CLIPROXY_BASE_URL
      : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
    const normalized = normalizeBaseUrl(baseUrl || fallback);

    if (authMethod === 'cliproxy_claude_oauth' && normalized.endsWith('/v1')) {
      return normalized.slice(0, -3);
    }
    if (authMethod === 'cliproxy_codex_oauth' && !normalized.endsWith('/v1')) {
      return `${normalized}/v1`;
    }
    return normalized;
  }

  function getTestSignature(): string {
    return JSON.stringify({
      authMethod,
      provider: effectiveProvider,
      model: model.trim(),
      apiKey: apiKey.trim(),
      baseUrl: normalizedProviderBaseUrl,
      openaiApiMode: effectiveProvider === 'openai' || effectiveProvider === 'openrouter'
        ? openaiApiMode
        : null,
    });
  }

  function resetVerification() {
    testStatus = 'idle';
    testMessage = '';
    saveStatus = 'idle';
    saveMessage = '';
    testResponse = null;
    lastSuccessfulTestSignature = '';
  }

  function handleAuthMethodSelect(method: AuthMethod) {
    authMethod = method;
  }

  function handleDirectProviderChange(provider: LLMProvider) {
    directProvider = provider;
  }

  function goNext() {
    if (step < 3) {
      step = (step + 1) as WizardStep;
    }
  }

  function goBack() {
    if (step > 1) {
      step = (step - 1) as WizardStep;
    }
  }

  function buildTestRequest() {
    return {
      llm_provider: effectiveProvider,
      llm_model: model.trim(),
      api_key: apiKey.trim(),
      llm_base_url: normalizedProviderBaseUrl || null,
      openai_api_mode: effectiveProvider === 'openai' || effectiveProvider === 'openrouter'
        ? openaiApiMode
        : null,
    };
  }

  function buildSettingsUpdate(): ServerSettingsUpdate {
    const updates: ServerSettingsUpdate = {
      llm_provider: effectiveProvider,
      llm_model: model.trim(),
      llm_base_url: authMethod === 'api_key' ? '' : normalizedProviderBaseUrl,
    };

    if (effectiveProvider === 'openai' || effectiveProvider === 'openrouter') {
      updates.openai_api_mode = openaiApiMode;
    }

    if (authMethod === 'cliproxy_claude_oauth') {
      updates.anthropic_api_key = apiKey.trim();
    } else if (authMethod === 'cliproxy_codex_oauth') {
      updates.openai_api_key = apiKey.trim();
      updates.openai_api_mode = 'responses';
    } else if (directProvider === 'anthropic') {
      updates.anthropic_direct_api_key = apiKey.trim();
    } else if (directProvider === 'openai') {
      updates.openai_api_key = apiKey.trim();
    } else if (directProvider === 'openrouter') {
      updates.openrouter_api_key = apiKey.trim();
    }

    return updates;
  }

  async function handleTestProvider() {
    if (!testCanRun) return;

    testStatus = 'testing';
    testMessage = '';
    saveStatus = 'idle';
    saveMessage = '';
    testResponse = null;
    lastSuccessfulTestSignature = '';

    try {
      const result = await api.testLLMProviderConfig(buildTestRequest());
      testResponse = result;
      if (result.ok) {
        testStatus = 'success';
        testMessage = result.message || 'Provider test succeeded.';
        lastSuccessfulTestSignature = currentTestSignature;
      } else {
        testStatus = 'error';
        testMessage = result.message || 'Provider test failed.';
      }
    } catch (e) {
      testStatus = 'error';
      testMessage = e instanceof Error ? e.message : 'Provider test failed.';
    }
  }

  async function handleSaveProvider() {
    if (!testIsCurrent) return;

    saveStatus = 'saving';
    saveMessage = '';

    try {
      const result = await api.updateServerSettings(buildSettingsUpdate());
      saveStatus = 'success';
      saveMessage = result.restart_required
        ? 'Provider settings saved. Restart the backend for all changes to take effect.'
        : 'Provider settings saved and applied.';
      await onSaved();
    } catch (e) {
      saveStatus = 'error';
      saveMessage = e instanceof Error ? e.message : 'Failed to save provider settings.';
    }
  }
</script>

<Modal title="Provider Setup" {isOpen} {onClose}>
  <div class="provider-wizard">
    <div class="steps" aria-label="Provider setup progress">
      <button class="step" class:active={step === 1} class:complete={step > 1} onclick={() => (step = 1)}>
        <span>1</span>
        Auth
      </button>
      <button class="step" class:active={step === 2} class:complete={step > 2} onclick={() => (step = 2)}>
        <span>2</span>
        Credentials
      </button>
      <button class="step" class:active={step === 3} onclick={() => (step = 3)} disabled={!testCanRun}>
        <span>3</span>
        Test
      </button>
    </div>

    {#if step === 1}
      <div class="wizard-body">
        <div class="field">
          <span class="field-label">Authentication</span>
          <div class="method-grid">
            <button
              class="method-option"
              class:selected={authMethod === 'api_key'}
              onclick={() => handleAuthMethodSelect('api_key')}
            >
              <Icon name="bolt" size={18} />
              <span>
                <strong>Direct API key</strong>
                <small>Anthropic, OpenAI, or OpenRouter billing</small>
              </span>
            </button>
            <button
              class="method-option"
              class:selected={authMethod === 'cliproxy_claude_oauth'}
              onclick={() => handleAuthMethodSelect('cliproxy_claude_oauth')}
            >
              <Icon name="server" size={18} />
              <span>
                <strong>CLIProxy Claude OAuth</strong>
                <small>Claude subscription via an existing proxy</small>
              </span>
            </button>
            <button
              class="method-option"
              class:selected={authMethod === 'cliproxy_codex_oauth'}
              onclick={() => handleAuthMethodSelect('cliproxy_codex_oauth')}
            >
              <Icon name="terminal" size={18} />
              <span>
                <strong>CLIProxy Codex OAuth</strong>
                <small>OpenAI-compatible proxy using Responses API</small>
              </span>
            </button>
          </div>
        </div>

        {#if authMethod === 'api_key'}
          <div class="field">
            <label for="provider-setup-provider">Provider</label>
            <select
              id="provider-setup-provider"
              value={directProvider}
              onchange={(e) => handleDirectProviderChange(e.currentTarget.value as LLMProvider)}
            >
              {#each directProviders as provider}
                <option value={provider.value}>{provider.label}</option>
              {/each}
            </select>
          </div>
        {:else}
          <div class="notice">
            <Icon name="info" size={16} />
            <span>This configures a backend to use an already-running CLIProxy endpoint. Desktop installed builds do not start or manage CLIProxy.</span>
          </div>
        {/if}

        <div class="field">
          <label for="provider-setup-model">Model</label>
          <select id="provider-setup-model" bind:value={model}>
            {#each modelOptions[effectiveProvider] ?? [] as option}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
          <input
            id="provider-setup-model-custom"
            class="model-input"
            type="text"
            bind:value={model}
            placeholder="Custom model ID"
          />
        </div>
      </div>
    {:else if step === 2}
      <div class="wizard-body">
        <div class="field">
          <label for="provider-setup-api-key">
            {authMethod === 'api_key' ? `${providerLabel(effectiveProvider)} API Key` : 'CLIProxy Gatekeeper Key'}
          </label>
          <input
            id="provider-setup-api-key"
            type="password"
            bind:value={apiKey}
            placeholder={authMethod === 'api_key' ? 'Provider API key' : 'cpx-...'}
            oninput={resetVerification}
          />
          <p class="hint">
            {#if authMethod === 'cliproxy_claude_oauth'}
              Use the local <code>cpx-...</code> key from CLIProxy, not an upstream Anthropic key.
            {:else if authMethod === 'cliproxy_codex_oauth'}
              Use the local <code>cpx-...</code> key from CLIProxy. Do not reuse it for embeddings.
            {:else}
              This value is sent once to the backend and remains write-only in settings responses.
            {/if}
          </p>
        </div>

        {#if authMethod !== 'api_key'}
          <div class="field">
            <label for="provider-setup-base-url">Proxy Base URL</label>
            <input
              id="provider-setup-base-url"
              type="text"
              bind:value={baseUrl}
              placeholder={authMethod === 'cliproxy_codex_oauth' ? DEFAULT_OPENAI_CLIPROXY_BASE_URL : DEFAULT_CLIPROXY_BASE_URL}
              oninput={resetVerification}
            />
            <p class="hint">
              {#if authMethod === 'cliproxy_claude_oauth'}
                Claude/Anthropic uses the proxy root URL with no <code>/v1</code> suffix.
              {:else}
                Codex/OpenAI uses an OpenAI-compatible URL ending in <code>/v1</code>.
              {/if}
            </p>
            {#if normalizeBaseUrl(baseUrl) && normalizeBaseUrl(baseUrl) !== normalizedProviderBaseUrl}
              <p class="hint adjusted">Will save as <code>{normalizedProviderBaseUrl}</code>.</p>
            {/if}
          </div>
        {/if}

        {#if effectiveProvider === 'openai' || effectiveProvider === 'openrouter'}
          <div class="field">
            <label for="provider-setup-api-mode">API Mode</label>
            <select id="provider-setup-api-mode" bind:value={openaiApiMode} disabled={authMethod === 'cliproxy_codex_oauth'}>
              <option value="responses">Responses API</option>
              <option value="chat_completions">Chat Completions</option>
            </select>
            {#if authMethod === 'cliproxy_codex_oauth'}
              <p class="hint">CLIProxy Codex OAuth is saved with Responses API mode.</p>
            {/if}
          </div>
        {/if}
      </div>
    {:else}
      <div class="wizard-body">
        <div class="summary">
          <div>
            <span>Auth</span>
            <strong>{methodLabels[authMethod]}</strong>
          </div>
          <div>
            <span>Provider</span>
            <strong>{providerLabel(effectiveProvider)}</strong>
          </div>
          <div>
            <span>Model</span>
            <strong>{model}</strong>
          </div>
          <div>
            <span>Base URL</span>
            <strong>{normalizedProviderBaseUrl || 'Provider default'}</strong>
          </div>
        </div>

        <div class="test-panel">
          <Button variant="secondary" onclick={handleTestProvider} disabled={!testCanRun || testStatus === 'testing'} loading={testStatus === 'testing'}>
            <Icon name="refresh" size={14} />
            {testStatus === 'testing' ? 'Testing' : 'Test Provider'}
          </Button>
          <p class="hint">The backend sends a small request using this exact provider configuration before saving it.</p>
        </div>

        {#if testMessage}
          <div class="result" class:success={testStatus === 'success'} class:error={testStatus === 'error'}>
            <Icon name={testStatus === 'success' ? 'success' : 'error'} size={16} />
            <span>{testMessage}</span>
          </div>
        {/if}

        {#if testResponse?.openai_api_mode}
          <p class="hint">Tested with {testResponse.openai_api_mode === 'responses' ? 'Responses API' : 'Chat Completions'}.</p>
        {/if}

        {#if saveMessage}
          <div class="result" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
            <Icon name={saveStatus === 'success' ? 'success' : 'error'} size={16} />
            <span>{saveMessage}</span>
          </div>
        {/if}
      </div>
    {/if}

    <div class="wizard-footer">
      <Button variant="ghost" onclick={onClose}>
        Close
      </Button>
      <div class="footer-actions">
        {#if step > 1}
          <Button variant="secondary" onclick={goBack}>
            Back
          </Button>
        {/if}
        {#if step < 3}
          <Button variant="primary" onclick={goNext} disabled={step === 2 && !testCanRun}>
            Next
          </Button>
        {:else}
          <Button
            variant="primary"
            onclick={handleSaveProvider}
            disabled={!testIsCurrent || saveStatus === 'saving'}
            loading={saveStatus === 'saving'}
          >
            {saveStatus === 'saving' ? 'Saving' : 'Save Provider'}
          </Button>
        {/if}
      </div>
    </div>
  </div>
</Modal>

<style>
  .provider-wizard {
    width: min(720px, calc(100vw - 4rem));
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .steps {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    /* §3 chip-row gap — wizard step pills sit at 8px, not 4px. */
    gap: var(--spacing-sm);
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-md);
  }

  .step {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm);
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    border: 1px solid transparent;
  }

  .step span {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    border-radius: var(--radius-full);
    background: var(--bg-elevated-2);
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .step.active {
    color: var(--accent-primary);
    border-color: rgba(var(--accent-primary-rgb), 0.35);
    background: rgba(var(--accent-primary-rgb), 0.08);
  }

  .step.complete span {
    background: var(--success);
    color: var(--bg-base);
  }

  .step:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .wizard-body {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    min-height: 340px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  label,
  .field-label {
    font-weight: 500;
    /* §4 — labels recede behind the input value (which is --text-primary),
       so the eye finds the answer before the question. */
    color: var(--text-secondary);
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
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }

  .model-input {
    margin-top: var(--spacing-xs);
  }

  .method-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--spacing-sm);
  }

  .method-option {
    min-height: 112px;
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated-2);
    color: var(--text-secondary);
    text-align: left;
    transition: all var(--transition-fast);
  }

  .method-option:hover {
    border-color: var(--border-default);
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .method-option.selected {
    border-color: var(--accent-primary);
    background: rgba(var(--accent-primary-rgb), 0.08);
    color: var(--text-primary);
    box-shadow: 0 0 0 3px rgba(var(--accent-primary-rgb), 0.12);
  }

  .method-option span {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .method-option strong {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .method-option small {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.35;
  }

  .notice,
  .result {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .notice {
    background: rgba(56, 189, 248, 0.1);
    border: 1px solid rgba(56, 189, 248, 0.24);
    color: var(--text-secondary);
  }

  .result.success {
    background: rgba(var(--success-rgb), 0.15);
    color: var(--success);
  }

  .result.error {
    background: rgba(var(--error-rgb), 0.15);
    color: var(--error);
  }

  .hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
  }

  .hint code {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    background: var(--bg-elevated);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
  }

  .adjusted {
    color: var(--accent-primary);
  }

  .summary {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--spacing-sm);
  }

  .summary div {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
    min-width: 0;
  }

  .summary span {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .summary strong {
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    overflow-wrap: anywhere;
  }

  .test-panel {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-md);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated-2);
  }

  .test-panel .hint {
    flex: 1;
  }

  .wizard-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--spacing-sm);
    padding-top: var(--spacing-md);
    border-top: 1px solid var(--border-subtle);
  }

  .footer-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--spacing-sm);
  }

  @media (max-width: 760px) {
    .provider-wizard {
      width: calc(100vw - 2rem);
    }

    .method-grid,
    .summary {
      grid-template-columns: 1fr;
    }

    .test-panel,
    .wizard-footer {
      align-items: stretch;
      flex-direction: column;
    }

    .footer-actions {
      width: 100%;
    }

    .footer-actions :global(.btn) {
      flex: 1;
    }
  }
</style>
