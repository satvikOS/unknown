/**
 * Pure control-state logic for the red STOP and green START (ADR-0010, book Ch. 22). Unit tested.
 */
import type { ControlState, Overview, PreflightResponse, StopProgress } from '../api/types';
import type { Tone } from '../ui/primitives';

// ------------------------------------------------------------------ entry state (header)

export interface EntryState {
  /** Short status text, always shown in words. */
  label: string;
  tone: Tone;
  detail: string;
  /** True when green START would change something (stop latched or a resume hold). */
  startApplies: boolean;
}

export function entryState(control: ControlState | null | undefined, overview?: Pick<Overview, 'exceptions_open' | 'risk'> | null): EntryState {
  if (!control) {
    return { label: 'CONTROL UNKNOWN', tone: 'neutral', detail: 'No control record yet for this account.', startApplies: false };
  }
  if (control.stop_latched) {
    return {
      label: 'STOPPED',
      tone: 'bad',
      detail: `Stop latched (${control.latch_reason ?? 'no reason recorded'}): new entries are blocked.`,
      startApplies: true,
    };
  }
  if (control.resume_requires_owner) {
    return {
      label: 'HELD',
      tone: 'warn',
      detail: `Entries held until the owner starts (${control.resume_reason ?? 'resume hold'}).`,
      startApplies: true,
    };
  }
  if (control.needs_reconciliation) {
    return { label: 'RECONCILING', tone: 'warn', detail: 'Entries wait for a clean reconciliation.', startApplies: false };
  }
  const blocking = (overview?.exceptions_open ?? []).filter((e) => e.blocking);
  if (blocking.length) {
    return {
      label: 'BLOCKED',
      tone: 'warn',
      detail: `${blocking.length} blocking exception${blocking.length === 1 ? '' : 's'} open.`,
      startApplies: false,
    };
  }
  if (overview?.risk?.breach) {
    return { label: 'BLOCKED', tone: 'bad', detail: `Loss budget breached: ${overview.risk.breach}.`, startApplies: false };
  }
  return { label: 'ENTRIES PERMITTED', tone: 'ok', detail: 'No stop, hold, reconciliation or exception gate is active.', startApplies: false };
}

// ------------------------------------------------------------------ stop progress

export type StepState = 'done' | 'pending' | 'attention';

export interface StopStep {
  id: 'entries_blocked' | 'cancels' | 'unknown_orders' | 'residual_positions' | 'reconciled_flat';
  label: string;
  state: StepState;
  value: string;
}

export function residualCount(progress: StopProgress): number {
  return Object.values(progress.residual_positions).reduce((n, byInst) => n + Object.keys(byInst).length, 0);
}

/**
 * The five things the owner must see after red (book Ch. 22). "Flat" is reported only from the
 * server's `reconciled_flat` (clean reconciliation, no positions, no open orders), never inferred from
 * an empty order list.
 */
export function stopSteps(progress: StopProgress): StopStep[] {
  const residual = residualCount(progress);
  const outstanding = progress.open_buys + progress.cancels_pending;
  return [
    {
      id: 'entries_blocked',
      label: 'New entries blocked',
      state: progress.entries_blocked ? 'done' : 'pending',
      value: progress.entries_blocked ? 'yes (stop latched)' : 'no: stop not latched',
    },
    {
      id: 'cancels',
      label: 'Open buys / cancels pending',
      state: outstanding === 0 ? 'done' : 'pending',
      value: `${progress.open_buys} open buy${progress.open_buys === 1 ? '' : 's'}, ${progress.cancels_pending} cancel${progress.cancels_pending === 1 ? '' : 's'} pending`,
    },
    {
      id: 'unknown_orders',
      label: 'Unknown order outcomes',
      state: progress.unknown_orders === 0 ? 'done' : 'attention',
      value:
        progress.unknown_orders === 0
          ? 'none'
          : `${progress.unknown_orders} unknown (stay reserved until resolved by query or reconciliation)`,
    },
    {
      id: 'residual_positions',
      label: 'Residual positions',
      state: residual === 0 ? 'done' : 'attention',
      value: residual === 0 ? 'none' : `${residual} open (not sold automatically)`,
    },
    {
      id: 'reconciled_flat',
      label: 'Reconciled flat',
      state: progress.reconciled_flat ? 'done' : 'pending',
      value: progress.reconciled_flat ? 'yes: clean reconciliation, no positions, no open orders' : 'not confirmed',
    },
  ];
}

// ------------------------------------------------------------------ preflight blockers

export interface BlockerInfo {
  code: string;
  /** Text after the first ":" in the code (e.g. instrument id or exception kinds). */
  subject: string | null;
  title: string;
  resolution: string;
  /** True when re-entering the passphrase (step-up) resolves it. */
  stepUp: boolean;
}

const BLOCKERS: Record<string, { title: string; resolution: string }> = {
  NO_ACTIVE_POLICY: { title: 'No active policy', resolution: 'Publish and activate a policy for this account.' },
  OWNER_REQUIRED: { title: 'Owner authority required', resolution: 'Only the owner can start.' },
  OWNER_REAUTHENTICATION_REQUIRED: {
    title: 'Passphrase re-entry required',
    resolution: 'Re-enter the passphrase (step-up). You will be asked when you request start.',
  },
  POLICY_MODE_MISMATCH: { title: 'Policy mode differs from account mode', resolution: 'Activate a policy whose mode matches the account.' },
  LIVE_NOT_AUTHORIZED: {
    title: 'Live trading not authorized',
    resolution: 'Deployment gates (GATE-01) are unresolved; live mode is refused in this release.',
  },
  EXECUTION_OWNERSHIP_NOT_HELD: {
    title: 'Execution ownership not held',
    resolution: 'Acquire execution ownership for this deployment (one executing deployment per account).',
  },
  BROKER_DISCONNECTED: { title: 'Broker disconnected', resolution: 'Restore the broker connection; reconciliation follows.' },
  BROKER_AUTHENTICATION_REQUIRED: {
    title: 'Broker authentication expired',
    resolution: 'Re-authenticate the broker session on the host running the core.',
  },
  CLOCK_SKEW: { title: 'Clock skew too large', resolution: 'Fix host time synchronisation (policy max_clock_skew_ms).' },
  DISK_SPACE: { title: 'Low disk space', resolution: 'Free disk space above the policy minimum (min_free_disk_mb).' },
  RECONCILIATION_REQUIRED: {
    title: 'Reconciliation required',
    resolution: 'Wait for a clean reconciliation in this process epoch; resolve any differences it reports.',
  },
  RECONCILIATION_STALE: { title: 'Reconciliation stale', resolution: 'Wait for the next clean reconciliation (policy max age).' },
  BLOCKING_EXCEPTIONS: { title: 'Blocking exceptions open', resolution: 'Investigate and resolve each blocking exception case.' },
  UNKNOWN_ORDERS: {
    title: 'Orders with unknown outcome',
    resolution: 'Wait for broker lookup or reconciliation to establish each order outcome.',
  },
  LOSS_BUDGET: { title: 'Loss budget breached', resolution: 'Review the breach; the policy loss budget blocks new exposure.' },
  VALUATION_STALE: { title: 'Valuation stale', resolution: 'Restore fresh quotes for held instruments so positions can be marked.' },
  MARKET_DATA_STALE: { title: 'Market data stale', resolution: 'Restore a fresh quote for this held instrument.' },
};

export function describeBlocker(code: string): BlockerInfo {
  const i = code.indexOf(':');
  const base = (i >= 0 ? code.slice(0, i) : code).trim();
  const subject = i >= 0 ? code.slice(i + 1).trim() : null;
  const known = BLOCKERS[base];
  return {
    code,
    subject,
    title: known?.title ?? base.replace(/_/g, ' ').toLowerCase().replace(/^\w/, (c) => c.toUpperCase()),
    resolution: known?.resolution ?? 'Reported by the core preflight; resolve the named cause.',
    stepUp: base === 'OWNER_REAUTHENTICATION_REQUIRED',
  };
}

// ------------------------------------------------------------------ start availability

export interface StartAvailability {
  enabled: boolean;
  reason: string;
  /** Blockers that step-up resolves (the request will prompt for the passphrase). */
  needsStepUp: boolean;
}

export function startAvailability(
  control: ControlState | null | undefined,
  preflight: PreflightResponse | null | undefined,
  accountKey: string | null | undefined,
): StartAvailability {
  if (!accountKey) return { enabled: false, reason: 'Account not known yet.', needsStepUp: false };
  if (!control) return { enabled: false, reason: 'Control state not loaded.', needsStepUp: false };
  if (!preflight) return { enabled: false, reason: 'Preflight not loaded.', needsStepUp: false };
  if (!control.stop_latched && !control.resume_requires_owner) {
    return { enabled: false, reason: 'Nothing to start: no stop is latched and no resume hold is active.', needsStepUp: false };
  }
  const hard = preflight.blockers.filter((b) => !describeBlocker(b).stepUp);
  const needsStepUp = hard.length !== preflight.blockers.length;
  if (hard.length) {
    return {
      enabled: false,
      reason: `${hard.length} blocker${hard.length === 1 ? '' : 's'} must be resolved first. There is no bypass.`,
      needsStepUp,
    };
  }
  return {
    enabled: true,
    reason: needsStepUp ? 'Ready; you will be asked to re-enter the passphrase.' : 'Preflight clear.',
    needsStepUp,
  };
}

/** Blockers attached to a PREFLIGHT_FAILED rejection (details.blockers), if any. */
export function rejectionBlockers(details: Record<string, unknown> | undefined): string[] {
  const b = details?.blockers;
  return Array.isArray(b) ? b.filter((x): x is string => typeof x === 'string') : [];
}
