import { configDefaults, defineConfig } from 'vitest/config';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';

export default defineConfig({
  plugins: [
    svelte({
      hot: false,
      preprocess: vitePreprocess(),
      compilerOptions: { hmr: false },
    }),
  ],
  resolve: {
    alias: {
      $lib: path.resolve(__dirname, 'src/lib'),
    },
    conditions: ['browser'],
  },
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: 'unit',
          environment: 'node',
          include: ['src/**/*.test.ts'],
          exclude: [...configDefaults.exclude, 'src/**/*.svelte.test.ts'],
        },
      },
      {
        // Rune and effect tests (`*.svelte.test.ts`): the node environment's
        // SSR transform compiles Svelte modules for the server, where
        // `$effect` never runs. This one transforms for the client instead
        // (still no DOM, so no component mounting).
        extends: true,
        test: {
          name: 'runes',
          environment: './vitest.rune-environment.ts',
          include: ['src/**/*.svelte.test.ts'],
        },
      },
    ],
  },
});
