<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type {
    CLIProxyProviderInfo,
    LLMProvider,
    LLMProviderTestResponse,
    OpenAIApiMode,
    ServerSettings,
    ServerSettingsUpdate,
  } from '$lib/types';
  import { modelOptions } from '$lib/utils/modelOptions';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
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

  type AuthMethod = 'api_key' | 'cliproxy';

  // Offline fallback when the backend catalog is unreachable: a static copy
  // of the backend catalog's route shapes (refreshed by loadCatalog when the
  // backend answers). Needed in full so settings -> wizard detection can
  // resolve every CLI, not just claude/codex.
  const FALLBACK_CLIPROXY_CATALOG: CLIProxyProviderInfo[] = [
    {
      id: 'claude', label: 'Claude (Max/Pro subscription)', description: '',
      flow: 'browser', nymeria_provider: 'anthropic', url_shape: 'root',
      api_mode: '', key_env_var: 'ANTHROPIC_API_KEY',
      default_model: 'claude-opus-4-7', tos_warning: '', auth_file_provider: 'claude',
      supported: null, logged_in: null
    },
    {
      id: 'codex', label: 'Codex (ChatGPT Plus/Pro subscription)', description: '',
      flow: 'browser', nymeria_provider: 'openai', url_shape: 'v1',
      api_mode: 'responses', key_env_var: 'OPENAI_API_KEY',
      default_model: 'gpt-5.5', tos_warning: '', auth_file_provider: 'codex',
      supported: null, logged_in: null
    },
    {
      id: 'gemini-cli', label: 'Gemini CLI (Google account)', description: '',
      flow: 'browser', nymeria_provider: 'openai', url_shape: 'v1',
      api_mode: 'chat_completions', key_env_var: 'OPENAI_API_KEY',
      default_model: 'gemini-3-pro-preview', tos_warning: '', auth_file_provider: 'gemini',
      supported: null, logged_in: null
    },
    {
      id: 'antigravity', label: 'Antigravity (Google account)', description: '',
      flow: 'browser', nymeria_provider: 'openai', url_shape: 'v1',
      api_mode: 'chat_completions', key_env_var: 'OPENAI_API_KEY',
      default_model: 'gemini-3-pro-preview', tos_warning: '', auth_file_provider: 'antigravity',
      supported: null, logged_in: null
    },
    {
      id: 'kimi', label: 'Kimi (Moonshot subscription)', description: '',
      flow: 'device', nymeria_provider: 'openai', url_shape: 'v1',
      api_mode: 'chat_completions', key_env_var: 'OPENAI_API_KEY',
      default_model: 'kimi-k2.5', tos_warning: '', auth_file_provider: 'kimi',
      supported: null, logged_in: null
    },
    {
      id: 'grok', label: 'Grok (SuperGrok/X Premium subscription)', description: '',
      flow: 'browser', nymeria_provider: 'openai', url_shape: 'v1',
      api_mode: 'chat_completions', key_env_var: 'OPENAI_API_KEY',
      default_model: 'grok-4.3', tos_warning: '', auth_file_provider: 'xai',
      supported: null, logged_in: null
    }
  ];

  // Settings -> wizard reverse mapping. Codex only when the saved route is
  // actually in Responses mode; the chat_completions shape is shared by the
  // other CLIs, so resolve by the saved model's catalog default and fall back
  // to the first chat-mode entry. This keeps the save path (which writes the
  // selected spec's api_mode) from silently flipping a Gemini/Kimi/Grok route
  // to Responses mode.
  function detectCliproxySelection(settings: ServerSettings): string {
    if (settings.llm_provider === 'anthropic') return 'claude';
    if ((settings.openai_api_mode ?? 'responses') === 'responses') return 'codex';
    const model = (settings.llm_model || '').trim();
    const byModel = cliproxyCatalog.find(
      (entry) => entry.api_mode === 'chat_completions' && entry.default_model === model
    );
    if (byModel) return byModel.id;
    const chatEntry = cliproxyCatalog.find((entry) => entry.api_mode === 'chat_completions');
    return chatEntry?.id ?? 'codex';
  }
  type WizardStep = 1 | 2 | 3;
  type TestStatus = 'idle' | 'testing' | 'success' | 'error';
  type SaveStatus = 'idle' | 'saving' | 'success' | 'error';

  let { isOpen, currentSettings, onClose, onSaved = () => {} }: Props = $props();

  const directProviders: { value: LLMProvider; label: string }[] = [
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'openai', label: 'OpenAI' },
    { value: 'openrouter', label: 'OpenRouter' },
  ];



  const providerLabels: Record<string, string> = {
    anthropic: 'Anthropic',
    openai: 'OpenAI',
    openrouter: 'OpenRouter',
  };

  let step = $state<WizardStep>(1);
  let authMethod = $state<AuthMethod>('api_key');
  let cliproxyCatalog = $state<CLIProxyProviderInfo[]>(FALLBACK_CLIPROXY_CATALOG);
  let cliproxySelection = $state('claude');
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
  let lastAuthKey = $state('api_key');

  const cliproxySpec = $derived<CLIProxyProviderInfo | null>(
    cliproxyCatalog.find((entry) => entry.id === cliproxySelection) ?? null
  );
  const effectiveProvider = $derived<LLMProvider>(
    authMethod === 'cliproxy'
      ? ((cliproxySpec?.nymeria_provider as LLMProvider) ?? 'anthropic')
      : directProvider
  );
  const authKey = $derived(
    authMethod === 'cliproxy' ? `cliproxy:${cliproxySelection}` : 'api_key'
  );

  function methodLabel(): string {
    if (authMethod === 'api_key') return 'Direct API key';
    return `Subscription OAuth: ${cliproxySpec?.label ?? cliproxySelection}`;
  }

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
      void loadCatalog();
      wasOpen = true;
    } else if (!isOpen) {
      wasOpen = false;
    }
  });

  async function loadCatalog() {
    const catalog = await api.getCLIProxyCatalog();
    if (catalog.length > 0) {
      cliproxyCatalog = catalog;
    }
  }

  $effect(() => {
    if (effectiveProvider !== lastProvider) {
      model = defaultModelFor(effectiveProvider);
      lastProvider = effectiveProvider;
      resetVerification();
    }
  });

  $effect(() => {
    if (authKey !== lastAuthKey) {
      if (authMethod === 'cliproxy' && cliproxySpec) {
        const fallback = cliproxySpec.url_shape === 'root'
          ? DEFAULT_CLIPROXY_BASE_URL
          : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
        baseUrl = currentSettings?.llm_provider === cliproxySpec.nymeria_provider
          && currentSettings.llm_base_url
          ? currentSettings.llm_base_url
          : fallback;
        if (cliproxySpec.api_mode) {
          openaiApiMode = cliproxySpec.api_mode as OpenAIApiMode;
        }
        model = cliproxySpec.default_model || model;
      } else {
        baseUrl = '';
      }
      apiKey = '';
      lastAuthKey = authKey;
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
        authMethod = 'cliproxy';
        cliproxySelection = 'claude';
        baseUrl = currentSettings.llm_base_url;
      } else if (currentSettings.llm_provider === 'openai' && currentSettings.llm_base_url) {
        authMethod = 'cliproxy';
        cliproxySelection = detectCliproxySelection(currentSettings);
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
    lastAuthKey = authKey;
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

    const shape = cliproxySpec?.url_shape ?? 'root';
    const fallback = shape === 'root'
      ? DEFAULT_CLIPROXY_BASE_URL
      : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
    const normalized = normalizeBaseUrl(baseUrl || fallback);

    if (shape === 'root' && normalized.endsWith('/v1')) {
      return normalized.slice(0, -3);
    }
    if (shape === 'v1' && !normalized.endsWith('/v1')) {
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

    if (authMethod === 'cliproxy') {
      // The catalog's key slot: the cpx- gatekeeper goes to ANTHROPIC_API_KEY
      // or OPENAI_API_KEY, never the *_DIRECT_* slots.
      if (cliproxySpec?.key_env_var === 'OPENAI_API_KEY') {
        updates.openai_api_key = apiKey.trim();
      } else {
        updates.anthropic_api_key = apiKey.trim();
      }
      if (cliproxySpec?.api_mode) {
        updates.openai_api_mode = cliproxySpec.api_mode as OpenAIApiMode;
      }
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
      testMessage = humanizeErrorText(e, { action: 'test', resource: 'the provider' });
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
      saveMessage = humanizeErrorText(e, { action: 'save', resource: 'the provider settings' });
    }
  }
</script>

<Modal title="Provider Setup" {isOpen} {onClose}>
  <div class="provider-wizard">
    <div class="steps" aria-label="Provider setup progress">
      <button type="button" class="step" class:active={step === 1} class:complete={step > 1} aria-current={step === 1 ? 'step' : undefined} onclick={() => (step = 1)}>
        <span>1</span>
        Auth
      </button>
      <button type="button" class="step" class:active={step === 2} class:complete={step > 2} aria-current={step === 2 ? 'step' : undefined} onclick={() => (step = 2)}>
        <span>2</span>
        Credentials
      </button>
      <button type="button" class="step" class:active={step === 3} aria-current={step === 3 ? 'step' : undefined} onclick={() => (step = 3)} disabled={!testCanRun}>
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
            {#each cliproxyCatalog as entry (entry.id)}
              <button
                class="method-option"
                class:selected={authMethod === 'cliproxy' && cliproxySelection === entry.id}
                onclick={() => {
                  cliproxySelection = entry.id;
                  handleAuthMethodSelect('cliproxy');
                }}
              >
                <Icon name="server" size={18} />
                <span>
                  <strong>{entry.label}</strong>
                  <small>{entry.tos_warning || 'Subscription OAuth via CLIProxy'}</small>
                </span>
              </button>
            {/each}
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
            {#if authMethod === 'cliproxy'}
              Use the proxy's own <code>cpx-...</code> gatekeeper key (from its api-keys list),
              never an upstream provider key. Log in to the subscription itself from the
              CLIProxy tab in Settings.
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
              placeholder={cliproxySpec?.url_shape === 'v1' ? DEFAULT_OPENAI_CLIPROXY_BASE_URL : DEFAULT_CLIPROXY_BASE_URL}
              oninput={resetVerification}
            />
            <p class="hint">
              {#if cliproxySpec?.url_shape === 'root'}
                This CLI uses the proxy root URL with no <code>/v1</code> suffix.
              {:else}
                This CLI uses the proxy's OpenAI-compatible URL ending in <code>/v1</code>.
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
            <select id="provider-setup-api-mode" bind:value={openaiApiMode} disabled={authMethod === 'cliproxy' && !!cliproxySpec?.api_mode}>
              <option value="responses">Responses API</option>
              <option value="chat_completions">Chat Completions</option>
            </select>
            {#if authMethod === 'cliproxy' && cliproxySpec?.api_mode}
              <p class="hint">This CLI is saved with {cliproxySpec.api_mode === 'responses' ? 'Responses API' : 'Chat Completions'} mode.</p>
            {/if}
          </div>
        {/if}
      </div>
    {:else}
      <div class="wizard-body">
        <div class="summary">
          <div>
            <span>Auth</span>
            <strong>{methodLabel()}</strong>
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
