<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
    size?: 'sm' | 'md' | 'lg';
    disabled?: boolean;
    loading?: boolean;
    type?: 'button' | 'submit' | 'reset';
    title?: string;
    onclick?: (e: MouseEvent) => void;
    children: Snippet;
  }

  let {
    variant = 'primary',
    size = 'md',
    disabled = false,
    loading = false,
    type = 'button',
    title,
    onclick,
    children
  }: Props = $props();
</script>

<button
  class="btn btn-{variant} btn-{size}"
  {type}
  {title}
  disabled={disabled || loading}
  {onclick}
>
  {#if loading}
    <span class="spinner"></span>
  {/if}
  <span class="content" class:loading>
    {@render children()}
  </span>
</button>

<style>
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    border-radius: var(--radius-md);
    font-weight: 500;
    transition: all var(--transition-fast);
    white-space: nowrap;
  }

  .btn:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
  }

  /* Sizes */
  .btn-sm {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: var(--font-size-sm);
  }

  .btn-md {
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: var(--font-size-base);
  }

  .btn-lg {
    padding: var(--spacing-md) var(--spacing-lg);
    font-size: var(--font-size-lg);
  }

  /* Variants */
  .btn-primary {
    background: var(--accent-primary);
    color: var(--bg-base);
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--accent-hover);
    transform: translateY(-1px);
    box-shadow: var(--accent-glow-sm);
  }

  .btn-primary:active:not(:disabled) {
    transform: translateY(0);
    box-shadow: none;
  }

  .btn-secondary {
    background: var(--bg-elevated-2);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--bg-hover);
    border-color: var(--text-muted);
  }

  .btn-ghost {
    background: transparent;
    color: var(--text-secondary);
  }

  .btn-ghost:hover:not(:disabled) {
    background: var(--bg-hover);
    color: var(--text-primary);
    box-shadow: inset 0 0 0 1px var(--border-subtle);
  }

  .btn-danger {
    background: var(--error);
    color: white;
  }

  .btn-danger:hover:not(:disabled) {
    background: #ef4444;
  }

  /* States */
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .content {
    display: inline-flex;
    align-items: center;
    gap: inherit;
  }

  .content.loading {
    opacity: 0;
  }

  .spinner {
    position: absolute;
    width: 16px;
    height: 16px;
    border: 2px solid transparent;
    border-top-color: currentColor;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
