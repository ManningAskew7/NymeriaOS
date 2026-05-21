<script lang="ts">
  import { onMount } from 'svelte';

  let visible = $state(false);
  let mouseX = $state(0);
  let mouseY = $state(0);
  let showCrosshair = $state(true);
  let measureMode = $state(false);

  type Guide = { id: number; orientation: 'h' | 'v'; pos: number };
  let guides = $state<Guide[]>([]);
  let nextId = 1;
  let dragGuideId = $state<number | null>(null);
  let selectedGuideId = $state<number | null>(null);

  let measureBox = $state<{ x1: number; y1: number; x2: number; y2: number } | null>(null);
  let measuring = $state(false);

  let panelX = $state(20);
  let panelY = $state(20);
  let panelDragOffsetX = 0;
  let panelDragOffsetY = 0;
  let draggingPanel = $state(false);

  function onKey(e: KeyboardEvent) {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'r') {
      e.preventDefault();
      visible = !visible;
      return;
    }
    if (!visible) return;

    if (e.key === 'Escape') {
      if (measureMode) {
        measureMode = false;
        measureBox = null;
      } else if (selectedGuideId !== null) {
        selectedGuideId = null;
      } else {
        visible = false;
      }
      return;
    }

    // Don't intercept arrow keys / delete while the user is typing in a field.
    const ae = document.activeElement as HTMLElement | null;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) {
      return;
    }

    if (selectedGuideId === null) return;
    const g = guides.find(x => x.id === selectedGuideId);
    if (!g) return;

    if (e.key === 'Delete' || e.key === 'Backspace') {
      removeGuide(g.id);
      e.preventDefault();
      return;
    }

    const step = e.shiftKey ? 10 : 1;
    let handled = false;
    if (g.orientation === 'h') {
      if (e.key === 'ArrowUp') { g.pos -= step; handled = true; }
      else if (e.key === 'ArrowDown') { g.pos += step; handled = true; }
    } else {
      if (e.key === 'ArrowLeft') { g.pos -= step; handled = true; }
      else if (e.key === 'ArrowRight') { g.pos += step; handled = true; }
    }
    if (handled) e.preventDefault();
  }

  function onPointerMove(e: PointerEvent) {
    mouseX = e.clientX;
    mouseY = e.clientY;

    if (dragGuideId !== null) {
      const g = guides.find(x => x.id === dragGuideId);
      if (g) {
        g.pos = g.orientation === 'h' ? Math.round(e.clientY) : Math.round(e.clientX);
      }
    }

    if (measuring && measureBox) {
      measureBox.x2 = e.clientX;
      measureBox.y2 = e.clientY;
    }

    if (draggingPanel) {
      panelX = Math.max(0, Math.min(window.innerWidth - 200, e.clientX - panelDragOffsetX));
      panelY = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - panelDragOffsetY));
    }
  }

  function onPointerUp() {
    dragGuideId = null;
    measuring = false;
    draggingPanel = false;
  }

  function startGuideDrag(e: PointerEvent, g: Guide) {
    dragGuideId = g.id;
    selectedGuideId = g.id;
    e.preventDefault();
    e.stopPropagation();
  }

  function startMeasure(e: PointerEvent) {
    if (!measureMode) return;
    if ((e.target as HTMLElement).closest('.ruler-panel')) return;
    measureBox = { x1: e.clientX, y1: e.clientY, x2: e.clientX, y2: e.clientY };
    measuring = true;
    e.preventDefault();
  }

  function startPanelDrag(e: PointerEvent) {
    if ((e.target as HTMLElement).closest('button')) return;
    draggingPanel = true;
    panelDragOffsetX = e.clientX - panelX;
    panelDragOffsetY = e.clientY - panelY;
    e.preventDefault();
  }

  function addH() {
    const id = nextId++;
    guides = [...guides, { id, orientation: 'h', pos: Math.round(window.innerHeight / 2) }];
    selectedGuideId = id;
  }

  function addV() {
    const id = nextId++;
    guides = [...guides, { id, orientation: 'v', pos: Math.round(window.innerWidth / 2) }];
    selectedGuideId = id;
  }

  function removeGuide(id: number) {
    guides = guides.filter(g => g.id !== id);
    if (selectedGuideId === id) selectedGuideId = null;
  }

  function clearAll() {
    guides = [];
    measureBox = null;
    measureMode = false;
    selectedGuideId = null;
  }

  onMount(() => {
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    };
  });
</script>

{#if !visible}
  <button class="ruler-pin" onclick={() => (visible = true)} aria-label="Open ruler" data-tooltip="Ruler (Ctrl+Shift+R)">
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 3 3 21" />
      <path d="m7 17 1.5-1.5" />
      <path d="m10 14 2-2" />
      <path d="m14 10 2-2" />
      <path d="m17 7 1.5-1.5" />
    </svg>
  </button>
{/if}

{#if visible}
  <div class="ruler-root">
    {#if measureMode}
      <div class="capture-layer" onpointerdown={startMeasure}></div>
    {/if}

    {#if showCrosshair}
      <div class="crosshair-h" style:top="{mouseY}px"></div>
      <div class="crosshair-v" style:left="{mouseX}px"></div>
      <div class="coord-tag" style:left="{Math.min(mouseX + 14, window.innerWidth - 80)}px" style:top="{Math.min(mouseY + 14, window.innerHeight - 24)}px">
        {mouseX}, {mouseY}
      </div>
    {/if}

    {#each guides as g (g.id)}
      {#if g.orientation === 'h'}
        <div
          class="guide guide-h"
          class:active={dragGuideId === g.id || selectedGuideId === g.id}
          style:top="{g.pos}px"
          onpointerdown={(e) => startGuideDrag(e, g)}
          ondblclick={() => removeGuide(g.id)}
          role="separator"
          aria-orientation="horizontal"
        >
          <span class="guide-tag guide-tag-h">y: {g.pos}</span>
        </div>
      {:else}
        <div
          class="guide guide-v"
          class:active={dragGuideId === g.id || selectedGuideId === g.id}
          style:left="{g.pos}px"
          onpointerdown={(e) => startGuideDrag(e, g)}
          ondblclick={() => removeGuide(g.id)}
          role="separator"
          aria-orientation="vertical"
        >
          <span class="guide-tag guide-tag-v">x: {g.pos}</span>
        </div>
      {/if}
    {/each}

    {#if measureBox}
      {@const left = Math.min(measureBox.x1, measureBox.x2)}
      {@const top = Math.min(measureBox.y1, measureBox.y2)}
      {@const w = Math.round(Math.abs(measureBox.x2 - measureBox.x1))}
      {@const h = Math.round(Math.abs(measureBox.y2 - measureBox.y1))}
      <div class="measure-box" style:left="{left}px" style:top="{top}px" style:width="{w}px" style:height="{h}px">
        <span class="measure-tag">{w} × {h}</span>
      </div>
    {/if}

    <div class="ruler-panel" style:left="{panelX}px" style:top="{panelY}px">
      <div class="panel-header" onpointerdown={startPanelDrag}>
        <span class="panel-title">Ruler</span>
        <button class="close-btn" onclick={() => (visible = false)} aria-label="Close ruler" data-tooltip="Close (Esc)">×</button>
      </div>
      <div class="panel-body">
        <button class="row-btn" onclick={addH}>+ Horizontal guide</button>
        <button class="row-btn" onclick={addV}>+ Vertical guide</button>
        <button class="row-btn" class:on={measureMode} onclick={() => (measureMode = !measureMode)}>
          {measureMode ? 'Stop measuring' : 'Measure box'}
        </button>
        <button class="row-btn" class:on={showCrosshair} onclick={() => (showCrosshair = !showCrosshair)}>
          {showCrosshair ? 'Hide crosshair' : 'Show crosshair'}
        </button>
        <button class="row-btn ghost" onclick={clearAll}>Clear all</button>
        <div class="panel-hint">
          Click a guide to select • Arrow keys nudge 1px (Shift = 10px) • Delete removes it • Double-click also removes • Ctrl+Shift+R to toggle
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  .ruler-pin {
    position: fixed;
    right: 14px;
    bottom: 14px;
    width: 26px;
    height: 26px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-subtle);
    border-radius: 50%;
    color: var(--text-muted);
    cursor: pointer;
    opacity: 0.45;
    transition: opacity 120ms ease, color 120ms ease, transform 120ms ease;
    z-index: 99998;
  }
  .ruler-pin:hover {
    opacity: 1;
    color: var(--accent-primary);
    transform: scale(1.08);
  }

  .ruler-root {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 99999;
  }

  .capture-layer {
    position: absolute;
    inset: 0;
    pointer-events: auto;
    cursor: crosshair;
  }

  .crosshair-h,
  .crosshair-v {
    position: absolute;
    background: rgba(34, 211, 238, 0.45);
    pointer-events: none;
  }
  .crosshair-h { left: 0; right: 0; height: 1px; }
  .crosshair-v { top: 0; bottom: 0; width: 1px; }

  .coord-tag {
    position: absolute;
    padding: 2px 6px;
    background: rgba(0, 0, 0, 0.85);
    color: rgb(34, 211, 238);
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border-radius: 3px;
    border: 1px solid rgba(34, 211, 238, 0.35);
    pointer-events: none;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .guide {
    position: absolute;
    pointer-events: auto;
  }
  .guide-h {
    left: 0; right: 0;
    height: 9px;
    cursor: ns-resize;
    transform: translateY(-4px);
  }
  .guide-h::before {
    content: '';
    position: absolute;
    left: 0; right: 0; top: 4px;
    height: 1px;
    background: #ff5599;
  }
  .guide-v {
    top: 0; bottom: 0;
    width: 9px;
    cursor: ew-resize;
    transform: translateX(-4px);
  }
  .guide-v::before {
    content: '';
    position: absolute;
    top: 0; bottom: 0; left: 4px;
    width: 1px;
    background: #ff5599;
  }
  .guide.active::before {
    background: #ff2277;
    box-shadow: 0 0 6px rgba(255, 85, 153, 0.7);
  }
  .guide.active .guide-tag {
    background: #ff2277;
  }

  .guide-tag {
    position: absolute;
    padding: 2px 6px;
    background: rgba(255, 85, 153, 0.95);
    color: #fff;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border-radius: 3px;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    pointer-events: none;
  }
  .guide-tag-h { left: 10px; top: 7px; }
  .guide-tag-v { top: 10px; left: 7px; }

  .measure-box {
    position: absolute;
    border: 1px dashed #fbbf24;
    background: rgba(251, 191, 36, 0.08);
    pointer-events: none;
  }
  .measure-tag {
    position: absolute;
    top: -22px;
    left: 0;
    padding: 2px 6px;
    background: rgba(0, 0, 0, 0.9);
    color: #fbbf24;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border-radius: 3px;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .ruler-panel {
    position: absolute;
    width: 200px;
    background: var(--bg-elevated-2);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-md);
    pointer-events: auto;
    color: var(--text-primary);
    user-select: none;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 6px 6px 10px;
    border-bottom: 1px solid var(--border-subtle);
    cursor: move;
  }
  .panel-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary);
  }
  .close-btn {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-muted);
    font-size: 16px;
    line-height: 1;
    padding: 0 6px;
    border-radius: 3px;
  }
  .close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-elevated);
  }

  .panel-body {
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .row-btn {
    width: 100%;
    padding: 5px 8px;
    background: var(--bg-elevated);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    color: var(--text-primary);
    font-size: 11px;
    text-align: left;
    cursor: pointer;
    transition: background 100ms ease, border-color 100ms ease, color 100ms ease;
  }
  .row-btn:hover {
    background: var(--bg-base);
    border-color: var(--border-default);
  }
  .row-btn.on {
    background: var(--accent-primary);
    color: #000;
    border-color: transparent;
  }
  .row-btn.ghost {
    background: transparent;
  }

  .panel-hint {
    font-size: 10px;
    color: var(--text-muted);
    padding: 4px 2px 0;
    line-height: 1.4;
  }
</style>
