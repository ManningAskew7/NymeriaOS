import type { CommandExecuteResponse, SlashCommandInfo } from '$lib/types';
import { TriggersApi } from './triggers';

type CommandActor = 'user' | 'agent' | 'system';
type CommandSurface = 'desktop' | 'mobile' | 'cli' | 'discord' | 'telegram' | 'slack' | 'matrix' | 'whatsapp' | 'messenger' | 'instagram' | 'webex' | 'mattermost' | 'zulip' | 'rocketchat' | 'teams' | 'googlechat' | 'line' | 'signal' | 'twitch' | 'api' | 'agent';
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

export class CommandsApi extends TriggersApi {
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
      const message = await this._toastAndExtractError(response, 'Command failed');
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
      const message = await this._toastAndExtractError(response, 'Failed to load commands');
      throw new Error(message);
    }

    return await response.json() as SlashCommandInfo[];
  }
}
