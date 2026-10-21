/**
 * Demo history, CSV parsing, and the shared validation both feed into.
 *
 * All three ways of supplying history (demo, upload, manual) converge on
 * `validateHistory`, so the Generate button enables under exactly one set of
 * rules no matter where the rows came from.
 */
import { addDays, inclusiveDayCount, toIsoDate } from '@/lib/format';
import type { HistoryPoint } from '@/lib/types';

/** Matches `FORECAST_MIN_HISTORY_DAYS`; the API is still the authority. */
export const MIN_HISTORY_DAYS = 30;

/** Below this the deepest rolling window is thin, so the UI nudges upward. */
export const RECOMMENDED_HISTORY_DAYS = 60;

/** A problem that blocks generating a forecast, or a caveat that does not. */
export interface HistoryIssue {
  severity: 'error' | 'warning';
  message: string;
}

export interface HistoryStats {
  rowCount: number;
  startDate: string | null;
  endDate: string | null;
  missingDays: number;
  zeroDays: number;
  mean: number;
  min: number;
  max: number;
}

export interface HistoryValidation {
  issues: HistoryIssue[];
  stats: HistoryStats;
  /** True when the API would accept this history. */
  isValid: boolean;
}

const EMPTY_STATS: HistoryStats = {
  rowCount: 0,
  startDate: null,
  endDate: null,
  missingDays: 0,
  zeroDays: 0,
  mean: 0,
  min: 0,
  max: 0,
};

/**
 * Deterministic demo history ending yesterday.
 *
 * Seeded rather than random so the demo path is reproducible: a forecast a user
 * shows a colleague should be the same one. Weekly seasonality and a mild trend
 * are included so the chart shows structure rather than a flat line.
 */
export function buildSampleHistory(days = RECOMMENDED_HISTORY_DAYS): HistoryPoint[] {
  const end = new Date();
  end.setDate(end.getDate() - 1);
  const start = new Date(end);
  start.setDate(start.getDate() - (days - 1));

  const base = 3000;
  // A small linear congruential generator: a fixed seed makes the series
  // identical on every call and in every browser.
  let seed = 20261021;
  const next = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };

  const points: HistoryPoint[] = [];
  for (let offset = 0; offset < days; offset += 1) {
    const date = new Date(start);
    date.setDate(start.getDate() + offset);
    const weekday = date.getDay();
    const weekendLift = weekday === 0 || weekday === 6 ? 1.18 : 1;
    const trend = 1 + (offset / days) * 0.06;
    const noise = 0.96 + next() * 0.08;
    points.push({
      date: toIsoDate(date),
      sales: Math.round(base * weekendLift * trend * noise),
    });
  }
  return points;
}

/** Blank rows for manual entry, pre-dated so a user only types sales. */
export function buildManualTemplate(days = MIN_HISTORY_DAYS): HistoryPoint[] {
  return buildSampleHistory(days).map((point) => ({ date: point.date, sales: 0 }));
}

interface CsvParseResult {
  rows: HistoryPoint[];
  issues: HistoryIssue[];
  /** Columns found in the file, for the preview. */
  headers: string[];
}

const DATE_ALIASES = ['date', 'day', 'ds', 'timestamp'];
const SALES_ALIASES = ['sales', 'revenue', 'y', 'total_sales', 'amount'];
const OPTIONAL_NUMERIC = ['quantity_sold', 'profit', 'customer_traffic'] as const;

/**
 * Parse a sales CSV.
 *
 * Hand-rolled rather than pulling in a CSV library: the accepted shape is two
 * required columns and a few optional numerics, and quoted-field handling is a
 * dozen lines. It covers quoted values and both line endings, and reports what
 * it could not read instead of dropping rows silently.
 */
export function parseHistoryCsv(text: string): CsvParseResult {
  const issues: HistoryIssue[] = [];
  const lines = text
    .split(/\r\n|\n|\r/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  if (lines.length < 2) {
    return {
      rows: [],
      issues: [{ severity: 'error', message: 'The file has no data rows.' }],
      headers: [],
    };
  }

  const headers = splitCsvLine(lines[0] as string).map((header) =>
    header.trim().toLowerCase().replace(/\s+/g, '_'),
  );
  const dateIndex = headers.findIndex((header) => DATE_ALIASES.includes(header));
  const salesIndex = headers.findIndex((header) => SALES_ALIASES.includes(header));

  if (dateIndex === -1 || salesIndex === -1) {
    return {
      rows: [],
      issues: [
        {
          severity: 'error',
          message:
            'The file needs a "date" column and a "sales" column. ' +
            `Found: ${headers.join(', ') || 'no columns'}.`,
        },
      ],
      headers,
    };
  }

  const storeIndex = headers.indexOf('store_id');
  const optionalIndexes = OPTIONAL_NUMERIC.map(
    (column) => [column, headers.indexOf(column)] as const,
  ).filter(([, index]) => index !== -1);

  const rows: HistoryPoint[] = [];
  let unparseableDates = 0;
  let unparseableSales = 0;

  for (let lineNumber = 1; lineNumber < lines.length; lineNumber += 1) {
    const cells = splitCsvLine(lines[lineNumber] as string);
    const date = normaliseDate(cells[dateIndex]);
    const sales = Number(String(cells[salesIndex] ?? '').replace(/[,\s]/g, ''));

    if (!date) {
      unparseableDates += 1;
      continue;
    }
    if (!Number.isFinite(sales)) {
      unparseableSales += 1;
      continue;
    }

    const point: HistoryPoint = { date, sales: Math.max(0, sales) };
    const store = storeIndex === -1 ? undefined : cells[storeIndex]?.trim();
    if (store) point.store_id = store;
    for (const [column, index] of optionalIndexes) {
      const value = Number(String(cells[index] ?? '').replace(/[,\s]/g, ''));
      if (Number.isFinite(value)) point[column] = value;
    }
    rows.push(point);
  }

  if (unparseableDates > 0) {
    issues.push({
      severity: 'warning',
      message: `${unparseableDates} row(s) had an unreadable date and were skipped.`,
    });
  }
  if (unparseableSales > 0) {
    issues.push({
      severity: 'warning',
      message: `${unparseableSales} row(s) had an unreadable sales value and were skipped.`,
    });
  }

  rows.sort((left, right) => left.date.localeCompare(right.date));
  return { rows, issues, headers };
}

/** Split one CSV line, honouring double-quoted fields. */
function splitCsvLine(line: string): string[] {
  const cells: string[] = [];
  let current = '';
  let inQuotes = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === '"') {
      // A doubled quote inside a quoted field is a literal quote.
      if (inQuotes && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === ',' && !inQuotes) {
      cells.push(current);
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current);
  return cells.map((cell) => cell.trim());
}

/** Coerce a date cell to `YYYY-MM-DD`, or return null if it is unreadable. */
function normaliseDate(raw: string | undefined): string | null {
  const value = (raw ?? '').trim();
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);

  // Accept the common slash forms, assuming day-first is not in play: an
  // ambiguous 03/04/2026 is read as US month-first, matching the ISO examples
  // in the UI.
  const slashed = value.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (slashed) {
    const [, month, day, year] = slashed;
    return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return toIsoDate(parsed);
}

/**
 * Check history against the rules the API enforces.
 *
 * Mirrored client-side so the Generate button can be disabled with a reason
 * instead of letting a request fail. The API remains the authority: this is a
 * courtesy, not a substitute.
 */
export function validateHistory(rows: HistoryPoint[]): HistoryValidation {
  if (rows.length === 0) {
    return {
      issues: [{ severity: 'error', message: 'No history yet.' }],
      stats: EMPTY_STATS,
      isValid: false,
    };
  }

  const issues: HistoryIssue[] = [];
  const sorted = [...rows].sort((left, right) => left.date.localeCompare(right.date));
  const startDate = sorted[0]!.date;
  const endDate = sorted[sorted.length - 1]!.date;

  const seen = new Set<string>();
  let duplicates = 0;
  for (const row of sorted) {
    if (seen.has(row.date)) duplicates += 1;
    seen.add(row.date);
  }
  if (duplicates > 0) {
    issues.push({
      severity: 'error',
      message: `${duplicates} duplicate date(s). Each day may appear only once.`,
    });
  }

  const expectedDays = inclusiveDayCount(startDate, endDate);
  const missingDays = Math.max(0, expectedDays - seen.size);
  if (missingDays > 0) {
    issues.push({
      severity: 'warning',
      message:
        `${missingDays} calendar day(s) missing between ${startDate} and ${endDate}. ` +
        'Lag and rolling features are positional, so gaps shift those windows.',
    });
  }

  if (sorted.length < MIN_HISTORY_DAYS) {
    issues.push({
      severity: 'error',
      message: `At least ${MIN_HISTORY_DAYS} days are required; you have ${sorted.length}.`,
    });
  } else if (sorted.length < RECOMMENDED_HISTORY_DAYS) {
    issues.push({
      severity: 'warning',
      message:
        `${sorted.length} days supplied. ${RECOMMENDED_HISTORY_DAYS} or more gives the ` +
        'model’s 30-day rolling features a fuller window.',
    });
  }

  const values = sorted.map((row) => row.sales);
  const positives = values.filter((value) => value > 0);
  if (positives.length === 0) {
    issues.push({
      severity: 'error',
      message: 'Every day is zero, so there is no sales level to forecast from.',
    });
  }

  const zeroDays = values.length - positives.length;
  if (zeroDays > 0 && positives.length > 0) {
    issues.push({
      severity: 'warning',
      message: `${zeroDays} day(s) have zero sales.`,
    });
  }

  const stats: HistoryStats = {
    rowCount: sorted.length,
    startDate,
    endDate,
    missingDays,
    zeroDays,
    mean: values.reduce((total, value) => total + value, 0) / values.length,
    min: Math.min(...values),
    max: Math.max(...values),
  };

  return {
    issues,
    stats,
    isValid: !issues.some((issue) => issue.severity === 'error'),
  };
}

/** The first forecast date for a given history: the day after it ends. */
export function firstForecastDate(rows: HistoryPoint[]): string | null {
  if (rows.length === 0) return null;
  const sorted = [...rows].sort((left, right) => left.date.localeCompare(right.date));
  return addDays(sorted[sorted.length - 1]!.date, 1);
}
