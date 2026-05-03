<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

  interface Props {
    title: string;
    onClose: () => void;
    children: Snippet;
    footer?: Snippet;
  }

  let { title, onClose, children, footer }: Props = $props();
</script>

<div class="wizard-backdrop" role="dialog" aria-modal="true" aria-label={title}>
  <div class="wizard">
    <header class="wizard-header">
      <button class="back-btn" type="button" onclick={onClose} aria-label="Close">
        <Icon name="chevronLeft" size={22} />
      </button>
      <h3>{title}</h3>
    </header>

    <div class="wizard-body">
      {@render children()}
    </div>

    {#if footer}
      <div class="wizard-footer">
        {@render footer()}
      </div>
    {/if}
  </div>
</div>

<style>
  .wizard-backdrop {
    position: fixed;
    inset: 0;
    z-index: 400;
    background: var(--bg-base);
    display: flex;
    flex-direction: column;
  }

  .wizard {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .wizard-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: 0 var(--spacing-md);
    height: var(--header-height, 56px);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
    padding-top: env(safe-area-inset-top);
  }

  .back-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    background: transparent;
    border: none;
  }

  .back-btn:active {
    background: var(--bg-hover);
  }

  .wizard-header h3 {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: 600;
    flex: 1;
  }

  .wizard-body {
    padding: var(--spacing-lg);
    overflow-y: auto;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
    padding-bottom: calc(var(--spacing-lg) + env(safe-area-inset-bottom));
  }

  .wizard-body :global(p) {
    margin: 0;
    line-height: 1.5;
  }

  .wizard-footer {
    padding: var(--spacing-md) var(--spacing-lg);
    padding-bottom: calc(var(--spacing-md) + env(safe-area-inset-bottom));
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
</style>
