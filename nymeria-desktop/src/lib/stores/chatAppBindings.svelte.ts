import type { ChatAppBinding } from '$lib/types';
import { api } from '$lib/services/api.svelte';

/**
 * Per-thread chat-app binding cache (e.g. Telegram chats bound to threads).
 *
 * Mirrors the `threadConfigStore` runes-closure pattern. The wizard polls
 * `loadBindings(threadId)` after issuing a bind code to detect when the
 * bot has consumed the code; the Chat App settings tab calls it on mount
 * and after any unbind.
 */
function createChatAppBindingsStore() {
  let bindingsByThread = $state<Map<string, ChatAppBinding[]>>(new Map());
  let loading = $state<Set<string>>(new Set());

  return {
    get bindingsByThread() {
      return bindingsByThread;
    },

    getBindings(threadId: string): ChatAppBinding[] {
      return bindingsByThread.get(threadId) ?? [];
    },

    isLoading(threadId: string): boolean {
      return loading.has(threadId);
    },

    async loadBindings(threadId: string): Promise<ChatAppBinding[]> {
      loading = new Set([...loading, threadId]);
      try {
        const list = await api.listThreadBindings(threadId);
        bindingsByThread = new Map(bindingsByThread).set(threadId, list);
        return list;
      } finally {
        const next = new Set(loading);
        next.delete(threadId);
        loading = next;
      }
    },

    async unbind(threadId: string, bindingId: number): Promise<void> {
      await api.unbindThreadChatApp(threadId, bindingId);
      const current = bindingsByThread.get(threadId) ?? [];
      const next = new Map(bindingsByThread).set(
        threadId,
        current.filter((b) => b.id !== bindingId)
      );
      bindingsByThread = next;
    },

    clearCache() {
      bindingsByThread = new Map();
    }
  };
}

export const chatAppBindingsStore = createChatAppBindingsStore();
