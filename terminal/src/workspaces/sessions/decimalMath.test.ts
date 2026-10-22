import { describe, expect, it } from 'vitest';
import {
  addFixed,
  cmpFixed,
  compareDecimals,
  divFixed,
  fixedToString,
  mulInt,
  parseFixed,
  requiredReturnPct,
  roundDiv,
  subDecimals,
  subFixed,
  sumDecimals,
} from './decimalMath';

const f = (s: string) => parseFixed(s)!;

describe('parseFixed / fixedToString', () => {
  it('round-trips while preserving scale', () => {
    for (const s of ['0', '-0.005', '300.000000', '12.340', '-1.050', '9007199254740993.000001']) {
      expect(fixedToString(f(s))).toBe(s.startsWith('-0.') || s === '0' ? s : s);
    }
    expect(fixedToString({ m: 5n, s: 3 })).toBe('0.005');
    expect(fixedToString({ m: -5n, s: 3 })).toBe('-0.005');
  });

  it('rejects anything that is not a plain decimal', () => {
    for (const s of ['', '1e3', '1,000', '.5', '5.', 'NaN', ' - 1', '0x10', '1.2.3']) expect(parseFixed(s)).toBeNull();
    expect(parseFixed('+5')).toEqual({ m: 5n, s: 0 });
  });
});

describe('exact arithmetic', () => {
  it('adds and subtracts across scales without floats', () => {
    expect(fixedToString(addFixed(f('0.1'), f('0.2')))).toBe('0.3');
    expect(fixedToString(subFixed(f('105.00'), f('100.000000')))).toBe('5.000000');
    expect(sumDecimals(['300.000000', '-100.00', '0.005'])).toBe('200.005000');
    expect(sumDecimals(['1', 'x'])).toBeNull();
    expect(subDecimals('9007199254740993', '1')).toBe('9007199254740992');
    expect(cmpFixed(f('10.50'), f('10.5'))).toBe(0);
    expect(compareDecimals('-2', '1')).toBe(-1);
    expect(compareDecimals('bad', '1')).toBeNull();
    expect(fixedToString(mulInt(f('0.0500'), 100n))).toBe('5.0000');
  });

  it('ports the core round_div for every mode, including negatives', () => {
    // 7 / 2 = 3.5 ; -7 / 2 = -3.5 ; 5 / 2 = 2.5
    expect(roundDiv(7n, 2n, 'HALF_EVEN')).toBe(4n);
    expect(roundDiv(5n, 2n, 'HALF_EVEN')).toBe(2n);
    expect(roundDiv(-7n, 2n, 'HALF_EVEN')).toBe(-4n);
    expect(roundDiv(-5n, 2n, 'HALF_EVEN')).toBe(-2n);
    expect(roundDiv(7n, 2n, 'FLOOR')).toBe(3n);
    expect(roundDiv(-7n, 2n, 'FLOOR')).toBe(-4n);
    expect(roundDiv(7n, 2n, 'CEILING')).toBe(4n);
    expect(roundDiv(-7n, 2n, 'CEILING')).toBe(-3n);
    expect(roundDiv(-7n, 2n, 'DOWN')).toBe(-3n);
    expect(roundDiv(7n, -2n, 'FLOOR')).toBe(-4n);
    expect(roundDiv(6n, 3n, 'CEILING')).toBe(2n);
    expect(() => roundDiv(1n, 0n, 'FLOOR')).toThrow(RangeError);
  });

  it('divides at an explicit scale', () => {
    expect(fixedToString(divFixed(f('1'), f('3'), 4, 'HALF_EVEN'))).toBe('0.3333');
    expect(fixedToString(divFixed(f('2'), f('3'), 4, 'HALF_EVEN'))).toBe('0.6667');
    expect(fixedToString(divFixed(f('1.200000'), f('100.00'), 4, 'HALF_EVEN'))).toBe('0.0120');
  });
});

describe('requiredReturnPct (core rule: ratio HALF_EVEN to 4 places, then x100)', () => {
  it('matches the figures the core records', () => {
    expect(requiredReturnPct('100.00', '105.00')).toBe('5.0000'); // SessionRequested info, live core
    expect(requiredReturnPct('1', '10000')).toBe('999900.0000'); // book Ch. 22: $1 -> $10,000
    expect(requiredReturnPct('1.00', '10000.00')).toBe('999900.0000');
    expect(requiredReturnPct('3', '4')).toBe('33.3300'); // 0.33333 -> 0.3333
    expect(requiredReturnPct('300', '300.01')).toBe('0.0000'); // 0.0000333 rounds to 0 at 4 places
    expect(requiredReturnPct('8', '9')).toBe('12.5000'); // exact
  });

  it('rounds exact halves to even at the 4th place', () => {
    // (target - seed)/seed = 0.00005 exactly -> 0.0000 (even), 0.00015 -> 0.0002
    expect(requiredReturnPct('20000', '20001')).toBe('0.0000');
    expect(requiredReturnPct('20000', '20003')).toBe('0.0200');
  });

  it('is undefined for malformed or non-positive seeds', () => {
    expect(requiredReturnPct('0', '5')).toBeNull();
    expect(requiredReturnPct('-1', '5')).toBeNull();
    expect(requiredReturnPct('abc', '5')).toBeNull();
  });
});
