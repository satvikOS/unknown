/**
 * Typed view of the policy fields that govern sessions (policy/model.py SessionTemplate,
 * AccountLimits, FeeModel, SuccessorRule, WindDown). Policy bodies arrive as JSON objects; every
 * field is read with a type check, and a missing or mistyped field is left undefined rather than
 * guessed. Money fields stay decimal strings.
 */
import type { PolicyBody } from '../../api/types';

export interface SessionPolicyView {
  policyHash: string;
  name: string;
  currency: string;
  mode: string | undefined;
  template: {
    seed?: string;
    target?: string;
    strategies?: string[];
    drawdownTriggerBps?: number;
    maxLoss?: string;
    autoStart?: boolean;
    /** null means the template sets no expiry. */
    expiresAfterS?: number | null;
    belowTarget?: string;
    maxResumes?: number;
  };
  limits: {
    authorizedCapital?: string;
    operationalBuffer?: string;
    maxOrderNotional?: string;
  };
  fees: { minimum?: string; estimateMarginBps?: number; perShare?: string; maximumBpsOfNotional?: number };
  successor: { createOnSuccess?: boolean; mandateSource?: string; reinvestProtected?: boolean };
  windDown: { orderStyle?: string; collarBps?: number; repriceAfterS?: number; maxAttempts?: number };
  onStopLiquidation?: string;
}

function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}
function str(v: unknown): string | undefined {
  return typeof v === 'string' ? v : undefined;
}
function int(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isInteger(v) ? v : undefined;
}
function bool(v: unknown): boolean | undefined {
  return typeof v === 'boolean' ? v : undefined;
}

export function readSessionPolicy(record: { policy_hash: string; name: string; body: PolicyBody }): SessionPolicyView {
  const b = record.body;
  const t = obj(b.session_template);
  const l = obj(b.account_limits);
  const f = obj(b.fees);
  const s = obj(b.successor);
  const w = obj(b.wind_down);
  const strategies = Array.isArray(t.strategy_eligibility) ? t.strategy_eligibility.filter((x): x is string => typeof x === 'string') : undefined;
  const expires = t.expires_after_s === null ? null : int(t.expires_after_s);
  return {
    policyHash: record.policy_hash,
    name: record.name,
    currency: typeof b.currency === 'string' ? b.currency : '',
    mode: str(b.mode),
    template: {
      seed: str(t.seed),
      target: str(t.target_terminal_equity),
      strategies,
      drawdownTriggerBps: int(t.session_drawdown_trigger_bps),
      maxLoss: str(t.session_max_loss),
      autoStart: bool(t.auto_start),
      expiresAfterS: expires,
      belowTarget: str(t.below_target_after_target_observed),
      maxResumes: int(t.max_resumes),
    },
    limits: {
      authorizedCapital: str(l.authorized_capital),
      operationalBuffer: str(l.operational_buffer),
      maxOrderNotional: str(l.max_order_notional),
    },
    fees: {
      minimum: str(f.minimum),
      estimateMarginBps: int(f.estimate_margin_bps),
      perShare: str(f.per_share),
      maximumBpsOfNotional: int(f.maximum_bps_of_notional),
    },
    successor: {
      createOnSuccess: bool(s.create_on_success),
      mandateSource: str(s.mandate_source),
      reinvestProtected: bool(s.reinvest_protected_proceeds),
    },
    windDown: {
      orderStyle: str(w.order_style),
      collarBps: int(w.collar_bps),
      repriceAfterS: int(w.reprice_after_s),
      maxAttempts: int(w.max_attempts),
    },
    onStopLiquidation: str(b.on_stop_liquidation),
  };
}

/** "300 s", "1 h 30 m", "2 d": durations from policy seconds (integers only). */
export function formatDurationS(seconds: number): string {
  if (seconds < 120) return `${seconds} s`;
  const m = Math.floor(seconds / 60);
  if (m < 120) return `${m} m${seconds % 60 ? ` ${seconds % 60} s` : ''}`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h${m % 60 ? ` ${m % 60} m` : ''}`;
  const d = Math.floor(h / 24);
  return `${d} d${h % 24 ? ` ${h % 24} h` : ''}`;
}
