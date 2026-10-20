/**
 * Display formatters for scenario data.
 *
 * Every function tolerates `null` and `undefined` and renders an em dash,
 * because the API deliberately returns null for percentages it refuses to
 * compute (a zero or negative baseline). Rendering `0%` or `NaN%` there would
 * be a lie; a dash is honest.
 */

export const EMPTY_VALUE = '—';

const DEFAULT_LOCALE: string | undefined = undefined; // follow the user's locale

export interface NumberFormatOptions {
  locale?: string;
  currency?: string;
  maximumFractionDigits?: number;
  minimumFractionDigits?: number;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** A plain number, e.g. `1,234.5`. */
export function formatNumber(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(options.locale ?? DEFAULT_LOCALE, {
    maximumFractionDigits: options.maximumFractionDigits ?? 1,
    minimumFractionDigits: options.minimumFractionDigits ?? 0,
  }).format(value);
}

/**
 * A currency amount.
 *
 * Note: with the Kaggle M5 dataset the API's "sales" are unit counts, not money
 * (the ingest sets `unit_price = 1.0`). Pass a currency only when the active
 * dataset genuinely carries monetary values.
 */
export function formatCurrency(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(options.locale ?? DEFAULT_LOCALE, {
    style: 'currency',
    currency: options.currency ?? 'USD',
    maximumFractionDigits: options.maximumFractionDigits ?? 0,
  }).format(value);
}

/**
 * A percentage that the API already expressed in percent units.
 *
 * The API returns `12.5` for "12.5%", not `0.125`, so this must not multiply.
 */
export function formatPercent(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const formatted = new Intl.NumberFormat(options.locale ?? DEFAULT_LOCALE, {
    maximumFractionDigits: options.maximumFractionDigits ?? 1,
    minimumFractionDigits: options.minimumFractionDigits ?? 0,
  }).format(value);
  return `${formatted}%`;
}

/** A fraction rendered as a percentage, e.g. `0.25` -> `25%`. */
export function formatFractionAsPercent(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return formatPercent(value * 100, options);
}

/** A signed value, so an uplift reads `+150` and a shortfall `-150`. */
export function formatSignedNumber(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const formatted = formatNumber(Math.abs(value), options);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
}

/** A signed percentage, e.g. `+12.5%`. */
export function formatSignedPercent(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const formatted = formatPercent(Math.abs(value), options);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
}

/** Direction of an impact, for colour and iconography. */
export type ImpactDirection = 'increase' | 'decrease' | 'neutral';

export function impactDirection(value: number | null | undefined): ImpactDirection {
  if (!isFiniteNumber(value) || value === 0) return 'neutral';
  return value > 0 ? 'increase' : 'decrease';
}

/**
 * An ISO date rendered for display.
 *
 * Parsed as UTC rather than local time: `new Date('2026-02-01')` is already UTC
 * midnight, and formatting it in a negative-offset zone would show January 31.
 */
export function formatDate(
  value: string | null | undefined,
  options: { locale?: string; month?: 'short' | 'long' | 'numeric' } = {},
): string {
  if (!value) return EMPTY_VALUE;
  const parsed = parseIsoDate(value);
  if (!parsed) return EMPTY_VALUE;
  return new Intl.DateTimeFormat(options.locale ?? DEFAULT_LOCALE, {
    year: 'numeric',
    month: options.month ?? 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(parsed);
}

/** A date range, e.g. `Feb 1 - Feb 7, 2026`. */
export function formatDateRange(
  start: string | null | undefined,
  end: string | null | undefined,
  options: { locale?: string } = {},
): string {
  if (!start && !end) return EMPTY_VALUE;
  if (!end) return formatDate(start, options);
  if (!start) return formatDate(end, options);
  return `${formatDate(start, options)} – ${formatDate(end, options)}`;
}

/** An ISO timestamp rendered with time of day. */
export function formatDateTime(
  value: string | null | undefined,
  options: { locale?: string } = {},
): string {
  if (!value) return EMPTY_VALUE;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return EMPTY_VALUE;
  return new Intl.DateTimeFormat(options.locale ?? DEFAULT_LOCALE, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
}

/** Parse an ISO date (`YYYY-MM-DD`) as UTC, or null when unusable. */
export function parseIsoDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  const parsed = new Date(`${match[1]}-${match[2]}-${match[3]}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** A duration in milliseconds, e.g. `1.4s`. */
export function formatDuration(ms: number | null | undefined): string {
  if (!isFiniteNumber(ms)) return EMPTY_VALUE;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

/** A quantile level rendered as its conventional label, e.g. `0.9` -> `P90`. */
export function formatQuantileLabel(quantile: number | null | undefined): string {
  if (!isFiniteNumber(quantile)) return EMPTY_VALUE;
  return `P${Math.round(quantile * 100)}`;
}

/** Humanise a snake_case feature or status name for display. */
export function humanizeIdentifier(value: string | null | undefined): string {
  if (!value) return EMPTY_VALUE;
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^./, (char) => char.toUpperCase());
}

/**
 * A compact number, e.g. `180000` -> `180K`.
 *
 * Used for headline figures where precision matters less than legibility.
 */
export function formatCompactNumber(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(options.locale ?? DEFAULT_LOCALE, {
    notation: 'compact',
    maximumFractionDigits: options.maximumFractionDigits ?? 1,
  }).format(value);
}

/**
 * A compact amount, with a currency only when one is supplied.
 *
 * Deliberately unit-agnostic by default: with the Kaggle M5 dataset the API's
 * "sales" are unit counts rather than money, so stamping a currency symbol on
 * them would misrepresent the figure.
 */
export function formatCompactValue(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  if (!options.currency) return formatCompactNumber(value, options);
  return new Intl.NumberFormat(options.locale ?? DEFAULT_LOCALE, {
    style: 'currency',
    currency: options.currency,
    notation: 'compact',
    maximumFractionDigits: options.maximumFractionDigits ?? 1,
  }).format(value);
}

/** A signed compact value, e.g. `+180K`. */
export function formatSignedCompactValue(
  value: number | null | undefined,
  options: NumberFormatOptions = {},
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const formatted = formatCompactValue(Math.abs(value), options);
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
}

/** Short weekday label for a heatmap axis, e.g. `Mon`. */
export function formatWeekdayShort(
  value: string | null | undefined,
  options: { locale?: string } = {},
): string {
  const parsed = parseIsoDate(value);
  if (!parsed) return EMPTY_VALUE;
  return new Intl.DateTimeFormat(options.locale ?? DEFAULT_LOCALE, {
    weekday: 'short',
    timeZone: 'UTC',
  }).format(parsed);
}

/** Compact date for a dense chart axis, e.g. `Feb 1`. */
export function formatAxisDate(
  value: string | null | undefined,
  options: { locale?: string } = {},
): string {
  const parsed = parseIsoDate(value);
  if (!parsed) return '';
  return new Intl.DateTimeFormat(options.locale ?? DEFAULT_LOCALE, {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(parsed);
}
