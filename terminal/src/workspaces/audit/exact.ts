/**
 * Display-side journal checks ("this entry balances per currency") on the shared exact decimal
 * arithmetic (format/exact.ts, BigInt, no floating point). The ledger's own verification stays on the server.
 */
import { sumDecimals, toScaled } from '../../format/exact';

export { sumDecimals };

export function isZeroDecimal(value: string): boolean {
  return toScaled(value).units === 0n;
}

/** Per-currency totals of signed amounts, currencies sorted. */
export function totalsByCurrency(lines: readonly { currency: string; amount: string }[]): [string, string][] {
  const by = new Map<string, string[]>();
  for (const l of lines) {
    const list = by.get(l.currency) ?? [];
    list.push(l.amount);
    by.set(l.currency, list);
  }
  return [...by.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([ccy, amounts]) => [ccy, sumDecimals(amounts)]);
}
