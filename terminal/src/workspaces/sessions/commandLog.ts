/**
 * Which owner commands this browser sent for which session, so a session page can keep showing the
 * durable command id and its live status (from the shared command tracker) after navigation.
 * Only references are stored here; the status itself always comes from the tracker, and the
 * session's state always comes from the server. Nothing is optimistic.
 */
import type { TrackedCommand } from '../../state/commands';

export interface SessionCommandRef {
  commandId: string;
  kind: string;
  /** null for create_session until the core returns the new session id. */
  sessionId: string | null;
  atMs: number;
}

type Listener = () => void;

export class SessionCommandLog {
  private items: readonly SessionCommandRef[] = [];
  private readonly listeners = new Set<Listener>();

  constructor(private readonly limit = 100) {}

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): readonly SessionCommandRef[] => this.items;

  private emit(): void {
    for (const l of [...this.listeners]) l();
  }

  /** Record (or update) a command reference; newest first. */
  record(ref: SessionCommandRef): void {
    this.items = [ref, ...this.items.filter((r) => r.commandId !== ref.commandId)].slice(0, this.limit);
    this.emit();
  }

  /** Attach a command (e.g. create_session) to the session id the core returned. */
  bind(commandId: string, sessionId: string): void {
    const cur = this.items.find((r) => r.commandId === commandId);
    if (!cur || cur.sessionId === sessionId) return;
    this.items = this.items.map((r) => (r.commandId === commandId ? { ...r, sessionId } : r));
    this.emit();
  }

  forSession(sessionId: string): SessionCommandRef[] {
    return this.items.filter((r) => r.sessionId === sessionId);
  }

  latestFor(sessionId: string): SessionCommandRef | undefined {
    return this.items.find((r) => r.sessionId === sessionId);
  }

  clear(): void {
    this.items = [];
    this.emit();
  }
}

export const sessionCommandLog = new SessionCommandLog();

/** The core has not yet decided (sending) or its events are not yet visible (pending). */
export function isInFlight(t: Pick<TrackedCommand, 'status'> | undefined): boolean {
  return t?.status === 'sending' || t?.status === 'pending';
}

/** The session id a create_session response carries, if the core accepted it. */
export function createdSessionId(t: Pick<TrackedCommand, 'response'> | undefined): string | null {
  const body = t?.response?.accepted ? t.response.body : null;
  const id = body && typeof body === 'object' ? (body as { session_id?: unknown }).session_id : null;
  return typeof id === 'string' && id ? id : null;
}
