import type { Action } from 'svelte/action';

// Moves the node out to <body> so its fixed positioning is always measured
// against the viewport. An ancestor with backdrop-filter, filter, or transform
// (e.g. the glass .modal surface, or a panel's tab-fade transform) becomes the
// containing block for position: fixed descendants, which clips and mis-places
// any overlay rendered inside it. Portaling makes overlay placement independent
// of where the component is instantiated. Theme custom properties live on
// document.documentElement, so portaled nodes keep inheriting them.
export const portal: Action<HTMLElement> = (node) => {
  document.body.appendChild(node);
  return {
    destroy() {
      node.remove();
    },
  };
};
