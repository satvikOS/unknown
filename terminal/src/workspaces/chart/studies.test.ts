/**
 * Reference values for the studies. Each expected number is derived by hand from the definitions in studies.ts
 * (worked in the comments), so the implementation is checked against arithmetic, not against itself.
 */
import { describe, expect, it } from 'vitest';
import { atr, bollinger, ema, macd, rsi, sma, vwap, type Ohlcv } from './studies';

function close(actual: (number | null)[], expected: (number | null)[], digits = 4) {
  expect(actual.length).toBe(expected.length);
  expected.forEach((e, i) => {
    if (e === null) expect(actual[i]).toBeNull();
    else expect(actual[i]).toBeCloseTo(e, digits);
  });
}

const bar = (time: number, high: number, low: number, c: number, volume: number | null = 100): Ohlcv => ({
  time,
  open: c,
  high,
  low,
  close: c,
  volume,
});

describe('studies (chart.studies@1)', () => {
  it('SMA warms up for n-1 bars', () => {
    close(sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  });

  it('EMA is seeded with the SMA and uses alpha = 2/(n+1)', () => {
    // n=3, alpha=0.5: seed (2+4+6)/3 = 4; 0.5*8 + 0.5*4 = 6; 0.5*12 + 0.5*6 = 9
    close(ema([2, 4, 6, 8, 12], 3), [null, null, 4, 6, 9]);
  });

  it('Bollinger uses the population standard deviation', () => {
    // window [1,2,3]: mean 2, variance (1+0+1)/3 = 2/3, sd 0.816497; k=2 -> 2 +/- 1.632993
    const b = bollinger([1, 2, 3, 4], 3, 2);
    close(b.middle, [null, null, 2, 3]);
    close(b.upper, [null, null, 3.632993, 4.632993]);
    close(b.lower, [null, null, 0.367007, 1.367007]);
  });

  it('RSI uses Wilder smoothing', () => {
    // n=2, changes +1,-1,+1,+1. t=2: gain (1+0)/2=0.5, loss 0.5 -> RS 1 -> 50.
    // t=3: gain (0.5+1)/2=0.75, loss (0.5+0)/2=0.25 -> RS 3 -> 75. t=4: 0.875/0.125 = 7 -> 87.5
    close(rsi([1, 2, 1, 2, 3], 2), [null, null, 50, 75, 87.5]);
    close(rsi([1, 2, 3, 4], 2), [null, null, 100, 100]);
    close(rsi([5, 5, 5], 2), [null, null, 50]);
  });

  it('MACD is the EMA difference with an EMA signal', () => {
    // fast EMA2 (alpha 2/3) seed t1 = 1.5; t2 = 3.166667; t3 = 6.388889; t4 = 12.796296; t5 = 25.598765
    // slow EMA3 (alpha 1/2) seed t2 = 7/3; t3 = 5.166667; t4 = 10.583333; t5 = 21.291667
    // MACD t2..t5 = 0.833333, 1.222222, 2.212963, 4.307099
    // signal EMA2 seed t3 = (0.833333+1.222222)/2 = 1.027778; t4 = 1.817901; t5 = 3.477366
    const m = macd([1, 2, 4, 8, 16, 32], 2, 3, 2);
    close(m.macd, [null, null, 0.833333, 1.222222, 2.212963, 4.307099]);
    close(m.signal, [null, null, null, 1.027778, 1.817901, 3.477366]);
    close(m.histogram, [null, null, null, 0.194444, 0.395062, 0.829733]);
  });

  it('ATR uses true range from the previous close and Wilder smoothing', () => {
    // TR1 = max(2, |11-9|, |9-9|) = 2; TR2 = max(3, |12-10|, |9-10|) = 3; TR3 = max(1, 0, 1) = 1
    // ATR(2): t2 = (2+3)/2 = 2.5; t3 = (2.5*1 + 1)/2 = 1.75
    const bars = [bar(1, 10, 8, 9), bar(2, 11, 9, 10), bar(3, 12, 9, 11), bar(4, 11, 10, 10.5)];
    close(atr(bars, 2), [null, null, 2.5, 1.75]);
  });

  it('VWAP is cumulative per session and resets', () => {
    // typical (12+9+9)/3 = 10 (vol 100), (13+11+12)/3 = 12 (vol 300) -> (1000 + 3600)/400 = 11.5
    const bars = [bar(1, 12, 9, 9, 100), bar(2, 13, 11, 12, 300), bar(3, 21, 18, 18, 10)];
    close(vwap(bars, (b) => (b.time < 3 ? 'd1' : 'd2')), [10, 11.5, 19]);
    close(vwap([bar(1, 1, 1, 1, null), bar(2, 2, 2, 2, 5)]), [null, null]);
  });
});
