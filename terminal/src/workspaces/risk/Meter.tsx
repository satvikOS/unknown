/**
 * Usage meter for a loss budget or an exposure cap. Text carries the full reading ("1.35 USD of
 * 15.00 USD, 9.00%, 13.65 USD left") and the state word; the bar and its colour only supplement it.
 * Exposed as an ARIA meter with the same text as its value.
 */
import type { ReactNode } from 'react';
import { formatMoney } from '../../format/decimal';
import { StatusPill, type Tone } from '../../ui/primitives';
import { bpsToPercent } from '../../format/exact';
import type { BudgetState, BudgetUse } from './riskModel';
import './risk.css';

const STATE_TONE: Record<BudgetState, Tone> = { within: 'ok', approaching: 'warn', reached: 'bad' };

export interface UsageMeterProps {
  id: string;
  label: string;
  use: BudgetUse;
  currency: string;
  /** Words for the limit, e.g. "allowance", "trigger", "cap". */
  limitWord: string;
  /** State words, e.g. { reached: 'trigger reached' }. */
  stateText?: Partial<Record<BudgetState, string>>;
  /** Where the figure comes from (shown under the bar). */
  basis?: ReactNode;
}

export function UsageMeter({ id, label, use, currency, limitWord, stateText, basis }: UsageMeterProps) {
  const pct = use.bps === null ? null : use.bps;
  const width = pct === null ? 0 : Math.min(100, Math.max(0, Number(pct) / 100));
  const percentText = pct === null ? 'no ratio (limit is zero)' : bpsToPercent(pct);
  const words = { within: 'within', approaching: 'approaching', reached: 'reached', ...stateText };
  const reading = `${formatMoney(use.used, currency)} of ${formatMoney(use.limit, currency)} ${limitWord} (${percentText}); ${formatMoney(use.remaining, currency)} left`;
  const tone = STATE_TONE[use.state];
  return (
    <div className="rk-meter">
      <div className="rk-meter-head">
        <span id={`${id}-label`} className="rk-meter-label">
          {label}
        </span>
        <StatusPill label={words[use.state]} tone={tone} compact />
      </div>
      <div
        className="rk-meter-track"
        role="meter"
        aria-labelledby={`${id}-label`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct === null ? undefined : Math.min(100, Number(pct) / 100)}
        aria-valuetext={`${reading}; ${words[use.state]}`}
      >
        <div className={`rk-meter-fill tone-bg-${tone}`} style={{ width: `${width}%` }} />
        <div className="rk-meter-mark" style={{ left: '50%' }} aria-hidden="true" />
      </div>
      <div className="rk-meter-text num">
        <span>
          <strong>{formatMoney(use.used, currency)}</strong> of {formatMoney(use.limit, currency)} {limitWord}
        </span>
        <span>{percentText}</span>
        <span className="muted">{formatMoney(use.remaining, currency)} left</span>
      </div>
      {basis ? <div className="rk-meter-basis muted">{basis}</div> : null}
    </div>
  );
}
