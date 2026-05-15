import type { CommandExecuteResponse, SlashCommandInfo } from '$lib/types';
import { TriggersApi } from './triggers';

export class CommandsApi extends TriggersApi {
  async executeCommand(command: string, threadId?: string): Promise<CommandExecuteResponse> {
    const response = await fetch(`${this.getBaseUrl()}/commands/execute`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({
        command,
        thread_id: threadId,
        source: 'user'
      })
    });

    if (!response.ok) {
      const message = await this._toastAndExtractError(response, 'Command failed');
      throw new Error(message);
    }

    return await response.json() as CommandExecuteResponse;
  }

  async listCommands(source: 'user' | 'agent' | 'cli' = 'user'): Promise<SlashCommandInfo[]> {
    const params = new URLSearchParams({ source });
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
