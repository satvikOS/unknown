/**
 * Step-up prompt broker. The API client calls `request()` on HTTP 428; the StepUpDialog renders
 * whatever is pending and settles it. Concurrent requests share one prompt.
 */
import type { StepUpRequest } from '../api/client';

export interface StepUpSnapshot {
  open: boolean;
  request: StepUpRequest | null;
}

type Listener = () => void;

export class StepUpBroker {
  private snapshot: StepUpSnapshot = { open: false, request: null };
  private resolvers: ((ok: boolean) => void)[] = [];
  private readonly listeners = new Set<Listener>();

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): StepUpSnapshot => this.snapshot;

  private set(next: StepUpSnapshot): void {
    this.snapshot = next;
    for (const l of [...this.listeners]) l();
  }

  /** Ask the owner to re-enter the passphrase. Resolves true after a successful POST /api/auth/reauth. */
  request = (req: StepUpRequest): Promise<boolean> =>
    new Promise<boolean>((resolve) => {
      this.resolvers.push(resolve);
      if (!this.snapshot.open) this.set({ open: true, request: req });
    });

  /** Called by the dialog: true after reauth succeeded, false when cancelled. */
  settle(ok: boolean): void {
    const pending = this.resolvers;
    this.resolvers = [];
    this.set({ open: false, request: null });
    for (const r of pending) r(ok);
  }
}
