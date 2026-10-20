/**
 * Tests for the scenario form state machine.
 *
 * The reducer and selectors are pure, so the rules that matter -- result
 * clearing, staleness, idempotency-key reuse, per-type validation -- are tested
 * directly, with no renderer involved.
 */

import { describe, expect, it } from 'vitest';

import {
  CONFIDENCE_QUANTILES,
  FIELDS_BY_SCENARIO_TYPE,
  HOLIDAY_OPTIONS,
  HORIZON_OPTIONS,
  addDays,
  buildAssumptions,
  buildScenarioRequest,
  expandDateRange,
  initialFormState,
  pctToMultiplier,
  requestSignature,
  scenarioFormReducer,
  validateForm,
  type ScenarioFormState,
} from '@/hooks/use-scenario-form';
import type { HistoryPoint } from '@/lib/types/scenario';

function state(overrides: Partial<ScenarioFormState> = {}): ScenarioFormState {
  return { ...initialFormState({ storeId: 'CA_1' }), ...overrides };
}

function history(count = 10): HistoryPoint[] {
  return Array.from({ length: count }, (_, index) => ({
    date: `2026-01-${String(index + 1).padStart(2, '0')}`,
    sales: 500 + index,
  }));
}

/* -------------------------------------------------------------------------- */
/* Defaults                                                                    */
/* -------------------------------------------------------------------------- */

describe('initial state', () => {
  it('defaults to a 30-day horizon, latest model, and an 80% band', () => {
    const initial = initialFormState();

    expect(initial.horizonDays).toBe(30);
    expect(initial.modelVersion).toBe('latest');
    expect(initial.confidenceLevel).toBe(80);
    expect(initial.scenarioType).toBe('promotion');
    expect(initial.resultStatus).toBe('none');
    expect(initial.idempotency).toBeNull();
  });

  it('offers exactly the specified horizons', () => {
    expect([...HORIZON_OPTIONS]).toEqual([7, 14, 30, 60, 90]);
  });

  it('maps confidence levels to the right quantiles', () => {
    expect(CONFIDENCE_QUANTILES[80]).toEqual([0.1, 0.5, 0.9]);
    expect(CONFIDENCE_QUANTILES[95]).toEqual([0.025, 0.5, 0.975]);
  });
});

/* -------------------------------------------------------------------------- */
/* Result clearing vs staleness (requirement 9)                                */
/* -------------------------------------------------------------------------- */

describe('result lifecycle', () => {
  const fresh = () => state({ resultStatus: 'fresh', idempotency: { signature: 's', key: 'k' } });

  it.each([
    ['store', { type: 'setStoreId', value: 'TX_1' } as const],
    ['model', { type: 'setModelVersion', value: '21' } as const],
    ['horizon', { type: 'setHorizonDays', value: 60 } as const],
  ])('clears the result when %s changes', (_label, action) => {
    const next = scenarioFormReducer(fresh(), action);

    expect(next.resultStatus).toBe('none');
    // A different question needs a different idempotency key.
    expect(next.idempotency).toBeNull();
  });

  it('marks the result stale when an assumption changes', () => {
    const next = scenarioFormReducer(fresh(), {
      type: 'setAssumption',
      field: 'discountPct',
      value: 40,
    });

    expect(next.resultStatus).toBe('stale');
    expect(next.assumptions.discountPct).toBe(40);
    expect(next.touched.discountPct).toBe(true);
  });

  it('marks the result stale when the scenario type changes', () => {
    expect(
      scenarioFormReducer(fresh(), { type: 'setScenarioType', value: 'traffic' })
        .resultStatus,
    ).toBe('stale');
  });

  it('marks the result stale when the confidence band changes', () => {
    expect(
      scenarioFormReducer(fresh(), { type: 'setConfidenceLevel', value: 95 }).resultStatus,
    ).toBe('stale');
  });

  it('does not invent staleness when there is no result', () => {
    const next = scenarioFormReducer(state(), {
      type: 'setAssumption',
      field: 'discountPct',
      value: 40,
    });

    expect(next.resultStatus).toBe('none');
  });

  it('returns the same object when a value does not actually change', () => {
    const current = state();
    expect(scenarioFormReducer(current, { type: 'setStoreId', value: 'CA_1' })).toBe(current);
  });

  it('marks fresh after a successful run', () => {
    expect(scenarioFormReducer(state(), { type: 'runSucceeded' }).resultStatus).toBe('fresh');
  });

  it('reset returns to defaults', () => {
    const dirty = scenarioFormReducer(fresh(), {
      type: 'setAssumption',
      field: 'discountPct',
      value: 99,
    });

    const next = scenarioFormReducer(dirty, { type: 'reset' });

    expect(next.resultStatus).toBe('none');
    expect(next.assumptions.discountPct).toBe(20);
    expect(next.touched).toEqual({});
  });
});

/* -------------------------------------------------------------------------- */
/* Request building                                                            */
/* -------------------------------------------------------------------------- */

describe('assumption mapping', () => {
  it('derives a promotion end date from the duration', () => {
    const assumptions = buildAssumptions(
      state({
        scenarioType: 'promotion',
        assumptions: { ...initialFormState().assumptions, startDate: '2026-02-01', durationDays: 3 },
      }),
    );

    expect(assumptions.start_date).toBe('2026-02-01');
    // Inclusive of the start day: 3 days means Feb 1-3.
    expect(assumptions.end_date).toBe('2026-02-03');
    expect(assumptions.discount_depth).toBe(0.2);
    expect(assumptions.promotion_intensity).toBe('medium');
  });

  it('converts traffic percentages to the API multiplier range', () => {
    expect(pctToMultiplier(50)).toBe(1.5);
    expect(pctToMultiplier(-30)).toBe(0.7);
    // Clamped into the API's accepted 0.5-2.0 window.
    expect(pctToMultiplier(-90)).toBe(0.5);
    expect(pctToMultiplier(300)).toBe(2);
  });

  it('expands a closure range into individual dates', () => {
    const assumptions = buildAssumptions(
      state({
        scenarioType: 'closure',
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-01',
          endDate: '2026-02-03',
        },
      }),
    );

    expect(assumptions.closure_dates).toEqual(['2026-02-01', '2026-02-02', '2026-02-03']);
  });

  it('maps a holiday selection onto its date', () => {
    const holiday = HOLIDAY_OPTIONS[0]!;
    const assumptions = buildAssumptions(
      state({
        scenarioType: 'holiday',
        assumptions: { ...initialFormState().assumptions, holidayId: holiday.id, intensity: 'high' },
      }),
    );

    expect(assumptions.start_date).toBe(holiday.date);
    expect(assumptions.end_date).toBe(holiday.date);
    expect(assumptions.promotion_intensity).toBe('high');
  });

  it('maps a custom override to the feature map', () => {
    const assumptions = buildAssumptions(
      state({
        scenarioType: 'custom',
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-01',
          customFeature: 'customer_traffic',
          customValue: 450,
        },
      }),
    );

    expect(assumptions.custom_overrides).toEqual({ customer_traffic: 450 });
  });

  it('builds a complete API request', () => {
    const request = buildScenarioRequest(
      state({
        assumptions: { ...initialFormState().assumptions, startDate: '2026-02-01' },
      }),
      history(),
    );

    expect(request.store_id).toBe('CA_1');
    expect(request.horizon_days).toBe(30);
    expect(request.confidence_levels).toEqual([0.1, 0.5, 0.9]);
    expect(request.history).toHaveLength(10);
    expect(request.scenario.scenario_type).toBe('promotion');
  });

  it('handles date arithmetic across a month boundary', () => {
    expect(addDays('2026-01-30', 3)).toBe('2026-02-02');
    expect(expandDateRange('2026-02-03', '2026-02-01')).toEqual([]);
  });
});

/* -------------------------------------------------------------------------- */
/* Idempotency signatures                                                      */
/* -------------------------------------------------------------------------- */

describe('request signatures', () => {
  it('is stable for structurally identical requests', () => {
    const a = buildScenarioRequest(state(), history());
    const b = buildScenarioRequest(state(), history());

    expect(requestSignature(a)).toBe(requestSignature(b));
  });

  it('changes when an assumption changes, so a new key is minted', () => {
    const before = buildScenarioRequest(state(), history());
    const after = buildScenarioRequest(
      state({ assumptions: { ...initialFormState().assumptions, discountPct: 40 } }),
      history(),
    );

    expect(requestSignature(before)).not.toBe(requestSignature(after));
  });

  it('ignores key ordering', () => {
    const first = requestSignature({
      store_id: 'CA_1',
      horizon_days: 7,
      history: [],
      scenario: { scenario_type: 'promotion' },
    } as never);
    const second = requestSignature({
      scenario: { scenario_type: 'promotion' },
      history: [],
      horizon_days: 7,
      store_id: 'CA_1',
    } as never);

    expect(first).toBe(second);
  });
});

/* -------------------------------------------------------------------------- */
/* Validation                                                                  */
/* -------------------------------------------------------------------------- */

describe('validation', () => {
  it('requires a store', () => {
    expect(validateForm(initialFormState()).storeId).toBe('Select a store.');
  });

  it('requires enough history', () => {
    const errors = validateForm(state(), { historyCount: 3 });
    expect(errors.history).toBe('At least 7 days of history are required.');
  });

  it('accepts sufficient history', () => {
    expect(validateForm(state(), { historyCount: 10 }).history).toBeUndefined();
  });

  it('blocks an unsupported scenario type', () => {
    const errors = validateForm(state(), { scenarioSupported: false });
    expect(errors.scenarioType).toBe(
      'This scenario is not supported by the selected model.',
    );
  });

  it('requires a start date for a promotion', () => {
    expect(validateForm(state()).startDate).toBe('Choose a start date.');
  });

  it('rejects a duration longer than the horizon', () => {
    const errors = validateForm(
      state({
        horizonDays: 7,
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-01',
          durationDays: 14,
        },
      }),
    );

    expect(errors.durationDays).toBe('Duration cannot exceed the forecast horizon.');
  });

  it('rejects an out-of-range traffic change', () => {
    const errors = validateForm(
      state({
        scenarioType: 'traffic',
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-01',
          trafficChangePct: 80,
        },
      }),
    );

    expect(errors.trafficChangePct).toContain('between -30% and +50%');
  });

  it('requires an ordered closure range', () => {
    const errors = validateForm(
      state({
        scenarioType: 'closure',
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-05',
          endDate: '2026-02-01',
        },
      }),
    );

    expect(errors.endDate).toBe('End date must be on or after the start date.');
  });

  it('requires a holiday selection', () => {
    expect(validateForm(state({ scenarioType: 'holiday' })).holidayId).toBe(
      'Choose a holiday.',
    );
  });

  it('rejects a custom feature the model does not expose', () => {
    const errors = validateForm(
      state({
        scenarioType: 'custom',
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-01',
          customFeature: 'weather_index',
        },
      }),
      { availableFeatures: ['customer_traffic', 'price'] },
    );

    expect(errors.customFeature).toBe(
      'The selected model does not include this feature.',
    );
  });

  it('passes a fully specified promotion', () => {
    const errors = validateForm(
      state({
        assumptions: {
          ...initialFormState().assumptions,
          startDate: '2026-02-01',
          durationDays: 3,
        },
      }),
      { historyCount: 10, scenarioSupported: true },
    );

    expect(errors).toEqual({});
  });

  it('only validates the fields the selected type uses', () => {
    // A traffic scenario must not demand a holiday or a custom feature.
    const errors = validateForm(
      state({
        scenarioType: 'traffic',
        assumptions: { ...initialFormState().assumptions, startDate: '2026-02-01' },
      }),
      { historyCount: 10 },
    );

    expect(errors.holidayId).toBeUndefined();
    expect(errors.customFeature).toBeUndefined();
    expect(errors).toEqual({});
  });

  it('declares which fields each scenario type uses', () => {
    expect(FIELDS_BY_SCENARIO_TYPE.closure).toEqual(['startDate', 'endDate']);
    expect(FIELDS_BY_SCENARIO_TYPE.holiday).toEqual(['holidayId', 'intensity']);
    expect(FIELDS_BY_SCENARIO_TYPE.traffic).toContain('trafficChangePct');
  });
});
