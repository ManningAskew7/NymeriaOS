<script lang="ts">
  import { configStore } from '$lib/stores/config.svelte';
  import { getThemeList, getThemePreviewColors, type ThemeName } from '$lib/themes';

  const themeList = getThemeList();

  function pick(theme: ThemeName) {
    configStore.setTheme(theme);
  }
</script>

<p class="sf-hint lead">
  Applied instantly, saved on this device. Light is the default; the dark themes are one
  click away here or in Settings later.
</p>

<div class="theme-grid" role="radiogroup" aria-label="Theme">
  {#each themeList as theme (theme.id)}
    {@const colors = getThemePreviewColors(theme.id)}
    <button
      type="button"
      role="radio"
      aria-checked={configStore.theme === theme.id}
      class="theme-card"
      class:selected={configStore.theme === theme.id}
      onclick={() => pick(theme.id)}
    >
      <span class="swatch" style:background={colors.bg} aria-hidden="true">
        <span class="swatch-accent" style:background={colors.accent}></span>
        <span class="swatch-text" style:background={colors.text}></span>
      </span>
      <span class="theme-meta">
        <strong>{theme.name}{theme.id === 'light' ? ' (default)' : ''}</strong>
        <small>{theme.description}</small>
      </span>
    </button>
  {/each}
</div>

<style>
  .lead {
    margin: 0;
  }

  .theme-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--spacing-sm-plus);
  }

  .theme-card {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm-plus);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-subtle);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    text-align: left;
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .theme-card:hover {
    border-color: var(--border-default);
  }

  .theme-card.selected {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 3px var(--accent-tint-bg);
  }

  .swatch {
    position: relative;
    display: block;
    height: 64px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-subtle);
    overflow: hidden;
  }

  .swatch-accent {
    position: absolute;
    left: 10px;
    bottom: 10px;
    width: 34px;
    height: 12px;
    border-radius: var(--radius-sm);
  }

  .swatch-text {
    position: absolute;
    left: 10px;
    top: 12px;
    width: 56px;
    height: 6px;
    border-radius: 3px;
    opacity: 0.85;
  }

  .theme-meta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-2xs);
  }

  .theme-meta strong {
    font-size: var(--font-size-sm);
    font-weight: 600;
    color: var(--text-primary);
  }

  .theme-meta small {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  @media (max-width: 900px) {
    .theme-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
