<script lang="ts">
  import { api } from '$lib/services/api.svelte';
  import type { RagCatalog, ServerSettings } from '$lib/types';
  import { humanizeErrorText } from '$lib/services/api/humanizeError';
  import {
    buildRagUpdate,
    embedderIdForSettings,
    rerankerIdForSettings,
  } from '$lib/utils/onboardingSetup';
  import Button from '$lib/components/common/Button.svelte';
  import Icon from '$lib/components/common/Icon.svelte';

  interface Props {
    settings: ServerSettings | null;
    ragCatalog: RagCatalog | null;
    refresh: () => Promise<void>;
  }

  let { settings, ragCatalog, refresh }: Props = $props();

  let embedderId = $state('');
  let rerankerId = $state('none');
  let retrievalMode = $state<'hybrid' | 'vector'>('hybrid');
  let embeddingKey = $state('');
  let rerankKey = $state('');

  let saveStatus = $state<'idle' | 'saving' | 'success' | 'error'>('idle');
  let saveMessage = $state('');
  let initializedFor = $state<ServerSettings | null>(null);

  const selectedEmbedder = $derived(
    ragCatalog?.embedders.find((o) => o.id === embedderId) ?? null
  );
  const selectedReranker = $derived(
    ragCatalog?.rerankers.find((o) => o.id === rerankerId) ?? null
  );
  // One Voyage key powers both sides; surface that instead of asking twice.
  const sharedKeyVendor = $derived(
    !!selectedEmbedder?.key_vendor &&
      selectedEmbedder.key_vendor === selectedReranker?.key_vendor
  );

  $effect(() => {
    if (!settings || !ragCatalog || settings === initializedFor) return;
    initializedFor = settings;
    embedderId = embedderIdForSettings(ragCatalog, settings) ?? '';
    rerankerId = rerankerIdForSettings(ragCatalog, settings) ?? 'none';
    retrievalMode = settings.rag_retrieval_mode === 'vector' ? 'vector' : 'hybrid';
  });

  function tierTitle(tier: string): string {
    if (tier === 'premium') return 'Premium';
    if (tier === 'value') return 'Value';
    if (tier === 'local') return 'Local';
    return '';
  }

  async function handleSave() {
    if (!ragCatalog || !embedderId) return;
    saveStatus = 'saving';
    saveMessage = '';
    try {
      const updates = buildRagUpdate({
        catalog: ragCatalog,
        embedderId,
        rerankerId,
        retrievalMode,
        embeddingKey,
        rerankKey,
      });
      if (!updates) {
        saveStatus = 'error';
        saveMessage = 'Pick an embedder first.';
        return;
      }
      const result = await api.updateServerSettings(updates);
      saveStatus = 'success';
      saveMessage = result.restart_required
        ? 'RAG settings saved. Restart the backend to re-index with the new embedder.'
        : 'RAG settings saved and applied.';
      embeddingKey = '';
      rerankKey = '';
      await refresh();
    } catch (e) {
      saveStatus = 'error';
      saveMessage = humanizeErrorText(e, { action: 'save', resource: 'the RAG settings' });
    }
  }
</script>

{#if !ragCatalog}
  <p class="sf-hint">The RAG catalog could not be loaded from the backend.</p>
{:else}
  <div class="sf-field">
    <span class="sf-label">Embedder</span>
    <div class="option-list" role="radiogroup" aria-label="Embedder">
      {#each ragCatalog.embedders as option (option.id)}
        <button
          type="button"
          role="radio"
          aria-checked={embedderId === option.id}
          class="rag-option"
          class:selected={embedderId === option.id}
          onclick={() => (embedderId = option.id)}
        >
          <span class="rag-head">
            <strong>{option.label}</strong>
            <span class="rag-tags">
              {#if tierTitle(option.tier)}<span class="rag-tier {option.tier}">{tierTitle(option.tier)}</span>{/if}
              {#if option.eval_tag}<span class="rag-eval">{option.eval_tag}</span>{/if}
            </span>
          </span>
          <small>{option.description}</small>
        </button>
      {/each}
    </div>
  </div>

  <div class="sf-field">
    <span class="sf-label">Reranker</span>
    <div class="option-list" role="radiogroup" aria-label="Reranker">
      {#each ragCatalog.rerankers as option (option.id)}
        <button
          type="button"
          role="radio"
          aria-checked={rerankerId === option.id}
          class="rag-option"
          class:selected={rerankerId === option.id}
          onclick={() => (rerankerId = option.id)}
        >
          <span class="rag-head">
            <strong>{option.label}</strong>
            <span class="rag-tags">
              {#if tierTitle(option.tier)}<span class="rag-tier {option.tier}">{tierTitle(option.tier)}</span>{/if}
              {#if option.eval_tag}<span class="rag-eval">{option.eval_tag}</span>{/if}
            </span>
          </span>
          <small>{option.description}</small>
        </button>
      {/each}
    </div>
  </div>

  <div class="sf-field">
    <span class="sf-label">Retrieval mode</span>
    <div class="mode-row" role="radiogroup" aria-label="Retrieval mode">
      <button
        type="button"
        role="radio"
        aria-checked={retrievalMode === 'hybrid'}
        class="mode-chip"
        class:selected={retrievalMode === 'hybrid'}
        onclick={() => (retrievalMode = 'hybrid')}
      >
        Hybrid (vector + keyword)
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={retrievalMode === 'vector'}
        class="mode-chip"
        class:selected={retrievalMode === 'vector'}
        onclick={() => (retrievalMode = 'vector')}
      >
        Vector only
      </button>
    </div>
  </div>

  {#if selectedEmbedder?.requires_key}
    <div class="sf-field">
      <label class="sf-label" for="setup-embedding-key">
        {selectedEmbedder.key_label}{sharedKeyVendor ? ' (shared with the reranker)' : ''}
      </label>
      <input
        id="setup-embedding-key"
        class="sf-input"
        type="password"
        bind:value={embeddingKey}
        placeholder="Leave blank to keep the stored key"
        autocomplete="off"
      />
      {#if selectedEmbedder.pricing}
        <p class="sf-hint">{selectedEmbedder.pricing} (prices may have changed).</p>
      {/if}
    </div>
  {/if}

  {#if selectedReranker?.requires_key && !sharedKeyVendor}
    <div class="sf-field">
      <label class="sf-label" for="setup-rerank-key">{selectedReranker.key_label}</label>
      <input
        id="setup-rerank-key"
        class="sf-input"
        type="password"
        bind:value={rerankKey}
        placeholder="Leave blank to keep the stored key"
        autocomplete="off"
      />
      {#if selectedReranker.pricing}
        <p class="sf-hint">{selectedReranker.pricing} (prices may have changed).</p>
      {/if}
    </div>
  {/if}

  <div class="sf-actions">
    <Button
      variant="primary"
      onclick={handleSave}
      disabled={saveStatus === 'saving' || !settings || !embedderId}
      loading={saveStatus === 'saving'}
    >
      {saveStatus === 'saving' ? 'Saving' : 'Save RAG settings'}
    </Button>
  </div>

  {#if saveMessage}
    <div class="sf-result" class:success={saveStatus === 'success'} class:error={saveStatus === 'error'}>
      <Icon name={saveStatus === 'success' ? 'success' : 'error'} size={16} />
      <span>{saveMessage}</span>
    </div>
  {/if}
{/if}

<style>
  .option-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .rag-option {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    padding: var(--spacing-sm-plus) var(--spacing-md);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    text-align: left;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .rag-option:hover {
    border-color: var(--border-default);
    color: var(--text-primary);
  }

  .rag-option.selected {
    border-color: var(--accent-primary);
    background: var(--accent-tint-bg);
    color: var(--text-primary);
  }

  .rag-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .rag-head strong {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .rag-tags {
    display: inline-flex;
    align-items: baseline;
    gap: var(--spacing-sm);
  }

  .rag-tier {
    font-size: var(--font-size-3xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-muted);
  }

  .rag-tier.premium {
    color: var(--accent-secondary);
  }

  .rag-tier.local {
    color: var(--success);
  }

  .rag-eval {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
    font-family: var(--font-mono);
  }

  .rag-option small {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
  }

  .mode-row {
    display: flex;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
  }

  .mode-chip {
    padding: var(--spacing-xs) var(--spacing-sm-plus);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .mode-chip:hover {
    border-color: var(--border-default);
    color: var(--text-primary);
  }

  .mode-chip.selected {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    color: var(--text-on-accent);
  }
</style>
