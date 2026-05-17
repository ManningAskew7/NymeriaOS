import type {
  AccountIdentity,
  AdminUser,
  ChatAppBinding,
  ChatAppBindCodeResponse,
  ChatAppProvider,
  IssuedTokenResponse,
  MyTelegramBot,
  PlatformIdentity,
  RotatedTokensResponse,
  TokenInfo,
  UserRole
} from '$lib/types';
import { SystemApi } from './system';

export class AccountsApi extends SystemApi {
  async updateMe(displayName: string): Promise<AccountIdentity> {
    const response = await fetch(`${this.getBaseUrl()}/me`, {
      method: 'PATCH',
      headers: this.getHeaders(),
      body: JSON.stringify({ display_name: displayName })
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to update profile'));
    }
    return response.json();
  }

  // Self-scoped token management — wraps GET/POST/DELETE /me/tokens. The raw
  // token from issueMyToken is returned ONCE; the UI must surface it in a
  // copy-once dialog and drop the value.
  async listMyTokens(): Promise<TokenInfo[]> {
    const response = await fetch(`${this.getBaseUrl()}/me/tokens`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to list tokens'));
    }
    return response.json();
  }

  async issueMyToken(label?: string): Promise<IssuedTokenResponse> {
    const response = await fetch(`${this.getBaseUrl()}/me/tokens`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ label: label ?? null })
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to issue token'));
    }
    return response.json();
  }

  async revokeMyToken(tokenHashPrefix: string): Promise<{ revoked: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/me/tokens/${encodeURIComponent(tokenHashPrefix)}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to revoke token'));
    }
    return response.json();
  }

  // ---------------------------------------------------------------------------
  // Admin user management — all gated by require_admin_user. The caller's
  // bearer token must belong to an admin account; non-admins get 403.
  // ---------------------------------------------------------------------------

  async listAdminUsers(): Promise<AdminUser[]> {
    const response = await fetch(`${this.getBaseUrl()}/admin/users`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to list users'));
    }
    return response.json();
  }

  async createAdminUser(body: {
    email: string;
    display_name?: string;
    role?: UserRole;
    id?: string;
    token_label?: string;
  }): Promise<IssuedTokenResponse> {
    const response = await fetch(`${this.getBaseUrl()}/admin/users`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to create user'));
    }
    return response.json();
  }

  async getAdminUser(userId: string): Promise<AdminUser> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to load user'));
    }
    return response.json();
  }

  async updateAdminUser(
    userId: string,
    patch: { display_name?: string; role?: UserRole; disabled?: boolean }
  ): Promise<AdminUser> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}`,
      { method: 'PATCH', headers: this.getHeaders(), body: JSON.stringify(patch) }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to update user'));
    }
    return response.json();
  }

  async deleteAdminUser(userId: string): Promise<{ deleted: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to delete user'));
    }
    return response.json();
  }

  async listUserTokens(userId: string): Promise<TokenInfo[]> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/tokens`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to list tokens'));
    }
    return response.json();
  }

  async issueUserToken(userId: string, label?: string): Promise<IssuedTokenResponse> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/tokens`,
      { method: 'POST', headers: this.getHeaders(), body: JSON.stringify({ label: label ?? null }) }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to issue token'));
    }
    return response.json();
  }

  async rotateUserTokens(
    userId: string,
    label?: string
  ): Promise<RotatedTokensResponse> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/tokens/rotate`,
      { method: 'POST', headers: this.getHeaders(), body: JSON.stringify({ label: label ?? null }) }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to rotate tokens'));
    }
    return response.json();
  }

  async revokeUserToken(userId: string, tokenHashPrefix: string): Promise<{ revoked: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/tokens/${encodeURIComponent(tokenHashPrefix)}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to revoke token'));
    }
    return response.json();
  }

  async listUserPlatforms(userId: string): Promise<PlatformIdentity[]> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/platforms`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to list platforms'));
    }
    return response.json();
  }

  async linkUserPlatform(
    userId: string,
    body: { provider: ChatAppProvider; provider_user_id: string }
  ): Promise<PlatformIdentity> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/platforms`,
      { method: 'POST', headers: this.getHeaders(), body: JSON.stringify(body) }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to link platform'));
    }
    return response.json();
  }

  async unlinkUserPlatform(
    userId: string,
    provider: ChatAppProvider,
    providerUserId: string
  ): Promise<{ unlinked: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/admin/users/${encodeURIComponent(userId)}/platforms/${encodeURIComponent(provider)}/${encodeURIComponent(providerUserId)}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(await this._toastAndExtractError(response, 'Failed to unlink platform'));
    }
    return response.json();
  }

  // ── Self-service platform identities & per-thread chat-app bindings ──

  /** List the current user's own linked platform identities. Self-service
   * equivalent of `listUserPlatforms` (which is admin-only). Used by the
   * Chat App wizard to skip the link step when the user is already linked. */
  async listMyPlatforms(): Promise<PlatformIdentity[]> {
    const response = await fetch(`${this.getBaseUrl()}/me/platforms`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(response, 'Failed to list platforms')
      );
    }
    return response.json();
  }

  /** Issue a short-lived code the user types into the bot (or taps a
   * deep link) to associate their Telegram identity with their Nymeria
   * account. Replaces the previously admin-only `users link-platform` CLI. */
  async requestSelfPlatformLinkCode(
    provider: ChatAppProvider
  ): Promise<ChatAppBindCodeResponse> {
    const response = await fetch(
      `${this.getBaseUrl()}/me/platform-link-codes`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ provider })
      }
    );
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(
          response,
          'Failed to issue platform-link code'
        )
      );
    }
    return response.json();
  }

  /** Issue a short-lived code the user types into the bot to bind a chat
   * to this thread. The wizard polls `listThreadBindings` until the code is
   * consumed and the binding row appears. */
  async issueChatAppBindCode(
    threadId: string,
    provider: ChatAppProvider
  ): Promise<ChatAppBindCodeResponse> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/chatapp/bind-code`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({ provider })
      }
    );
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(
          response,
          'Failed to issue bind code'
        )
      );
    }
    return response.json();
  }

  /** List chat-app bindings for the given thread. Caller must own the thread. */
  async listThreadBindings(threadId: string): Promise<ChatAppBinding[]> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/chatapp/bindings`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(response, 'Failed to list bindings')
      );
    }
    return response.json();
  }

  /** Remove a chat-app binding. Caller must own the thread. */
  async unbindThreadChatApp(
    threadId: string,
    bindingId: number
  ): Promise<{ unbound: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/threads/${encodeURIComponent(threadId)}/chatapp/bindings/${bindingId}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(response, 'Failed to unbind chat')
      );
    }
    return response.json();
  }

  // ── BYO Telegram bots (user-owned, paste-token wizard) ────────────────

  /** List the current user's registered BYO Telegram bots. Tokens are never
   * surfaced — just metadata. The Chat App tab shows this list with the
   * bot username and last-seen timestamp. */
  async listMyTelegramBots(): Promise<MyTelegramBot[]> {
    const response = await fetch(`${this.getBaseUrl()}/me/telegram-bots`, {
      headers: this.getHeaders()
    });
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(
          response,
          'Failed to list Telegram bots'
        )
      );
    }
    return response.json();
  }

  /** Single-bot fetch. The wizard polls this after registration to wait
   * for the supervisor's first heartbeat (``last_seen_at`` becomes non-null)
   * before showing the bind-code step. */
  async getMyTelegramBot(botId: number): Promise<MyTelegramBot> {
    const response = await fetch(
      `${this.getBaseUrl()}/me/telegram-bots/${botId}`,
      { headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(response, 'Failed to load bot status')
      );
    }
    return response.json();
  }

  /** Register a BYO bot from a BotFather token. The server validates via
   * ``getMe``, encrypts the token, and starts polling on the next
   * supervisor tick (~15s). Idempotent — re-pasting the same token returns
   * the existing record rather than failing. */
  async registerMyTelegramBot(botToken: string): Promise<MyTelegramBot> {
    const response = await fetch(`${this.getBaseUrl()}/me/telegram-bots`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ bot_token: botToken })
    });
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(
          response,
          'Failed to register Telegram bot'
        )
      );
    }
    return response.json();
  }

  /** Remove a BYO bot. Cascades to its bindings — chats served by this bot
   * lose their thread routing immediately. */
  async removeMyTelegramBot(botId: number): Promise<{ deleted: boolean }> {
    const response = await fetch(
      `${this.getBaseUrl()}/me/telegram-bots/${botId}`,
      { method: 'DELETE', headers: this.getHeaders() }
    );
    if (!response.ok) {
      throw new Error(
        await this._toastAndExtractError(
          response,
          'Failed to remove Telegram bot'
        )
      );
    }
    return response.json();
  }
}
