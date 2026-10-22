import { describe, expect, it } from 'vitest';
import { formatAge, formatClock, formatTimestampMs, formatTimestampNs, nsToMs } from './time';

describe('time formatting', () => {
  it('converts server nanoseconds at millisecond resolution', () => {
    expect(nsToMs(1792675849903826000)).toBe(1792675849903);
    expect(formatTimestampNs(1792675849903826000, 'utc', true)).toBe('2026-10-22 13:30:49.903 UTC');
  });

  it('formats UTC timestamps without locale dependence', () => {
    expect(formatTimestampMs(Date.UTC(2026, 0, 2, 3, 4, 5, 6))).toBe('2026-01-02 03:04:05 UTC');
    expect(formatClock(Date.UTC(2026, 0, 2, 3, 4, 5), 'utc')).toBe('03:04:05');
    expect(formatTimestampMs(Number.NaN)).toBe('invalid time');
  });

  it('formats ages compactly', () => {
    expect(formatAge(850)).toBe('850 ms');
    expect(formatAge(4200)).toBe('4.2 s');
    expect(formatAge(37_000)).toBe('37 s');
    expect(formatAge(4 * 60_000 + 3000)).toBe('4 m 03 s');
    expect(formatAge(2 * 3_600_000 + 5 * 60_000)).toBe('2 h 05 m');
    expect(formatAge(3 * 86_400_000 + 4 * 3_600_000)).toBe('3 d 04 h');
    expect(formatAge(-1500)).toBe('1.5 s ahead');
    expect(formatAge(Number.POSITIVE_INFINITY)).toBe('unknown');
  });
});
