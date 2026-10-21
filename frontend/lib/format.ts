/**
 * Display formatting.
 *
 * Centralised so a number means the same thing everywhere: a KPI card, a chart
 * tooltip, and a table cell showing different roundings of the same value is a
 * bug users notice immediately.
 *
 * Sales figures are deliberately rendered as plain numbers, not currency: the
 * M5 target is a unit count, and a currency symbol would assert something about
 * the data that is not true.
 */

const DECIMAL = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 0,
});

const DECIMAL_PRECISE = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** A sales figure, rounded to whole units. */
export function formatSales(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return DECIMAL.format(value);
}

/** A compact sales figure for a tight KPI slot: 1.2M, 48.3K. */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) return `${DECIMAL_PRECISE.format(value / 1_000_000)}M`;
  if (absolute >= 10_000) return `${DECIMAL_PRECISE.format(value / 1_000)}K`;
  return DECIMAL.format(value);
}

/** A metric with more precision than a sales total deserves. */
export function formatMetric(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(fractionDigits);
}

/** A fraction as a percentage: 0.95 -> "95%". */
export function formatPercent(
  value: number | null | undefined,
  fractionDigits = 0,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(fractionDigits)}%`;
}

/**
 * Parse a `YYYY-MM-DD` string without a timezone shift.
 *
 * `new Date('2026-10-21')` is parsed as UTC midnight and then rendered in local
 * time, which moves the date backwards for anyone west of UTC. Constructing
 * from parts keeps the calendar day the API sent.
 */
export function parseIsoDate(value: string): Date {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year ?? 1970, (month ?? 1) - 1, day ?? 1);
}

/** `2026-10-21` -> `Oct 21`. */
export function formatShortDate(value: string): string {
  const date = parseIsoDate(value);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** `2026-10-21` -> `Wed, Oct 21, 2026`. */
export function formatLongDate(value: string): string {
  const date = parseIsoDate(value);
  return date.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

/** An inclusive date range: `Oct 21 – Nov 19, 2026`. */
export function formatDateRange(start: string, end: string): string {
  const from = parseIsoDate(start);
  const to = parseIsoDate(end);
  const sameYear = from.getFullYear() === to.getFullYear();
  const left = from.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
  const right = to.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
  return `${left} – ${right}`;
}

/** An ISO timestamp as a readable local time. */
export function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

/** How long ago a timestamp was, for a freshness badge. */
export function formatRelative(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return formatTimestamp(value);
}

/** Add days to a `YYYY-MM-DD` string, returning the same format. */
export function addDays(value: string, days: number): string {
  const date = parseIsoDate(value);
  date.setDate(date.getDate() + days);
  return toIsoDate(date);
}

/** A `Date` as `YYYY-MM-DD` in local time. */
export function toIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** Whole days between two `YYYY-MM-DD` strings, inclusive of both ends. */
export function inclusiveDayCount(start: string, end: string): number {
  const from = parseIsoDate(start).getTime();
  const to = parseIsoDate(end).getTime();
  return Math.round((to - from) / 86_400_000) + 1;
}
