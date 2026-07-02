<script lang="ts">
  import { onMount } from 'svelte';
  import { Icon } from '$lib/components/common';
  import ThreadSettingsSection from './ThreadSettingsSection.svelte';
  import { hooksStore } from '$lib/stores/hooks.svelte';
  import {
    hookCategory,
    HOOK_ACTION_META,
    HOOK_CATEGORIES,
    HOOK_EVENT_META,
  } from '$lib/utils/hooks';
  import type { Thread } from '$lib/types';

  /**
   * Hooks tab: per-thread enablement, not authoring. Authoring lives in the
   * dashboard Hooks panel (or the `/hook` command). Here the user flips the
   * per-thread master switch and per-hook overrides that resolve in
   * `agent_safety.get_effective_hook_enabled`.
   *
   * Both fields feed the panel-wide Save batch (like the other tabs):
   *  - `hooksEnabled`: null = inherit the global setting, true/false = override.
   *  - `hookOverrides`: hook id -> enabled; absent = fall back to the hook's own default.
   */
  interface Props {
    thread: Thread;
    hooksEnabled: boolean | null;
    hookOverrides: Record<string, boolean>;
  }

  let {
    thread,
    hooksEnabled = $bindable(),
    hookOverrides = $bindable(),
  }: Props = $props();

  onMount(() => {
    if (!hooksStore.loaded && !hooksStore.loading) {
      hooksStore.loadHooks();
    }
  });

  const threadHooks = $derived(hooksStore.threadHooks(thread.id));
  const groups = $derived(
    HOOK_CATEGORIES.map((meta) => ({
      meta,
      hooks: threadHooks.filter((h) => hookCategory(h.action) === meta.key),
    })).filter((g) => g.hooks.length > 0)
  );

  type Master = 'inherit' | 'on' | 'off';
  const masterValue = $derived<Master>(
    hooksEnabled === null || hooksEnabled === undefined ? 'inherit' : hooksEnabled ? 'on' : 'off'
  );

  function setMaster(v: Master) {
    hooksEnabled = v === 'inherit' ? null : v === 'on';
  }

  type Override = 'default' | 'on' | 'off';
  function overrideValue(id: string): Override {
    const v = hookOverrides[id];
    return v === undefined ? 'default' : v ? 'on' : 'off';
  }

  function setOverride(id: string, v: Override) {
    const next = { ...hookOverrides };
    if (v === 'default') {
      delete next[id];
    } else {
      next[id] = v === 'on';
    }
    hookOverrides = next;
  }

  // The master kill switch off makes per-hook overrides moot; dim them.
  const masterOff = $derived(masterValue === 'off');
</script>

<div class="tab-body">
  <ThreadSettingsSection
    title="Hooks"
    description="Lifecycle hooks run deterministic actions around a turn: guard tool calls, inject context, or react when the turn ends. This tab controls which hooks run on THIS thread. Create and edit hooks from the dashboard Hooks panel or the /hook command."
  >
    <div class="master-row">
      <span class="master-label">Hooks on this thread</span>
      <div class="segmented" role="group" aria-label="Hooks master switch">
        <button class="seg" class:active={masterValue === 'inherit'} type="button" onclick={() => setMaster('inherit')}>Inherit</button>
        <button class="seg" class:active={masterValue === 'on'} type="button" onclick={() => setMaster('on')}>On</button>
        <button class="seg" class:active={masterValue === 'off'} type="button" onclick={() => setMaster('off')}>Off</button>
      </div>
    </div>
    <p class="section-note">
      <strong>Inherit</strong> follows the global hooks setting. <strong>On</strong> / <strong>Off</strong> force hooks for this thread regardless of the global setting.
    </p>

    {#if masterOff}
      <p class="muted-note">Hooks are off for this thread, so the per-hook overrides below are inactive.</p>
    {/if}

    {#if threadHooks.length === 0}
      <p class="empty-note">No hooks apply to this thread yet. Global hooks and hooks created for this thread will appear here.</p>
    {:else}
      <div class="hook-groups" class:dimmed={masterOff}>
        {#each groups as group (group.meta.key)}
          <div class="hook-group">
            <div class="group-head">
              <Icon name={group.meta.icon} size={12} />
              <span>{group.meta.label}</span>
            </div>
            {#each group.hooks as hook (hook.id)}
              <div class="hook-row">
                <div class="hook-meta">
                  <span class="hook-name">{hook.name}</span>
                  <span class="hook-sub">
                    {HOOK_EVENT_META[hook.event].label} · {HOOK_ACTION_META[hook.action].label}{hook.scope === 'global' ? ' · Global' : ''}
                  </span>
                </div>
                <div class="segmented small" role="group" aria-label="Override for {hook.name}">
                  <button class="seg" class:active={overrideValue(hook.id) === 'default'} type="button" onclick={() => setOverride(hook.id, 'default')}>Default</button>
                  <button class="seg" class:active={overrideValue(hook.id) === 'on'} type="button" onclick={() => setOverride(hook.id, 'on')}>On</button>
                  <button class="seg" class:active={overrideValue(hook.id) === 'off'} type="button" onclick={() => setOverride(hook.id, 'off')}>Off</button>
                </div>
              </div>
            {/each}
          </div>
        {/each}
      </div>
      <p class="input-hint">
        <strong>Default</strong> uses the hook's own enabled state. <strong>On</strong> / <strong>Off</strong> override it for this thread only.
      </p>
    {/if}
  </ThreadSettingsSection>
</div>

<style>
  .tab-body { padding: var(--spacing-lg); }

  .master-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    margin-bottom: var(--spacing-xs);
    flex-wrap: wrap;
  }

  .master-label {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
  }

  .section-note {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0 0 var(--spacing-md);
    max-width: 60ch;
  }

  .section-note strong,
  .input-hint strong { color: var(--text-secondary); font-weight: 600; }

  .muted-note,
  .empty-note {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0 0 var(--spacing-md);
    max-width: 60ch;
  }

  .segmented {
    display: inline-flex;
    gap: 2px;
    padding: 2px;
    background: var(--bg-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-md);
    flex-shrink: 0;
  }

  .seg {
    padding: 4px 12px;
    font-size: var(--font-size-2xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
    white-space: nowrap;
  }

  .segmented.small .seg { padding: 3px 9px; }

  .seg:hover { color: var(--text-primary); }

  .seg.active {
    background: var(--accent-primary);
    color: var(--text-on-accent);
  }

  .hook-groups {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    transition: opacity var(--transition-fast);
  }

  .hook-groups.dimmed { opacity: 0.5; }

  .hook-group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
  }

  .group-head {
    display: inline-flex;
    align-items: center;
    gap: var(--spacing-xs);
    font-size: var(--font-size-3xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    margin-bottom: var(--spacing-2xs);
  }

  .hook-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) 0;
    border-top: 1px solid var(--border-subtle, var(--border-default));
  }

  .hook-group .hook-row:first-of-type { border-top: none; }

  .hook-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .hook-name {
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .hook-sub {
    font-size: var(--font-size-2xs);
    color: var(--text-muted);
  }

  .input-hint {
    display: block;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.45;
    margin-top: var(--spacing-md);
    max-width: 60ch;
  }
</style>
