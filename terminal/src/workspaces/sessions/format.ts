/** Display text for exact decimal strings (no floats; never rounds digits away). */
import { currencyMinorUnits, formatDecimal } from '../../format/decimal';
import { divFixed, fixedToString } from './decimalMath';

/** "1,234.50 USD": exact, grouped, at least the currency's minor units (display text only). */
export function moneyText(amount: string, currency: string): string {
  return `${formatDecimal(amount, { minFraction: currencyMinorUnits(currency) })} ${currency}`;
}

/** "5.00%" from the core's percent string ("5.0000"); exact, never rounds away digits. */
export function pctText(pct: string): string {
  return `${formatDecimal(pct, { minFraction: 2 })}%`;
}

/** Basis points as a percent without floats: 2000 -> "20%", 25 -> "0.25%", 0 -> "0%". */
export function bpsToPercent(bps: number): string {
  const f = divFixed({ m: BigInt(bps), s: 0 }, { m: 100n, s: 0 }, 2, 'HALF_EVEN');
  return `${fixedToString(f).replace(/\.?0+$/, '')}%`;
}
