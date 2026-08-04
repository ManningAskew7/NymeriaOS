import type { CommandExecuteResponse, SlashCommandInfo } from '$lib/types';
import { LlmFallbackApi } from './llm-fallback';

type CommandActor = 'user' | 'agent' | 'system';
type CommandSurface = 'desktop' | 'mobile' | 'cli' | 'discord' | 'telegram' | 'slack' | 'whatsapp' | 'teams' | 'twitch' | 'api' | 'agent';
type CommandWindow = Window & {
  __TAURI__?: unknown;
  __TAURI_INTERNALS__?: unknown;
  Capacitor?: unknown;
};

function defaultCommandSurface(): CommandSurface {
  if (typeof window === 'undefined') return 'desktop';
  const platformWindow = window as CommandWindow;
  if (platformWindow.Capacitor) return 'mobile';
  return 'desktop';
}

export class CommandsApi extends LlmFallbackApi {
  async executeCommand(
    command: string,
    threadId?: string,
    surface: CommandSurface = defaultCommandSurface()
  ): Promise<CommandExecuteResponse> {
    const response = await fetch(`${this.getBaseUrl()}/commands/execute`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({
        command,
        thread_id: threadId,
        source: 'user',
        actor: 'user',
        surface
      })
    });

    if (!response.ok) {
      // The command-result card is the ONE error surface for command
      // execution (backlog #135): no toast on top of it, except the
      // session-level 401 handling the base helper preserves.
      const message = await this._extractErrorWithAuthSignal(response, 'Command failed');
      throw new Error(message);
    }

    return await response.json() as CommandExecuteResponse;
  }

  async listCommands(
    actor: CommandActor = 'user',
    surface: CommandSurface = defaultCommandSurface()
  ): Promise<SlashCommandInfo[]> {
    const params = new URLSearchParams({ actor, surface });
    const response = await fetch(`${this.getBaseUrl()}/commands?${params}`, {
      headers: this.getHeaders()
    });

    if (!response.ok) {
      // Fully quiet by design, 401 included: this is a passive background
      // fetch (the commands store retries with backoff and degrades to its
      // fallback roots), so it must not sign the user out or toast on a
      // transient auth hiccup; user-initiated calls carry the auth signal.
      const message = await this._extractError(response, 'Failed to load commands');
      throw new Error(message);
    }

    return await response.json() as SlashCommandInfo[];
  }
}
