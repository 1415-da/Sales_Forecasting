/**
 * Tests for the results-panel derivations.
 *
 * Chart shaping, weekly/cumulative views, heatmap layout, sorting, CSV
 * assembly, and the narrative are all pure, so they are tested directly rather
 * than through rendered components.
 */

import { describe, expect, it } from 'vitest';

import {
  CSV_COLUMNS,
  FORBIDDEN_CAUSAL_TERMS,
  aggregateWeekly,
  buildComparisonCsv,
  buildHeatmap,
  buildInsightDetails,
  buildInsightSentence,
  comparisonFileName,
  describeScenario,
  hasConfidenceBands,
  impactTone,
  scenarioWindow,
  sortImpactPoints,
  toChartPoints,
  toCumulative,
  weekStart,
} from '@/lib/utils/scenario-results';
import type {
  ScenarioComparisonResponse,
  ScenarioForecastPoint,
} from '@/lib/types/scenario';

import { COMPARISON_PAYLOAD } from './fixtures';

/* -------------------------------------------------------------------------- */
/* Builders                                                                    */
/* -------------------------------------------------------------------------- */

function point(overrides: Partial<ScenarioForecastPoint> = {}): ScenarioForecastPoint {
  return {
    date: '2026-02-01',
    baseline_forecast: 100,
    scenario_forecast: 120,
    daily_difference: 20,
    daily_difference_pct: 20,
    cumulative_difference: 20,
    baseline_p10: 90,
    baseline_p50: 100,
    baseline_p90: 110,
    scenario_p10: 108,
    scenario_p50: 120,
    scenario_p90: 132,
    difference_p10: 18,
    difference_p50: 20,
    difference_p90: 22,
    ...overrides,
  };
}

/** Fourteen days from 2026-02-02 (a Monday), so weeks align cleanly. */
function series(count = 14, step = 0): ScenarioForecastPoint[] {
  return Array.from({ length: count }, (_, index) => {
    const day = String(2 + index).padStart(2, '0');
    const difference = 20 + index * step;
    return point({
      date: `2026-02-${day}`,
      daily_difference: difference,
      scenario_forecast: 100 + difference,
      daily_difference_pct: difference,
    });
  });
}

function comparison(
  overrides: Partial<ScenarioComparisonResponse> = {},
): ScenarioComparisonResponse {
  return {
    ...(COMPARISON_PAYLOAD as unknown as ScenarioComparisonResponse),
    ...overrides,
  };
}

/* -------------------------------------------------------------------------- */
/* Tone                                                                       */
/* -------------------------------------------------------------------------- */

describe('impact tone', () => {
  it('classifies direction', () => {
    expect(impactTone(10)).toBe('positive');
    expect(impactTone(-10)).toBe('negative');
    expect(impactTone(0)).toBe('neutral');
  });

  it('treats a missing impact as neutral, never positive', () => {
    expect(impactTone(null)).toBe('neutral');
    expect(impactTone(undefined)).toBe('neutral');
    expect(impactTone(Number.NaN)).toBe('neutral');
  });
});

/* -------------------------------------------------------------------------- */
/* Chart shaping                                                              */
/* -------------------------------------------------------------------------- */

describe('chart points', () => {
  it('flattens points and computes band heights', () => {
    const [row] = toChartPoints([point()], ['2026-02-01']);

    expect(row!.baseline).toBe(100);
    expect(row!.scenario).toBe(120);
    expect(row!.bandFloor).toBe(90);
    expect(row!.bandHeight).toBe(20); // p90 - p10
    expect(row!.scenarioBandHeight).toBe(24);
    expect(row!.inScenarioWindow).toBe(true);
  });

  it('leaves band fields null when the model gave no quantiles', () => {
    const [row] = toChartPoints([
      point({ baseline_p10: null, baseline_p90: null, scenario_p10: null, scenario_p90: null }),
    ]);

    expect(row!.bandHeight).toBeNull();
    expect(row!.scenarioBandHeight).toBeNull();
  });

  it('detects whether bands are available at all', () => {
    expect(hasConfidenceBands([point()])).toBe(true);
    expect(
      hasConfidenceBands([point({ baseline_p10: null, scenario_p10: null })]),
    ).toBe(false);
  });

  it('reports the scenario window from the modified dates', () => {
    expect(scenarioWindow(['2026-02-05', '2026-02-03', '2026-02-04'])).toEqual({
      start: '2026-02-03',
      end: '2026-02-05',
    });
    expect(scenarioWindow([])).toBeNull();
  });
});

describe('weekly aggregation', () => {
  it('buckets by Monday-based week start', () => {
    expect(weekStart('2026-02-04')).toBe('2026-02-02'); // Wed -> Mon
    expect(weekStart('2026-02-02')).toBe('2026-02-02');
    expect(weekStart('2026-02-08')).toBe('2026-02-02'); // Sun stays in its week
  });

  it('sums totals across each week', () => {
    const weeks = aggregateWeekly(toChartPoints(series(14)));

    expect(weeks).toHaveLength(2);
    expect(weeks[0]!.date).toBe('2026-02-02');
    expect(weeks[0]!.baseline).toBe(700); // 7 x 100
    expect(weeks[0]!.scenario).toBe(840); // 7 x 120
    expect(weeks[0]!.difference).toBe(140);
  });

  it('recomputes weekly percentage from totals, not by averaging daily percentages', () => {
    const weeks = aggregateWeekly(toChartPoints(series(7)));

    // 140 / 700 = 20%, which here coincides with the daily figure; the point is
    // that it is derived from the totals.
    expect(weeks[0]!.differencePct).toBeCloseTo(20);
  });

  it('keeps the window flag when any day in the week was modified', () => {
    const weeks = aggregateWeekly(toChartPoints(series(7), ['2026-02-04']));
    expect(weeks[0]!.inScenarioWindow).toBe(true);
  });
});

describe('cumulative view', () => {
  it('accumulates baseline and scenario totals', () => {
    const rows = toCumulative(toChartPoints(series(3)));

    expect(rows[0]!.baseline).toBe(100);
    expect(rows[1]!.baseline).toBe(200);
    expect(rows[2]!.baseline).toBe(300);
    expect(rows[2]!.scenario).toBe(360);
    expect(rows[2]!.difference).toBe(60);
  });

  it('drops bands, since summing intervals would overstate certainty', () => {
    const rows = toCumulative(toChartPoints(series(3)));

    expect(rows.every((row) => row.bandHeight === null)).toBe(true);
    expect(rows.every((row) => row.scenarioBandHeight === null)).toBe(true);
  });
});

/* -------------------------------------------------------------------------- */
/* Table sorting                                                              */
/* -------------------------------------------------------------------------- */

describe('table sorting', () => {
  const rows = [
    point({ date: '2026-02-03', daily_difference: 5 }),
    point({ date: '2026-02-01', daily_difference: 50 }),
    point({ date: '2026-02-02', daily_difference: -20 }),
  ];

  it('sorts by date in both directions', () => {
    expect(sortImpactPoints(rows, 'date', 'asc').map((row) => row.date)).toEqual([
      '2026-02-01',
      '2026-02-02',
      '2026-02-03',
    ]);
    expect(sortImpactPoints(rows, 'date', 'desc').map((row) => row.date)).toEqual([
      '2026-02-03',
      '2026-02-02',
      '2026-02-01',
    ]);
  });

  it('sorts by difference, keeping sign order', () => {
    expect(
      sortImpactPoints(rows, 'difference', 'desc').map((row) => row.daily_difference),
    ).toEqual([50, 5, -20]);
  });

  it('breaks ties by date so the order is deterministic', () => {
    const tied = [
      point({ date: '2026-02-03', daily_difference: 10 }),
      point({ date: '2026-02-01', daily_difference: 10 }),
    ];

    expect(sortImpactPoints(tied, 'difference', 'desc').map((row) => row.date)).toEqual([
      '2026-02-01',
      '2026-02-03',
    ]);
  });

  it('does not mutate the input', () => {
    const original = [...rows];
    sortImpactPoints(rows, 'difference', 'asc');
    expect(rows).toEqual(original);
  });
});

/* -------------------------------------------------------------------------- */
/* Heatmap                                                                    */
/* -------------------------------------------------------------------------- */

describe('calendar heatmap', () => {
  it('lays days out in Monday-based weeks', () => {
    const weeks = buildHeatmap(series(14));

    expect(weeks).toHaveLength(2);
    expect(weeks[0]!.weekStart).toBe('2026-02-02');
    expect(weeks[0]!.days).toHaveLength(7);
    expect(weeks[0]!.days[0]!.date).toBe('2026-02-02');
  });

  it('leaves slots outside the horizon empty', () => {
    // Three days from a Wednesday: Mon and Tue have no data.
    const weeks = buildHeatmap([
      point({ date: '2026-02-04' }),
      point({ date: '2026-02-05' }),
      point({ date: '2026-02-06' }),
    ]);

    expect(weeks[0]!.days[0]).toBeNull();
    expect(weeks[0]!.days[1]).toBeNull();
    expect(weeks[0]!.days[2]!.date).toBe('2026-02-04');
  });

  it('scales intensity against the largest absolute impact', () => {
    const weeks = buildHeatmap([
      point({ date: '2026-02-02', daily_difference: 100 }),
      point({ date: '2026-02-03', daily_difference: 50 }),
      point({ date: '2026-02-04', daily_difference: 0 }),
    ]);

    const [strong, mid, flat] = weeks[0]!.days;
    expect(strong!.intensity).toBe(1);
    expect(mid!.intensity).toBe(0.5);
    expect(flat!.intensity).toBe(0);
    expect(flat!.tone).toBe('neutral');
  });

  it('tones cells by direction', () => {
    const weeks = buildHeatmap([
      point({ date: '2026-02-02', daily_difference: 10 }),
      point({ date: '2026-02-03', daily_difference: -10 }),
    ]);

    expect(weeks[0]!.days[0]!.tone).toBe('positive');
    expect(weeks[0]!.days[1]!.tone).toBe('negative');
  });

  it('handles an empty series', () => {
    expect(buildHeatmap([])).toEqual([]);
  });
});

/* -------------------------------------------------------------------------- */
/* CSV                                                                        */
/* -------------------------------------------------------------------------- */

describe('CSV export', () => {
  it('emits the specified columns in order', () => {
    const csv = buildComparisonCsv(comparison());

    expect(csv.split('\n')[0]).toBe(
      'date,baseline_p50,scenario_p50,difference,difference_pct,baseline_p10,baseline_p90,scenario_p10,scenario_p90',
    );
    expect([...CSV_COLUMNS]).toHaveLength(9);
  });

  it('writes one row per forecast day', () => {
    const csv = buildComparisonCsv(
      comparison({ daily_forecasts: series(5) }),
    );

    expect(csv.split('\n')).toHaveLength(6); // header + 5 rows
  });

  it('falls back to the point forecast when there is no median', () => {
    const csv = buildComparisonCsv(
      comparison({
        daily_forecasts: [
          point({ baseline_p50: null, scenario_p50: null, baseline_p10: null, baseline_p90: null }),
        ],
      }),
    );

    const [, row] = csv.split('\n');
    const cells = row!.split(',');
    expect(cells[1]).toBe('100'); // baseline_forecast stands in for the median
    expect(cells[2]).toBe('120');
    // Absent bounds stay empty rather than being invented.
    expect(cells[5]).toBe('');
    expect(cells[6]).toBe('');
  });

  it('leaves a null percentage empty', () => {
    const csv = buildComparisonCsv(
      comparison({ daily_forecasts: [point({ daily_difference_pct: null })] }),
    );

    expect(csv.split('\n')[1]!.split(',')[4]).toBe('');
  });

  it('builds a descriptive file name', () => {
    expect(comparisonFileName(comparison(), 'csv')).toBe(
      'scenario-store_001-scn_abc123.csv',
    );
  });
});

/* -------------------------------------------------------------------------- */
/* Narrative                                                                  */
/* -------------------------------------------------------------------------- */

describe('insight narrative', () => {
  it('produces a sentence in the documented shape', () => {
    const sentence = buildInsightSentence(comparison());

    expect(sentence).toContain('is associated with');
    expect(sentence).toContain('forecast increase');
    expect(sentence).toMatch(/over \d+ days\.$/);
  });

  it('never uses causal language', () => {
    const variants = [
      comparison(),
      comparison({ scenario_type: 'closure' }),
      comparison({
        impact: { ...comparison().impact, total_difference: -500, total_difference_pct: -12 },
      }),
      comparison({
        impact: { ...comparison().impact, total_difference: 0, total_difference_pct: 0 },
      }),
    ];

    for (const variant of variants) {
      const text = [
        buildInsightSentence(variant),
        ...buildInsightDetails(variant),
      ]
        .join(' ')
        .toLowerCase();

      for (const term of FORBIDDEN_CAUSAL_TERMS) {
        expect(text).not.toContain(term);
      }
    }
  });

  it('describes a decrease as a decrease', () => {
    const sentence = buildInsightSentence(
      comparison({
        impact: { ...comparison().impact, total_difference: -400, total_difference_pct: -8 },
      }),
    );

    expect(sentence).toContain('forecast decrease');
    expect(sentence).toContain('-8%');
  });

  it('reports no material change when the impact is zero', () => {
    const sentence = buildInsightSentence(
      comparison({
        impact: { ...comparison().impact, total_difference: 0, total_difference_pct: 0 },
      }),
    );

    expect(sentence).toContain('no material change');
  });

  it('omits the percentage when the API declined to compute it', () => {
    const sentence = buildInsightSentence(
      comparison({
        impact: { ...comparison().impact, total_difference: 120, total_difference_pct: null },
      }),
    );

    expect(sentence).toContain('forecast increase');
    expect(sentence).not.toContain('%');
  });

  it('describes the scenario shape', () => {
    expect(describeScenario(comparison())).toContain('promotion');
    expect(
      describeScenario(comparison({ scenario_type: 'closure', modified_dates: ['a', 'b'] })),
    ).toBe('2-day store closure');
  });

  it('reports a traffic scenario by its percentage change', () => {
    const text = describeScenario(
      comparison({
        scenario_type: 'traffic',
        assumptions: { ...comparison().assumptions!, traffic_multiplier: 0.7 },
      }),
    );

    expect(text).toContain('traffic change of -30%');
  });

  it('surfaces peak day, day counts, and the quantile range', () => {
    const details = buildInsightDetails(comparison());

    expect(details.some((line) => line.includes('Largest single-day movement'))).toBe(true);
    expect(details.some((line) => line.includes('forecast above baseline'))).toBe(true);
    expect(details.some((line) => line.includes('P10-P90'))).toBe(true);
  });

  it('flags a partial scenario when overrides were dropped', () => {
    const details = buildInsightDetails(
      comparison({
        unsupported_overrides: [
          { feature_name: 'is_promotion', reason: 'missing', error_code: 'FEATURE_NOT_AVAILABLE' },
        ],
      }),
    );

    expect(details.some((line) => line.includes('Partial scenario'))).toBe(true);
  });

  it('formats a currency only when one is supplied', () => {
    const plain = buildInsightSentence(comparison());
    const inr = buildInsightSentence(comparison(), { currency: 'INR', locale: 'en-IN' });

    expect(plain).not.toContain('₹');
    expect(inr).toContain('₹');
  });
});
