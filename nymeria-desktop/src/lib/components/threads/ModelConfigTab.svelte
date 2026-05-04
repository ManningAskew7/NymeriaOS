<script lang="ts">
  import { modelsStore } from '$lib/stores/models.svelte';
  import { serverSettingsStore } from '$lib/stores/serverSettings.svelte';
  import { clearAvailableModels, loadAvailableModels, type AvailableModelsState } from '$lib/utils/models';
  import {
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    fromThreadDisplayProvider,
    supportsOpenAiApiMode,
    type ThreadDisplayProvider,
  } from '$lib/utils/providerMapping';

  interface Props {
    threadDisplayProvider: ThreadDisplayProvider;
    llmProvider: string;
    llmModel: string;
    llmBaseUrl: string;
    llmApiKey: string;
    llmTemperature: string;
    llmMaxTokens: string;
    llmExtendedThinking: 'default' | 'true' | 'false';
    llmReasoningEffort: string;
    llmUseModelDefaults: 'default' | 'true' | 'false';
    llmOpenAiApiMode: 'default' | 'chat_completions' | 'responses';
  }

  let {
    threadDisplayProvider = $bindable(),
    llmProvider = $bindable(),
    llmModel = $bindable(),
    llmBaseUrl = $bindable(),
    llmApiKey = $bindable(),
    llmTemperature = $bindable(),
    llmMaxTokens = $bindable(),
    llmExtendedThinking = $bindable(),
    llmReasoningEffort = $bindable(),
    llmUseModelDefaults = $bindable(),
    llmOpenAiApiMode = $bindable(),
  }: Props = $props();

  const threadModelMeta = $derived(modelsStore.getById(llmModel));

  let availableModelsState = $state<AvailableModelsState>({
    models: [],
    provider: '',
    loading: false,
  });

  function getEffectiveProvider(): string {
    return llmProvider || serverSettingsStore.provider || '';
  }

  $effect(() => {
    const { provider } = fromThreadDisplayProvider(threadDisplayProvider);
    llmProvider = provider;
    if (threadDisplayProvider === 'openai_custom') {
      clearAvailableModels(availableModelsState);
      return;
    }
    const ep = getEffectiveProvider();
    void loadAvailableModels(ep, availableModelsState);
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
    <select id="llm-provider" class="field-select" bind:value={threadDisplayProvider}>
      <option value="">Default (inherit global)</option>
      <option value="anthropic_proxy">Anthropic (Subscription)</option>
      <option value="anthropic_direct">Anthropic (Direct API)</option>
      <option value="openai">OpenAI</option>
      <option value="openrouter">OpenRouter</option>
      <option value="openai_custom">OpenAI (Custom base URL)</option>
    </select>
  </div>

  {#if threadDisplayProvider === 'openai_custom'}
    <div class="field-group">
      <label class="field-label" for="llm-base-url">API Base URL</label>
      <input
        id="llm-base-url"
        class="field-input"
        type="text"
        bind:value={llmBaseUrl}
        placeholder="http://cli-proxy-api-latest:8317/v1"
      />
      <span class="field-hint">
        Any OpenAI-compatible endpoint reachable from inside the Nymeria api container, such as a CLIProxy sidecar (e.g. <code>cli-proxy-api-latest:8317/v1</code>) or a local inference server (<code>host.docker.internal:8080/v1</code>).
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
        Required when pointing at a CLIProxy sidecar with its own <code>api-keys</code> list (e.g. <code>cpx-latest-local-test</code> for the GPT-5.5 sidecar). Stored per-thread in the Nymeria data directory.
      </span>
    </div>
  {/if}

  {#if supportsOpenAiApiMode(getEffectiveProvider())}
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
    {#if threadDisplayProvider === 'openai_custom'}
      <input
        id="llm-model"
        class="field-input"
        type="text"
        bind:value={llmModel}
        placeholder="Model name (e.g. gpt-5.5, local-llm)"
      />
      <span class="field-hint">
        The model name the endpoint reports (e.g. <code>gpt-5.5</code> for the CLIProxy sidecar, or the <code>--alias</code> flag value for a local server).
      </span>
    {:else if availableModelsState.models.length > 0}
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
