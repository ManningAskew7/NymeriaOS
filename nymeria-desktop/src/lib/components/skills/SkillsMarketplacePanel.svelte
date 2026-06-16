<script lang="ts">
  import { trapFocus } from '$lib/actions/focus';
  import { Button, Icon } from '$lib/components/common';
  import InlineLoader from '$lib/components/common/InlineLoader.svelte';
  import { skillsStore } from '$lib/stores/skills.svelte';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import type { SkillMarketplaceSource } from '$lib/types';

  interface Props {
    onClose: () => void;
  }

  let { onClose }: Props = $props();

  let source = $state<SkillMarketplaceSource>('anthropic');
  let query = $state('');
  let scope = $state<'user' | 'global'>('user');
  let installError = $state<string | null>(null);
  let debounceHandle: ReturnType<typeof setTimeout> | null = null;

  // Fire initial search on mount (empty query returns full index).
  $effect(() => {
    skillsStore.loadInstalled();
    void skillsStore.searchMarketplace(source, '');
  });

  function scheduleSearch() {
    if (debounceHandle) clearTimeout(debounceHandle);
    debounceHandle = setTimeout(() => {
      void skillsStore.searchMarketplace(source, query);
    }, 250);
  }

  function onSourceChange(newSource: SkillMarketplaceSource) {
    source = newSource;
    query = '';
    if (newSource === 'anthropic') {
      void skillsStore.searchMarketplace(newSource, '');
    }
  }

  async function handleInstall(name: string) {
    installError = null;
    try {
      await skillsStore.install(name, source, scope);
    } catch (e) {
      installError = humanizeErrorText(e, { action: 'install', resource: `the "${name}" skill` });
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div
  class="marketplace-backdrop"
>
  <button
    class="marketplace-backdrop-button"
    type="button"
    tabindex="-1"
    aria-label="Close skills marketplace"
    onclick={onClose}
  ></button>
  <div class="marketplace-panel" role="dialog" aria-modal="true" aria-labelledby="marketplace-title" tabindex="-1" use:trapFocus>
    <div class="marketplace-header">
      <h3 id="marketplace-title">Skills Marketplace</h3>
      <button class="close-btn" onclick={onClose} type="button" aria-label="Close">
        <Icon name="x" size={20} />
      </button>
    </div>

    <div class="marketplace-controls">
      <div class="control">
        <label for="mp-source">Source</label>
        <select
          id="mp-source"
          bind:value={source}
          onchange={() => onSourceChange(source)}
        >
          <option value="anthropic">Anthropic (anthropics/skills)</option>
          <option value="clawhub" disabled>ClawHub (coming soon)</option>
          <option value="git" disabled>Git URL (coming soon)</option>
        </select>
      </div>
      <div class="control control-search">
        <label for="mp-query">Search</label>
        <input
          id="mp-query"
          type="text"
          placeholder="Search by name or description…"
          bind:value={query}
          oninput={scheduleSearch}
        />
      </div>
      <div class="control">
        <label for="mp-scope">Install to</label>
        <select id="mp-scope" bind:value={scope}>
          <option value="user">User scope (me only)</option>
          <option value="global">Global scope (all users)</option>
        </select>
      </div>
    </div>

    {#if installError}
      <div class="banner banner-error"><Icon name="error" size={14} /><span>{installError}</span></div>
    {/if}
    {#if skillsStore.marketplaceError}
      <div class="banner banner-error"><Icon name="error" size={14} /><span>{skillsStore.marketplaceError}</span></div>
    {/if}

    <div class="marketplace-results">
      {#if skillsStore.marketplaceSearching}
        <p class="status"><InlineLoader text={`Loading skills from ${source}…`} /></p>
      {:else if skillsStore.marketplaceResults.length === 0}
        <p class="status">No skills matched.</p>
      {:else}
        {#each skillsStore.marketplaceResults as entry (entry.name)}
          {@const installed = skillsStore.isInstalled(entry.name)}
          {@const pendingState = skillsStore.isPending(entry.name)}
          <div class="result-row" class:installed>
            <div class="result-body">
              <div class="result-title">
                <span class="result-name">{entry.name}</span>
                <span class="result-source">{entry.source}</span>
                {#if installed}<span class="badge badge-installed">Installed</span>{/if}
              </div>
              <p class="result-desc">{entry.description}</p>
              {#if entry.repo_url}
                <a class="result-link" href={entry.repo_url} target="_blank" rel="noopener">
                  View source ↗
                </a>
              {/if}
            </div>
            <div class="result-actions">
              {#if installed}
                <Button variant="ghost" size="sm" disabled>Installed</Button>
              {:else if pendingState === 'installing'}
                <Button variant="primary" size="sm" disabled>Installing…</Button>
              {:else}
                <Button variant="primary" size="sm" onclick={() => handleInstall(entry.name)}>
                  Install
                </Button>
              {/if}
            </div>
          </div>
        {/each}
      {/if}
    </div>
  </div>
</div>

<style>
  .marketplace-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .marketplace-backdrop-button {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: transparent;
  }

  .marketplace-panel {
    position: relative;
    background: var(--bg-elevated);
    border-radius: var(--radius-lg);
    width: 90vw;
    max-width: 780px;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    /* §7 — floating marketplace overlay: shadow alone defines elevation;
       border would be redundant chrome. Tokenized to --shadow-xl
       (matches the existing 0 20px 60px / 0.4 within rounding). */
    box-shadow: var(--shadow-xl);
  }

  .marketplace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
  }

  .close-btn {
    padding: var(--spacing-xs);
    color: var(--text-secondary);
    border-radius: var(--radius-sm);
    transition: all var(--transition-fast);
  }

  .close-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .marketplace-controls {
    display: grid;
    grid-template-columns: 1fr 2fr 1fr;
    gap: var(--spacing-md);
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-subtle);
  }

  .control {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .control label {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .control select,
  .control input {
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    padding: 6px 10px;
    font-size: var(--font-size-sm);
  }

  .banner {
    padding: var(--spacing-sm) var(--spacing-lg);
    font-size: var(--font-size-sm);
  }
  .banner-error {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    background: color-mix(in srgb, var(--error) 10%, transparent);
    color: var(--error);
  }

  .marketplace-results {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-md) var(--spacing-lg);
  }

  .status {
    color: var(--text-muted);
    font-size: var(--font-size-sm);
    text-align: center;
    padding: var(--spacing-lg) 0;
  }

  .result-row {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-subtle);
  }
  .result-row:last-child {
    border-bottom: none;
  }
  .result-row.installed {
    opacity: 0.75;
  }

  .result-body {
    flex: 1;
    min-width: 0;
  }

  .result-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin-bottom: 4px;
  }

  .result-name {
    font-weight: 600;
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    font-family: var(--font-mono);
  }

  .result-source {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    text-transform: lowercase;
  }

  .badge {
    font-size: var(--font-size-xs);
    padding: 1px 6px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    /* Optical centering: lowercase text in a pill-shaped chip reads as
       left-shifted. A small positive text-indent nudges the text into the
       optical center without changing the chip's size or rounding. */
    text-indent: 1px;
  }
  .badge-installed {
    background: var(--accent-tint-bg);
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .result-desc {
    margin: 0;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    line-height: 1.45;
  }

  .result-link {
    display: inline-block;
    margin-top: 4px;
    font-size: var(--font-size-xs);
    color: var(--accent-primary);
    text-decoration: none;
  }
  .result-link:hover {
    text-decoration: underline;
  }

  .result-actions {
    flex-shrink: 0;
  }

</style>
