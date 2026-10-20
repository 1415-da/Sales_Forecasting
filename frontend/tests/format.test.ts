import { describe, expect, it } from 'vitest';

import {
  EMPTY_VALUE,
  formatCurrency,
  formatDate,
  formatDateRange,
  formatDuration,
  formatFractionAsPercent,
  formatNumber,
  formatPercent,
  formatQuantileLabel,
  formatSignedNumber,
  formatSignedPercent,
  humanizeIdentifier,
  impactDirection,
  parseIsoDate,
} from '@/lib/utils/format';

describe('null handling', () => {
  it.each([
    formatNumber,
    formatCurrency,
    formatPercent,
    formatSignedNumber,
    formatSignedPercent,
    formatFractionAsPercent,
  ])('renders a dash for null and undefined', (fn) => {
    expect(fn(null)).toBe(EMPTY_VALUE);
    expect(fn(undefined)).toBe(EMPTY_VALUE);
  });

  it('renders a dash rather than NaN for non-finite values', () => {
    expect(formatNumber(Number.NaN)).toBe(EMPTY_VALUE);
    expect(formatPercent(Number.POSITIVE_INFINITY)).toBe(EMPTY_VALUE);
  });
});

describe('numbers and percentages', () => {
  it('formats numbers with grouping', () => {
    expect(formatNumber(1234.56, { locale: 'en-US' })).toBe('1,234.6');
  });

  it('does not rescale percentages the API already expressed in percent', () => {
    expect(formatPercent(12.5, { locale: 'en-US' })).toBe('12.5%');
  });

  it('rescales genuine fractions', () => {
    expect(formatFractionAsPercent(0.25, { locale: 'en-US' })).toBe('25%');
  });

  it('signs impacts so direction is unmistakable', () => {
    expect(formatSignedNumber(150, { locale: 'en-US' })).toBe('+150');
    expect(formatSignedNumber(-150, { locale: 'en-US' })).toBe('-150');
    expect(formatSignedNumber(0, { locale: 'en-US' })).toBe('0');
    expect(formatSignedPercent(12.5, { locale: 'en-US' })).toBe('+12.5%');
    expect(formatSignedPercent(-12.5, { locale: 'en-US' })).toBe('-12.5%');
  });

  it('formats currency', () => {
    expect(formatCurrency(1234, { locale: 'en-US', currency: 'USD' })).toBe('$1,234');
  });

  it('classifies impact direction', () => {
    expect(impactDirection(10)).toBe('increase');
    expect(impactDirection(-10)).toBe('decrease');
    expect(impactDirection(0)).toBe('neutral');
    expect(impactDirection(null)).toBe('neutral');
  });
});

describe('dates', () => {
  it('formats an ISO date in UTC, not the local zone', () => {
    // A naive local parse would render Jan 31 west of Greenwich.
    expect(formatDate('2026-02-01', { locale: 'en-US' })).toBe('Feb 1, 2026');
  });

  it('formats a range', () => {
    expect(formatDateRange('2026-02-01', '2026-02-07', { locale: 'en-US' })).toBe(
      'Feb 1, 2026 – Feb 7, 2026',
    );
  });

  it('degrades gracefully for missing or malformed dates', () => {
    expect(formatDate(null)).toBe(EMPTY_VALUE);
    expect(formatDate('not-a-date')).toBe(EMPTY_VALUE);
    expect(formatDateRange(null, null)).toBe(EMPTY_VALUE);
    expect(parseIsoDate('nope')).toBeNull();
  });

  it('parses an ISO date as UTC midnight', () => {
    expect(parseIsoDate('2026-02-01')?.toISOString()).toBe('2026-02-01T00:00:00.000Z');
  });
});

describe('misc formatters', () => {
  it('formats durations', () => {
    expect(formatDuration(450)).toBe('450ms');
    expect(formatDuration(1420)).toBe('1.4s');
    expect(formatDuration(65_000)).toBe('1m 5s');
    expect(formatDuration(null)).toBe(EMPTY_VALUE);
  });

  it('labels quantiles conventionally', () => {
    expect(formatQuantileLabel(0.1)).toBe('P10');
    expect(formatQuantileLabel(0.9)).toBe('P90');
    expect(formatQuantileLabel(null)).toBe(EMPTY_VALUE);
  });

  it('humanises identifiers', () => {
    expect(humanizeIdentifier('is_promotion')).toBe('Is promotion');
    expect(humanizeIdentifier('scenario_completed')).toBe('Scenario completed');
    expect(humanizeIdentifier(null)).toBe(EMPTY_VALUE);
  });
});
