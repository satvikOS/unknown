/**
 * Owner authentication state (ADR-0013). The session cookie is HttpOnly; this store only knows
 * whether the API considers us logged in, when the passphrase was last entered, and any error.
 */
import type { ApiClient } from '../api/client';
import { describeError, InvalidPassphraseError, RateLimitedError } from '../api/client';

export type AuthStatus = 'checking' | 'authenticated' | 'unauthenticated' | 'unreachable';

export interface AuthSnapshot {
  status: AuthStatus;
  /** Last passphrase entry (login or step-up), server clock, ns. */
  authenticatedAtNs: number | null;
  /** Server clock minus browser clock at the last session check, ms (for step-up age display). */
  clockOffsetMs: number;
  error: string | null;
  /** Why we returned to the login screen, e.g. "session expired". */
  notice: string | null;
}

type Listener = () => void;

export class AuthStore {
  private snapshot: AuthSnapshot = {
    status: 'checking',
    authenticatedAtNs: null,
    clockOffsetMs: 0,
    error: null,
    notice: null,
  };
  private readonly listeners = new Set<Listener>();

  constructor(private readonly client: ApiClient) {}

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): AuthSnapshot => this.snapshot;

  private set(patch: Partial<AuthSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const l of [...this.listeners]) l();
  }

  /** GET /api/auth/session. */
  async check(): Promise<void> {
    try {
      const s = await this.client.session();
      const ended = !s.authenticated && this.snapshot.status === 'authenticated';
      this.set({
        status: s.authenticated ? 'authenticated' : 'unauthenticated',
        authenticatedAtNs: s.authenticated_at_ns,
        clockOffsetMs: Math.round(s.now_ns / 1e6 - Date.now()),
        error: null,
        ...(ended ? { notice: 'Your session ended (sessions do not survive an API restart). Sign in again.' } : {}),
      });
    } catch (err) {
      this.set({ status: 'unreachable', error: describeError(err) });
    }
  }

  /** Returns an error message for the login form, or null on success. */
  async login(passphrase: string): Promise<string | null> {
    try {
      const r = await this.client.login(passphrase);
      this.set({ status: 'authenticated', authenticatedAtNs: r.authenticated_at_ns, error: null, notice: null });
      return null;
    } catch (err) {
      if (err instanceof InvalidPassphraseError) return 'That passphrase was not accepted.';
      if (err instanceof RateLimitedError) return `${err.serverMessage ?? 'Too many attempts'}. Wait before retrying.`;
      return describeError(err);
    }
  }

  /** Step-up re-authentication. Returns an error message, or null on success. */
  async reauth(passphrase: string): Promise<string | null> {
    try {
      const r = await this.client.reauth(passphrase);
      this.set({ authenticatedAtNs: r.authenticated_at_ns });
      return null;
    } catch (err) {
      if (err instanceof InvalidPassphraseError) return 'That passphrase was not accepted.';
      if (err instanceof RateLimitedError) return `${err.serverMessage ?? 'Too many attempts'}. Wait before retrying.`;
      return describeError(err);
    }
  }

  async logout(): Promise<void> {
    try {
      await this.client.logout();
    } finally {
      this.set({ status: 'unauthenticated', authenticatedAtNs: null, notice: 'Signed out.' });
    }
  }

  /** Any 401 from the API lands here. */
  markUnauthenticated(notice = 'Your session ended. Sign in again.'): void {
    if (this.snapshot.status === 'unauthenticated') return;
    this.client.setCsrfToken(null);
    this.set({ status: 'unauthenticated', authenticatedAtNs: null, notice });
  }
}
