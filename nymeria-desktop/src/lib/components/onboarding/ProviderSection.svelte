<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type {
    AvailableModel,
    CLIProxyProviderInfo,
    LLMProviderSpec,
    OpenAIApiMode,
    ServerSettings,
  } from '$lib/types';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import {
    DEFAULT_CLIPROXY_BASE_URL,
    DEFAULT_OPENAI_CLIPROXY_BASE_URL,
    normalizeBaseUrl,
  } from '$lib/utils/providerMapping';
  import {
    DEFAULT_LOCAL_MODEL_BASE_URL,
    buildProviderSaveUpdate,
    detectAuthPath,
    detectCliproxyEntry,
    normalizeCliproxyBaseUrl,
    providerSupportsApiMode,
    registryProviderGroups,
    type SetupAuthPath,
  } from '$lib/utils/onboardingSetup';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import ProviderSelect from '$lib/components/common/ProviderSelect.svelte';

  interface Props {
    settings: ServerSettings | null;
    providerCatalog: LLMProviderSpec[];
    cliproxyCatalog: CLIProxyProviderInfo[];
    refresh: () => Promise<void>;
  }

  let { settings, providerCatalog, cliproxyCatalog, refresh }: Props = $props();

  let authPath = $state<SetupAuthPath>('api_key');
  let providerId = $state('anthropic');
  let cliproxyId = $state('claude');
  let model = $state('');
  let apiKey = $state('');
  let baseUrl = $state('');
  let apiMode = $state<OpenAIApiMode>('responses');

  type TestStatus = 'idle' | 'testing' | 'success' | 'error';
  let testStatus = $state<TestStatus>('idle');
  let testMessage = $state('');
  let lastGoodTestSignature = $state('');
  let saveStatus = $state<'idle' | 'saving' | 'success' | 'error'>('idle');
  let saveMessage = $state('');

  let availableModels = $state<AvailableModel[]>([]);
  let modelsLoading = $state(false);

  let initializedFor = $state<ServerSettings | null>(null);
  // Guard so the provider-change effect reacts to USER picks, not the
  // settings prefill (the same lastProvider pattern ProviderSetupWizard uses).
  let lastProviderId = $state('anthropic');

  const providerGroups = $derived(registryProviderGroups(providerCatalog));
  const spec = $derived<LLMProviderSpec | null>(
    providerCatalog.find((s) => s.id === providerId || (s.aliases ?? []).includes(providerId)) ??
      null
  );
  const cliproxySpec = $derived<CLIProxyProviderInfo | null>(
    cliproxyCatalog.find((e) => e.id === cliproxyId) ?? null
  );

  // The provider id the save/test payloads actually carry per auth path.
  const effectiveProvider = $derived(
    authPath === 'cliproxy'
      ? (cliproxySpec?.nymeria_provider ?? 'anthropic')
      : authPath === 'local'
        ? 'ollama'
        : providerId
  );

  const effectiveBaseUrl = $derived.by(() => {
    if (authPath === 'cliproxy') {
      const shape = cliproxySpec?.url_shape ?? 'root';
      const fallback = shape === 'root' ? DEFAULT_CLIPROXY_BASE_URL : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
      return normalizeCliproxyBaseUrl(baseUrl || fallback, shape);
    }
    return normalizeBaseUrl(baseUrl);
  });

  const effectiveApiMode = $derived.by<OpenAIApiMode | null>(() => {
    if (authPath === 'cliproxy') {
      return (cliproxySpec?.api_mode as OpenAIApiMode) || apiMode;
    }
    if (authPath === 'api_key' && providerSupportsApiMode(spec)) return apiMode;
    return null;
  });

  const needsKey = $derived(
    authPath === 'cliproxy' || (authPath === 'api_key' && (spec?.requires_api_key ?? true))
  );

  const testCanRun = $derived(
    model.trim().length > 0 && (!needsKey || apiKey.trim().length > 0)
  );

  const currentTestSignature = $derived(
    JSON.stringify({
      authPath,
      provider: effectiveProvider,
      model: model.trim(),
      apiKey: apiKey.trim(),
      baseUrl: effectiveBaseUrl,
      apiMode: effectiveApiMode,
    })
  );
  const testIsCurrent = $derived(
    testStatus === 'success' && lastGoodTestSignature === currentTestSignature
  );

  // Prefill from the saved settings exactly once per settings object, so a
  // CLI-configured backend shows its real state and the user can just leave.
  $effect(() => {
    if (!settings || settings === initializedFor) return;
    initializedFor = settings;
    authPath = detectAuthPath(settings);
    if (authPath === 'cliproxy') {
      cliproxyId = detectCliproxyEntry(cliproxyCatalog, settings);
      baseUrl = settings.llm_base_url ?? '';
    } else if (authPath === 'local') {
      baseUrl = settings.llm_base_url ?? '';
    } else {
      providerId = settings.llm_provider;
      baseUrl = settings.llm_base_url ?? '';
    }
    lastProviderId = providerId;
    model = settings.llm_model ?? '';
    apiMode = settings.openai_api_mode ?? 'responses';
    void loadModels();
  });

  // React to user provider picks from the select (prefill sets lastProviderId
  // in the same tick, so it never double-fires).
  $effect(() => {
    if (providerId !== lastProviderId) {
      lastProviderId = providerId;
      handleProviderChange();
    }
  });

  function resetVerification() {
    testStatus = 'idle';
    testMessage = '';
    saveStatus = 'idle';
    saveMessage = '';
    lastGoodTestSignature = '';
  }

  function selectAuthPath(path: SetupAuthPath) {
    if (authPath === path) return;
    authPath = path;
    apiKey = '';
    if (path === 'cliproxy') {
      const shape = cliproxySpec?.url_shape ?? 'root';
      baseUrl = shape === 'root' ? DEFAULT_CLIPROXY_BASE_URL : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
      model = cliproxySpec?.default_model || model;
    } else if (path === 'local') {
      baseUrl = '';
      model = '';
    } else {
      baseUrl = '';
      model = spec?.default_model ?? '';
    }
    resetVerification();
    void loadModels();
  }

  function selectCliproxyEntry(id: string) {
    cliproxyId = id;
    const entry = cliproxyCatalog.find((e) => e.id === id);
    if (entry) {
      const shape = entry.url_shape ?? 'root';
      baseUrl = shape === 'root' ? DEFAULT_CLIPROXY_BASE_URL : DEFAULT_OPENAI_CLIPROXY_BASE_URL;
      model = entry.default_model || model;
      if (entry.api_mode) apiMode = entry.api_mode as OpenAIApiMode;
    }
    resetVerification();
  }

  function handleProviderChange() {
    model = spec?.default_model ?? '';
    if (spec) {
      apiMode = (spec.default_api_mode as OpenAIApiMode) || 'responses';
    }
    resetVerification();
    void loadModels();
  }

  // Live model list. Uses the backend's stored credentials, so before the
  // first save it only resolves for keyless providers (Ollama and friends);
  // after a save, Refresh fills it for the configured provider too. The CLI
  // has the same list live because it holds the key in hand; the free-text
  // model field keeps parity for the rest.
  async function loadModels() {
    modelsLoading = true;
    try {
      const listBase = authPath === 'local' ? baseUrl || DEFAULT_LOCAL_MODEL_BASE_URL : effectiveBaseUrl;
      availableModels = await api.getAvailableModels(effectiveProvider, listBase || undefined);
    } finally {
      modelsLoading = false;
    }
  }

  async function handleTest() {
    if (!testCanRun) return;
    testStatus = 'testing';
    testMessage = '';
    saveStatus = 'idle';
    saveMessage = '';
    try {
      const result = await api.testLLMProviderConfig({
        llm_provider: effectiveProvider,
        llm_model: model.trim(),
        api_key: apiKey.trim() || null,
        llm_base_url: effectiveBaseUrl || null,
        openai_api_mode: effectiveApiMode,
      });
      if (result.ok) {
        testStatus = 'success';
        testMessage = result.message || 'Provider test succeeded.';
        lastGoodTestSignature = currentTestSignature;
      } else {
        testStatus = 'error';
        testMessage = result.message || 'Provider test failed.';
      }
    } catch (e) {
      testStatus = 'error';
      testMessage = humanizeErrorText(e, { action: 'test', resource: 'the provider' });
    }
  }

  async function handleSave() {
    if (!testIsCurrent) return;
    saveStatus = 'saving';
    saveMessage = '';
    try {
      const result = await api.updateServerSettings(
        buildProviderSaveUpdate({
          authPath,
          provider: effectiveProvider,
          model,
          apiKey,
          baseUrl: effectiveBaseUrl,
          apiMode: effectiveApiMode,
          cliproxySpec,
        })
      );
      saveStatus = 'success';
      saveMessage = result.restart_required
        ? 'Provider saved. Restart the backend for every change to take effect.'
        : 'Provider saved and applied.';
      await refresh();
    } catch (e) {
      saveStatus = 'error';
      saveMessage = humanizeErrorText(e, { action: 'save', resource: 'the provider settings' });
    }
  }
</script>

<div class="sf-field">
  <span class="sf-label">How should Nymeria reach a model?</span>
  <div class="path-grid">
    <button
      type="button"
      class="path-option"
      class:selected={authPath === 'api_key'}
      onclick={() => selectAuthPath('api_key')}
    >
      <Icon name="key" size={18} />
      <span>
        <strong>Direct API key</strong>
        <small>Pay-per-token key from any of {providerCatalog.length || 'the'} registry providers</small>
      </span>
    </button>
    <button
      type="button"
      class="path-option"
      class:selected={authPath === 'cliproxy'}
      onclick={() => selectAuthPath('cliproxy')}
    >
      <Icon name="server" size={18} />
      <span>
        <strong>Subscription login</strong>
        <small>Claude, ChatGPT, Gemini and other subscriptions via CLIProxy</small>
      </span>
    </button>
    <button
      type="button"
      class="path-option"
      class:selected={authPath === 'local'}
      onclick={() => selectAuthPath('local')}
    >
      <Icon name="bolt" size={18} />
      <span>
        <strong>Local model</strong>
        <small>Ollama on this machine. Private, free, no key</small>
      </span>
    </button>
  </div>
</div>

{#if authPath === 'api_key'}
  <div class="sf-field">
    <label class="sf-label" for="setup-provider">Provider</label>
    <ProviderSelect
      id="setup-provider"
      bind:value={providerId}
      groups={providerGroups}
      ariaLabel="LLM provider"
    />
    {#if spec?.signup_url}
      <p class="sf-hint signup">
        <a href={spec.signup_url} target="_blank" rel="noreferrer">
          Get a {spec.label} key
          <Icon name="externalLink" size={11} />
        </a>
        {#if spec.signup_guidance}
          <span>{spec.signup_guidance}</span>
        {/if}
      </p>
    {:else if spec?.docs_url}
      <p class="sf-hint signup">
        <a href={spec.docs_url} target="_blank" rel="noreferrer">
          {spec.label} API docs
          <Icon name="externalLink" size={11} />
        </a>
      </p>
    {/if}
  </div>

  {#if spec && !spec.requires_api_key}
    <p class="sf-hint">This provider does not need an API key.</p>
  {:else}
    <div class="sf-field">
      <label class="sf-label" for="setup-provider-key">API key</label>
      <input
        id="setup-provider-key"
        class="sf-input"
        type="password"
        bind:value={apiKey}
        oninput={resetVerification}
        placeholder="Provider API key"
        autocomplete="off"
      />
      <p class="sf-hint">
        Sent once to the backend and stored server-side. It is never shown again in settings.
      </p>
    </div>
  {/if}

  <div class="sf-field">
    <label class="sf-label" for="setup-provider-base-url">Base URL (optional)</label>
    <input
      id="setup-provider-base-url"
      class="sf-input"
      type="text"
      bind:value={baseUrl}
      oninput={resetVerification}
      placeholder={spec?.default_base_url ?? 'Provider default'}
    />
  </div>

  {#if providerSupportsApiMode(spec)}
    <div class="sf-field">
      <label class="sf-label" for="setup-api-mode">API mode</label>
      <select id="setup-api-mode" class="sf-input" bind:value={apiMode} onchange={resetVerification}>
        <option value="responses">Responses API</option>
        <option value="chat_completions">Chat Completions</option>
      </select>
    </div>
  {/if}
{:else if authPath === 'cliproxy'}
  <div class="sf-field">
    <span class="sf-label">Subscription</span>
    <div class="path-grid wide">
      {#each cliproxyCatalog as entry (entry.id)}
        <button
          type="button"
          class="path-option"
          class:selected={cliproxyId === entry.id}
          onclick={() => selectCliproxyEntry(entry.id)}
        >
          <Icon name="server" size={18} />
          <span>
            <strong>{entry.label}</strong>
            <small>{entry.tos_warning || 'Subscription OAuth via CLIProxy'}</small>
          </span>
        </button>
      {/each}
    </div>
    <p class="sf-hint">
      This points the backend at an already-running CLIProxy endpoint. Log in to the
      subscription itself from the CLIProxy tab in Settings, or with <code>nymeria init</code>.
    </p>
  </div>

  <div class="sf-field">
    <label class="sf-label" for="setup-gatekeeper-key">CLIProxy gatekeeper key</label>
    <input
      id="setup-gatekeeper-key"
      class="sf-input"
      type="password"
      bind:value={apiKey}
      oninput={resetVerification}
      placeholder="cpx-..."
      autocomplete="off"
    />
    <p class="sf-hint">
      The proxy's own <code>cpx-...</code> key from its api-keys list, never an upstream
      provider key.
    </p>
  </div>

  <div class="sf-field">
    <label class="sf-label" for="setup-proxy-base-url">Proxy base URL</label>
    <input
      id="setup-proxy-base-url"
      class="sf-input"
      type="text"
      bind:value={baseUrl}
      oninput={resetVerification}
      placeholder={cliproxySpec?.url_shape === 'v1'
        ? DEFAULT_OPENAI_CLIPROXY_BASE_URL
        : DEFAULT_CLIPROXY_BASE_URL}
    />
    {#if normalizeBaseUrl(baseUrl) && normalizeBaseUrl(baseUrl) !== effectiveBaseUrl}
      <p class="sf-hint">Will save as <code>{effectiveBaseUrl}</code>.</p>
    {/if}
  </div>
{:else}
  <div class="sf-field">
    <label class="sf-label" for="setup-ollama-base-url">Ollama URL</label>
    <input
      id="setup-ollama-base-url"
      class="sf-input"
      type="text"
      bind:value={baseUrl}
      oninput={resetVerification}
      placeholder={DEFAULT_LOCAL_MODEL_BASE_URL}
    />
    <p class="sf-hint">
      Leave empty for the default local endpoint. Install models with
      <code>ollama pull &lt;model&gt;</code>.
    </p>
  </div>
{/if}

<div class="sf-field">
  <label class="sf-label" for="setup-model">Model</label>
  {#if availableModels.length > 0}
    <select
      id="setup-model-select"
      class="sf-input"
      bind:value={model}
      onchange={resetVerification}
      aria-label="Model from live provider list"
    >
      {#each availableModels as m (m.id)}
        <option value={m.id}>{m.name || m.id}</option>
      {/each}
    </select>
  {/if}
  <div class="model-row">
    <input
      id="setup-model"
      class="sf-input"
      type="text"
      bind:value={model}
      oninput={resetVerification}
      placeholder={authPath === 'api_key'
        ? (spec?.default_model ?? 'Model ID')
        : authPath === 'cliproxy'
          ? (cliproxySpec?.default_model ?? 'Model ID')
          : 'e.g. qwen3:8b'}
    />
    <Button
      variant="ghost"
      size="sm"
      onclick={loadModels}
      disabled={modelsLoading}
      ariaLabel="Refresh model list"
      dataTooltip="Refresh the live model list"
    >
      <Icon name="refresh" size={14} />
    </Button>
  </div>
  {#if modelsLoading}
    <p class="sf-hint">Loading live model list…</p>
  {:else if availableModels.length === 0}
    <p class="sf-hint">
      Live model lists appear once the provider is reachable with stored credentials; until
      then, type the model ID.
    </p>
  {/if}
</div>

<div class="sf-actions">
  <Button
    variant="secondary"
    onclick={handleTest}
    disabled={!testCanRun || testStatus === 'testing'}
    loading={testStatus === 'testing'}
  >
    {testStatus === 'testing' ? 'Testing' : 'Test provider'}
  </Button>
  <Button
    variant="primary"
    onclick={handleSave}
    disabled={!testIsCurrent || saveStatus === 'saving'}
    loading={saveStatus === 'saving'}
  >
    {saveStatus === 'saving' ? 'Saving' : 'Save provider'}
  </Button>
</div>
<p class="sf-hint">
  Test sends one tiny request with exactly this configuration before anything is saved.
</p>

{#if testMessage}
  <div class="sf-result" class:success={testStatus === 'success'} class:error={testStatus === 'error'}>
    <Icon name={testStatus === 'success' ? 'success' : 'error'} size={16} />
    <span>{testMessage}</span>
  </div>
{/if}
{#if saveMessage}
  <div class="sf-result" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
    <Icon name={saveStatus === 'success' ? 'success' : 'error'} size={16} />
    <span>{saveMessage}</span>
  </div>
{/if}

<style>
  .path-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--spacing-sm);
  }

  .path-grid.wide {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .path-option {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-md);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    text-align: left;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .path-option:hover {
    border-color: var(--border-default);
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .path-option.selected {
    border-color: var(--accent-primary);
    background: var(--accent-tint-bg);
    color: var(--text-primary);
  }

  .path-option span {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .path-option strong {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .path-option small {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.35;
  }

  .model-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .model-row .sf-input {
    flex: 1;
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

  @media (max-width: 900px) {
    .path-grid,
    .path-grid.wide {
      grid-template-columns: 1fr;
    }
  }
</style>
