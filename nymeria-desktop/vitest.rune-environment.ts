import type { Environment } from 'vitest/environments';

// Plain Node globals, but modules are transformed for the CLIENT, so Svelte
// compiles `.svelte.ts` modules with its client runtime and `$effect` runs
// (the built-in `node` environment uses the SSR transform, where effects are
// no-ops). No DOM: for rune and effect tests only, not component mounting.
// Used by the `runes` project in vitest.config.ts (`*.svelte.test.ts`); a
// per-file `@vitest-environment` docblock does NOT switch the transform.
const runeEnvironment: Environment = {
  name: 'svelte-runes',
  viteEnvironment: 'client',
  setup() {
    return { teardown() {} };
  },
};

export default runeEnvironment;
