<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type {
    CLIProxyProviderInfo,
    LLMProvider,
    LLMProviderSpec,
    LLMProviderTestResponse,
    OpenAIApiMode,
    ServerSettings,
  } from '$lib/types';
  import { modelOptions } from '$lib/utils/modelOptions';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import {
    DEFAULT_CLIPROXY_BASE_URL,
    DEFAULT_OPENAI_CLIPROXY_BASE_URL,
    normalizeBaseUrl,
  } from '$lib/utils/providerMapping';
  import {
    buildProviderSaveUpdate,
    detectCliproxyEntry,
    providerSupportsApiMode,
    registryProviderGroups,
  } from '$lib/utils/onboardingSetup';
  import Button from './Button.svelte';
  import Icon from './Icon.svelte';
  import Modal from './Modal.svelte';
  import ProviderSelect from './ProviderSelect.svelte';
  import SegmentedTabs from './SegmentedTabs.svelte';

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

  type WizardStep = 1 | 2 | 3;
  type TestStatus = 'idle' | 'testing' | 'success' | 'error';
  type SaveStatus = 'idle' | 'saving' | 'success' | 'error';

  let { isOpen, currentSettings, onClose, onSaved = () => {} }: Props = $props();

  let step = $state<WizardStep>(1);
  let authMethod = $state<AuthMethod>('api_key');
  let cliproxyCatalog = $state<CLIProxyProviderInfo[]>(FALLBACK_CLIPROXY_CATALOG);
  // Full registry catalog from GET /settings/llm/providers (130+ providers).
  // Empty until the backend answers; registryProviderGroups has an offline
  // fallback of the ids every backend knows.
  let providerCatalog = $state<LLMProviderSpec[]>([]);
  let cliproxySelection = $state('claude');
  let directProvider = $state<LLMProvider>('anthropic');
  let model = $state('claude-sonnet-4-6');
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

  function specFor(provider: string): LLMProviderSpec | null {
    const target = (provider || '').trim().toLowerCase();
    return (
      providerCatalog.find((s) => s.id === target || (s.aliases ?? []).includes(target)) ?? null
    );
  }

  // Registry spec for the selected direct provider (null offline or on the
  // cliproxy path, where the CLIProxy catalog entry is authoritative).
  const spec = $derived<LLMProviderSpec | null>(
    authMethod === 'api_key' ? specFor(directProvider) : null
  );
  const providerGroups = $derived(registryProviderGroups(providerCatalog));
  const needsKey = $derived(
    authMethod === 'cliproxy' || (spec?.requires_api_key ?? true)
  );

  // Whether the API-mode picker applies. Registry spec drives the direct
  // path; the no-spec fallback keeps the picker for openai/openrouter when
  // the catalog is unreachable (the pre-registry behavior).
  const supportsApiModePick = $derived(
    authMethod === 'cliproxy'
      ? effectiveProvider === 'openai'
      : providerSupportsApiMode(spec)
        || (!spec && (directProvider === 'openai' || directProvider === 'openrouter'))
  );

  // The api-mode value the test and save payloads carry, or null when the
  // provider has a single API surface. CLIProxy entries pin their own mode.
  const effectiveApiMode = $derived.by<OpenAIApiMode | null>(() => {
    if (authMethod === 'cliproxy') {
      return (cliproxySpec?.api_mode as OpenAIApiMode) || openaiApiMode;
    }
    return supportsApiModePick ? openaiApiMode : null;
  });

  function methodLabel(): string {
    if (authMethod === 'api_key') return 'Direct API key';
    return `Subscription OAuth: ${cliproxySpec?.label ?? cliproxySelection}`;
  }

  const normalizedProviderBaseUrl = $derived(getNormalizedBaseUrl());
  const testCanRun = $derived(
    model.trim().length > 0
      && (!needsKey || apiKey.trim().length > 0)
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
    const [cliproxy, providers] = await Promise.all([
      api.getCLIProxyCatalog(),
      api.getLLMProviderCatalog(),
    ]);
    if (cliproxy.length > 0) {
      cliproxyCatalog = cliproxy;
    }
    providerCatalog = providers;
  }

  $effect(() => {
    if (effectiveProvider !== lastProvider) {
      model = defaultModelFor(effectiveProvider);
      // A base-URL override belongs to the provider it was typed for; the
      // cliproxy path re-derives its own URL in the authKey effect below.
      if (authMethod === 'api_key') {
        baseUrl = '';
      }
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
      const isCliproxyShape =
        (currentSettings.llm_provider === 'anthropic' || currentSettings.llm_provider === 'openai')
        && !!currentSettings.llm_base_url;
      if (isCliproxyShape) {
        authMethod = 'cliproxy';
        cliproxySelection = detectCliproxyEntry(cliproxyCatalog, currentSettings);
        baseUrl = currentSettings.llm_base_url ?? '';
      } else {
        authMethod = 'api_key';
        directProvider = currentSettings.llm_provider;
        // Keep a saved base-URL override visible so re-saving does not wipe it.
        baseUrl = currentSettings.llm_base_url ?? '';
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
    return (
      specFor(provider)?.default_model
      ?? modelOptions[provider]?.[0]?.value
      ?? (provider === 'anthropic' ? 'claude-sonnet-4-6' : '')
    );
  }

  function providerLabel(provider: LLMProvider): string {
    return specFor(provider)?.label ?? provider;
  }

  function getNormalizedBaseUrl(): string {
    // Direct providers: optional override; empty means the registry default.
    // anthropic/openai never carry one (their base_url slot is the CLIProxy
    // detection signal; the field is hidden for them in step 2).
    if (authMethod === 'api_key') {
      if (directProvider === 'anthropic' || directProvider === 'openai') return '';
      return normalizeBaseUrl(baseUrl);
    }

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
      apiKey: needsKey ? apiKey.trim() : '',
      baseUrl: normalizedProviderBaseUrl,
      openaiApiMode: effectiveApiMode,
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
      api_key: needsKey ? apiKey.trim() || null : null,
      llm_base_url: normalizedProviderBaseUrl || null,
      openai_api_mode: effectiveApiMode,
    };
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
      // Shared save-payload builder (same one GUI onboarding uses): direct
      // keys ride the generic llm_api_key slot the backend routes to the
      // provider's declared env var; the cliproxy path keeps the dedicated
      // gateway slots for the cpx- gatekeeper.
      const result = await api.updateServerSettings(
        buildProviderSaveUpdate({
          authPath: authMethod,
          provider: effectiveProvider,
          model,
          // A key typed for an earlier pick must not ride into a keyless
          // provider's slot once the key field is hidden.
          apiKey: needsKey ? apiKey : '',
          baseUrl: normalizedProviderBaseUrl,
          apiMode: effectiveApiMode,
          cliproxySpec: authMethod === 'cliproxy' ? cliproxySpec : null,
        })
      );
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
    <div class="steps">
      <SegmentedTabs
        ariaLabel="Provider setup progress"
        tabs={[
          { id: '1', label: 'Auth', badge: '1', complete: step > 1 },
          { id: '2', label: 'Credentials', badge: '2', complete: step > 2 },
          { id: '3', label: 'Test', badge: '3', disabled: !testCanRun },
        ]}
        active={String(step)}
        onSelect={(id) => (step = Number(id) as WizardStep)}
        fill
      />
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
                <small>Pay-per-token key from any of {providerCatalog.length || 'the'} registry providers</small>
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
            <ProviderSelect
              id="provider-setup-provider"
              bind:value={directProvider}
              groups={providerGroups}
              ariaLabel="LLM provider"
            />
            {#if spec?.signup_url}
              <p class="hint signup">
                <a href={spec.signup_url} target="_blank" rel="noreferrer">
                  Get a {spec.label} key
                  <Icon name="externalLink" size={11} />
                </a>
                {#if spec.signup_guidance}
                  <span>{spec.signup_guidance}</span>
                {/if}
              </p>
            {:else if spec?.docs_url}
              <p class="hint signup">
                <a href={spec.docs_url} target="_blank" rel="noreferrer">
                  {spec.label} API docs
                  <Icon name="externalLink" size={11} />
                </a>
              </p>
            {/if}
          </div>
        {:else}
          <div class="notice">
            <Icon name="info" size={16} />
            <span>This configures a backend to use an already-running CLIProxy endpoint. Desktop installed builds do not start or manage CLIProxy.</span>
          </div>
        {/if}

        <div class="field">
          <label for="provider-setup-model">Model</label>
          {#if (modelOptions[effectiveProvider] ?? []).length > 0}
            <select
              id="provider-setup-model-preset"
              bind:value={model}
              aria-label="Suggested models"
            >
              {#each modelOptions[effectiveProvider] ?? [] as option}
                <option value={option.value}>{option.label}</option>
              {/each}
            </select>
          {/if}
          <input
            id="provider-setup-model"
            class="model-input"
            type="text"
            bind:value={model}
            placeholder={authMethod === 'api_key'
              ? (spec?.default_model ?? 'Model ID')
              : (cliproxySpec?.default_model ?? 'Model ID')}
          />
        </div>
      </div>
    {:else if step === 2}
      <div class="wizard-body">
        {#if !needsKey}
          <p class="hint">This provider does not need an API key.</p>
        {:else}
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
        {/if}

        {#if authMethod === 'api_key'}
          <!-- No base-URL override for anthropic/openai: a saved
               anthropic/openai + base_url pair is detected as the CLIProxy
               shape on reopen, so a direct override would not round-trip. -->
          {#if directProvider !== 'anthropic' && directProvider !== 'openai'}
            <div class="field">
              <label for="provider-setup-direct-base-url">Base URL (optional)</label>
              <input
                id="provider-setup-direct-base-url"
                type="text"
                bind:value={baseUrl}
                placeholder={spec?.default_base_url ?? 'Provider default'}
                oninput={resetVerification}
              />
              <p class="hint">Leave empty to use the provider's default endpoint.</p>
            </div>
          {/if}
        {:else}
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

        {#if supportsApiModePick}
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
    border-bottom: 1px solid var(--border-subtle);
    padding-bottom: var(--spacing-md);
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

  .signup {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
  }

  .signup a {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    color: var(--accent-primary);
    font-weight: 500;
    text-decoration: none;
  }

  .signup a:hover {
    text-decoration: underline;
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
