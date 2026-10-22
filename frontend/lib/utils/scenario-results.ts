/**
 * Pure derivations for the scenario results panel.
 *
 * Chart shaping, table sorting, heatmap bucketing, CSV assembly, and the
 * narrative sentence all live here rather than inside components, so each is
 * testable without a DOM or a chart library.
 */

import type {
  ScenarioComparisonResponse,
  ScenarioForecastPoint,
} from '@/lib/types/scenario';
import {
  formatCompactValue,
  formatDate,
  formatSignedPercent,
  humanizeIdentifier,
  parseIsoDate,
} from '@/lib/utils/format';

/* -------------------------------------------------------------------------- */
/* Tone                                                                        */
/* -------------------------------------------------------------------------- */

export type ImpactTone = 'positive' | 'negative' | 'neutral';

/**
 * Classify an impact for colour.
 *
 * A null or non-finite impact is neutral, never "positive": the API returns
 * null for figures it declines to compute, and rendering that as a gain would
 * invent a result.
 */
export function impactTone(value: number | null | undefined): ImpactTone {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) {
    return 'neutral';
  }
  return value > 0 ? 'positive' : 'negative';
}

/** Tailwind classes per tone, with dark-mode variants. */
export const TONE_CLASSES: Record<
  ImpactTone,
  { text: string; bg: string; border: string; chart: string }
> = {
  positive: {
    text: 'text-emerald-700 dark:text-emerald-400',
    bg: 'bg-emerald-50 dark:bg-emerald-950/40',
    border: 'border-emerald-200 dark:border-emerald-900',
    chart: 'hsl(var(--chart-positive))',
  },
  negative: {
    text: 'text-red-700 dark:text-red-400',
    bg: 'bg-red-50 dark:bg-red-950/40',
    border: 'border-red-200 dark:border-red-900',
    chart: 'hsl(var(--chart-negative))',
  },
  neutral: {
    text: 'text-amber-700 dark:text-amber-400',
    bg: 'bg-amber-50 dark:bg-amber-950/40',
    border: 'border-amber-200 dark:border-amber-900',
    chart: 'hsl(var(--chart-neutral))',
  },
};

/* -------------------------------------------------------------------------- */
/* Chart shaping                                                               */
/* -------------------------------------------------------------------------- */

/** One row of chart data, flattened for Recharts. */
export interface ChartPoint {
  date: string;
  baseline: number;
  scenario: number;
  difference: number;
  differencePct: number | null;
  /** Band floor and height, so a stacked area renders a p10-p90 ribbon. */
  bandFloor: number | null;
  bandHeight: number | null;
  scenarioBandFloor: number | null;
  scenarioBandHeight: number | null;
  inScenarioWindow: boolean;
}

export type ChartView = 'daily' | 'weekly' | 'cumulative';

/** Flatten comparison points into chart rows. */
export function toChartPoints(
  points: ScenarioForecastPoint[],
  modifiedDates: string[] = [],
): ChartPoint[] {
  const modified = new Set(modifiedDates);
  return points.map((point) => ({
    date: point.date,
    baseline: point.baseline_forecast,
    scenario: point.scenario_forecast,
    difference: point.daily_difference,
    differencePct: point.daily_difference_pct,
    bandFloor: point.baseline_p10,
    bandHeight:
      point.baseline_p10 !== null && point.baseline_p90 !== null
        ? point.baseline_p90 - point.baseline_p10
        : null,
    scenarioBandFloor: point.scenario_p10,
    scenarioBandHeight:
      point.scenario_p10 !== null && point.scenario_p90 !== null
        ? point.scenario_p90 - point.scenario_p10
        : null,
    inScenarioWindow: modified.has(point.date),
  }));
}

/** Monday-based ISO week start for a date. */
export function weekStart(isoDate: string): string {
  const parsed = parseIsoDate(isoDate);
  if (!parsed) return isoDate;
  const weekday = (parsed.getUTCDay() + 6) % 7; // Monday = 0
  parsed.setUTCDate(parsed.getUTCDate() - weekday);
  return parsed.toISOString().slice(0, 10);
}

/**
 * Aggregate chart rows into weeks.
 *
 * Totals are summed; percentage change is recomputed from the weekly totals
 * rather than averaged, because a mean of daily percentages is not the
 * percentage change of the week.
 */
export function aggregateWeekly(points: ChartPoint[]): ChartPoint[] {
  const byWeek = new Map<string, ChartPoint[]>();
  for (const point of points) {
    const key = weekStart(point.date);
    const bucket = byWeek.get(key);
    if (bucket) bucket.push(point);
    else byWeek.set(key, [point]);
  }

  return [...byWeek.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([week, bucket]) => {
      const baseline = sum(bucket.map((point) => point.baseline));
      const scenario = sum(bucket.map((point) => point.scenario));
      const bandFloor = sumNullable(bucket.map((point) => point.bandFloor));
      const bandHeight = sumNullable(bucket.map((point) => point.bandHeight));
      return {
        date: week,
        baseline,
        scenario,
        difference: scenario - baseline,
        differencePct: baseline > 0 ? ((scenario - baseline) / baseline) * 100 : null,
        bandFloor,
        bandHeight,
        scenarioBandFloor: sumNullable(bucket.map((point) => point.scenarioBandFloor)),
        scenarioBandHeight: sumNullable(bucket.map((point) => point.scenarioBandHeight)),
        inScenarioWindow: bucket.some((point) => point.inScenarioWindow),
      };
    });
}

/** Running totals, so the chart can show cumulative divergence. */
export function toCumulative(points: ChartPoint[]): ChartPoint[] {
  let baseline = 0;
  let scenario = 0;
  return points.map((point) => {
    baseline += point.baseline;
    scenario += point.scenario;
    return {
      ...point,
      baseline,
      scenario,
      difference: scenario - baseline,
      differencePct: baseline > 0 ? ((scenario - baseline) / baseline) * 100 : null,
      // A cumulative band would compound interval widths, overstating
      // certainty growth, so bands are dropped in this view.
      bandFloor: null,
      bandHeight: null,
      scenarioBandFloor: null,
      scenarioBandHeight: null,
    };
  });
}

/** Apply a view transform to chart rows. */
export function applyChartView(points: ChartPoint[], view: ChartView): ChartPoint[] {
  if (view === 'weekly') return aggregateWeekly(points);
  if (view === 'cumulative') return toCumulative(points);
  return points;
}

/** First and last dates the scenario actually modified, for a highlight band. */
export function scenarioWindow(
  modifiedDates: string[],
): { start: string; end: string } | null {
  if (modifiedDates.length === 0) return null;
  const sorted = [...modifiedDates].sort();
  return { start: sorted[0]!, end: sorted[sorted.length - 1]! };
}

/** Whether any point carries a full baseline and scenario band. */
export function hasConfidenceBands(points: ScenarioForecastPoint[]): boolean {
  return points.some(
    (point) =>
      point.baseline_p10 !== null &&
      point.baseline_p90 !== null &&
      point.scenario_p10 !== null &&
      point.scenario_p90 !== null,
  );
}

/* -------------------------------------------------------------------------- */
/* Table                                                                       */
/* -------------------------------------------------------------------------- */

export type TableSortKey = 'date' | 'difference';
export type SortDirection = 'asc' | 'desc';

/** Sort table rows. Ties keep date order, so the result is deterministic. */
export function sortImpactPoints(
  points: ScenarioForecastPoint[],
  key: TableSortKey,
  direction: SortDirection,
): ScenarioForecastPoint[] {
  const factor = direction === 'asc' ? 1 : -1;
  return [...points].sort((left, right) => {
    if (key === 'date') return left.date.localeCompare(right.date) * factor;
    const delta = left.daily_difference - right.daily_difference;
    if (delta !== 0) return delta * factor;
    return left.date.localeCompare(right.date);
  });
}

/* -------------------------------------------------------------------------- */
/* Heatmap                                                                     */
/* -------------------------------------------------------------------------- */

export interface HeatmapCell {
  date: string;
  difference: number;
  differencePct: number | null;
  tone: ImpactTone;
  /** 0-1 magnitude relative to the largest absolute impact in the series. */
  intensity: number;
}

export interface HeatmapWeek {
  weekStart: string;
  /** Seven slots, Monday to Sunday; null where the horizon has no data. */
  days: (HeatmapCell | null)[];
}

/**
 * Lay points out as calendar weeks.
 *
 * Intensity is scaled against the largest absolute impact so a scenario with
 * small effects is still readable, rather than rendering uniformly pale.
 */
export function buildHeatmap(points: ScenarioForecastPoint[]): HeatmapWeek[] {
  if (points.length === 0) return [];

  const peak = Math.max(...points.map((point) => Math.abs(point.daily_difference)), 0);
  const byWeek = new Map<string, HeatmapWeek>();

  for (const point of points) {
    const week = weekStart(point.date);
    if (!byWeek.has(week)) {
      byWeek.set(week, { weekStart: week, days: Array.from({ length: 7 }, () => null) });
    }
    const parsed = parseIsoDate(point.date);
    if (!parsed) continue;
    const slot = (parsed.getUTCDay() + 6) % 7;
    byWeek.get(week)!.days[slot] = {
      date: point.date,
      difference: point.daily_difference,
      differencePct: point.daily_difference_pct,
      tone: impactTone(point.daily_difference),
      intensity: peak > 0 ? Math.abs(point.daily_difference) / peak : 0,
    };
  }

  return [...byWeek.values()].sort((a, b) => a.weekStart.localeCompare(b.weekStart));
}

/* -------------------------------------------------------------------------- */
/* CSV                                                                         */
/* -------------------------------------------------------------------------- */

/** Column order for the exported comparison, as specified. */
export const CSV_COLUMNS = [
  'date',
  'baseline_p50',
  'scenario_p50',
  'difference',
  'difference_pct',
  'baseline_p10',
  'baseline_p90',
  'scenario_p10',
  'scenario_p90',
] as const;

function csvCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '';
  const text = String(value);
  // Quote anything that would otherwise break the row structure.
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/**
 * Build the comparison CSV.
 *
 * `*_p50` falls back to the point forecast when the model produced no
 * quantiles, since the median and the point estimate coincide there; the p10
 * and p90 columns stay empty rather than being fabricated.
 */
export function buildComparisonCsv(comparison: ScenarioComparisonResponse): string {
  const header = CSV_COLUMNS.join(',');
  const rows = comparison.daily_forecasts.map((point) =>
    [
      point.date,
      point.baseline_p50 ?? point.baseline_forecast,
      point.scenario_p50 ?? point.scenario_forecast,
      point.daily_difference,
      point.daily_difference_pct,
      point.baseline_p10,
      point.baseline_p90,
      point.scenario_p10,
      point.scenario_p90,
    ]
      .map(csvCell)
      .join(','),
  );
  return [header, ...rows].join('\n');
}

/** Filename for a downloaded comparison. */
export function comparisonFileName(
  comparison: ScenarioComparisonResponse,
  extension: 'csv' | 'json',
): string {
  const store = comparison.store_id ?? 'store';
  return `scenario-${store}-${comparison.scenario_id}.${extension}`;
}

/* -------------------------------------------------------------------------- */
/* Narrative                                                                   */
/* -------------------------------------------------------------------------- */

export interface InsightOptions {
  locale?: string;
  /** Supply only when the dataset's values are genuinely monetary. */
  currency?: string;
}

/**
 * Words that assert causation. The API's own limitations say this is a
 * predictive what-if, not causal evidence, so the narrative must not claim
 * otherwise.
 */
export const FORBIDDEN_CAUSAL_TERMS = [
  'will cause',
  'causes',
  'guarantees',
  'guaranteed',
  'results in',
  'leads to',
  'because of',
  'due to',
  'proves',
];

/** Display name of a scenario type; acronyms stay acronyms. */
export function scenarioTypeLabel(type: string): string {
  if (type === 'snap') return 'SNAP benefit days';
  return humanizeIdentifier(type);
}

/** Display name of a model family, e.g. `xgboost` -> `XGBoost`. */
export function modelFamilyLabel(model: string | null | undefined): string {
  const labels: Record<string, string> = {
    ensemble: 'Ensemble',
    xgboost: 'XGBoost',
    lightgbm: 'LightGBM',
  };
  return model ? labels[model] ?? humanizeIdentifier(model) : 'Ensemble';
}

/** Describe the scenario's shape, e.g. `7-day medium promotion`. */
export function describeScenario(comparison: ScenarioComparisonResponse): string {
  const assumptions = comparison.assumptions;
  const type = humanizeIdentifier(comparison.scenario_type).toLowerCase();
  const dayCount = comparison.modified_dates.length;
  const duration = dayCount > 0 ? `${dayCount}-day ` : '';

  const intensity =
    assumptions && assumptions.promotion_intensity !== 'none'
      ? `${assumptions.promotion_intensity} `
      : '';

  if (comparison.scenario_type === 'traffic' && assumptions?.traffic_multiplier) {
    const pct = Math.round((assumptions.traffic_multiplier - 1) * 100);
    return `${duration}traffic change of ${pct > 0 ? '+' : ''}${pct}%`;
  }
  if (comparison.scenario_type === 'closure') {
    return `${duration}store closure`;
  }
  if (comparison.scenario_type === 'snap') {
    return `${duration}SNAP benefit ${assumptions?.snap_mode === 'remove' ? 'removal' : 'addition'}`;
  }
  return `${duration}${intensity}${type}`.trim();
}

/**
 * Build the headline narrative.
 *
 * Phrased associatively ("is associated with") throughout: the model observes
 * correlation in historical patterns and cannot attribute the change to the
 * scenario.
 */
export function buildInsightSentence(
  comparison: ScenarioComparisonResponse,
  options: InsightOptions = {},
): string {
  const { impact } = comparison;
  const tone = impactTone(impact.total_difference);
  const descriptor = describeScenario(comparison);
  const magnitude = formatCompactValue(Math.abs(impact.total_difference), options);
  const pct =
    impact.total_difference_pct === null
      ? null
      : formatSignedPercent(impact.total_difference_pct, options);

  if (tone === 'neutral') {
    return `A ${descriptor} is associated with no material change to the forecast over ${impact.horizon_days} days.`;
  }

  const direction = tone === 'positive' ? 'increase' : 'decrease';
  const pctClause = pct ? ` (${pct})` : '';
  return `A ${descriptor} is associated with a forecast ${direction} of ${magnitude}${pctClause} over ${impact.horizon_days} days.`;
}

/** Supporting bullets: peak day, spread of affected days, quantile range. */
export function buildInsightDetails(
  comparison: ScenarioComparisonResponse,
  options: InsightOptions = {},
): string[] {
  const { impact } = comparison;
  const details: string[] = [];

  if (impact.peak_impact_date && impact.peak_impact_value !== null) {
    const tone = impactTone(impact.peak_impact_value);
    details.push(
      `Largest single-day movement is ${formatCompactValue(
        Math.abs(impact.peak_impact_value),
        options,
      )} on ${formatDate(impact.peak_impact_date, options)} (${
        tone === 'negative' ? 'below' : 'above'
      } baseline).`,
    );
  }

  if (impact.positive_days || impact.negative_days) {
    details.push(
      `${impact.positive_days} of ${impact.horizon_days} days forecast above baseline, ${impact.negative_days} below.`,
    );
  }

  const range = impact.impact_range;
  if (range.total_difference_p10 !== null && range.total_difference_p90 !== null) {
    details.push(
      `Plausible range spans ${formatCompactValue(
        range.total_difference_p10,
        options,
      )} to ${formatCompactValue(range.total_difference_p90, options)} (P10-P90).`,
    );
  }

  if (comparison.unsupported_overrides.length > 0) {
    details.push(
      `Partial scenario: ${comparison.unsupported_overrides
        .map((override) => humanizeIdentifier(override.feature_name))
        .join(', ')} could not be applied to this model.`,
    );
  }

  return details;
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

/** Sum that stays null when any contributor is missing. */
function sumNullable(values: (number | null)[]): number | null {
  if (values.some((value) => value === null)) return null;
  return sum(values as number[]);
}
