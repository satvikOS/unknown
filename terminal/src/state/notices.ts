/** Toast / notice area. Errors persist until dismissed; other tones expire. */

export type NoticeTone = 'info' | 'ok' | 'warn' | 'error';

export interface Notice {
  id: number;
  tone: NoticeTone;
  title: string;
  detail?: string;
  /** Durable command id, shown in monospace and copyable. */
  commandId?: string;
  createdAtMs: number;
}

type Listener = () => void;

export class NoticeStore {
  private items: Notice[] = [];
  private nextId = 1;
  private readonly listeners = new Set<Listener>();
  private readonly timers = new Map<number, ReturnType<typeof setTimeout>>();

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): readonly Notice[] => this.items;

  private emit(): void {
    for (const l of [...this.listeners]) l();
  }

  push(notice: Omit<Notice, 'id' | 'createdAtMs'>, ttlMs?: number): number {
    const id = this.nextId++;
    this.items = [...this.items, { ...notice, id, createdAtMs: Date.now() }].slice(-6);
    this.emit();
    const ttl = ttlMs ?? (notice.tone === 'error' ? 0 : notice.tone === 'warn' ? 12_000 : 6_000);
    if (ttl > 0) this.timers.set(id, setTimeout(() => this.dismiss(id), ttl));
    return id;
  }

  dismiss(id: number): void {
    const t = this.timers.get(id);
    if (t) clearTimeout(t);
    this.timers.delete(id);
    const next = this.items.filter((n) => n.id !== id);
    if (next.length !== this.items.length) {
      this.items = next;
      this.emit();
    }
  }

  clear(): void {
    for (const t of this.timers.values()) clearTimeout(t);
    this.timers.clear();
    this.items = [];
    this.emit();
  }
}
