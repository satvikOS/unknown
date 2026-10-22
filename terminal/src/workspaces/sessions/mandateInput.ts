/**
 * Client-side check of a create/amend session request (usability only: the core repeats every
 * check in Mandate.from_template and create_session, and decides funding itself).
 */
import type { Tone } from '../../ui/primitives';
import { addFixed, cmpFixed, divFixed, fixedToString, mulInt, parseFixed, requiredReturnPct, subFixed, type Fixed } from './decimalMath';
import { bpsToPercent, moneyText, pctText } from './format';

export interface MandateDefaults {
  seed: string;
  target: string;
}

export interface MandatePolicyContext {
  currency: string;
  /** Ledger scale for the currency (the core rejects more fraction digits). */
  ledgerScale: number;
  /** account_limits.authorized_capital of the policy that will govern the mandate. */
  authorizedCapitalLimit?: string;
  /** Fee policy fields used for the reservation envelope. */
  feeMinimum?: string;
  feeEstimateMarginBps?: number;
}

export interface FundingContext {
  /** /api/buckets owner "unallocated" available_cash. */
  unallocatedAvailable?: string;
  /** account_limits.operational_buffer. */
  operationalBuffer?: string;
  stopLatched?: boolean;
  needsReconciliation?: boolean;
  blockingExceptions?: number;
}

export interface MandateFieldError {
  field: 'seed' | 'target';
  message: string;
}

export interface FeasibilityNote {
  tone: Tone;
  text: string;
}

export interface MandateCheck {
  ok: boolean;
  /** Values the request will carry (input or default), as typed (trimmed). */
  seed: string;
  target: string;
  /** Payload overrides: only fields the owner actually entered. */
  overrides: { seed?: string; target_terminal_equity?: string };
  errors: MandateFieldError[];
  requiredPct: string | null;
  notes: FeasibilityNote[];
}

const PLAIN_AMOUNT = /^\d+(?:\.\d+)?$/;



function checkAmount(field: 'seed' | 'target', raw: string, ctx: MandatePolicyContext): MandateFieldError | null {
  const label = field === 'seed' ? 'Seed' : 'Target terminal equity';
  if (!PLAIN_AMOUNT.test(raw)) return { field, message: `${label} must be a plain amount like 100.00 (digits and one decimal point, no sign or separators).` };
  const frac = raw.split('.')[1] ?? '';
  if (frac.length > ctx.ledgerScale) return { field, message: `${label} has more than ${ctx.ledgerScale} decimal places (${ctx.currency} ledger scale).` };
  return null;
}

/** Fee envelope a buy reserves at minimum: the minimum fee plus the estimate margin, rounded up (risk/fees.py envelope). */
export function minimumFeeEnvelope(feeMinimum: string, marginBps: number, ledgerScale: number): string | null {
  const min = parseFixed(feeMinimum);
  if (!min || !Number.isInteger(marginBps) || marginBps < 0) return null;
  const est: Fixed = cmpFixed(min, { m: 0n, s: 0 }) < 0 ? { m: 0n, s: ledgerScale } : divFixed(min, { m: 1n, s: 0 }, ledgerScale, 'CEILING');
  const margin = divFixed(mulInt(est, BigInt(marginBps)), { m: 10_000n, s: 0 }, ledgerScale, 'CEILING');
  return fixedToString(addFixed(est, margin));
}

/**
 * Client-side check of a create/amend request. Usability only: the core repeats every check
 * (Mandate.from_template, authorized capital) and decides funding itself.
 */
export function checkMandateInput(
  input: { seed: string; target: string },
  defaults: MandateDefaults,
  policy: MandatePolicyContext,
  funding: FundingContext = {},
): MandateCheck {
  const seedRaw = input.seed.trim();
  const targetRaw = input.target.trim();
  const seed = seedRaw || defaults.seed;
  const target = targetRaw || defaults.target;
  const overrides: MandateCheck['overrides'] = {};
  if (seedRaw) overrides.seed = seedRaw;
  if (targetRaw) overrides.target_terminal_equity = targetRaw;
  const errors: MandateFieldError[] = [];
  const notes: FeasibilityNote[] = [];

  const seedErr = checkAmount('seed', seed, policy);
  const targetErr = checkAmount('target', target, policy);
  if (seedErr) errors.push(seedErr);
  if (targetErr) errors.push(targetErr);
  const s = seedErr ? null : parseFixed(seed);
  const t = targetErr ? null : parseFixed(target);
  if (s && s.m <= 0n) errors.push({ field: 'seed', message: 'Seed must be greater than zero.' });
  if (s && t && cmpFixed(t, s) <= 0) {
    errors.push({ field: 'target', message: 'Target terminal equity must exceed the seed: it is the equity to end with, not the profit.' });
  }
  if (s && policy.authorizedCapitalLimit !== undefined) {
    const lim = parseFixed(policy.authorizedCapitalLimit);
    if (lim && cmpFixed(s, lim) > 0) {
      errors.push({
        field: 'seed',
        message: `Seed exceeds the policy’s authorized capital limit of ${moneyText(policy.authorizedCapitalLimit, policy.currency)}; the core rejects it (MANDATE_INVALID).`,
      });
    }
  }
  const requiredPct = s && t && s.m > 0n ? requiredReturnPct(seed, target) : null;

  if (s && s.m > 0n && policy.feeMinimum !== undefined && policy.feeEstimateMarginBps !== undefined) {
    const env = minimumFeeEnvelope(policy.feeMinimum, policy.feeEstimateMarginBps, policy.ledgerScale);
    const envF = env ? parseFixed(env) : null;
    if (env && envF && envF.m > 0n) {
      const ccy = policy.currency;
      if (cmpFixed(s, envF) <= 0) {
        notes.push({
          tone: 'bad',
          text: `Infeasible: every buy reserves a fee envelope of at least ${moneyText(env, ccy)} (policy minimum fee ${moneyText(policy.feeMinimum, ccy)} plus a ${bpsToPercent(policy.feeEstimateMarginBps)} estimate margin), which is not less than this seed, so no buy order can be reserved.`,
        });
      } else {
        const share = fixedToString(mulInt(divFixed(envF, s, 4, 'HALF_EVEN'), 100n));
        notes.push({
          tone: cmpFixed(parseFixed(share)!, { m: 10n, s: 0 }) >= 0 ? 'warn' : 'info',
          text: `Each buy reserves at least ${moneyText(env, ccy)} for fees (policy minimum ${moneyText(policy.feeMinimum, ccy)} plus ${bpsToPercent(policy.feeEstimateMarginBps)} margin): ${pctText(share)} of this seed.`,
        });
      }
    }
  }

  if (s && s.m > 0n) {
    if (funding.stopLatched) {
      notes.push({ tone: 'warn', text: 'A stop is latched: the request will be recorded but wait for funds until START.' });
    } else if (funding.needsReconciliation) {
      notes.push({ tone: 'warn', text: 'Reconciliation is required: the request will wait for funds until a clean reconciliation.' });
    } else if (funding.blockingExceptions) {
      notes.push({ tone: 'warn', text: 'Blocking exceptions are open: the request will wait for funds until they are resolved.' });
    }
    if (funding.unallocatedAvailable !== undefined && funding.operationalBuffer !== undefined) {
      const avail = parseFixed(funding.unallocatedAvailable);
      const buf = parseFixed(funding.operationalBuffer);
      if (avail && buf) {
        const usable = subFixed(avail, buf);
        const ccy = policy.currency;
        if (cmpFixed(s, usable) > 0) {
          notes.push({
            tone: 'warn',
            text: `Unallocated available cash ${moneyText(funding.unallocatedAvailable, ccy)} less the operational buffer ${moneyText(funding.operationalBuffer, ccy)} leaves ${moneyText(fixedToString(usable), ccy)}; the request would wait for funds with a shortfall of ${moneyText(fixedToString(subFixed(s, usable)), ccy)}.`,
          });
        } else {
          notes.push({
            tone: 'ok',
            text: `Unallocated available cash ${moneyText(funding.unallocatedAvailable, ccy)} less the operational buffer ${moneyText(funding.operationalBuffer, ccy)} covers this seed now. The core decides funding when it records the request.`,
          });
        }
      }
    }
  }
  return { ok: errors.length === 0, seed, target, overrides, errors, requiredPct, notes };
}
