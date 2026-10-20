import { describe, expect, it, vi } from 'vitest';

import {
  IDEMPOTENCY_KEY_HEADER,
  REQUEST_ID_HEADER,
  createScenarioApiClient,
  generateIdempotencyKey,
  generateRequestId,
  scenarioKeys,
} from '@/lib/api/scenario-api';
import {
  scenarioCapabilitiesResponseSchema,
  scenarioComparisonResponseSchema,
} from '@/lib/types/scenario';
import { CLIENT_ERROR_CODES, ScenarioApiError } from '@/lib/utils/errors';

import {
  AUDIT_PAYLOAD,
  CAPABILITIES_PAYLOAD,
  COMPARISON_PAYLOAD,
  ROLLBACK_PAYLOAD,
  buildRequest,
  errorEnvelope,
  headerOf,
  stubFetch,
} from './fixtures';

function client(replies: Parameters<typeof stubFetch>[0]) {
  const { impl, calls } = stubFetch(replies);
  return {
    api: createScenarioApiClient({ baseUrl: 'http://api.test', fetchImpl: impl }),
    calls,
  };
}

/* -------------------------------------------------------------------------- */
/* Valid response parsing                                                      */
/* -------------------------------------------------------------------------- */

describe('valid API response parsing', () => {
  it('parses a capabilities response', async () => {
    const { api } = client([{ body: CAPABILITIES_PAYLOAD }]);

    const result = await api.getScenarioCapabilities('14');

    expect(result.model_version).toBe('14');
    expect(result.capabilities).toHaveLength(2);
    expect(result.capabilities[0]!.supported).toBe(true);
    expect(result.capabilities[1]!.missing_features).toEqual(['customer_traffic']);
    expect(result.max_horizon_days).toBe(90);
  });

  it('parses a comparison response', async () => {
    const { api } = client([{ body: COMPARISON_PAYLOAD }]);

    const result = await api.createScenarioComparison(buildRequest());

    expect(result.scenario_id).toBe('scn_abc123');
    expect(result.forecast_type).toBe('predictive_what_if');
    expect(result.impact.total_difference).toBe(20);
    expect(result.impact.impact_range.total_difference_p90).toBe(22);
    expect(result.daily_forecasts[0]!.difference_p50).toBe(20);
    expect(result.limitations).toHaveLength(4);
  });

  it('parses a rollback response', async () => {
    const { api } = client([{ body: ROLLBACK_PAYLOAD }]);

    const result = await api.rollbackScenario('scn_abc123', 'assumptions were wrong');

    expect(result.rolled_back).toBe(true);
    expect(result.status).toBe('rolled_back');
    expect(result.baseline_preserved).toBe(true);
  });

  it('parses an audit response', async () => {
    const { api } = client([{ body: AUDIT_PAYLOAD }]);

    const result = await api.getScenarioAudit('scn_abc123');

    expect(result.event_count).toBe(2);
    expect(result.events.map((event) => event.status)).toEqual([
      'scenario_completed',
      'rolled_back',
    ]);
  });

  it('fills defaults for omitted optional fields', () => {
    const slim = { ...CAPABILITIES_PAYLOAD } as Record<string, unknown>;
    delete slim.warnings;
    delete slim.features;

    const parsed = scenarioCapabilitiesResponseSchema.parse(slim);

    expect(parsed.warnings).toEqual([]);
    expect(parsed.features).toEqual([]);
  });

  it('ignores unknown fields so a newer backend does not break the client', () => {
    const parsed = scenarioComparisonResponseSchema.parse({
      ...COMPARISON_PAYLOAD,
      brand_new_field: 'from a future release',
    });

    expect(parsed.scenario_id).toBe('scn_abc123');
    expect('brand_new_field' in parsed).toBe(false);
  });

  it('preserves null percentages rather than coercing them to zero', async () => {
    // The API returns null for a percentage it refuses to compute.
    const zeroBaseline = structuredClone(COMPARISON_PAYLOAD) as Record<string, unknown>;
    const impact = zeroBaseline.impact as Record<string, unknown>;
    impact.total_difference_pct = null;
    impact.daily_difference_pct = [null];
    const { api } = client([{ body: zeroBaseline }]);

    const result = await api.getScenarioById('scn_abc123');

    expect(result.impact.total_difference_pct).toBeNull();
    expect(result.impact.daily_difference_pct[0]).toBeNull();
  });
});

/* -------------------------------------------------------------------------- */
/* Invalid response handling                                                   */
/* -------------------------------------------------------------------------- */

describe('invalid API response handling', () => {
  it('raises INVALID_RESPONSE when a required field is missing', async () => {
    const broken = { ...COMPARISON_PAYLOAD } as Record<string, unknown>;
    delete broken.impact;
    const { api } = client([{ body: broken }]);

    await expect(api.getScenarioById('scn_abc123')).rejects.toMatchObject({
      code: CLIENT_ERROR_CODES.INVALID_RESPONSE,
    });
  });

  it('records which fields failed, for diagnostics only', async () => {
    const { api } = client([{ body: { nonsense: true } }]);

    const error = await api.getScenarioCapabilities('14').catch((e) => e);

    expect(error).toBeInstanceOf(ScenarioApiError);
    expect(error.code).toBe(CLIENT_ERROR_CODES.INVALID_RESPONSE);
    expect(error.message).toContain('unexpected response');
    expect(Array.isArray(error.details.issues)).toBe(true);
  });

  it('rejects a wrong-typed field', async () => {
    const wrongType = structuredClone(COMPARISON_PAYLOAD) as Record<string, unknown>;
    (wrongType.impact as Record<string, unknown>).total_difference = 'a lot';
    const { api } = client([{ body: wrongType }]);

    await expect(api.getScenarioById('scn_abc123')).rejects.toMatchObject({
      code: CLIENT_ERROR_CODES.INVALID_RESPONSE,
    });
  });

  it('treats an empty 200 body as an invalid response', async () => {
    const { api } = client([{ status: 200 }]);

    await expect(api.getScenarioAudit('scn_abc123')).rejects.toMatchObject({
      code: CLIENT_ERROR_CODES.INVALID_RESPONSE,
    });
  });

  it('surfaces a network failure as NETWORK_ERROR', async () => {
    const api = createScenarioApiClient({
      baseUrl: 'http://api.test',
      fetchImpl: (() => Promise.reject(new TypeError('fetch failed'))) as typeof fetch,
    });

    const error = await api.getScenarioCapabilities('14').catch((e) => e);

    expect(error.code).toBe(CLIENT_ERROR_CODES.NETWORK_ERROR);
    expect(error.isRetryable).toBe(true);
  });

  it('maps an abort to REQUEST_ABORTED and does not mark it retryable', async () => {
    const api = createScenarioApiClient({
      baseUrl: 'http://api.test',
      fetchImpl: (() =>
        Promise.reject(new DOMException('aborted', 'AbortError'))) as typeof fetch,
    });

    const error = await api.getScenarioCapabilities('14').catch((e) => e);

    expect(error.code).toBe(CLIENT_ERROR_CODES.REQUEST_ABORTED);
    expect(error.isRetryable).toBe(false);
  });
});

/* -------------------------------------------------------------------------- */
/* Error responses                                                             */
/* -------------------------------------------------------------------------- */

describe('API error responses', () => {
  it('maps a 404 envelope to a typed error', async () => {
    const { api } = client([{ status: 404, body: errorEnvelope('MODEL_NOT_FOUND') }]);

    const error = await api.getScenarioCapabilities('99').catch((e) => e);

    expect(error).toBeInstanceOf(ScenarioApiError);
    expect(error.code).toBe('MODEL_NOT_FOUND');
    expect(error.status).toBe(404);
    expect(error.message).toBe('Selected model version is unavailable.');
    expect(error.requestId).toBe('req_server');
  });

  it('keeps the blamed field for inline form errors', async () => {
    const { api } = client([
      {
        status: 409,
        body: errorEnvelope('SCENARIO_NOT_SUPPORTED', {
          field: 'scenario.scenario_type',
          details: { missing_features: ['is_promotion'] },
        }),
      },
    ]);

    const error = await api.createScenarioComparison(buildRequest()).catch((e) => e);

    expect(error.field).toBe('scenario.scenario_type');
    expect(error.details.missing_features).toEqual(['is_promotion']);
    expect(error.isInputError).toBe(true);
  });

  it('derives a code from the status when the body is not the documented envelope', async () => {
    const { api } = client([{ status: 503, body: { detail: 'upstream down' } }]);

    const error = await api.getScenarioById('scn_abc123').catch((e) => e);

    expect(error.code).toBe('MLFLOW_UNAVAILABLE');
    expect(error.status).toBe(503);
    expect(error.isRetryable).toBe(true);
  });

  it('never echoes the raw server message for an unknown code', async () => {
    const { api } = client([
      {
        status: 500,
        body: errorEnvelope('SOME_FUTURE_CODE', {
          message: 'internal detail that should not be shown',
        }),
      },
    ]);

    const error = await api.getScenarioById('scn_abc123').catch((e) => e);

    expect(error.code).toBe('SOME_FUTURE_CODE');
    // User-facing text is always ours; the server's text survives only for logs.
    expect(error.message).toBe('Something went wrong. Please try again.');
    expect(error.toPresentation().message).toBe('Something went wrong. Please try again.');
    expect(error.serverMessage).toBe('internal detail that should not be shown');
  });
});

/* -------------------------------------------------------------------------- */
/* Headers                                                                     */
/* -------------------------------------------------------------------------- */

describe('request id and idempotency headers', () => {
  it('sends a generated request id on every call', async () => {
    const { api, calls } = client([{ body: CAPABILITIES_PAYLOAD }]);

    await api.getScenarioCapabilities('14');

    const requestId = headerOf(calls[0]!.init, REQUEST_ID_HEADER);
    expect(requestId).toMatch(/^req_/);
  });

  it('reuses a caller-supplied request id', async () => {
    const { api, calls } = client([{ body: CAPABILITIES_PAYLOAD }]);

    await api.getScenarioCapabilities('14', { requestId: 'req_from_caller' });

    expect(headerOf(calls[0]!.init, REQUEST_ID_HEADER)).toBe('req_from_caller');
  });

  it('reports the request id it used', async () => {
    const onRequestId = vi.fn();
    const { impl } = stubFetch([{ body: CAPABILITIES_PAYLOAD }]);
    const api = createScenarioApiClient({
      baseUrl: 'http://api.test',
      fetchImpl: impl,
      onRequestId,
    });

    await api.getScenarioCapabilities('14');

    expect(onRequestId).toHaveBeenCalledWith(expect.stringMatching(/^req_/));
  });

  it('prefers the server request id on an error, since that is what is logged', async () => {
    const { api } = client([
      {
        status: 500,
        body: errorEnvelope('PREDICTION_FAILED', { request_id: null }),
        headers: { [REQUEST_ID_HEADER]: 'req_from_server' },
      },
    ]);

    const error = await api.createScenarioComparison(buildRequest()).catch((e) => e);

    expect(error.requestId).toBe('req_from_server');
  });

  it('sends an idempotency key only when one is supplied', async () => {
    const { api, calls } = client([{ body: COMPARISON_PAYLOAD }]);

    await api.createScenarioComparison(buildRequest(), { idempotencyKey: 'key-1' });

    expect(headerOf(calls[0]!.init, IDEMPOTENCY_KEY_HEADER)).toBe('key-1');
  });

  it('omits the idempotency header when no key is given', async () => {
    const { api, calls } = client([{ body: COMPARISON_PAYLOAD }]);

    await api.createScenarioComparison(buildRequest());

    expect(headerOf(calls[0]!.init, IDEMPOTENCY_KEY_HEADER)).toBeUndefined();
  });

  it('never sends an idempotency key on a read', async () => {
    const { api, calls } = client([
      { body: CAPABILITIES_PAYLOAD },
      { body: AUDIT_PAYLOAD },
    ]);

    await api.getScenarioCapabilities('14');
    await api.getScenarioAudit('scn_abc123');

    for (const call of calls) {
      expect(headerOf(call.init, IDEMPOTENCY_KEY_HEADER)).toBeUndefined();
    }
  });

  it('surfaces an idempotency conflict with its own message', async () => {
    const { api } = client([
      {
        status: 409,
        body: errorEnvelope('IDEMPOTENCY_KEY_CONFLICT', { field: 'Idempotency-Key' }),
      },
    ]);

    const error = await api
      .createScenarioComparison(buildRequest(), { idempotencyKey: 'key-1' })
      .catch((e) => e);

    expect(error.code).toBe('IDEMPOTENCY_KEY_CONFLICT');
    expect(error.message).toBe(
      'This scenario request was already submitted with different inputs.',
    );
    expect(error.isRetryable).toBe(false);
    expect(error.isInputError).toBe(true);
  });

  it('generates distinct ids and keys', () => {
    expect(generateRequestId()).not.toBe(generateRequestId());
    expect(generateIdempotencyKey()).not.toBe(generateIdempotencyKey());
    expect(generateRequestId()).toMatch(/^req_/);
  });

  it('sets a JSON content type only on writes', async () => {
    const { api, calls } = client([
      { body: CAPABILITIES_PAYLOAD },
      { body: COMPARISON_PAYLOAD },
    ]);

    await api.getScenarioCapabilities('14');
    await api.createScenarioComparison(buildRequest());

    expect(headerOf(calls[0]!.init, 'Content-Type')).toBeUndefined();
    expect(headerOf(calls[1]!.init, 'Content-Type')).toBe('application/json');
  });
});

/* -------------------------------------------------------------------------- */
/* Request shaping                                                             */
/* -------------------------------------------------------------------------- */

describe('request construction', () => {
  it('calls the documented endpoints', async () => {
    const { api, calls } = client([
      { body: CAPABILITIES_PAYLOAD },
      { body: COMPARISON_PAYLOAD },
      { body: COMPARISON_PAYLOAD },
      { body: ROLLBACK_PAYLOAD },
      { body: AUDIT_PAYLOAD },
    ]);

    await api.getScenarioCapabilities('14');
    await api.createScenarioComparison(buildRequest());
    await api.getScenarioById('scn_abc123');
    await api.rollbackScenario('scn_abc123', 'reason enough');
    await api.getScenarioAudit('scn_abc123');

    expect(calls.map((call) => `${call.init.method} ${new URL(call.url).pathname}`)).toEqual([
      'GET /v1/scenario/capabilities',
      'POST /v1/scenario/compare',
      'GET /v1/scenario/scn_abc123',
      'POST /v1/scenario/scn_abc123/rollback',
      'GET /v1/scenario/scn_abc123/audit',
    ]);
    expect(new URL(calls[0]!.url).searchParams.get('model_version')).toBe('14');
  });

  it('applies request defaults before sending', async () => {
    const { api, calls } = client([{ body: COMPARISON_PAYLOAD }]);

    await api.createScenarioComparison(buildRequest());

    const body = JSON.parse(String(calls[0]!.init.body));
    expect(body.model_version).toBe('latest');
    expect(body.confidence_levels).toEqual([0.1, 0.5, 0.9]);
    expect(body.scenario.closure_dates).toEqual([]);
  });

  it('url-encodes a scenario id', async () => {
    const { api, calls } = client([{ body: AUDIT_PAYLOAD }]);

    await api.getScenarioAudit('scn/../admin');

    expect(new URL(calls[0]!.url).pathname).toBe('/v1/scenario/scn%2F..%2Fadmin/audit');
  });

  it('rejects an invalid request before any network call', async () => {
    const { api, calls } = client([{ body: COMPARISON_PAYLOAD }]);

    const error = await api
      .createScenarioComparison(buildRequest({ history: [] }))
      .catch((e) => e);

    expect(error.code).toBe('SCENARIO_VALIDATION_ERROR');
    expect(error.field).toBe('history');
    expect(calls).toHaveLength(0);
  });

  it.each([
    ['unsorted history', { history: buildRequest().history.slice().reverse() }],
    ['over-long horizon', { horizon_days: 500 }],
    ['unsorted confidence levels', { confidence_levels: [0.9, 0.1] }],
    ['empty store id', { store_id: '' }],
  ])('validates %s locally', async (_label, overrides) => {
    const { api, calls } = client([{ body: COMPARISON_PAYLOAD }]);

    await expect(
      api.createScenarioComparison(buildRequest(overrides as never)),
    ).rejects.toMatchObject({ code: 'SCENARIO_VALIDATION_ERROR' });
    expect(calls).toHaveLength(0);
  });

  it('exposes hierarchical query keys for invalidation', () => {
    expect(scenarioKeys.capabilities('14')).toEqual(['scenario', 'capabilities', '14']);
    expect(scenarioKeys.detail('scn_1')).toEqual(['scenario', 'detail', 'scn_1']);
    expect(scenarioKeys.audit('scn_1')).toEqual(['scenario', 'audit', 'scn_1']);
    expect(scenarioKeys.capabilities('14').slice(0, 1)).toEqual([...scenarioKeys.all]);
  });
});
