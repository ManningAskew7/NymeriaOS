<script lang="ts">
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import HookItem from './HookItem.svelte';
  import HookForm from './HookForm.svelte';
  import { onMount } from 'svelte';
  import type { Hook } from '$lib/types';
  import { hookCategory, HOOK_CATEGORIES } from '$lib/utils/hooks';

  interface Props {
    threadId?: string;
    threadTitleMap?: Record<string, string>;
    onNavigateToThread?: (threadId: string) => void;
  }

  let { threadId, threadTitleMap, onNavigateToThread }: Props = $props();

  let showForm = $state(false);
  let editTarget = $state<Hook | undefined>(undefined);

  onMount(() => {
    if (!hooksStore.loaded && !hooksStore.loading) {
      hooksStore.loadHooks();
    }
  });

  // Per-thread view shows this thread's own hooks plus globals (they fire on
  // every thread); the global view shows all hooks.
  const filteredHooks = $derived(
    threadId ? hooksStore.threadHooks(threadId) : hooksStore.hooks
  );

  // Group by category in the canonical order (Guardrails, Context, Reactions).
  const groups = $derived(
    HOOK_CATEGORIES.map((meta) => ({
      meta,
      hooks: filteredHooks.filter((h) => hookCategory(h.action) === meta.key),
    })).filter((g) => g.hooks.length > 0)
  );

  function handleEdit(hook: Hook) {
    editTarget = hook;
    showForm = true;
  }

  function closeForm() {
    showForm = false;
    editTarget = undefined;
  }
</script>

<div class="hook-feed">
  {#if hooksStore.error}
    <div class="error-state">
      <p>{hooksStore.error}</p>
      <button class="retry-btn" onclick={() => hooksStore.loadHooks()} type="button">Retry</button>
    </div>
  {:else if filteredHooks.length === 0}
    <div class="empty-state">
      {#if threadId}
        <p>No hooks apply to this thread</p>
        <p class="hint">Create a hook to guard tool calls or inject context automatically</p>
      {:else}
        <p>No hooks yet</p>
        <p class="hint">Create your first hook to guard tools, inject context, or react to events</p>
      {/if}
    </div>
  {:else}
    {#each groups as group (group.meta.key)}
      <div class="hook-group">
        <h3 class="group-label section-label">
          <span class="label-text">{group.meta.label}</span>
          <span class="count">{group.hooks.length}</span>
        </h3>
        <div class="group-items">
          {#each group.hooks as hook, i (hook.id)}
            <HookItem
              {hook}
              onEdit={handleEdit}
              animationDelay={i * 30}
              threadTitle={threadTitleMap && hook.thread_id ? threadTitleMap[hook.thread_id] : undefined}
              onNavigateToThread={onNavigateToThread && hook.thread_id ? () => onNavigateToThread!(hook.thread_id) : undefined}
            />
          {/each}
        </div>
      </div>
    {/each}
  {/if}
</div>

{#if showForm}
  <HookForm {threadId} editHook={editTarget} onClose={closeForm} />
{/if}

<style>
  .hook-feed {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  .error-state,
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: var(--spacing-lg);
    border: 1px dashed var(--border-subtle);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .empty-state p { margin: 0; }

  .empty-state .hint {
    font-size: var(--font-size-xs);
    margin-top: var(--spacing-xs);
  }

  .error-state {
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-sm);
  }

  .error-state p { margin: 0 0 var(--spacing-sm); }

  .retry-btn {
    padding: var(--spacing-xs) var(--spacing-md);
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--error) 50%, transparent);
    border-radius: var(--radius-md);
    color: color-mix(in srgb, var(--error) 80%, var(--text-muted));
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .retry-btn:hover {
    background: color-mix(in srgb, var(--error) 15%, transparent);
    border-color: var(--error);
    color: var(--error);
  }

  .hook-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .group-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    margin: 0 0 var(--spacing-xs) 0;
    padding-left: var(--spacing-sm);
  }

  .label-text {
    min-width: var(--count-col-label, 0px);
  }

  .count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 16px;
    height: 16px;
    padding: 0 5px;
    font-size: var(--font-size-3xs);
    font-weight: 500;
    background: var(--bg-hover);
    color: var(--text-secondary);
    border-radius: var(--radius-full);
  }

  .group-items {
    display: flex;
    flex-direction: column;
    gap: 0;
  }
</style>
