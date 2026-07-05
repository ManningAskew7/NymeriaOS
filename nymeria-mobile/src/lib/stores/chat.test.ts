import { describe, expect, it, vi, beforeEach } from 'vitest';

// The chat store touches the api singleton (only in stopGenerating).
// Mock it so the heavy api/index.ts module chain isn't pulled in.
vi.mock('$lib/services/api.svelte', () => ({
  abortCurrentStream: vi.fn(),
  api: { stopThread: vi.fn().mockResolvedValue(undefined) },
}));

import { createChatStore } from './chat.svelte';
import type { Message } from '$lib/types';

function makeCompletedAssistant(content: string, id = 'prev-turn'): Message {
  return {
    id,
    role: 'assistant',
    content,
    timestamp: new Date('2026-05-17T12:00:00Z'),
    status: 'complete',
    steps: [{ type: 'response', content }],
    intermediateContent: undefined,
    toolCalls: [],
  };
}

function makeUser(content: string, id = 'user-1'): Message {
  return {
    id,
    role: 'user',
    content,
    timestamp: new Date('2026-05-17T12:00:01Z'),
    status: 'complete',
  };
}

describe('chatStore: edit-previous-prompt state (backlog #12)', () => {
  let store: ReturnType<typeof createChatStore>;

  function seedTranscript(): Message[] {
    const transcript = [
      makeUser('first prompt', 'u1'),
      makeCompletedAssistant('first reply', 'a1'),
      makeUser('second prompt', 'u2'),
      makeCompletedAssistant('second reply', 'a2'),
    ];
    store.setMessages(transcript);
    return transcript;
  }

  beforeEach(() => {
    store = createChatStore();
  });

  it('beginEdit/cancelEdit set and clear the edit fields', () => {
    seedTranscript();
    const image = {
      id: 'img-1',
      type: 'image' as const,
      dataUrl: 'data:image/png;base64,abc',
      mimeType: 'image/png',
      name: 'shot.png',
      size: 3,
    };

    store.beginEdit('u2', 'second prompt', [image]);
    expect(store.isEditing).toBe(true);
    expect(store.editingMessageId).toBe('u2');
    expect(store.editingDraft).toBe('second prompt');
    expect(store.editingImageAttachments).toEqual([image]);

    store.cancelEdit();
    expect(store.isEditing).toBe(false);
    expect(store.editingMessageId).toBeNull();
    expect(store.editingDraft).toBe('');
    expect(store.editingImageAttachments).toEqual([]);
  });

  it('truncateFromMessage drops the target and everything after it', () => {
    seedTranscript();
    store.truncateFromMessage('u2');
    expect(store.messages.map((m) => m.id)).toEqual(['u1', 'a1']);
  });

  it('truncateFromMessage is a no-op for unknown ids', () => {
    seedTranscript();
    store.truncateFromMessage('nope');
    expect(store.messages.map((m) => m.id)).toEqual(['u1', 'a1', 'u2', 'a2']);
  });

  it('truncating away the edited message clears edit state', () => {
    seedTranscript();
    store.beginEdit('u2', 'second prompt');
    store.truncateFromMessage('u2');
    expect(store.isEditing).toBe(false);
    expect(store.editingDraft).toBe('');
  });

  it('truncating after the edited message keeps edit state', () => {
    seedTranscript();
    store.beginEdit('u1', 'first prompt');
    store.truncateFromMessage('u2');
    expect(store.isEditing).toBe(true);
    expect(store.editingMessageId).toBe('u1');
  });

  it('prepareForThreadSwitch cancels an in-progress edit', () => {
    seedTranscript();
    store.beginEdit('u2', 'second prompt');
    store.prepareForThreadSwitch();
    expect(store.isEditing).toBe(false);
  });

  it('clearMessages cancels an in-progress edit (new-thread flow)', () => {
    seedTranscript();
    store.beginEdit('u2', 'second prompt');
    store.clearMessages();
    expect(store.isEditing).toBe(false);
    expect(store.editingDraft).toBe('');
  });
});

describe('chatStore: action sheet state (mobile)', () => {
  let store: ReturnType<typeof createChatStore>;

  beforeEach(() => {
    store = createChatStore();
  });

  it('openActionSheet/closeActionSheet set and clear the target id', () => {
    expect(store.actionSheetMessageId).toBeNull();
    store.openActionSheet('u2');
    expect(store.actionSheetMessageId).toBe('u2');
    store.closeActionSheet();
    expect(store.actionSheetMessageId).toBeNull();
  });

  it('prepareForThreadSwitch closes an open action sheet', () => {
    store.openActionSheet('u2');
    store.prepareForThreadSwitch();
    expect(store.actionSheetMessageId).toBeNull();
  });

  it('clearMessages closes an open action sheet (new-thread flow)', () => {
    store.openActionSheet('u2');
    store.clearMessages();
    expect(store.actionSheetMessageId).toBeNull();
  });
});
