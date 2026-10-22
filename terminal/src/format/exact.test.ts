import { describe, expect, it } from 'vitest';
import { addDecimal, bpsToPercent, clampAtZero, fromScaled, maxDecimal, mulDecimal, negateDecimal, ratioBps, subDecimal, sumDecimals, toScaled } from './exact';

describe('exact decimal arithmetic', () => {
  it('parses into scaled integers and renders back exactly', () => {
    expect(toScaled('300.000000')).toEqual({ units: 300_000_000n, scale: 6 });
    expect(toScaled('-0.005')).toEqual({ units: -5n, scale: 3 });
    expect(fromScaled(-135n, 2)).toBe('-1.35');
    expect(fromScaled(5n, 4)).toBe('0.0005');
    expect(fromScaled(0n, 3)).toBe('0.000');
    expect(() => toScaled('1e3')).toThrow(/not a decimal/);
    expect(() => toScaled('NaN')).toThrow(/not a decimal/);
  });

  it('adds and subtracts without float error', () => {
    expect(addDecimal('0.1', '0.2')).toBe('0.3');
    expect(subDecimal('300.000000', '298.65')).toBe('1.350000');
    expect(subDecimal('0', '0.000001')).toBe('-0.000001');
    expect(addDecimal('-49.850000', '49.75')).toBe('-0.100000');
    expect(sumDecimals(['200.000000', '49.150000', '49.500000'])).toBe('298.650000');
    expect(sumDecimals([])).toBe('0');
    // 2^53 + 1 is exact as a decimal string
    expect(addDecimal('9007199254740992', '1')).toBe('9007199254740993');
  });

  it('multiplies exactly', () => {
    expect(mulDecimal('5', '9.97')).toBe('49.85');
    expect(mulDecimal('1.5', '-0.003')).toBe('-0.0045');
    expect(mulDecimal(mulDecimal('3', '41.24'), '1')).toBe('123.72');
  });

  it('negates, maxes and clamps', () => {
    expect(negateDecimal('1.50')).toBe('-1.50');
    expect(negateDecimal('-0.01')).toBe('0.01');
    expect(maxDecimal('0.000000', '-1.35')).toBe('0.000000');
    expect(maxDecimal('2.5', '2.50001')).toBe('2.50001');
    expect(clampAtZero('-3')).toBe('0');
    expect(clampAtZero('3.1')).toBe('3.1');
  });

  it('computes ratios in basis points with explicit rounding', () => {
    expect(ratioBps('1.35', '15.00')).toBe(900n);
    expect(ratioBps('1', '3', 'ceil')).toBe(3334n);
    expect(ratioBps('1', '3', 'floor')).toBe(3333n);
    expect(ratioBps('-1', '3', 'floor')).toBe(-3334n);
    expect(ratioBps('-1', '3', 'ceil')).toBe(-3333n);
    // half-even on an exact tie (0.00005 = half a basis point)
    expect(ratioBps('0.00005', '1', 'half-even')).toBe(0n);
    expect(ratioBps('0.00015', '1', 'half-even')).toBe(2n);
    expect(ratioBps('0.000151', '1', 'half-even')).toBe(2n);
    expect(ratioBps('9999', '1', 'half-even')).toBe(99_990_000n);
    expect(ratioBps('1', '0')).toBeNull();
    expect(ratioBps('1', '-5')).toBeNull();
  });

  it('formats basis points as exact percentages', () => {
    expect(bpsToPercent(900n)).toBe('9.00%');
    expect(bpsToPercent(1533n)).toBe('15.33%');
    expect(bpsToPercent(5n)).toBe('0.05%');
    expect(bpsToPercent(99_990_000n)).toBe('999,900.00%');
    expect(bpsToPercent(-250n)).toBe('−2.50%');
    expect(bpsToPercent(10_000)).toBe('100.00%');
  });
});
