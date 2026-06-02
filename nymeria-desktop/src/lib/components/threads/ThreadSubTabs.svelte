<script lang="ts">
  /**
   * Inner sub-view toggle used inside a tab body (e.g. Tools -> Native / MCP,
   * Model -> Provider / Generation / Context) to cut down on scrolling. Matches
   * the global settings `.llm-subview-btn` idiom: equal-width bordered buttons,
   * accent on the active segment. Same visual language as the global menu, only
   * the surrounding layout differs.
   */
  interface SubTab {
    id: string;
    label: string;
    count?: number | null;
  }

  interface Props {
    tabs: SubTab[];
    active: string;
    ariaLabel?: string;
  }

  let { tabs, active = $bindable(), ariaLabel = 'Section' }: Props = $props();
</script>

<div class="subtabs" role="tablist" aria-label={ariaLabel}>
  {#each tabs as tab (tab.id)}
    <button
      class="subtab"
      class:active={active === tab.id}
      type="button"
      role="tab"
      aria-selected={active === tab.id}
      onclick={() => (active = tab.id)}
    >
      <span class="subtab-label">{tab.label}</span>
      {#if tab.count != null && tab.count > 0}
        <span class="subtab-count">{tab.count}</span>
      {/if}
    </button>
  {/each}
</div>

<style>
  .subtabs {
    display: flex;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-md);
  }

  .subtab {
    flex: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-xs);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .subtab:hover {
    color: var(--text-primary);
    border-color: var(--text-muted);
  }

  .subtab.active {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
    background: color-mix(in srgb, var(--accent-primary) 15%, transparent);
  }

  .subtab-count {
    font-size: var(--font-size-3xs);
    font-weight: 600;
    color: inherit;
    opacity: 0.85;
  }
</style>
