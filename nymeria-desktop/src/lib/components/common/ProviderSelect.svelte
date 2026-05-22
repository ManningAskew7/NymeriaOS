<script lang="ts">
  import { onMount } from 'svelte';
  import type { ProviderTier } from '$lib/types';

  export type ProviderSelectOption = {
    value: string;
    label: string;
    /** Backend-derived tier; controls chip color and grouping. */
    tier?: ProviderTier;
    /** Optional warning text shown as a sub-line under the option. */
    notesForUser?: string;
    /** Optional descriptive sub-line for synthetic display entries
     *  (anthropic_proxy, anthropic_direct, local_openai, openai_custom). */
    description?: string;
  };

  export type ProviderSelectGroup = {
    label: string;
    /** Optional tier hint for the entire group header (chip color). */
    tier?: ProviderTier;
    options: ProviderSelectOption[];
  };

  interface Props {
    id?: string;
    value: string;
    groups: ProviderSelectGroup[];
    /** If true, an initial "Default" option is rendered above all groups.
     *  Its value is the empty string. */
    includeDefault?: boolean;
    defaultLabel?: string;
    /** Optional sub-line under the default option (e.g. "Inherit global"). */
    defaultDescription?: string;
    disabled?: boolean;
    ariaLabel?: string;
    placeholder?: string;
  }

  let {
    id = undefined,
    value = $bindable(),
    groups,
    includeDefault = false,
    defaultLabel = 'Default',
    defaultDescription,
    disabled = false,
    ariaLabel,
    placeholder = 'Select a provider',
  }: Props = $props();

  let open = $state(false);
  let activeIndex = $state(-1);
  let typeBuffer = $state('');
  let typeBufferResetAt = $state(0);

  let buttonEl = $state<HTMLButtonElement | null>(null);
  let listboxEl = $state<HTMLUListElement | null>(null);
  let rootEl = $state<HTMLDivElement | null>(null);

  // Flat option list (with optional default entry at index 0). Used for
  // keyboard navigation; group structure stays for rendering only.
  type FlatRow =
    | { kind: 'option'; option: ProviderSelectOption; groupIndex: number }
    | { kind: 'group-header'; label: string; tier?: ProviderTier; groupIndex: number };

  let flatRows = $derived.by<FlatRow[]>(() => {
    const rows: FlatRow[] = [];
    if (includeDefault) {
      rows.push({
        kind: 'option',
        option: { value: '', label: defaultLabel, description: defaultDescription },
        groupIndex: -1,
      });
    }
    groups.forEach((g, gi) => {
      rows.push({ kind: 'group-header', label: g.label, tier: g.tier, groupIndex: gi });
      g.options.forEach((o) => rows.push({ kind: 'option', option: o, groupIndex: gi }));
    });
    return rows;
  });

  let optionIndices = $derived(
    flatRows
      .map((row, i) => (row.kind === 'option' ? i : -1))
      .filter((i) => i >= 0)
  );

  let selectedOption = $derived.by<ProviderSelectOption | null>(() => {
    for (const row of flatRows) {
      if (row.kind === 'option' && row.option.value === value) return row.option;
    }
    return null;
  });

  function open_() {
    if (disabled || open) return;
    open = true;
    // Place cursor on the currently selected option, or first option if none.
    const idx = flatRows.findIndex(
      (r) => r.kind === 'option' && r.option.value === value
    );
    activeIndex = idx >= 0 ? idx : (optionIndices[0] ?? -1);
  }

  function close_(returnFocus = true) {
    if (!open) return;
    open = false;
    activeIndex = -1;
    typeBuffer = '';
    if (returnFocus) buttonEl?.focus();
  }

  function commit(option: ProviderSelectOption) {
    value = option.value;
    close_();
  }

  function moveActive(delta: number) {
    if (optionIndices.length === 0) return;
    const currentPos = optionIndices.indexOf(activeIndex);
    let nextPos: number;
    if (currentPos < 0) {
      nextPos = delta > 0 ? 0 : optionIndices.length - 1;
    } else {
      nextPos = (currentPos + delta + optionIndices.length) % optionIndices.length;
    }
    activeIndex = optionIndices[nextPos];
    scrollActiveIntoView();
  }

  function moveTo(position: 'first' | 'last') {
    if (optionIndices.length === 0) return;
    activeIndex = position === 'first' ? optionIndices[0] : optionIndices[optionIndices.length - 1];
    scrollActiveIntoView();
  }

  function scrollActiveIntoView() {
    queueMicrotask(() => {
      const el = listboxEl?.querySelector<HTMLLIElement>(`[data-row-index="${activeIndex}"]`);
      el?.scrollIntoView({ block: 'nearest' });
    });
  }

  function handleTypeahead(key: string) {
    // Reset the buffer if the user paused for >700ms.
    const now = Date.now();
    if (now - typeBufferResetAt > 700) typeBuffer = '';
    typeBuffer += key.toLowerCase();
    typeBufferResetAt = now;

    // Find the first option whose label starts with the buffer; cycle from
    // after the current active index so repeated presses advance.
    const startPos = Math.max(0, optionIndices.indexOf(activeIndex) + 1);
    const ordered = [
      ...optionIndices.slice(startPos),
      ...optionIndices.slice(0, startPos),
    ];
    for (const idx of ordered) {
      const row = flatRows[idx];
      if (row.kind !== 'option') continue;
      if (row.option.label.toLowerCase().startsWith(typeBuffer)) {
        activeIndex = idx;
        scrollActiveIntoView();
        return;
      }
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (disabled) return;
    if (!open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open_();
        return;
      }
      return;
    }
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        moveActive(1);
        return;
      case 'ArrowUp':
        e.preventDefault();
        moveActive(-1);
        return;
      case 'Home':
        e.preventDefault();
        moveTo('first');
        return;
      case 'End':
        e.preventDefault();
        moveTo('last');
        return;
      case 'Enter':
      case ' ': {
        e.preventDefault();
        const row = flatRows[activeIndex];
        if (row && row.kind === 'option') commit(row.option);
        return;
      }
      case 'Escape':
        e.preventDefault();
        close_();
        return;
      case 'Tab':
        close_(false);
        return;
      default:
        if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
          handleTypeahead(e.key);
        }
    }
  }

  function handleDocumentClick(e: MouseEvent) {
    if (!open) return;
    const target = e.target as Node | null;
    if (target && rootEl && !rootEl.contains(target)) close_(false);
  }

  onMount(() => {
    document.addEventListener('mousedown', handleDocumentClick, true);
    return () => document.removeEventListener('mousedown', handleDocumentClick, true);
  });

  function tierChipLabel(tier: ProviderTier | undefined): string {
    if (tier === 'native') return 'Native';
    if (tier === 'gateway') return 'Gateway';
    if (tier === 'unverified') return 'Unverified';
    return '';
  }
</script>

<div class="provider-select" bind:this={rootEl} class:open class:disabled>
  <button
    {id}
    bind:this={buttonEl}
    type="button"
    class="trigger"
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-label={ariaLabel}
    {disabled}
    onclick={() => (open ? close_() : open_())}
    onkeydown={handleKeydown}
  >
    <span class="trigger-content">
      {#if selectedOption}
        <span class="trigger-label">{selectedOption.label}</span>
        {#if selectedOption.tier}
          <span class="chip chip-{selectedOption.tier}">{tierChipLabel(selectedOption.tier)}</span>
        {/if}
      {:else if value === '' && includeDefault}
        <span class="trigger-label">{defaultLabel}</span>
      {:else}
        <span class="trigger-label trigger-placeholder">{value || placeholder}</span>
      {/if}
    </span>
    <span class="trigger-caret" aria-hidden="true">▾</span>
  </button>

  {#if open}
    <ul
      bind:this={listboxEl}
      class="listbox"
      role="listbox"
      aria-activedescendant={activeIndex >= 0 ? `provider-row-${activeIndex}` : undefined}
      tabindex="-1"
      onkeydown={handleKeydown}
    >
      {#each flatRows as row, i (i)}
        {#if row.kind === 'group-header'}
          <li class="group-header" role="presentation">
            <span class="group-label">{row.label}</span>
            {#if row.tier}
              <span class="chip chip-{row.tier} chip-sm">{tierChipLabel(row.tier)}</span>
            {/if}
          </li>
        {:else}
          <li
            id={`provider-row-${i}`}
            class="option"
            class:active={i === activeIndex}
            class:selected={row.option.value === value}
            data-row-index={i}
            role="option"
            aria-selected={row.option.value === value}
            onmouseenter={() => (activeIndex = i)}
            onmousedown={(e) => {
              e.preventDefault();
              commit(row.option);
            }}
          >
            <div class="option-main">
              <span class="option-label">{row.option.label}</span>
              {#if row.option.tier}
                <span class="chip chip-{row.option.tier} chip-sm">
                  {tierChipLabel(row.option.tier)}
                </span>
              {/if}
            </div>
            {#if row.option.description}
              <div class="option-description">{row.option.description}</div>
            {/if}
            {#if row.option.notesForUser}
              <div class="option-warning">{row.option.notesForUser}</div>
            {/if}
          </li>
        {/if}
      {/each}
    </ul>
  {/if}
</div>

<style>
  .provider-select {
    position: relative;
    width: 100%;
  }

  .trigger {
    display: flex;
    width: 100%;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 12px;
    background: var(--bg-elevated, rgba(255, 255, 255, 0.04));
    border: 1px solid var(--border-subtle, rgba(255, 255, 255, 0.12));
    border-radius: 6px;
    color: var(--text-primary, #e4e4e7);
    font: inherit;
    cursor: pointer;
    text-align: left;
  }

  .trigger:hover:not(:disabled) {
    border-color: var(--border-strong, rgba(255, 255, 255, 0.24));
  }

  .trigger:focus-visible {
    outline: 2px solid var(--accent, #6366f1);
    outline-offset: -1px;
  }

  .trigger:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .open .trigger {
    border-color: var(--accent, #6366f1);
  }

  .trigger-content {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 0;
  }

  .trigger-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .trigger-placeholder {
    color: var(--text-muted, rgba(255, 255, 255, 0.5));
  }

  .trigger-caret {
    flex-shrink: 0;
    color: var(--text-muted, rgba(255, 255, 255, 0.5));
    font-size: 10px;
    transition: transform 120ms;
  }

  .open .trigger-caret {
    transform: rotate(180deg);
  }

  .listbox {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    max-height: 360px;
    overflow-y: auto;
    margin: 0;
    padding: 4px 0;
    background: var(--bg-overlay, #1f1f2a);
    border: 1px solid var(--border-strong, rgba(255, 255, 255, 0.18));
    border-radius: 6px;
    list-style: none;
    z-index: 50;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }

  .group-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 12px 4px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-muted, rgba(255, 255, 255, 0.5));
    pointer-events: none;
  }

  .group-header:not(:first-child) {
    border-top: 1px solid var(--border-subtle, rgba(255, 255, 255, 0.08));
    margin-top: 4px;
  }

  .option {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 6px 12px;
    cursor: pointer;
    color: var(--text-primary, #e4e4e7);
  }

  .option.active {
    background: var(--bg-hover, rgba(99, 102, 241, 0.12));
  }

  .option.selected {
    background: var(--bg-selected, rgba(99, 102, 241, 0.18));
  }

  .option-main {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .option-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .option-description {
    font-size: 11px;
    color: var(--text-muted, rgba(255, 255, 255, 0.55));
    line-height: 1.35;
  }

  .option-warning {
    font-size: 11px;
    color: var(--warning, #fbbf24);
    line-height: 1.35;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-radius: 4px;
    line-height: 1.4;
    flex-shrink: 0;
  }

  .chip-sm {
    padding: 1px 5px;
    font-size: 9px;
  }

  .chip-native {
    color: var(--success, #4ade80);
    background: rgba(74, 222, 128, 0.12);
    border: 1px solid rgba(74, 222, 128, 0.4);
  }

  .chip-gateway {
    color: var(--info, #60a5fa);
    background: rgba(96, 165, 250, 0.12);
    border: 1px solid rgba(96, 165, 250, 0.4);
  }

  .chip-unverified {
    color: var(--warning, #fbbf24);
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.4);
  }

  /* Touch devices (mobile, tablets): enlarge tap targets to at least 44px so
   * the picker is usable without a fine pointer. The component file is shared
   * byte-identical between the desktop and mobile apps via the cross-app
   * drift check, so the divergence is handled with this media query rather
   * than two copies of the file. */
  @media (hover: none) and (pointer: coarse) {
    .trigger {
      padding: 12px 14px;
      min-height: 44px;
    }
    .option {
      padding: 10px 14px;
      min-height: 44px;
      gap: 4px;
    }
  }
</style>
