<script lang="ts">
  interface Props {
    checked: boolean;
    ariaLabel: string;
    disabled?: boolean;
    size?: 'sm' | 'md';
    variant?: 'solid' | 'outlined';
    title?: string;
    onclick?: (event: MouseEvent) => void | Promise<void>;
  }

  let {
    checked,
    ariaLabel,
    disabled = false,
    size = 'md',
    variant = 'solid',
    title,
    onclick,
  }: Props = $props();
</script>

<button
  class="toggle-switch toggle-switch-{size} toggle-switch-{variant}"
  class:checked
  type="button"
  aria-label={ariaLabel}
  aria-pressed={checked}
  {disabled}
  {title}
  {onclick}
>
  <span class="toggle-track" aria-hidden="true">
    <span class="toggle-thumb"></span>
  </span>
</button>

<style>
  .toggle-switch {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    padding: 0;
    background: none;
    border: none;
    cursor: pointer;
    line-height: 0;
    transform: translateY(-4px);
  }

  .toggle-switch:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 2px;
    border-radius: 999px;
  }

  .toggle-switch:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .toggle-track {
    display: block;
    position: relative;
    transition:
      background var(--transition-fast),
      border-color var(--transition-fast);
  }

  .toggle-thumb {
    position: absolute;
    border-radius: 50%;
    transition:
      left var(--transition-fast),
      background var(--transition-fast);
  }

  .toggle-switch-md .toggle-track {
    width: 32px;
    height: 18px;
    border-radius: 9px;
  }

  .toggle-switch-sm .toggle-track {
    width: 28px;
    height: 16px;
    border-radius: 8px;
  }

  .toggle-switch-solid .toggle-track {
    background: var(--text-muted);
  }

  .toggle-switch-solid.checked .toggle-track {
    background: var(--accent-primary);
  }

  .toggle-switch-solid .toggle-thumb {
    top: 2px;
    left: 2px;
    width: 14px;
    height: 14px;
    background: white;
  }

  .toggle-switch-solid.checked .toggle-thumb {
    left: 16px;
  }

  .toggle-switch-sm.toggle-switch-solid .toggle-thumb {
    top: 1px;
    width: 12px;
    height: 12px;
  }

  .toggle-switch-sm.toggle-switch-solid.checked .toggle-thumb {
    left: 13px;
  }

  .toggle-switch-outlined .toggle-track {
    background: var(--bg-elevated-2, var(--bg-base));
    border: 1px solid var(--border-default);
  }

  .toggle-switch-outlined.checked .toggle-track {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  .toggle-switch-outlined .toggle-thumb {
    background: var(--text-muted);
  }

  .toggle-switch-outlined.checked .toggle-thumb {
    background: white;
  }

  .toggle-switch-md.toggle-switch-outlined .toggle-thumb {
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
  }

  .toggle-switch-md.toggle-switch-outlined.checked .toggle-thumb {
    left: 16px;
  }

  .toggle-switch-sm.toggle-switch-outlined .toggle-thumb {
    top: 1px;
    left: 1px;
    width: 12px;
    height: 12px;
  }

  .toggle-switch-sm.toggle-switch-outlined.checked .toggle-thumb {
    left: 13px;
  }
</style>
