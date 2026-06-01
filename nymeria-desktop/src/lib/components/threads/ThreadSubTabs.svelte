<script lang="ts">
  /**
   * Inner segmented control used inside a tab body (e.g. Tools -> Native / MCP,
   * Model -> Provider / Generation / Context) to cut down on scrolling. Plain
   * neutral styling: a filled active segment, no accent fills.
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
    display: inline-flex;
    gap: 2px;
    padding: 3px;
    margin-bottom: var(--spacing-md);
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
  }

  .subtab {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 14px;
    font-size: var(--font-size-sm);
    font-weight: 500;
    color: var(--text-muted);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: color var(--transition-fast), background var(--transition-fast);
  }

  .subtab:hover {
    color: var(--text-primary);
  }

  .subtab.active {
    color: var(--text-primary);
    background: var(--bg-base);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.18);
  }

  .subtab-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 16px;
    height: 16px;
    padding: 0 5px;
    font-size: var(--font-size-3xs);
    font-weight: 600;
    color: var(--text-secondary);
    background: var(--bg-elevated);
    border-radius: var(--radius-full);
  }
</style>
