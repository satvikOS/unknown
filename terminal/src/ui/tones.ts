/** Status-to-tone mappings shared by workspaces (the visible text always carries the status). */
import type { Tone } from './primitives';

export function sessionStateTone(state: string): Tone {
  switch (state) {
    case 'RUNNING':
      return 'ok';
    case 'READY':
    case 'REQUESTED':
    case 'COMPLETED_UNSETTLED':
    case 'RECONCILING':
      return 'info';
    case 'WAITING_FUNDS':
    case 'SUSPENDED':
    case 'WINDING_DOWN':
      return 'warn';
    case 'FAILED_REVIEW':
      return 'bad';
    default:
      return 'neutral'; // COMPLETED_SETTLED, CLOSED, CANCELLED
  }
}

export function orderStateTone(state: string): Tone {
  switch (state) {
    case 'UNKNOWN':
      return 'bad';
    case 'CANCEL_PENDING':
    case 'SUBMIT_PENDING':
    case 'RESERVED':
      return 'warn';
    case 'ACKNOWLEDGED':
    case 'PARTIALLY_FILLED':
      return 'info';
    case 'FILLED':
      return 'ok';
    default:
      return 'neutral';
  }
}

export function severityTone(severity: string): Tone {
  switch (severity) {
    case 'critical':
    case 'high':
      return 'bad';
    case 'medium':
      return 'warn';
    default:
      return 'info';
  }
}

export function reconciliationTone(status: string): Tone {
  return status === 'CLEAN' ? 'ok' : status === 'INCOMPLETE' ? 'warn' : 'bad';
}

/** Register status words (ADR-0001) to tones. */
export function registerStatusTone(status: string): Tone {
  const s = status.toLowerCase();
  if (/(verified|tested|implemented|done|available)/.test(s)) return 'ok';
  if (/(blocked|refused|unavailable|not[-_ ]?available)/.test(s)) return 'bad';
  if (/(progress|partial|smoke)/.test(s)) return 'warn';
  return 'neutral';
}
