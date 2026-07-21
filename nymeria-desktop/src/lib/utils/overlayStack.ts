// Shared LIFO registry of open overlay layers (modals and dialog overlays).
//
// Overlays listen for Escape on the window, so when dialogs stack (e.g.
// Provider Setup opened from inside Global Settings) a single Escape press
// used to close every open layer at once. Each overlay registers a layer
// while open and only acts on Escape when it is the topmost layer, so
// stacked dialogs close one at a time, innermost first.

const stack: symbol[] = [];

export function pushOverlay(label = 'overlay'): symbol {
  const id = Symbol(label);
  stack.push(id);
  return id;
}

export function removeOverlay(id: symbol): void {
  const index = stack.lastIndexOf(id);
  if (index !== -1) stack.splice(index, 1);
}

export function isTopOverlay(id: symbol): boolean {
  return stack.length > 0 && stack[stack.length - 1] === id;
}
