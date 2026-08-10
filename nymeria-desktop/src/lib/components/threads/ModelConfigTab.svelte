<script lang="ts">
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { api } from '$lib/services/api.svelte';
  import type { ActiveLLMFallback, LLMProviderSpec, ProviderRoute } from '$lib/types';
  import ProviderSelect from '$lib/components/common/ProviderSelect.svelte';
  import { loadAvailableModels, type AvailableModelsState } from '$lib/utils/models';
  import {
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    buildProviderGroups,
    fromThreadDisplayProvider,
    toThreadDisplayProvider,
    supportsOpenAiApiMode,
    type ThreadDisplayProvider,
  } from '$lib/utils/providerMapping';
  import {
    coerceProviderRoute,
    hasRouteChoice,
    providerRouteLabel,
    providerSpecFor,
    supportedRoutesForProvider,
  } from '$lib/utils/providerRoutes';
  import {
    REASONING_EFFORT_LEVELS,
    effortExceedsModelMax,
    effortOptionDisabled,
    reasoningEffortLabel,
    supportedEffortSet,
  } from '$lib/utils/reasoningEffort';

  interface Props {
    /** Which model section this pane renders. */
    section?: 'provider' | 'generation' | 'context';
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
    proactiveCompactEnabled: 'default' | 'true' | 'false';
    proactiveCompactIdleSeconds: string;
    proactiveCompactMinPct: string;
    fallbackSwitchMode: 'default' | 'auto' | 'ask';
    refusalSwapMode: 'default' | 'off' | 'ask' | 'auto';
    /** Active fallback hold on this thread (read-only status row + Revert). */
    activeFallback?: ActiveLLMFallback | null;
    fallbackRevertBusy?: boolean;
    fallbackRevertError?: string;
    onRevertFallback?: () => void;
  }

  let {
    section = 'provider',
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
    proactiveCompactEnabled = $bindable(),
    proactiveCompactIdleSeconds = $bindable(),
    proactiveCompactMinPct = $bindable(),
    fallbackSwitchMode = $bindable(),
    refusalSwapMode = $bindable(),
    activeFallback = null,
    fallbackRevertBusy = false,
    fallbackRevertError = '',
    onRevertFallback = undefined,
  }: Props = $props();

  const threadModelMeta = $derived(modelsStore.getById(llmModel));
  let providerCatalog = $state<LLMProviderSpec[]>([]);
  let threadProviderGroups = $derived(
    buildProviderGroups(providerCatalog, { includeLocalOpenAISentinel: false })
  );

  let availableModelsState = $state<AvailableModelsState>({
    models: [],
    provider: '',
    loading: false,
  });

  const inheritsGlobalModel = $derived(!llmModel);
  const globalModel = $derived(serverSettingsStore.model || '');
  const fastTierRef = $derived(serverSettingsStore.fastModelResolved || '');
  const smartTierRef = $derived(serverSettingsStore.smartModelResolved || '');

  // Quick-pick: fill this thread's model (and provider, when the tier targets a
  // different one) from the resolved fast/smart tier so the user need not hunt
  // for the model id. The backend only prefixes provider: for cross-provider
  // tiers, so a bare ref maps straight onto the model field.
  function knownProviderIds(): Set<string> {
    return new Set([
      'anthropic', 'openai', 'openrouter', 'google', 'gemini', 'mistral',
      'groq', 'deepseek', 'xai', 'ollama', 'together', 'fireworks', 'custom',
      ...providerCatalog.map((p) => p.id),
    ]);
  }

  function applyTier(tier: 'fast' | 'smart') {
    const ref = tier === 'fast' ? fastTierRef : smartTierRef;
    if (!ref) return;
    const idx = ref.indexOf(':');
    if (idx > 0 && knownProviderIds().has(ref.slice(0, idx))) {
      const provider = ref.slice(0, idx);
      llmProvider = provider;
      threadDisplayProvider = toThreadDisplayProvider(provider, null);
      llmModel = ref.slice(idx + 1);
    } else {
      llmModel = ref;
    }
  }

  // Effort-clamp warning targets the model this thread will actually use
  // (the override, else the inherited global default). Unsupported levels
  // are clamped server-side, so this is a hint, not an error.
  const effortModelMeta = $derived(modelsStore.getById(llmModel || globalModel));
  const effortClampMax = $derived(effortModelMeta?.max_reasoning_effort ?? '');
  const effortSet = $derived(
    supportedEffortSet(effortModelMeta?.supported_reasoning_efforts)
  );
  const showEffortClampHint = $derived(
    effortExceedsModelMax(llmReasoningEffort, effortClampMax)
  );

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

  function isClaudeModel(model: string): boolean {
    const id = (model || '').toLowerCase();
    return id.includes('claude') || id.startsWith('anthropic/');
  }

  // Gateways flagged anthropic_native_for_claude drop Claude's signed thinking on
  // their OpenAI-compatible path; nudge toward the Anthropic Messages route when a
  // Claude model is selected and that route is not already active.
  function showAnthropicRouteNudge(): boolean {
    const spec = providerSpecFor(providerCatalog, getEffectiveProvider());
    return (
      Boolean(spec?.anthropic_native_for_claude) &&
      isClaudeModel(llmModel) &&
      selectedRoute() !== 'anthropic_messages'
    );
  }

  // Base URL + API key apply on the OpenAI-compatible path and on a gateway's
  // Anthropic Messages route (both still target the gateway endpoint).
  function supportsConnectionOverride(): boolean {
    const provider = getEffectiveProvider();
    if (!provider) return false;
    return supportsApiMode(provider) || selectedRoute() === 'anthropic_messages';
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

<div class="tab-body">
  {#if section === 'provider'}
    {#if activeFallback}
      <!-- Active fallback hold (llm-fallback-consent): this thread is pinned
           to its fallback model. Revert mirrors /fallback revert (the PATCH
           clear_active_fallback flag) and leaves the model-facing end note. -->
      <div class="active-fallback-row">
        <span class="active-fallback-text">
          Fallback active: <strong>{activeFallback.model}</strong>
          {activeFallback.reason === 'refusal' ? 'after a refusal' : 'after provider errors'}
          {#if activeFallback.expiresAt}
            (until {new Date(activeFallback.expiresAt).toLocaleString()})
          {:else}
            (until reverted)
          {/if}
        </span>
        <button
          type="button"
          class="revert-fallback-btn"
          onclick={() => onRevertFallback?.()}
          disabled={fallbackRevertBusy || !onRevertFallback}
        >
          Revert to {activeFallback.sourceModel}
        </button>
      </div>
      {#if fallbackRevertError}
        <span class="field-hint revert-error">{fallbackRevertError}</span>
      {/if}
    {/if}

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
        <label class="field-label" for="llm-provider-route">Provider route</label>
        <select id="llm-provider-route" class="field-select" bind:value={llmProviderRoute}>
          <option value="default">Default (inherit global)</option>
          {#each supportedRoutesForProvider(getEffectiveProvider(), providerCatalog) as route}
            <option value={route}>{providerRouteLabel(route)}</option>
          {/each}
        </select>
        <span class="field-hint">Current route: {providerRouteLabel(selectedRoute())}</span>
      </div>
    {/if}

    {#if showAnthropicRouteNudge()}
      <div class="route-nudge">
        <p class="route-nudge-text">
          This gateway's OpenAI-compatible path drops Claude's reasoning (thinking) signatures, so multi-turn reasoning passback breaks. Switch this thread to the Anthropic Messages route for native thinking via the gateway's /v1/messages endpoint.
        </p>
        <button
          type="button"
          class="route-nudge-button"
          onclick={() => (llmProviderRoute = 'anthropic_messages')}
        >
          Use Anthropic Messages route
        </button>
      </div>
    {/if}

    {#if supportsConnectionOverride()}
      <div class="grid-2">
        <div class="field-group">
          <label class="field-label" for="llm-base-url">API base URL</label>
          <input
            id="llm-base-url"
            class="field-input"
            type="text"
            bind:value={llmBaseUrl}
            placeholder={threadDisplayProvider === 'openai_custom' ? 'http://cli-proxy-api-latest:8317/v1' : 'Provider default'}
          />
          <span class="field-hint">Optional OpenAI-compatible endpoint override. Leave empty to inherit.</span>
        </div>
        <div class="field-group">
          <label class="field-label" for="llm-api-key">API key (optional)</label>
          <input
            id="llm-api-key"
            class="field-input"
            type="password"
            bind:value={llmApiKey}
            placeholder="Inherit global provider key"
            autocomplete="off"
          />
          <span class="field-hint">Per-thread key, stored in the NymeriaOS data directory.</span>
        </div>
      </div>
    {/if}

    {#if supportsApiMode(getEffectiveProvider())}
      <div class="field-group">
        <label class="field-label" for="llm-openai-api-mode">API mode</label>
        <select id="llm-openai-api-mode" class="field-select" bind:value={llmOpenAiApiMode}>
          <option value="default">Default (inherit global)</option>
          <option value="chat_completions">Chat Completions</option>
          <option value="responses">Responses API</option>
        </select>
        <span class="field-hint">Responses API is the default for OpenAI-compatible reasoning models. Chat Completions remains a compatibility override.</span>
      </div>
    {/if}

    <div class="grid-2">
      <div class="field-group">
        <label class="field-label" for="llm-fallback-switch-mode">Error fallback consent</label>
        <select id="llm-fallback-switch-mode" class="field-select" bind:value={fallbackSwitchMode}>
          <option value="default">Default (inherit global)</option>
          <option value="auto">Swap silently</option>
          <option value="ask">Ask first</option>
        </select>
        <span class="field-hint">Whether a provider-failure fallback swap asks before switching this thread's model.</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="llm-refusal-swap-mode">Refusal swap</label>
        <select id="llm-refusal-swap-mode" class="field-select" bind:value={refusalSwapMode}>
          <option value="default">Default (inherit global)</option>
          <option value="ask">Ask first</option>
          <option value="auto">Swap silently</option>
          <option value="off">Off (rewind and explain)</option>
        </select>
        <span class="field-hint">What happens when the model's safety classifier refuses a turn (a clean response, not an error).</span>
      </div>
    </div>

    <div class="field-group last">
      <label class="field-label" for="llm-model">Model</label>
      {#if availableModelsState.models.length > 0}
        <select id="llm-model" class="field-select" bind:value={llmModel}>
          <option value="">Default (inherit global{globalModel ? `: ${globalModel}` : ''})</option>
          {#each availableModelsState.models as model}
            <option value={model.id}>{model.name || model.id}</option>
          {/each}
          {#if llmModel && !availableModelsState.models.some((m) => m.id === llmModel)}
            <option value={llmModel}>{llmModel} (custom)</option>
          {/if}
        </select>
        <input
          class="field-input model-text-input"
          type="text"
          bind:value={llmModel}
          placeholder="…or type an exact model ID"
        />
      {:else}
        <input
          id="llm-model"
          class="field-input"
          type="text"
          bind:value={llmModel}
          placeholder={availableModelsState.loading
            ? 'Loading models… or type one (e.g. claude-opus-5)'
            : (globalModel ? `Default: ${globalModel}` : 'Leave empty for global default')}
        />
        {#if availableModelsState.loading}
          <span class="field-hint">Fetching available models from {getEffectiveProvider()}…</span>
        {/if}
      {/if}
      <span class="field-hint">
        {#if inheritsGlobalModel && globalModel}
          Inheriting the global default <strong>{globalModel}</strong>. Pick or type a model to override for this thread only.
        {:else}
          Model lists come from the provider's models endpoint when available. You can still enter an exact model ID manually.
        {/if}
      </span>
      {#if threadModelMeta && llmModel}
        <div class="model-meta-hint">
          <span class="meta-name">{threadModelMeta.name}</span>
          <span class="meta-details">
            {modelsStore.formatContext(threadModelMeta.context_length)} ctx
            {#if threadModelMeta.pricing_prompt != null}&middot; In: {modelsStore.formatPrice(threadModelMeta.pricing_prompt)}{/if}
            {#if threadModelMeta.pricing_completion != null}&middot; Out: {modelsStore.formatPrice(threadModelMeta.pricing_completion)}{/if}
            {#if threadModelMeta.input_modalities.includes('image')}&middot; Vision{/if}
            {#if threadModelMeta.supported_parameters.includes('reasoning')}&middot; Reasoning{/if}
          </span>
        </div>
      {/if}
      {#if fastTierRef || smartTierRef}
        <div class="tier-quickpick">
          <span class="field-hint">Quick tier:</span>
          {#if fastTierRef}
            <button type="button" class="tier-btn" onclick={() => applyTier('fast')} data-tooltip={`Fast tier: ${fastTierRef}`}>Fast</button>
          {/if}
          {#if smartTierRef}
            <button type="button" class="tier-btn" onclick={() => applyTier('smart')} data-tooltip={`Smart tier: ${smartTierRef}`}>Smart</button>
          {/if}
        </div>
      {/if}
    </div>
  {:else if section === 'generation'}
    <div class="grid-2">
      <div class="field-group">
        <label class="field-label" for="llm-use-defaults">Use model defaults</label>
        <select id="llm-use-defaults" class="field-select" bind:value={llmUseModelDefaults}>
          <option value="default">Default (inherit global)</option>
          <option value="true">On</option>
          <option value="false">Off</option>
        </select>
      </div>
      <div class="field-group">
        <label class="field-label" for="llm-temp">Temperature</label>
        <input id="llm-temp" class="field-input" type="number" min="0" max="2" step="0.1" bind:value={llmTemperature} placeholder="Default" disabled={llmUseModelDefaults === 'true'} />
      </div>
      <div class="field-group">
        <label class="field-label" for="llm-max-tokens">Max output tokens</label>
        <input id="llm-max-tokens" class="field-input" type="number" min="1" max="128000" step="1" bind:value={llmMaxTokens} placeholder="Default" />
      </div>
      <div class="field-group">
        <label class="field-label" for="llm-ext-thinking">Extended thinking</label>
        <select id="llm-ext-thinking" class="field-select" bind:value={llmExtendedThinking}>
          <option value="default">Default (inherit global)</option>
          <option value="true">Enabled</option>
          <option value="false">Disabled</option>
        </select>
      </div>
      <div class="field-group">
        <label class="field-label" for="llm-reasoning">Reasoning effort</label>
        <select id="llm-reasoning" class="field-select" bind:value={llmReasoningEffort}>
          <option value="">Default (inherit global)</option>
          {#each REASONING_EFFORT_LEVELS as level (level)}
            {@const unsupported = effortOptionDisabled(level, effortSet, llmReasoningEffort)}
            <option value={level} disabled={unsupported}>
              {reasoningEffortLabel(level)}{unsupported ? ' (not supported)' : ''}
            </option>
          {/each}
        </select>
        {#if showEffortClampHint}
          <div class="effort-clamp-note">
            This model supports up to {reasoningEffortLabel(effortClampMax)}. Higher settings are reduced automatically.
          </div>
        {/if}
      </div>
    </div>
  {:else if section === 'context'}
    <div class="grid-2">
      <div class="field-group">
        <label class="field-label" for="llm-context-length">Context window tokens</label>
        <input id="llm-context-length" class="field-input" type="number" min="1000" max="2000000" step="1" bind:value={llmContextLength} placeholder="Default" />
        <span class="field-hint">Manual local-model context override.</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="llm-ollama-num-ctx">Ollama num_ctx</label>
        <input id="llm-ollama-num-ctx" class="field-input" type="number" min="1000" max="2000000" step="1" bind:value={llmOllamaNumCtx} placeholder="Default" />
        <span class="field-hint">Per-thread Ollama options.num_ctx override.</span>
      </div>
    </div>

    <div class="field-group">
      <label class="field-label" for="thread-compact-mode">Auto-compact trigger</label>
      <select id="thread-compact-mode" class="field-select" bind:value={compactThresholdMode}>
        <option value="default">Default (inherit global)</option>
        <option value="percentage">Percentage of context window</option>
        <option value="tokens">Absolute input-token count</option>
      </select>
      <span class="field-hint">Overrides the global compact trigger for this thread only.</span>
    </div>

    {#if compactThresholdMode === 'percentage'}
      <div class="field-group">
        <label class="field-label" for="thread-compact-pct">Compact threshold (0.05 – 0.95)</label>
        <input id="thread-compact-pct" class="field-input narrow" type="number" min="0.05" max="0.95" step="0.01" bind:value={compactThresholdPct} placeholder="Inherit global" />
      </div>
    {:else if compactThresholdMode === 'tokens'}
      <div class="field-group">
        <label class="field-label" for="thread-compact-tokens">Compact token threshold</label>
        <input id="thread-compact-tokens" class="field-input narrow" type="number" min="1000" max="2000000" step="1000" bind:value={compactThresholdTokens} placeholder="Inherit global" />
        <span class="field-hint">Clamped to the model's context window at runtime.</span>
      </div>
    {/if}

    <div class="field-group" class:last={proactiveCompactEnabled !== 'true'}>
      <label class="field-label" for="thread-proactive-compact">Proactive idle compaction</label>
      <select id="thread-proactive-compact" class="field-select" bind:value={proactiveCompactEnabled}>
        <option value="default">Default (inherit global)</option>
        <option value="true">Enabled</option>
        <option value="false">Disabled</option>
      </select>
      <span class="field-hint">Compact this thread in the background once it sits idle near the compact trigger.</span>
    </div>

    {#if proactiveCompactEnabled === 'true'}
      <div class="grid-2">
        <div class="field-group last">
          <label class="field-label" for="thread-proactive-idle">Idle delay (seconds)</label>
          <input id="thread-proactive-idle" class="field-input" type="number" min="30" max="3600" step="10" bind:value={proactiveCompactIdleSeconds} placeholder="Inherit global" />
        </div>
        <div class="field-group last">
          <label class="field-label" for="thread-proactive-pct">Occupancy floor (% of trigger)</label>
          <input id="thread-proactive-pct" class="field-input" type="number" min="10" max="100" step="5" bind:value={proactiveCompactMinPct} placeholder="Inherit global" />
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .tab-body {
    padding: var(--spacing-lg);
  }

  .grid-2 {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--spacing-md);
  }
  .grid-2 .field-group { margin-bottom: var(--spacing-md); }

  .field-group {
    margin-bottom: var(--spacing-md);
  }
  .field-group.last {
    margin-bottom: 0;
  }

  .field-label {
    display: block;
    font-size: var(--font-size-sm);
    font-weight: 500;
    /* §4 — labels recede behind the input value (which is --text-primary),
       so the eye finds the answer before the question. */
    color: var(--text-secondary);
    margin-bottom: 4px;
  }

  .field-hint {
    display: block;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin-top: var(--spacing-xs);
    /* §5 — reading text capped to 60ch so multi-line hints stay readable
       on wide displays instead of stretching the full panel width. */
    max-width: 60ch;
  }

  .active-fallback-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    margin-bottom: var(--spacing-md);
    padding: 8px 12px;
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--bg-elevated) 88%, var(--warning, var(--accent-primary)));
    border: 1px solid var(--border-subtle);
  }

  .active-fallback-text {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .revert-fallback-btn {
    padding: 5px 12px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-primary);
    font-size: var(--font-size-xs);
    font-weight: 600;
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }

  .revert-fallback-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--bg-elevated) 80%, var(--accent-primary));
    border-color: var(--accent-primary);
  }

  .revert-fallback-btn:disabled {
    opacity: 0.55;
    cursor: default;
  }

  .revert-error {
    color: var(--error, #e5484d);
    margin-bottom: var(--spacing-md);
  }

  .tier-quickpick {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    margin-top: var(--spacing-xs);
  }
  .tier-quickpick .field-hint {
    margin-top: 0;
  }
  .tier-btn {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: border-color var(--transition-fast);
  }
  .tier-btn:hover {
    border-color: var(--accent-primary);
  }

  .route-nudge {
    margin-bottom: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--error);
    border-radius: var(--radius-sm);
    background: rgba(var(--error-rgb), 0.08);
  }
  .route-nudge-text {
    margin: 0 0 var(--spacing-sm) 0;
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    line-height: 1.45;
  }
  .route-nudge-button {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: border-color var(--transition-fast);
  }
  .route-nudge-button:hover {
    border-color: var(--accent-primary);
  }

  .effort-clamp-note {
    margin-top: var(--spacing-xs);
    padding: var(--spacing-sm) var(--spacing-md);
    border: 1px solid var(--warning);
    border-radius: var(--radius-sm);
    background: rgba(var(--warning-rgb), 0.08);
    font-size: var(--font-size-xs);
    color: var(--text-primary);
    line-height: 1.45;
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
  .field-input.narrow { max-width: 280px; }

  .field-input:focus,
  .field-select:focus {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px var(--accent-tint-bg);
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
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
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
