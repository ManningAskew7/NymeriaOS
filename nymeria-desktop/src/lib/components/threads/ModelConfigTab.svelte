<script lang="ts">
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { api } from '$lib/services/api.svelte';
  import type { LLMProviderSpec, ProviderRoute } from '$lib/types';
  import ProviderSelect from '$lib/components/common/ProviderSelect.svelte';
  import { loadAvailableModels, type AvailableModelsState } from '$lib/utils/models';
  import {
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    buildProviderGroups,
    fromThreadDisplayProvider,
    supportsOpenAiApiMode,
    type ThreadDisplayProvider,
  } from '$lib/utils/providerMapping';
  import {
    coerceProviderRoute,
    hasRouteChoice,
    providerRouteLabel,
    supportedRoutesForProvider,
  } from '$lib/utils/providerRoutes';

  interface Props {
    threadDisplayProvider: ThreadDisplayProvider;
    llmProvider: string;
    llmModel: string;
    llmBaseUrl: string;
    llmApiKey: string;
    llmTemperature: string;
    llmMaxTokens: string;
    llmContextLength: string;
    llmOllamaNumCtx: string;
    llmExtendedThinking: 'default' | 'true' | 'false';
    llmReasoningEffort: string;
    llmUseModelDefaults: 'default' | 'true' | 'false';
    llmProviderRoute: 'default' | ProviderRoute;
    llmOpenAiApiMode: 'default' | 'chat_completions' | 'responses';
    compactThresholdMode: 'default' | 'percentage' | 'tokens';
    compactThresholdPct: string;
    compactThresholdTokens: string;
  }

  let {
    threadDisplayProvider = $bindable(),
    llmProvider = $bindable(),
    llmModel = $bindable(),
    llmBaseUrl = $bindable(),
    llmApiKey = $bindable(),
    llmTemperature = $bindable(),
    llmMaxTokens = $bindable(),
    llmContextLength = $bindable(),
    llmOllamaNumCtx = $bindable(),
    llmExtendedThinking = $bindable(),
    llmReasoningEffort = $bindable(),
    llmUseModelDefaults = $bindable(),
    llmProviderRoute = $bindable(),
    llmOpenAiApiMode = $bindable(),
    compactThresholdMode = $bindable(),
    compactThresholdPct = $bindable(),
    compactThresholdTokens = $bindable(),
  }: Props = $props();

  const threadModelMeta = $derived(modelsStore.getById(llmModel));
  let providerCatalog = $state<LLMProviderSpec[]>([]);
  // Tier-grouped picker options. Drops the synthetic "Local LLM
  // (OpenAI-compatible)" entry here since the per-thread surface picks a
  // concrete provider id; users override base_url separately further down.
  let threadProviderGroups = $derived(
    buildProviderGroups(providerCatalog, { includeLocalOpenAISentinel: false })
  );

  let availableModelsState = $state<AvailableModelsState>({
    models: [],
    provider: '',
    loading: false,
  });

  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

  function selectedRoute(): ProviderRoute {
    const provider = getEffectiveProvider();
    const inherited = serverSettingsStore.providerRoute as ProviderRoute | null;
    const route = llmProviderRoute === 'default' ? inherited : llmProviderRoute;
    return coerceProviderRoute(provider, providerCatalog, route);
  }

  function showProviderRouteSelect(): boolean {
    return hasRouteChoice(getEffectiveProvider(), providerCatalog);
  }

  function supportsApiMode(provider: string = getEffectiveProvider()): boolean {
    if (!provider || provider === 'anthropic' || provider === 'bedrock') return false;
    if (hasRouteChoice(provider, providerCatalog)) return selectedRoute() === 'openai_compat';
    return supportsOpenAiApiMode(provider) && provider !== 'google' && provider !== 'ollama';
  }

  $effect(() => {
    const { provider } = fromThreadDisplayProvider(threadDisplayProvider);
    llmProvider = provider;
    const ep = getEffectiveProvider();
    const baseUrlOverride = supportsApiMode(ep) ? llmBaseUrl : '';
    void loadAvailableModels(ep, availableModelsState, baseUrlOverride);
  });

  $effect(() => {
    void api.getLLMProviderCatalog().then((catalog) => {
      providerCatalog = catalog;
    });
  });

  $effect(() => {
    if (threadDisplayProvider === 'openai_custom' && !llmBaseUrl) {
      llmBaseUrl = DEFAULT_CUSTOM_OPENAI_BASE_URL;
    }
  });
</script>

<div class="tab-panel">
  <div class="field-group">
    <label class="field-label" for="llm-provider">Provider</label>
    <ProviderSelect
      id="llm-provider"
      bind:value={threadDisplayProvider}
      groups={threadProviderGroups}
      includeDefault={true}
      defaultLabel="Default (inherit global)"
      defaultDescription="Use the provider configured in global Settings."
    />
  </div>

  {#if showProviderRouteSelect()}
    <div class="field-group">
      <label class="field-label" for="llm-provider-route">Provider Route</label>
      <select id="llm-provider-route" class="field-select" bind:value={llmProviderRoute}>
        <option value="default">Default (inherit global)</option>
        {#each supportedRoutesForProvider(getEffectiveProvider(), providerCatalog) as route}
          <option value={route}>{providerRouteLabel(route)}</option>
        {/each}
      </select>
      <span class="field-hint">
        Current route: {providerRouteLabel(selectedRoute())}
      </span>
    </div>
  {/if}

  {#if getEffectiveProvider() && supportsApiMode(getEffectiveProvider())}
    <div class="field-group">
      <label class="field-label" for="llm-base-url">API Base URL</label>
      <input
        id="llm-base-url"
        class="field-input"
        type="text"
        bind:value={llmBaseUrl}
        placeholder={threadDisplayProvider === 'openai_custom' ? 'http://cli-proxy-api-latest:8317/v1' : 'Provider default'}
      />
      <span class="field-hint">
        Optional OpenAI-compatible endpoint override reachable from the NymeriaOS backend. Leave empty to inherit global settings, a saved credential base URL, or the provider default.
      </span>
    </div>

    <div class="field-group">
      <label class="field-label" for="llm-api-key">API Key (Optional)</label>
      <input
        id="llm-api-key"
        class="field-input"
        type="password"
        bind:value={llmApiKey}
        placeholder="Leave empty to inherit global provider key"
        autocomplete="off"
      />
      <span class="field-hint">
        Required when pointing at a CLIProxy sidecar with its own <code>api-keys</code> list (e.g. <code>cpx-latest-local-test</code> for the GPT-5.5 sidecar). Stored per-thread in the NymeriaOS data directory.
      </span>
    </div>
  {/if}

  {#if supportsApiMode(getEffectiveProvider())}
    <div class="field-group">
      <label class="field-label" for="llm-openai-api-mode">API Mode</label>
      <select id="llm-openai-api-mode" class="field-select" bind:value={llmOpenAiApiMode}>
        <option value="default">Default (inherit global)</option>
        <option value="chat_completions">Chat Completions (not recommended if thinking is enabled)</option>
        <option value="responses">Responses API</option>
      </select>
      <span class="field-hint">
        Responses API is the default for OpenAI-compatible reasoning models and OpenRouter beta. Chat Completions remains available as a compatibility override.
      </span>
    </div>
  {/if}

  <div class="field-group">
    <label class="field-label" for="llm-model">Model</label>
    {#if availableModelsState.models.length > 0}
      <select id="llm-model" class="field-select" bind:value={llmModel}>
        <option value="">Default (inherit global)</option>
        {#each availableModelsState.models as model}
          <option value={model.id}>{model.name || model.id}</option>
        {/each}
      </select>
    {:else}
      <input
        id="llm-model"
        class="field-input"
        type="text"
        bind:value={llmModel}
        placeholder={availableModelsState.loading
          ? 'Loading models… or type one (e.g. claude-opus-4-7)'
          : 'Leave empty for global default'}
      />
      {#if availableModelsState.loading}
        <span class="field-hint">Fetching available models from {getEffectiveProvider()}…</span>
      {/if}
    {/if}
    <input
      class="field-input model-text-input"
      type="text"
      bind:value={llmModel}
      placeholder="Type an exact model ID"
    />
    <span class="field-hint">
      Model lists come from the provider's models endpoint when available. You can still enter an exact model ID manually.
    </span>
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
    <label class="field-label" for="llm-context-length">Context Window Tokens</label>
    <input
      id="llm-context-length"
      class="field-input"
      type="number"
      min="1000"
      max="2000000"
      step="1"
      bind:value={llmContextLength}
      placeholder="Default"
    />
    <span class="field-hint">Manual local-model context override. Leave empty to inherit global or auto-detected metadata.</span>
  </div>

  <div class="field-group">
    <label class="field-label" for="llm-ollama-num-ctx">Ollama num_ctx</label>
    <input
      id="llm-ollama-num-ctx"
      class="field-input"
      type="number"
      min="1000"
      max="2000000"
      step="1"
      bind:value={llmOllamaNumCtx}
      placeholder="Default"
    />
    <span class="field-hint">Per-thread Ollama options.num_ctx override. Leave empty to inherit global or auto-detect.</span>
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

  <div class="field-group">
    <label class="field-label" for="thread-compact-mode">Auto-Compact Trigger</label>
    <select id="thread-compact-mode" class="field-select" bind:value={compactThresholdMode}>
      <option value="default">Default (inherit global)</option>
      <option value="percentage">Percentage of context window</option>
      <option value="tokens">Absolute input-token count</option>
    </select>
    <span class="field-hint">
      Overrides the global compact trigger for this thread only. Real provider-reported input tokens are used either way.
    </span>
  </div>

  {#if compactThresholdMode === 'percentage'}
    <div class="field-group">
      <label class="field-label" for="thread-compact-pct">Compact Threshold (0.05 – 0.95)</label>
      <input
        id="thread-compact-pct"
        class="field-input"
        type="number"
        min="0.05"
        max="0.95"
        step="0.01"
        bind:value={compactThresholdPct}
        placeholder="Leave empty to inherit global"
      />
    </div>
  {:else if compactThresholdMode === 'tokens'}
    <div class="field-group">
      <label class="field-label" for="thread-compact-tokens">Compact Token Threshold (1,000 – 2,000,000)</label>
      <input
        id="thread-compact-tokens"
        class="field-input"
        type="number"
        min="1000"
        max="2000000"
        step="1000"
        bind:value={compactThresholdTokens}
        placeholder="Leave empty to inherit global"
      />
      <span class="field-hint">Clamped to the model's context window at runtime.</span>
    </div>
  {/if}
</div>

<style>
  .tab-panel {
    padding: var(--spacing-lg);
  }

  .field-group {
    margin-bottom: var(--spacing-md);
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

  .model-text-input {
    margin-top: var(--spacing-xs);
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
</style>
