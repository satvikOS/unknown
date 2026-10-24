/**
 * Baskets and command outcomes for the order workstation.
 *
 * A basket is several intents typed one per line ("BUY 10 AAPL.XNAS 189.50"). Each line becomes its own
 * submit_intent command with its own order id and command id, and is shown with its own outcome. Nothing is
 * netted, grouped or retried: an outcome the API could not confirm is UNKNOWN until the stored result is looked up
 * (state/commands.ts), and a resend only ever reuses the same command id and payload.
 */
import type { Instrument, Session } from '../../api/types';
import { lookupNote, type TrackedCommand } from '../../state/commands';
import type { Tone } from '../../ui/primitives';
import { instrumentForSecurity } from './ladder';
import { validateTicket, type TicketEligibility, type TicketInput } from './ticket';

export const MAX_BASKET_LINES = 20;

export interface BasketItem {
  lineNo: number;
  raw: string;
  input: TicketInput | null;
  instrument: Instrument | undefined;
  /** Why this line cannot be sent (syntax, or the ticket validation the single ticket runs). */
  error: string | null;
}

const LINE_RE = /^(BUY|SELL)\s+(\S+)\s+(\S+)\s+@?\s*(\S+)$/i;

/** Parses a basket, one intent per line; blank lines and lines starting with "#" are skipped. */
export function parseBasket(
  text: string,
  ctx: { session: Session | undefined; instruments: readonly Instrument[]; eligibility?: TicketEligibility; deadlineSeconds: string; timeInForce: string },
): BasketItem[] {
  const items: BasketItem[] = [];
  text.split('\n').forEach((line, i) => {
    const raw = line.trim();
    if (!raw || raw.startsWith('#')) return;
    const m = LINE_RE.exec(raw);
    if (!m) {
      items.push({ lineNo: i + 1, raw, input: null, instrument: undefined, error: 'Write SIDE QUANTITY SECURITY LIMIT, for example BUY 10 AAPL.XNAS 189.50.' });
      return;
    }
    const [, side = '', qty = '', sec = '', limit = ''] = m;
    const instrument = ctx.instruments.find((x) => x.instrument_id === sec) ?? instrumentForSecurity(sec, ctx.instruments);
    const input: TicketInput = {
      sessionId: ctx.session?.session_id ?? '',
      instrumentId: instrument?.instrument_id ?? '',
      side: side.toUpperCase() as 'BUY' | 'SELL',
      quantity: qty,
      limitPrice: limit,
      deadlineSeconds: ctx.deadlineSeconds,
      timeInForce: ctx.timeInForce,
      tradingSession: 'REGULAR',
    };
    const issues = validateTicket(input, { session: ctx.session, instrument, eligibility: ctx.eligibility });
    const first = Object.values(issues.errors)[0];
    const error = !instrument ? `${sec} is not one instrument in the instrument master (use SYMBOL.MIC or the instrument id).` : first ?? null;
    items.push({ lineNo: i + 1, raw, input, instrument, error });
  });
  if (items.length > MAX_BASKET_LINES) {
    for (const it of items.slice(MAX_BASKET_LINES)) it.error = `A basket holds at most ${MAX_BASKET_LINES} intents.`;
  }
  return items;
}

export type OutcomeKind = 'NOT_SENT' | 'SENDING' | 'ACCEPTED' | 'REJECTED' | 'UNKNOWN' | 'NOT_EXECUTED' | 'CANCELLED';

export interface Outcome {
  kind: OutcomeKind;
  /** Short status word shown in the row. */
  label: string;
  tone: Tone;
  detail: string;
}

/** What is known about one sent intent, from its tracked command (never inferred, never retried here). */
export function outcomeOf(t: TrackedCommand | undefined): Outcome {
  if (!t) return { kind: 'NOT_SENT', label: 'not sent', tone: 'neutral', detail: 'Not sent.' };
  switch (t.status) {
    case 'sending':
      return { kind: 'SENDING', label: 'sending', tone: 'info', detail: `Command ${t.commandId} is being sent.` };
    case 'pending':
    case 'applied':
      return { kind: 'ACCEPTED', label: 'accepted', tone: 'ok', detail: 'The core accepted the intent and reserved for it; its broker status is in the blotter.' };
    case 'rejected': {
      const code = t.rejection?.code ?? 'REJECTED';
      return { kind: 'REJECTED', label: `rejected ${code}`, tone: 'bad', detail: t.error ?? `The core refused the intent (${code}); nothing was reserved.` };
    }
    case 'cancelled':
      return { kind: 'CANCELLED', label: 'not sent', tone: 'neutral', detail: 'The step-up prompt was dismissed; nothing was sent to the core.' };
    case 'failed': {
      const note = lookupNote(t);
      if (t.lookup?.state === 'not_recorded' && t.lookup.definitive) {
        return { kind: 'NOT_EXECUTED', label: 'not executed', tone: 'warn', detail: note ?? 'The core has no record of the command.' };
      }
      if (t.outcomeUnknown) {
        return { kind: 'UNKNOWN', label: 'UNKNOWN', tone: 'warn', detail: note ?? 'The outcome is unknown. Look up the stored result before acting; nothing is resent automatically.' };
      }
      return { kind: 'NOT_EXECUTED', label: 'refused', tone: 'bad', detail: t.error ?? 'Refused by the API before the core.' };
    }
  }
}
