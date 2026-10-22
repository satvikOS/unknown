import { describe, expect, it } from 'vitest';
import {
  compareDecimal,
  currencyMinorUnits,
  formatDecimal,
  formatMoney,
  formatQuantity,
  isDecimal,
  MINUS,
  parseDecimal,
  signOf,
} from './decimal';

describe('parseDecimal', () => {
  it('normalizes leading and trailing zeros without changing the value', () => {
    expect(parseDecimal('000123.4500')).toEqual({ negative: false, int: '123', frac: '45' });
    expect(parseDecimal('-0.000')).toEqual({ negative: false, int: '0', frac: '' });
    expect(parseDecimal('+7')).toEqual({ negative: false, int: '7', frac: '' });
    expect(parseDecimal('-0.0010')).toEqual({ negative: true, int: '0', frac: '001' });
  });

  it('rejects anything that is not a plain decimal', () => {
    for (const bad of ['', '1e5', 'NaN', '1.', '.5', '1,000', '12a', '--1', ' ']) {
      expect(parseDecimal(bad)).toBeNull();
      expect(isDecimal(bad)).toBe(false);
    }
  });
});

describe('formatDecimal', () => {
  it('groups thousands by string manipulation', () => {
    expect(formatDecimal('1234567.891200')).toBe('1,234,567.8912');
    expect(formatDecimal('100')).toBe('100');
    expect(formatDecimal('1000')).toBe('1,000');
    expect(formatDecimal('123456')).toBe('123,456');
    expect(formatDecimal('1234', { group: false })).toBe('1234');
    expect(formatDecimal('1234567', { separator: ' ' })).toBe('1 234 567');
  });

  it('pads to the minimum fraction but never rounds or truncates', () => {
    expect(formatDecimal('300.000000', { minFraction: 2 })).toBe('300.00');
    expect(formatDecimal('0.005000', { minFraction: 2 })).toBe('0.005');
    expect(formatDecimal('9.999999', { minFraction: 2 })).toBe('9.999999');
    expect(formatDecimal('12', { minFraction: 2 })).toBe('12.00');
    expect(formatDecimal('12.5', { minFraction: 2 })).toBe('12.50');
  });

  it('is exact beyond float precision', () => {
    // 2^53 + 1 is not representable as a double; the string must survive untouched.
    expect(formatDecimal('9007199254740993.000000001')).toBe('9,007,199,254,740,993.000000001');
    expect(formatDecimal('0.1000000000000000055511151231257827')).toBe('0.1000000000000000055511151231257827');
  });

  it('uses a true minus sign and handles signed display', () => {
    expect(formatDecimal('-1234.5', { minFraction: 2 })).toBe(`${MINUS}1,234.50`);
    expect(formatDecimal('12.3', { signDisplay: 'always' })).toBe('+12.3');
    expect(formatDecimal('-12.3', { signDisplay: 'never' })).toBe('12.3');
    expect(formatDecimal('-0.000', { signDisplay: 'always', minFraction: 2 })).toBe('0.00');
  });

  it('returns malformed input unchanged so the raw value stays visible', () => {
    expect(formatDecimal('1e9')).toBe('1e9');
  });
});

describe('compareDecimal', () => {
  it('compares exactly, including values a float would equate', () => {
    expect(compareDecimal('9007199254740993', '9007199254740992')).toBe(1);
    expect(compareDecimal('0.30000000000000001', '0.3')).toBe(1);
    expect(compareDecimal('1.10', '1.1')).toBe(0);
    expect(compareDecimal('-5', '3')).toBe(-1);
    expect(compareDecimal('-5', '-3')).toBe(-1);
    expect(compareDecimal('-0.01', '0')).toBe(-1);
    expect(compareDecimal('0', '-0.000')).toBe(0);
    expect(compareDecimal('100', '99.999')).toBe(1);
  });

  it('throws on malformed input instead of mis-sorting money', () => {
    expect(() => compareDecimal('abc', '1')).toThrow();
    expect(() => signOf('')).toThrow();
  });

  it('reports signs', () => {
    expect(signOf('-0.00')).toBe(0);
    expect(signOf('-0.01')).toBe(-1);
    expect(signOf('42')).toBe(1);
  });
});

describe('money and quantity helpers', () => {
  it('uses currency minor units for padding only', () => {
    expect(currencyMinorUnits('USD')).toBe(2);
    expect(currencyMinorUnits('jpy')).toBe(0);
    expect(currencyMinorUnits('KWD')).toBe(3);
    expect(currencyMinorUnits('XYZ')).toBe(2);
    expect(formatMoney('300.000000', 'USD')).toBe('300.00 USD');
    expect(formatMoney('1500.000000', 'JPY')).toBe('1,500 JPY');
    expect(formatMoney('1500.250000', 'JPY')).toBe('1,500.25 JPY');
    expect(formatMoney('-2.5', 'EUR', { signDisplay: 'always' })).toBe(`${MINUS}2.50 EUR`);
  });

  it('formats quantities without padding', () => {
    expect(formatQuantity('100.000000')).toBe('100');
    expect(formatQuantity('2500.5')).toBe('2,500.5');
  });
});
