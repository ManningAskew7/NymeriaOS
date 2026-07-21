<script lang="ts">
  import Icon from './Icon.svelte';
  import { slide } from 'svelte/transition';
  import { portal } from '$lib/actions/portal';
  import { DROPDOWN_TRANSITION } from '$lib/utils/transitions';

  interface KebabMenuItem {
    label: string;
    icon: string;
    onSelect: () => void;
    destructive?: boolean;
  }

  interface Props {
    items: KebabMenuItem[];
    ariaLabel?: string;
    open?: boolean;
    iconSize?: number;
  }

  let { items, ariaLabel = 'Actions', open = $bindable(false), iconSize = 14 }: Props = $props();

  let kebabEl = $state<HTMLButtonElement>();
  let menuEl = $state<HTMLDivElement>();
  let menuStyle = $state('');

  const MENU_WIDTH = 184;
  // Per-row height (~36px), the menu's 4px top/bottom padding, plus a little
  // headroom for the optional divider before a destructive item. Used only to
  // decide whether the menu drops below the trigger or flips above it near the
  // viewport bottom; over-estimating only biases toward flipping up, which is
  // harmless.
  let menuEstHeight = $derived(items.length * 36 + 16);

  function openMenu() {
    if (!kebabEl) return;
    const r = kebabEl.getBoundingClientRect();
    // Menu's right edge aligns with the trigger's right edge, clamped to the
    // viewport so it never runs off-screen on a narrow panel.
    const left = Math.max(8, Math.min(r.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 8));
    const roomBelow = window.innerHeight - r.bottom;
    const top =
      roomBelow < menuEstHeight + 8
        ? Math.max(8, r.top - menuEstHeight - 4)
        : r.bottom + 4;
    menuStyle = `top:${top}px; left:${left}px;`;
    open = true;
  }

  function closeMenu() {
    // Restore focus to the trigger when closing while focus is still inside the
    // menu (Escape, arrow-then-activate, or a clicked item); don't yank focus
    // when the user dismissed it by clicking elsewhere with the mouse.
    if (menuEl?.contains(document.activeElement)) kebabEl?.focus();
    open = false;
  }

  function toggleMenu(e: MouseEvent) {
    e.stopPropagation();
    if (open) closeMenu();
    else openMenu();
  }

  function handleItemClick(e: MouseEvent, item: KebabMenuItem) {
    e.stopPropagation();
    closeMenu();
    item.onSelect();
  }

  function handleOutsideClick(e: MouseEvent) {
    const t = e.target as Node;
    // Scope the guard to THIS instance's own nodes: a class-based selector would
    // match every row's trigger, so clicking another row's kebab would leave
    // this menu open (two menus open at once). Treating any node outside this
    // trigger/menu as "outside" means opening another row's menu dismisses this
    // one.
    if (!menuEl?.contains(t) && !kebabEl?.contains(t)) closeMenu();
  }

  function handleMenuKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeMenu();
      return;
    }
    // Arrow keys cycle focus between the menu items (role="menu" contract).
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const btns = menuEl ? Array.from(menuEl.querySelectorAll<HTMLButtonElement>('button')) : [];
      if (!btns.length) return;
      const idx = btns.indexOf(document.activeElement as HTMLButtonElement);
      const next =
        e.key === 'ArrowDown'
          ? (idx + 1) % btns.length
          : (idx - 1 + btns.length) % btns.length;
      btns[next].focus();
    }
  }

  // While open: close on outside click / Escape / any scroll / resize (capture
  // phase, mirroring AccountMenu), and move focus to the first item so the menu
  // is keyboard-operable.
  $effect(() => {
    if (!open) return;
    document.addEventListener('click', handleOutsideClick, true);
    document.addEventListener('keydown', handleMenuKeydown, true);
    window.addEventListener('scroll', closeMenu, true);
    window.addEventListener('resize', closeMenu, true);
    return () => {
      document.removeEventListener('click', handleOutsideClick, true);
      document.removeEventListener('keydown', handleMenuKeydown, true);
      window.removeEventListener('scroll', closeMenu, true);
      window.removeEventListener('resize', closeMenu, true);
    };
  });

  $effect(() => {
    if (open && menuEl) {
      menuEl.querySelector<HTMLButtonElement>('button')?.focus();
    }
  });
</script>

<button
  class="kebab-trigger"
  bind:this={kebabEl}
  onclick={toggleMenu}
  type="button"
  data-tooltip={ariaLabel}
  aria-label={ariaLabel}
  aria-haspopup="menu"
  aria-expanded={open}
>
  <Icon name="moreVertical" size={iconSize} />
</button>

<!-- Overflow menu, rendered at the component root with fixed positioning so it
     escapes the host's scroll/overflow clipping (see openMenu for placement). -->
{#if open}
  <div
    class="kebab-menu"
    role="menu"
    bind:this={menuEl}
    use:portal
    style={menuStyle}
    transition:slide={DROPDOWN_TRANSITION}
  >
    {#each items as item, i}
      {#if item.destructive && i > 0}
        <!-- Separate the destructive action (Delete) from the routine ones above it. -->
        <div class="kebab-menu-divider" role="separator"></div>
      {/if}
      <button
        class="kebab-menu-item"
        class:destructive={item.destructive}
        type="button"
        role="menuitem"
        onclick={(e) => handleItemClick(e, item)}
      >
        <Icon name={item.icon} size={iconSize} />
        <span>{item.label}</span>
      </button>
    {/each}
  </div>
{/if}

<style>
  /* The (⋮) overflow trigger. Self-contained styling so every consumer gets an
     identical control; the HOST positions it and controls its reveal (opacity/
     visibility) via a wrapper. Padding sizes it to a 22px square tap target
     around the icon. */
  .kebab-trigger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 4px;
    border: none;
    background: transparent;
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast),
      transform var(--transition-fast);
  }

  .kebab-trigger:hover {
    background: var(--bg-active);
    color: var(--text-primary);
  }

  .kebab-trigger:active {
    transform: scale(var(--press-scale-icon));
  }

  /* Floating overflow menu (fixed-positioned from JS so the host's scroll/
     overflow can't clip it). Chrome mirrors AccountMenu: elevated surface,
     shadow-defined elevation, destructive item in --error. */
  .kebab-menu {
    position: fixed;
    width: 184px;
    padding: var(--spacing-xs);
    background: var(--bg-elevated-2, var(--bg-elevated));
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-md);
    z-index: 1000;
  }

  .kebab-menu-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    width: 100%;
    padding: var(--spacing-sm) var(--spacing-sm);
    background: transparent;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    text-align: left;
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast);
  }

  .kebab-menu-item span {
    flex: 1;
    /* Labels stay on one line; the menu width (above) is sized to fit the
       longest label, so nothing wraps. */
    white-space: nowrap;
  }

  /* Hairline above a destructive item (Delete), emitted by the each-loop. Uses
     the same subtle border token as the app's other row dividers. */
  .kebab-menu-divider {
    height: 0;
    margin: var(--spacing-xs) 0;
    border-top: 0.5px solid var(--border-subtle);
  }

  .kebab-menu-item:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .kebab-menu-item.destructive {
    color: var(--error);
  }

  .kebab-menu-item.destructive:hover {
    background: rgba(var(--error-rgb), 0.12);
    color: var(--error);
  }
</style>
