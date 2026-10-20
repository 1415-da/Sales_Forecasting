/** Fixtures mirroring real API payloads. */

import type { ScenarioRequestInput } from '@/lib/types/scenario';

export const CAPABILITIES_PAYLOAD = {
  model_version: '14',
  feature_schema_version: 'v1',
  run_id: 'a'.repeat(32),
  feature_count: 5,
  features: ['is_promotion', 'customer_traffic', 'is_closed', 'is_holiday', 'price'],
  capabilities: [
    {
      scenario_type: 'promotion',
      supported: true,
      required_features: ['is_promotion', 'promotion_intensity', 'discount_depth'],
      available_features: ['is_promotion'],
      missing_features: [],
      requirement_mode: 'any',
      reason: null,
    },
    {
      scenario_type: 'traffic',
      supported: false,
      required_features: ['customer_traffic'],
      available_features: [],
      missing_features: ['customer_traffic'],
      requirement_mode: 'any',
      reason: 'Model version 14 was not trained with any of: customer_traffic.',
    },
  ],
  warnings: [],
  schema_version: 'v1',
  min_history_rows: 7,
  min_horizon_days: 1,
  max_horizon_days: 90,
  default_horizon_days: 30,
  default_model_version: 'latest',
  default_confidence_levels: [0.1, 0.5, 0.9],
  generated_at: '2026-02-01T10:00:00Z',
};

const DAILY_POINT = {
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
};

export const COMPARISON_PAYLOAD = {
  scenario_id: 'scn_abc123',
  baseline_forecast_id: 'bf_abc123',
  scenario_forecast_id: 'sf_abc123',
  model_version: '14',
  model_stage: 'Production',
  scenario_type: 'promotion',
  forecast_type: 'predictive_what_if',
  baseline: {
    forecast_id: 'bf_abc123',
    horizon_days: 1,
    start_date: '2026-02-01',
    end_date: '2026-02-01',
    total_forecast: 100,
    average_daily_forecast: 100,
    min_daily_forecast: 100,
    max_daily_forecast: 100,
    uncertainty_method: 'quantile_model',
    has_quantiles: true,
  },
  scenario: {
    forecast_id: null,
    horizon_days: 1,
    start_date: '2026-02-01',
    end_date: '2026-02-01',
    total_forecast: 120,
    average_daily_forecast: 120,
    min_daily_forecast: 120,
    max_daily_forecast: 120,
    uncertainty_method: 'quantile_model',
    has_quantiles: true,
  },
  impact: {
    horizon_days: 1,
    dates: ['2026-02-01'],
    daily_difference: [20],
    daily_difference_pct: [20],
    cumulative_difference: [20],
    total_baseline_forecast: 100,
    total_scenario_forecast: 120,
    total_difference: 20,
    total_difference_pct: 20,
    positive_days: 1,
    negative_days: 0,
    neutral_days: 0,
    peak_impact_date: '2026-02-01',
    peak_impact_value: 20,
    quantile_impacts: [
      {
        quantile: 0.5,
        total_baseline_forecast: 100,
        total_scenario_forecast: 120,
        total_difference: 20,
        total_difference_pct: 20,
      },
    ],
    impact_range: {
      total_difference_p10: 18,
      total_difference_p50: 20,
      total_difference_p90: 22,
    },
    daily_impacts: [DAILY_POINT],
    notes: [],
  },
  daily_forecasts: [DAILY_POINT],
  applied_overrides: [
    {
      feature_name: 'is_promotion',
      override_type: 'set_constant',
      affected_date_count: 1,
      original_value_summary: {
        count: 1,
        dtype: 'int64',
        min: 0,
        max: 0,
        mean: 0,
        distinct: [0],
      },
      new_value_summary: {
        count: 1,
        dtype: 'int64',
        min: 1,
        max: 1,
        mean: 1,
        distinct: [1],
      },
      affected_dates: ['2026-02-01'],
      detail: 'set to 1',
    },
  ],
  unsupported_overrides: [],
  modified_features: ['is_promotion'],
  modified_dates: ['2026-02-01'],
  limitations: [
    'This is a predictive what-if estimate based on the selected production model.',
    'It does not establish causal impact.',
    'Assumes scenario effects resemble historical patterns.',
    'Uncertainty reflects model error and does not include external shocks.',
  ],
  generated_at: '2026-02-01T10:00:00Z',
  store_id: 'store_001',
  assumptions: {
    scenario_type: 'promotion',
    start_date: '2026-02-01',
    end_date: '2026-02-01',
    duration_days: 1,
    promotion_intensity: 'high',
    discount_depth: 0.25,
    traffic_multiplier: null,
    closure_dates: [],
    custom_overrides: {},
  },
  baseline_reused: false,
  warnings: [],
  schema_version: 'v1',
  dataset_lineage: null,
};

export const ROLLBACK_PAYLOAD = {
  rolled_back: true,
  scenario_id: 'scn_abc123',
  request_id: 'req_1',
  status: 'rolled_back',
  stage: null,
  reason: 'assumptions were wrong',
  error_code: null,
  baseline_forecast_id: 'bf_abc123',
  baseline_preserved: true,
  baseline_status: 'completed',
  audit_event_recorded: true,
  audit_history_preserved: true,
  partial_results_discarded: true,
  locks_released: false,
  rolled_back_at: '2026-02-01T11:00:00Z',
  notes: ['The baseline forecast was not deleted or invalidated.'],
};

export const AUDIT_PAYLOAD = {
  scenario_id: 'scn_abc123',
  event_count: 2,
  events: [
    {
      request_id: 'req_1',
      scenario_id: 'scn_abc123',
      baseline_forecast_id: 'bf_abc123',
      store_id: 'store_001',
      model_version: '14',
      scenario_type: 'promotion',
      status: 'scenario_completed',
      error_code: null,
      error_message: null,
      duration_ms: 1420,
      created_at: '2026-02-01T10:00:00Z',
    },
    {
      request_id: 'req_1',
      scenario_id: 'scn_abc123',
      baseline_forecast_id: 'bf_abc123',
      store_id: 'store_001',
      model_version: '14',
      scenario_type: 'promotion',
      status: 'rolled_back',
      error_code: null,
      error_message: 'assumptions were wrong',
      duration_ms: null,
      created_at: '2026-02-01T11:00:00Z',
    },
  ],
};

export function errorEnvelope(
  code: string,
  overrides: Record<string, unknown> = {},
): unknown {
  return {
    success: false,
    error: {
      code,
      message: 'server-side message',
      field: null,
      details: {},
      request_id: 'req_server',
      timestamp: '2026-02-01T10:00:00Z',
      ...overrides,
    },
  };
}

export function buildRequest(
  overrides: Partial<ScenarioRequestInput> = {},
): ScenarioRequestInput {
  return {
    store_id: 'store_001',
    horizon_days: 7,
    history: Array.from({ length: 10 }, (_, index) => ({
      date: `2026-01-${String(index + 1).padStart(2, '0')}`,
      sales: 500 + index,
      store_id: 'store_001',
    })),
    scenario: {
      scenario_type: 'promotion',
      start_date: '2026-02-01',
      end_date: '2026-02-03',
      promotion_intensity: 'high',
      discount_depth: 0.25,
    },
    ...overrides,
  };
}

/** A fetch stub that records calls and replies with queued responses. */
export function stubFetch(
  replies: Array<{ status?: number; body?: unknown; headers?: Record<string, string> }>,
) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  let index = 0;

  const impl = (async (url: string | URL | Request, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    const reply = replies[Math.min(index, replies.length - 1)];
    index += 1;
    const status = reply?.status ?? 200;
    return new Response(reply?.body === undefined ? '' : JSON.stringify(reply.body), {
      status,
      headers: { 'Content-Type': 'application/json', ...(reply?.headers ?? {}) },
    });
  }) as unknown as typeof fetch;

  return { impl, calls };
}

/** Read a header from a recorded fetch call. */
export function headerOf(init: RequestInit, name: string): string | undefined {
  const headers = (init.headers ?? {}) as Record<string, string>;
  return headers[name];
}
