<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/services/api.svelte';
  import type { CLIProxyProviderInfo, LLMProviderSpec, RagCatalog, ServerSettings } from '$lib/types';
  import { configStore } from '$lib/stores/config.svelte';
  import { onboardingStore } from '$lib/stores/onboarding.svelte';
  import { themes } from '$lib/themes';
  import { contextStrategyFromSettings } from '$lib/utils/onboardingSetup';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';
  import ConnectSection from './ConnectSection.svelte';
  import ProviderSection from './ProviderSection.svelte';
  import TiersSection from './TiersSection.svelte';
  import AgentSection from './AgentSection.svelte';
  import RagSection from './RagSection.svelte';
  import IntegrationsSection from './IntegrationsSection.svelte';
  import AppearanceSection from './AppearanceSection.svelte';

  type SectionId =
    | 'connect'
    | 'provider'
    | 'tiers'
    | 'agent'
    | 'rag'
    | 'integrations'
    | 'appearance';

  let view = $state<'welcome' | 'hub'>('welcome');
  let activeSection = $state<SectionId>('connect');

  let serverSettings = $state<ServerSettings | null>(null);
  // GET /settings is admin-only; a non-admin account degrades to read-nothing
  // and the server sections explain themselves instead of erroring.
  let settingsForbidden = $state(false);
  let providerCatalog = $state<LLMProviderSpec[]>([]);
  let cliproxyCatalog = $state<CLIProxyProviderInfo[]>([]);
  let ragCatalog = $state<RagCatalog | null>(null);

  const connected = $derived(configStore.isConfigured && !!configStore.identity);
  const isAdmin = $derived(configStore.identity?.role === 'admin');

  // Arm the session flag at mount, BEFORE any connection adoption can flip
  // needsSetup, so the root route keeps rendering this surface mid-session.
  onboardingStore.begin();

  onMount(() => {
    if (connected) void loadAll();
  });

  async function loadAll() {
    await Promise.all([refreshSettings(), loadCatalogs()]);
  }

  async function refreshSettings() {
    try {
      serverSettings = await api.getServerSettings();
      settingsForbidden = false;
    } catch {
      serverSettings = null;
      settingsForbidden = true;
    }
  }

  async function loadCatalogs() {
    const [providers, cliproxy, rag] = await Promise.all([
      api.getLLMProviderCatalog(),
      api.getCLIProxyCatalog(),
      api.getRagCatalog(),
    ]);
    providerCatalog = providers;
    if (cliproxy.length > 0) cliproxyCatalog = cliproxy;
    ragCatalog = rag;
  }

  async function handleConnected() {
    view = 'hub';
    activeSection = 'provider';
    await loadAll();
  }

  function enterHub() {
    view = 'hub';
    activeSection = connected ? 'provider' : 'connect';
  }

  function finish() {
    onboardingStore.finish();
  }

  const CONTEXT_LABELS: Record<string, string> = {
    compact_tokens: 'Compact by tokens',
    compact_percent: 'Compact by percent',
    sliding_window: 'Sliding window',
    none: 'No management',
  };

  interface SectionMeta {
    id: SectionId;
    group: 'Connect' | 'Configure' | 'Personalize';
    label: string;
    title: string;
    description: string;
    adminOnly: boolean;
  }

  const SECTIONS: SectionMeta[] = [
    {
      id: 'connect',
      group: 'Connect',
      label: 'Backend',
      title: 'Backend connection',
      description:
        'Point this app at your NymeriaOS backend. This is the one step the app needs; everything after it is optional.',
      adminOnly: false,
    },
    {
      id: 'provider',
      group: 'Configure',
      label: 'LLM Provider',
      title: 'LLM provider',
      description:
        'The model Nymeria thinks with. Same choices as the command-line wizard: a direct API key, a subscription through CLIProxy, or a local model.',
      adminOnly: true,
    },
    {
      id: 'tiers',
      group: 'Configure',
      label: 'Models & Tiers',
      title: 'Models and tiers',
      description:
        'Reasoning effort, sampling, and the fast/smart/background aliases features resolve against.',
      adminOnly: true,
    },
    {
      id: 'agent',
      group: 'Configure',
      label: 'Agent',
      title: 'Agent behavior',
      description: 'Context management, memory and tool limits, and your timezone.',
      adminOnly: true,
    },
    {
      id: 'rag',
      group: 'Configure',
      label: 'RAG',
      title: 'Semantic memory (RAG)',
      description:
        'The embedder and reranker behind semantic recall, ranked by an internal retrieval eval. The free local stack works with no keys.',
      adminOnly: true,
    },
    {
      id: 'integrations',
      group: 'Configure',
      label: 'Integrations',
      title: 'Integration keys',
      description: 'Optional keys that upgrade the built-in web search, fetch, and image tools.',
      adminOnly: true,
    },
    {
      id: 'appearance',
      group: 'Personalize',
      label: 'Appearance',
      title: 'Appearance',
      description: 'Pick a theme for this device.',
      adminOnly: false,
    },
  ];

  const NAV_GROUPS = ['Connect', 'Configure', 'Personalize'] as const;

  const activeMeta = $derived(SECTIONS.find((s) => s.id === activeSection) ?? SECTIONS[0]);

  function sectionValue(id: SectionId): string {
    switch (id) {
      case 'connect':
        return connected ? 'Connected' : 'Required';
      case 'provider':
        return serverSettings ? `${serverSettings.llm_provider} · ${serverSettings.llm_model}` : '';
      case 'tiers':
        return serverSettings ? `${serverSettings.llm_reasoning_effort ?? 'default'} effort` : '';
      case 'agent':
        return serverSettings
          ? (CONTEXT_LABELS[contextStrategyFromSettings(serverSettings)] ?? '')
          : '';
      case 'rag':
        return serverSettings ? serverSettings.embedding_model : '';
      case 'integrations':
        return '';
      case 'appearance':
        return themes[configStore.theme]?.name ?? '';
    }
  }
</script>

<div class="setup-surface">
  <header class="surface-header">
    <div class="brand">
      <img src="/wolfhead-transparent.png" alt="" class="brand-mark" />
      <span class="brand-name">Nymeria<span class="brand-os">OS</span></span>
      <span class="brand-context">Setup</span>
    </div>
    {#if view === 'hub'}
      <Button variant="ghost" onclick={finish} disabled={!connected}
        dataTooltip={connected ? undefined : 'Connect to a backend first'}>
        Skip setup
        <Icon name="chevronRight" size={14} />
      </Button>
    {/if}
  </header>

  {#if view === 'welcome'}
    <main class="welcome">
      <div class="welcome-inner">
        <h1>Welcome to NymeriaOS</h1>
        <p class="welcome-sub">
          A personal AI assistant platform you run yourself. Setup takes about two minutes:
          connect this app to your backend, pick the model it thinks with, and adjust anything
          else you care about. Every step here also lives in Settings, so nothing is locked in.
        </p>
        <ul class="welcome-points">
          <li>
            <Icon name="server" size={16} />
            <span><strong>Connect</strong> to your backend with its URL and account token. The only required step.</span>
          </li>
          <li>
            <Icon name="key" size={16} />
            <span><strong>Choose a model</strong> from an API key, a Claude or ChatGPT subscription, or a local model.</span>
          </li>
          <li>
            <Icon name="settings" size={16} />
            <span><strong>Make it yours</strong>: memory, retrieval, limits, and appearance. All optional.</span>
          </li>
        </ul>
        <div class="welcome-actions">
          <Button variant="primary" size="lg" onclick={enterHub}>
            {connected ? 'Review setup' : 'Get started'}
          </Button>
          <Button
            variant="ghost"
            size="lg"
            onclick={finish}
            disabled={!connected}
            dataTooltip={connected ? undefined : 'Connect to a backend first'}
          >
            Skip setup
          </Button>
        </div>
        {#if !connected}
          <p class="welcome-note">
            Already set up with <code>nymeria init</code>? Connect once and every value below
            shows what the wizard configured.
          </p>
        {/if}
      </div>
    </main>
  {:else}
    <div class="hub">
      <aside class="hub-rail">
        {#each NAV_GROUPS as group (group)}
          <div class="rail-group">
            <span class="rail-group-label">{group}</span>
            {#each SECTIONS.filter((s) => s.group === group) as section (section.id)}
              <button
                type="button"
                class="rail-item"
                class:active={activeSection === section.id}
                aria-current={activeSection === section.id ? 'page' : undefined}
                onclick={() => (activeSection = section.id)}
              >
                <span class="rail-item-label">{section.label}</span>
                {#if section.id === 'connect'}
                  {#if connected}
                    <span class="rail-state ok"><Icon name="success" size={13} /></span>
                  {:else}
                    <span class="rail-state required">Required</span>
                  {/if}
                {:else if sectionValue(section.id)}
                  <span class="rail-value">{sectionValue(section.id)}</span>
                {/if}
              </button>
            {/each}
          </div>
        {/each}
      </aside>

      <main class="hub-content">
        <div class="section-head">
          <h2>{activeMeta.title}</h2>
          <p>{activeMeta.description}</p>
        </div>

        <div class="section-body">
          {#if activeMeta.adminOnly && !connected}
            <div class="sf-result">
              <Icon name="info" size={16} />
              <span>Connect to a backend first; this section configures the server.</span>
            </div>
          {:else if activeMeta.adminOnly && !isAdmin}
            <div class="sf-result">
              <Icon name="info" size={16} />
              <span>
                Server settings are managed by an admin account on this backend. You can still
                pick your appearance, then finish.
              </span>
            </div>
          {:else if activeSection === 'connect'}
            <ConnectSection onConnected={handleConnected} />
          {:else if activeSection === 'provider'}
            <ProviderSection
              settings={serverSettings}
              {providerCatalog}
              {cliproxyCatalog}
              refresh={refreshSettings}
            />
          {:else if activeSection === 'tiers'}
            <TiersSection settings={serverSettings} refresh={refreshSettings} />
          {:else if activeSection === 'agent'}
            <AgentSection settings={serverSettings} refresh={refreshSettings} />
          {:else if activeSection === 'rag'}
            <RagSection settings={serverSettings} {ragCatalog} refresh={refreshSettings} />
          {:else if activeSection === 'integrations'}
            <IntegrationsSection settings={serverSettings} refresh={refreshSettings} />
          {:else if activeSection === 'appearance'}
            <AppearanceSection />
          {/if}

          {#if activeMeta.adminOnly && connected && isAdmin && settingsForbidden}
            <div class="sf-result error">
              <Icon name="error" size={16} />
              <span>Server settings could not be loaded; check the backend and try again.</span>
            </div>
          {/if}
        </div>
      </main>
    </div>

    <footer class="surface-footer">
      <span class="footer-summary">
        {#if serverSettings}
          {serverSettings.llm_provider} · {serverSettings.llm_model}
        {:else if connected}
          Connected to {configStore.apiUrl}
        {:else}
          Not connected
        {/if}
      </span>
      <Button
        variant="primary"
        onclick={finish}
        disabled={!connected}
        dataTooltip={connected ? undefined : 'Connect to a backend first'}
      >
        Finish setup
      </Button>
    </footer>
  {/if}
</div>

<style>
  .setup-surface {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    background: var(--bg-base);
    color: var(--text-primary);
  }

  /* ------------------------------------------------------------------ */
  /* Shared form vocabulary for the section components (sf-*). Declared  */
  /* once here with :global so every section renders identically without */
  /* seven copies of the same rules (flow-consistency, repair guide §7). */
  /* ------------------------------------------------------------------ */
  .setup-surface :global(.sf-field) {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .setup-surface :global(.sf-label) {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-secondary);
  }

  .setup-surface :global(.sf-input) {
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    color: var(--text-primary);
    font-size: var(--font-size-base);
    font-family: inherit;
  }

  .setup-surface :global(.sf-input:focus) {
    outline: none;
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }

  .setup-surface :global(.sf-hint) {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
  }

  .setup-surface :global(.sf-hint code) {
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    background: var(--bg-elevated-2);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
  }

  .setup-surface :global(.sf-actions) {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-top: var(--spacing-xs);
  }

  .setup-surface :global(.sf-result) {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    background: var(--bg-elevated-2);
    color: var(--text-secondary);
  }

  .setup-surface :global(.sf-result.success) {
    background: rgba(var(--success-rgb), 0.14);
    color: var(--success);
  }

  .setup-surface :global(.sf-result.error) {
    background: rgba(var(--error-rgb), 0.14);
    color: var(--error);
  }

  .setup-surface :global(.sf-grid) {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--spacing-sm-plus);
  }

  .setup-surface :global(.sf-grid.two) {
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }

  .setup-surface :global(.sf-sub) {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .setup-surface :global(.sf-sub > span) {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  /* ------------------------------------------------------------------ */

  .surface-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-xl);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm-plus);
  }

  .brand-mark {
    width: 28px;
    height: 28px;
    object-fit: contain;
  }

  .brand-name {
    font-family: var(--font-logo);
    font-size: var(--font-size-lg);
    font-weight: 650;
    letter-spacing: -0.01em;
  }

  .brand-os {
    color: var(--accent-primary);
    font-weight: 500;
  }

  .brand-context {
    margin-left: var(--spacing-xs);
    padding: var(--spacing-2xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    background: var(--bg-elevated-2);
    color: var(--text-muted);
    font-size: var(--font-size-2xs);
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  /* Welcome ----------------------------------------------------------- */

  .welcome {
    flex: 1;
    overflow-y: auto;
    display: flex;
    align-items: center;
  }

  .welcome-inner {
    width: min(620px, calc(100% - 2 * var(--spacing-xl)));
    margin: 0 auto;
    padding: var(--spacing-2xl) 0;
  }

  .welcome h1 {
    margin: 0 0 var(--spacing-md);
    font-family: var(--font-heading, var(--font-sans));
    font-size: 2.1rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }

  .welcome-sub {
    margin: 0 0 var(--spacing-xl);
    max-width: 56ch;
    color: var(--text-secondary);
    font-size: var(--font-size-base);
    line-height: 1.65;
  }

  .welcome-points {
    list-style: none;
    margin: 0 0 var(--spacing-xl);
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm-plus);
  }

  .welcome-points li {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm-plus);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.5;
  }

  .welcome-points li :global(svg) {
    color: var(--accent-primary);
    flex-shrink: 0;
    margin-top: 2px;
  }

  .welcome-points strong {
    color: var(--text-primary);
  }

  .welcome-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm-plus);
  }

  .welcome-note {
    margin: var(--spacing-lg) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .welcome-note code {
    font-family: var(--font-mono);
    font-size: var(--font-size-2xs);
    background: var(--bg-elevated-2);
    padding: 1px 4px;
    border-radius: var(--radius-sm);
  }

  /* Hub --------------------------------------------------------------- */

  .hub {
    flex: 1;
    display: grid;
    grid-template-columns: 250px minmax(0, 1fr);
    min-height: 0;
  }

  .hub-rail {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    padding: var(--spacing-lg) var(--spacing-md);
    border-right: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    overflow-y: auto;
  }

  .rail-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
  }

  .rail-group-label {
    padding: 0 var(--spacing-sm);
    margin-bottom: var(--spacing-xs);
    font-size: var(--font-size-3xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-muted);
  }

  .rail-item {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 2px;
    padding: var(--spacing-sm) var(--spacing-sm);
    border: none;
    border-radius: var(--radius-md);
    background: transparent;
    color: var(--text-secondary);
    text-align: left;
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .rail-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .rail-item.active {
    background: var(--accent-tint-bg);
    color: var(--text-primary);
  }

  .rail-item-label {
    font-size: var(--font-size-sm);
    font-weight: 500;
  }

  .rail-item.active .rail-item-label {
    font-weight: 600;
  }

  .rail-value {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .rail-state {
    align-self: flex-start;
  }

  .rail-state.ok {
    color: var(--success);
    display: inline-flex;
  }

  .rail-state.required {
    font-size: var(--font-size-3xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--warning);
  }

  .hub-content {
    overflow-y: auto;
    padding: var(--spacing-xl) var(--spacing-2xl);
  }

  .section-head {
    margin-bottom: var(--spacing-lg);
    max-width: 62ch;
  }

  .section-head h2 {
    margin: 0 0 var(--spacing-xs);
    font-family: var(--font-heading, var(--font-sans));
    font-size: 1.35rem;
    font-weight: 650;
    letter-spacing: -0.01em;
  }

  .section-head p {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.55;
  }

  .section-body {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
    max-width: 680px;
    padding-bottom: var(--spacing-2xl);
  }

  .surface-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm-plus) var(--spacing-xl);
    border-top: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    flex-shrink: 0;
  }

  .footer-summary {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 760px) {
    .hub {
      grid-template-columns: 1fr;
      grid-template-rows: auto minmax(0, 1fr);
    }

    .hub-rail {
      flex-direction: row;
      flex-wrap: wrap;
      gap: var(--spacing-sm);
      border-right: none;
      border-bottom: 1px solid var(--border-subtle);
      padding: var(--spacing-sm-plus) var(--spacing-md);
    }

    .rail-group {
      flex-direction: row;
      gap: var(--spacing-xs);
    }

    .rail-group-label {
      display: none;
    }

    .rail-value,
    .rail-state {
      display: none;
    }

    .hub-content {
      padding: var(--spacing-lg) var(--spacing-md);
    }

    .surface-footer {
      padding: var(--spacing-sm-plus) var(--spacing-md);
    }
  }
</style>
