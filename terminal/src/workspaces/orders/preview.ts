/**
 * Live pretrade preview for the order ticket, ladder and basket: GET /api/portfolio/whatif runs the core's own
 * submit_intent path (pretrade checks, fee envelope from the broker's fee schedule, reservation) on a private copy
 * of the core (analytics/whatif.py). It reserves nothing, places nothing and authorizes nothing; the real intent
 * passes the same gate again when it is sent. The terminal only splits the core's required cash into the notional
 * (ceil(qty x limit x multiplier) at the ledger scale, as the core computes it) and the fee envelope.
 */
import type { Instrument } from '../../api/types';
import { parseDecision, type PretradeCheck, type PretradeDecision } from './decision';
import { ceilToScale, mulExact, subExact, writtenScale } from './exact';
import { DEFAULT_TIF, isPositiveDecimal, type TicketInput } from './ticket';

export const PREVIEW_ENDPOINT = '/api/portfolio/whatif';
export const PREVIEW_POLL_MS = 2000;

export interface PreviewParams {
  [key: string]: string;
  session: string;
  instrument: string;
  side: 'BUY' | 'SELL';
  quantity: string;
  limit: string;
  tif: string;
}

/** Query parameters for the preview, or null while the ticket lacks a field the core needs to evaluate it. */
export function previewParams(input: TicketInput): PreviewParams | null {
  if (!input.sessionId || !input.instrumentId) return null;
  if (input.side !== 'BUY' && input.side !== 'SELL') return null;
  if (!isPositiveDecimal(input.quantity) || !isPositiveDecimal(input.limitPrice)) return null;
  return {
    session: input.sessionId,
    instrument: input.instrumentId,
    side: input.side,
    quantity: input.quantity.trim(),
    limit: input.limitPrice.trim(),
    tif: (input.timeInForce ?? DEFAULT_TIF).trim(),
  };
}

export interface ReservationRow {
  kind: string;
  unit: string;
  amount: string;
  status: string;
}

export interface PretradePreview {
  /** The core's label: hypothetical, computed on a private copy. */
  label: string;
  approved: boolean;
  /** "APPROVED", the decisive check, or a code raised before the checks ran. */
  code: string;
  message: string | null;
  decision: PretradeDecision | null;
  /** Cash the core would reserve for a BUY (notional + fee envelope); null for a SELL or a refusal. */
  requiredCash: string | null;
  notional: string | null;
  feeEnvelope: string | null;
  /** The FEE_SCHEDULE check's text: which schedule priced the envelope, or why none could. */
  feeSource: PretradeCheck | null;
  reservation: ReservationRow[];
  snapshotSeq: number | null;
  /** Set when the core faulted on the private copy: no decision is available. */
  fault: string | null;
}

function rec(x: unknown): Record<string, unknown> | null {
  return typeof x === 'object' && x !== null && !Array.isArray(x) ? (x as Record<string, unknown>) : null;
}

/** Splits required cash into notional and envelope exactly as the core built it (ceil at the ledger scale). */
export function splitRequiredCash(
  requiredCash: string,
  quantity: string,
  limit: string,
  multiplier: string,
): { notional: string; feeEnvelope: string } | null {
  const raw = mulExact(quantity, limit);
  const product = raw === null ? null : mulExact(raw, multiplier);
  if (product === null) return null;
  const notional = ceilToScale(product, writtenScale(requiredCash));
  const fee = notional === null ? null : subExact(requiredCash, notional);
  return notional !== null && fee !== null ? { notional, feeEnvelope: fee } : null;
}

export function parsePreview(x: unknown, intent: PreviewParams | null, instrument: Instrument | undefined): PretradePreview | null {
  const r = rec(x);
  if (!r || r.kind !== 'whatif') return null;
  const label = typeof r.label === 'string' ? r.label : 'HYPOTHETICAL';
  if (typeof r.fault === 'string') {
    return { label, approved: false, code: 'FAULT', message: typeof r.reason === 'string' ? r.reason : null, decision: null, requiredCash: null,
      notional: null, feeEnvelope: null, feeSource: null, reservation: [], snapshotSeq: null, fault: r.fault };
  }
  const decision = parseDecision(r.decision);
  const requiredCash = typeof r.required_cash === 'string' ? r.required_cash : null;
  const split = requiredCash && intent && instrument
    ? splitRequiredCash(requiredCash, intent.quantity, intent.limit, instrument.multiplier || '1')
    : null;
  const reservation: ReservationRow[] = Array.isArray(r.reservation_preview)
    ? r.reservation_preview.flatMap((h) => {
        const o = rec(h);
        return o && typeof o.amount === 'string'
          ? [{ kind: String(o.kind ?? ''), unit: String(o.unit ?? ''), amount: o.amount, status: String(o.status ?? '') }]
          : [];
      })
    : [];
  return {
    label,
    approved: r.approved === true,
    code: typeof r.code === 'string' ? r.code : decision?.code ?? 'UNKNOWN',
    message: typeof r.message === 'string' ? r.message : null,
    decision,
    requiredCash,
    notional: split?.notional ?? null,
    feeEnvelope: split?.feeEnvelope ?? null,
    feeSource: decision?.checks.find((c) => c.check === 'FEE_SCHEDULE') ?? null,
    reservation,
    snapshotSeq: typeof r.snapshot_seq === 'number' ? r.snapshot_seq : null,
    fault: null,
  };
}

/** True when the preview was computed for exactly these parameters (a slower answer for an older ticket is not shown as current). */
export function previewMatches(x: unknown, p: PreviewParams | null): boolean {
  const intent = rec(rec(x)?.intent);
  if (!p || !intent) return false;
  return intent.session_id === p.session && intent.instrument_id === p.instrument && intent.side === p.side
    && intent.quantity === p.quantity && intent.limit_price === p.limit && (intent.time_in_force ?? DEFAULT_TIF) === p.tif;
}
