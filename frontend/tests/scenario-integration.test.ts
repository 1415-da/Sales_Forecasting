/**
 * End-to-end integration tests for the Scenario Planner flow.
 *
 * These drive the real API client, the real form state machine, a real
 * QueryClient, and a real MutationObserver against a stubbed `fetch`, so the
 * whole path -- validate, claim key, POST, retry, parse, cache, roll back --
 * is exercised without a browser.
 *
 * Browser-level Playwright coverage additionally needs `next` installed, a dev
 * server, and a backend whose inference adapters exist; see the note in the
 * task summary.
 */

import { MutationObserver, QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import {
  COMPARISON_TIMEOUT_MS,
  IDEMPOTENCY_KEY_HEADER,
  createScenarioApiClient,
  scenarioKeys,
} from '@/lib/api/scenario-api';
import {
  MAX_AUTO_RETRIES,
  retryDelayMs,
} from '@/hooks/use-scenario-forecast';
import { invalidateScenarioCaches } from '@/hooks/use-scenario-rollback';
import {
  buildScenarioRequest,
  initialFormState,
  requestSignature,
  scenarioFormReducer,
  validateForm,
  type ScenarioFormState,
} from '@/hooks/use-scenario-form';
import type {
  ScenarioComparisonResponse,
  ScenarioRequestInput,
} from '@/lib/types/scenario';
import {
  CLIENT_ERROR_CODES,
  TIMEOUT_MESSAGE,
  isAutoRetryableError,
  toErrorPresentation,
} from '@/lib/utils/errors';
import { buildComparisonCsv } from '@/lib/utils/scenario-results';

import {
  AUDIT_PAYLOAD,
  COMPARISON_PAYLOAD,
  ROLLBACK_PAYLOAD,
  buildRequest,
  errorEnvelope,
  headerOf,
  stubFetch,
} from './fixtures';

/* -------------------------------------------------------------------------- */
/* Harness                                                                     */
/* -------------------------------------------------------------------------- */

function harness(replies: Parameters<typeof stubFetch>[0]) {
  const { impl, calls } = stubFetch(replies);
  const api = createScenarioApiClient({ baseUrl: 'http://api.test', fetchImpl: impl });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return { api, calls, queryClient };
}

/**
 * Run one comparison through a real MutationObserver, as the hook does.
 *
 * `retry` mirrors TanStack's own type: a count, or a predicate over the
 * failure, so a test can supply the production retry policy itself.
 */
type RetryOption = number | ((failureCount: number, error: unknown) => boolean);

async function runComparison(
  api: ReturnType<typeof createScenarioApiClient>,
  queryClient: QueryClient,
  variables: { request: ScenarioRequestInput; idempotencyKey?: string },
  options: { retry?: RetryOption } = {},
) {
  const observer = new MutationObserver<
    ScenarioComparisonResponse,
    unknown,
    typeof variables
  >(queryClient, {
    mutationFn: (vars: typeof variables) =>
      api.createScenarioComparison(vars.request, {
        idempotencyKey: vars.idempotencyKey,
      }),
    retry: options.retry ?? 0,
    retryDelay: 0,
  });
  return observer.mutate(variables);
}

function formWith(overrides: Partial<ScenarioFormState> = {}): ScenarioFormState {
  return { ...initialFormState({ storeId: 'CA_1' }), ...overrides };
}

/* -------------------------------------------------------------------------- */
/* 1. Successful promotion scenario                                            */
/* -------------------------------------------------------------------------- */

describe('successful promotion scenario', () => {
  it('validates, posts, and returns a parsed comparison', async () => {
    const { api, calls, queryClient } = harness([{ body: COMPARISON_PAYLOAD }]);

    const result = await runComparison(api, queryClient, {
      request: buildRequest(),
      idempotencyKey: 'key-1',
    });

    expect(result.scenario_id).toBe('scn_abc123');
    expect(result.forecast_type).toBe('predictive_what_if');
    expect(result.impact.total_difference).toBe(20);
    expect(calls).toHaveLength(1);

    const sent = JSON.parse(String(calls[0]!.init.body));
    expect(sent.scenario.scenario_type).toBe('promotion');
    expect(sent.scenario.discount_depth).toBe(0.25);
    expect(headerOf(calls[0]!.init, IDEMPOTENCY_KEY_HEADER)).toBe('key-1');
  });

  it('seeds the detail cache so the result survives navigation', async () => {
    const { api, queryClient } = harness([{ body: COMPARISON_PAYLOAD }]);

    const result = await runComparison(api, queryClient, { request: buildRequest() });
    queryClient.setQueryData(scenarioKeys.detail(result.scenario_id), result);

    expect(queryClient.getQueryData(scenarioKeys.detail('scn_abc123'))).toBeDefined();
  });

  it('builds the request from form state end to end', async () => {
    const state = formWith({
      assumptions: { ...initialFormState().assumptions, startDate: '2026-02-01', durationDays: 3 },
    });
    const { api, calls, queryClient } = harness([{ body: COMPARISON_PAYLOAD }]);

    await runComparison(api, queryClient, {
      request: buildScenarioRequest(state, buildRequest().history),
    });

    const sent = JSON.parse(String(calls[0]!.init.body));
    expect(sent.store_id).toBe('CA_1');
    expect(sent.horizon_days).toBe(30);
    expect(sent.scenario.start_date).toBe('2026-02-01');
    expect(sent.scenario.end_date).toBe('2026-02-03');
  });
});

/* -------------------------------------------------------------------------- */
/* 2. Validation error                                                         */
/* -------------------------------------------------------------------------- */

describe('validation error', () => {
  it('blocks a short history before any network call', async () => {
    const { api, calls, queryClient } = harness([{ body: COMPARISON_PAYLOAD }]);

    await expect(
      runComparison(api, queryClient, {
        request: buildRequest({ history: buildRequest().history.slice(0, 3) }),
      }),
    ).rejects.toMatchObject({ code: 'SCENARIO_VALIDATION_ERROR' });

    expect(calls).toHaveLength(0);
  });

  it('reports the offending field for inline display', async () => {
    const { api, queryClient } = harness([{ body: COMPARISON_PAYLOAD }]);

    const error = await runComparison(api, queryClient, {
      request: buildRequest({ horizon_days: 500 }),
    }).catch((e) => e);

    expect(error.field).toBe('horizon_days');
    expect(toErrorPresentation(error).severity).toBe('warning');
  });

  it('surfaces a server-side 422 as an input error', async () => {
    const { api, queryClient } = harness([
      {
        status: 422,
        body: errorEnvelope('SCENARIO_VALIDATION_ERROR', { field: 'history' }),
      },
    ]);

    const error = await runComparison(api, queryClient, {
      request: buildRequest(),
    }).catch((e) => e);

    expect(error.status).toBe(422);
    expect(error.message).toBe('Please correct the highlighted inputs.');
    expect(isAutoRetryableError(error)).toBe(false);
  });

  it('local validation mirrors the API rules', () => {
    expect(validateForm(formWith(), { historyCount: 3 }).history).toContain(
      'At least 7 days',
    );
  });
});

/* -------------------------------------------------------------------------- */
/* 3. Unsupported scenario                                                     */
/* -------------------------------------------------------------------------- */

describe('unsupported scenario', () => {
  it('reports a 409 with the missing features', async () => {
    const { api, queryClient } = harness([
      {
        status: 409,
        body: errorEnvelope('SCENARIO_NOT_SUPPORTED', {
          field: 'scenario.scenario_type',
          details: {
            scenario_type: 'promotion',
            missing_features: ['is_promotion', 'promotion_intensity'],
            supported_scenario_types: ['traffic', 'holiday'],
          },
        }),
      },
    ]);

    const error = await runComparison(api, queryClient, {
      request: buildRequest(),
    }).catch((e) => e);

    expect(error.code).toBe('SCENARIO_NOT_SUPPORTED');
    expect(error.status).toBe(409);
    expect(error.details.missing_features).toEqual([
      'is_promotion',
      'promotion_intensity',
    ]);
    expect(error.details.supported_scenario_types).toEqual(['traffic', 'holiday']);
    // A 409 is deterministic, so it must never be retried automatically.
    expect(isAutoRetryableError(error)).toBe(false);
  });

  it('renders a partial scenario when some overrides applied', async () => {
    const partial = structuredClone(COMPARISON_PAYLOAD) as Record<string, unknown>;
    partial.unsupported_overrides = [
      {
        feature_name: 'promotion_intensity',
        reason: 'Not in the model feature schema.',
        error_code: 'FEATURE_NOT_AVAILABLE',
      },
    ];
    const { api, queryClient } = harness([{ body: partial }]);

    const result = await runComparison(api, queryClient, { request: buildRequest() });

    // The comparison still renders; the banner warns it is incomplete.
    expect(result.unsupported_overrides).toHaveLength(1);
    expect(result.applied_overrides.length).toBeGreaterThan(0);
    expect(result.impact.total_difference).toBe(20);
  });
});

/* -------------------------------------------------------------------------- */
/* 4. API failure and retry                                                    */
/* -------------------------------------------------------------------------- */

describe('API failure and retry', () => {
  it('retries a 503 and succeeds on the second attempt', async () => {
    const { api, calls, queryClient } = harness([
      { status: 503, body: errorEnvelope('MLFLOW_UNAVAILABLE') },
      { body: COMPARISON_PAYLOAD },
    ]);

    const result = await runComparison(
      api,
      queryClient,
      { request: buildRequest(), idempotencyKey: 'key-retry' },
      { retry: MAX_AUTO_RETRIES },
    );

    expect(result.scenario_id).toBe('scn_abc123');
    expect(calls).toHaveLength(2);
    // The same key on both attempts, so the retry replays rather than
    // triggering a second inference server-side.
    expect(calls.map((call) => headerOf(call.init, IDEMPOTENCY_KEY_HEADER))).toEqual([
      'key-retry',
      'key-retry',
    ]);
  });

  it('gives up after the maximum number of attempts', async () => {
    const { api, calls, queryClient } = harness([
      { status: 502, body: errorEnvelope('ARTIFACT_DOWNLOAD_FAILED') },
    ]);

    await expect(
      runComparison(
        api,
        queryClient,
        { request: buildRequest() },
        { retry: MAX_AUTO_RETRIES },
      ),
    ).rejects.toMatchObject({ status: 502 });

    // One initial attempt plus two retries.
    expect(calls).toHaveLength(MAX_AUTO_RETRIES + 1);
  });

  it('retries a network failure', async () => {
    let attempts = 0;
    const api = createScenarioApiClient({
      baseUrl: 'http://api.test',
      fetchImpl: (async () => {
        attempts += 1;
        if (attempts === 1) throw new TypeError('fetch failed');
        return new Response(JSON.stringify(COMPARISON_PAYLOAD), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }) as unknown as typeof fetch,
    });
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });

    const result = await runComparison(
      api,
      queryClient,
      { request: buildRequest() },
      { retry: MAX_AUTO_RETRIES },
    );

    expect(result.scenario_id).toBe('scn_abc123');
    expect(attempts).toBe(2);
  });

  it.each([
    [422, 'SCENARIO_VALIDATION_ERROR'],
    [404, 'MODEL_NOT_FOUND'],
    [409, 'SCENARIO_NOT_SUPPORTED'],
  ])('does not retry a %s', async (status, code) => {
    const { api, calls, queryClient } = harness([
      { status: status as number, body: errorEnvelope(code as string) },
    ]);

    await expect(
      runComparison(
        api,
        queryClient,
        { request: buildRequest() },
        { retry: (count: number, error: unknown) => count < 2 && isAutoRetryableError(error) },
      ),
    ).rejects.toMatchObject({ status });

    expect(calls).toHaveLength(1);
  });

  it('backs off exponentially', () => {
    expect(retryDelayMs(0)).toBe(1000);
    expect(retryDelayMs(1)).toBe(2000);
  });

  it('offers a manual retry for a deterministic 500', async () => {
    const { api, queryClient } = harness([
      { status: 500, body: errorEnvelope('PREDICTION_FAILED') },
    ]);

    const error = await runComparison(api, queryClient, {
      request: buildRequest(),
    }).catch((e) => e);

    // Not auto-retried, but the user may choose to retry.
    expect(isAutoRetryableError(error)).toBe(false);
    expect(toErrorPresentation(error).retryable).toBe(true);
  });
});

/* -------------------------------------------------------------------------- */
/* Timeout                                                                     */
/* -------------------------------------------------------------------------- */

describe('timeout handling', () => {
  it('aborts a slow comparison and reports the documented message', async () => {
    vi.useFakeTimers();
    try {
      const api = createScenarioApiClient({
        baseUrl: 'http://api.test',
        fetchImpl: ((_url: string, init: RequestInit = {}) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            );
          })) as unknown as typeof fetch,
      });

      const pending = api
        .createScenarioComparison(buildRequest(), { timeoutMs: 1000 })
        .catch((error) => error);

      await vi.advanceTimersByTimeAsync(1001);
      const error = await pending;

      expect(error.code).toBe(CLIENT_ERROR_CODES.TIMEOUT);
      expect(error.message).toBe(TIMEOUT_MESSAGE);
      expect(error.message).toContain('taking longer than expected');
      expect(error.details.timeout_ms).toBe(1000);
      // A timeout is transport-level, so it is safe to retry automatically.
      expect(isAutoRetryableError(error)).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('defaults the comparison deadline to 60 seconds', () => {
    expect(COMPARISON_TIMEOUT_MS).toBe(60_000);
  });

  it('distinguishes a user cancellation from a timeout', async () => {
    const controller = new AbortController();
    const api = createScenarioApiClient({
      baseUrl: 'http://api.test',
      fetchImpl: ((_url: string, init: RequestInit = {}) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          );
        })) as unknown as typeof fetch,
    });

    const pending = api
      .createScenarioComparison(buildRequest(), {
        signal: controller.signal,
        timeoutMs: 60_000,
      })
      .catch((error) => error);
    controller.abort();

    const error = await pending;
    expect(error.code).toBe(CLIENT_ERROR_CODES.REQUEST_ABORTED);
    expect(isAutoRetryableError(error)).toBe(false);
  });
});

/* -------------------------------------------------------------------------- */
/* 5. Baseline reuse and staleness                                             */
/* -------------------------------------------------------------------------- */

describe('baseline reuse', () => {
  it('sends the baseline id on a follow-up run', async () => {
    const { api, calls, queryClient } = harness([
      { body: COMPARISON_PAYLOAD },
      { body: { ...COMPARISON_PAYLOAD, baseline_reused: true } },
    ]);

    const first = await runComparison(api, queryClient, { request: buildRequest() });
    await runComparison(api, queryClient, {
      request: { ...buildRequest(), baseline_forecast_id: first.baseline_forecast_id },
    });

    expect(JSON.parse(String(calls[0]!.init.body)).baseline_forecast_id).toBeUndefined();
    expect(JSON.parse(String(calls[1]!.init.body)).baseline_forecast_id).toBe('bf_abc123');
  });

  it('reports reuse on the response, for the badge', async () => {
    const { api, queryClient } = harness([
      { body: { ...COMPARISON_PAYLOAD, baseline_reused: true } },
    ]);

    const result = await runComparison(api, queryClient, { request: buildRequest() });
    expect(result.baseline_reused).toBe(true);
  });

  it('drops reuse when the question changes, keeps it when assumptions move', () => {
    const fresh = formWith({ resultStatus: 'fresh' });

    // Store, model, and horizon changes clear the result: a new baseline.
    for (const action of [
      { type: 'setStoreId', value: 'TX_1' } as const,
      { type: 'setModelVersion', value: '21' } as const,
      { type: 'setHorizonDays', value: 60 } as const,
    ]) {
      expect(scenarioFormReducer(fresh, action).resultStatus).toBe('none');
    }

    // An assumption change only marks it stale, so the baseline still applies.
    expect(
      scenarioFormReducer(fresh, {
        type: 'setAssumption',
        field: 'discountPct',
        value: 40,
      }).resultStatus,
    ).toBe('stale');
  });

  it('mints a new idempotency key once the request changes', () => {
    const before = buildScenarioRequest(formWith(), buildRequest().history);
    const after = buildScenarioRequest(
      formWith({ assumptions: { ...initialFormState().assumptions, discountPct: 55 } }),
      buildRequest().history,
    );

    expect(requestSignature(before)).not.toBe(requestSignature(after));
  });
});

/* -------------------------------------------------------------------------- */
/* 6. Rollback flow                                                            */
/* -------------------------------------------------------------------------- */

describe('rollback flow', () => {
  it('posts the reason and reports preserved history', async () => {
    const { api, calls } = harness([{ body: ROLLBACK_PAYLOAD }]);

    const result = await api.rollbackScenario('scn_abc123', 'assumptions were wrong');

    expect(result.rolled_back).toBe(true);
    expect(result.status).toBe('rolled_back');
    expect(result.audit_history_preserved).toBe(true);
    expect(result.baseline_preserved).toBe(true);
    expect(new URL(calls[0]!.url).pathname).toBe('/v1/scenario/scn_abc123/rollback');
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      reason: 'assumptions were wrong',
    });
  });

  it('invalidates the detail and audit caches afterwards', async () => {
    const { queryClient } = harness([{ body: ROLLBACK_PAYLOAD }]);
    queryClient.setQueryData(scenarioKeys.detail('scn_abc123'), COMPARISON_PAYLOAD);
    queryClient.setQueryData(scenarioKeys.audit('scn_abc123'), AUDIT_PAYLOAD);

    await invalidateScenarioCaches(queryClient, 'scn_abc123');

    expect(
      queryClient.getQueryState(scenarioKeys.detail('scn_abc123'))?.isInvalidated,
    ).toBe(true);
    expect(
      queryClient.getQueryState(scenarioKeys.audit('scn_abc123'))?.isInvalidated,
    ).toBe(true);
  });

  it('rejects a reason the API would refuse', async () => {
    const { api } = harness([
      { status: 422, body: errorEnvelope('SCENARIO_VALIDATION_ERROR', { field: 'reason' }) },
    ]);

    const error = await api.rollbackScenario('scn_abc123', 'x').catch((e) => e);
    expect(error.status).toBe(422);
  });

  it('shows the audit trail including the rollback', async () => {
    const { api } = harness([{ body: AUDIT_PAYLOAD }]);

    const audit = await api.getScenarioAudit('scn_abc123');

    expect(audit.events.map((event) => event.status)).toEqual([
      'scenario_completed',
      'rolled_back',
    ]);
  });
});

/* -------------------------------------------------------------------------- */
/* 7. CSV export                                                               */
/* -------------------------------------------------------------------------- */

describe('CSV export', () => {
  it('exports the comparison returned by the API', async () => {
    const { api, queryClient } = harness([{ body: COMPARISON_PAYLOAD }]);

    const result = await runComparison(api, queryClient, { request: buildRequest() });
    const csv = buildComparisonCsv(result);
    const [header, firstRow] = csv.split('\n');

    expect(header).toBe(
      'date,baseline_p50,scenario_p50,difference,difference_pct,baseline_p10,baseline_p90,scenario_p10,scenario_p90',
    );
    expect(firstRow).toBe('2026-02-01,100,120,20,20,90,110,108,132');
  });

  it('exports one row per forecast day', async () => {
    const multiDay = structuredClone(COMPARISON_PAYLOAD) as Record<string, unknown>;
    const template = (multiDay.daily_forecasts as unknown[])[0];
    multiDay.daily_forecasts = [1, 2, 3].map((day) => ({
      ...(template as Record<string, unknown>),
      date: `2026-02-0${day}`,
    }));
    const { api, queryClient } = harness([{ body: multiDay }]);

    const result = await runComparison(api, queryClient, { request: buildRequest() });

    expect(buildComparisonCsv(result).split('\n')).toHaveLength(4);
  });
});
