import { describe, expect, it, vi } from 'vitest';

import { scenarioErrorCodeSchema } from '@/lib/types/scenario';
import {
  CLIENT_ERROR_CODES,
  ERROR_MESSAGES,
  GENERIC_ERROR_MESSAGE,
  ScenarioApiError,
  friendlyErrorMessage,
  isRetryableError,
  parseErrorResponse,
  reportError,
  setToastSink,
  toErrorPresentation,
  toFieldErrors,
  toScenarioApiError,
} from '@/lib/utils/errors';

import { errorEnvelope } from './fixtures';

describe('error code mapping', () => {
  it.each([
    ['SCENARIO_VALIDATION_ERROR', 'Please correct the highlighted inputs.'],
    [
      'SCENARIO_NOT_SUPPORTED',
      'This scenario is not supported by the selected model.',
    ],
    [
      'FEATURE_NOT_AVAILABLE',
      'The selected model does not include the required features.',
    ],
    ['MODEL_NOT_FOUND', 'Selected model version is unavailable.'],
    [
      'MLFLOW_UNAVAILABLE',
      'Model registry is temporarily unavailable. Try again shortly.',
    ],
    ['DATABASE_UNAVAILABLE', 'Scenario history is temporarily unavailable.'],
    ['PREDICTION_FAILED', 'Forecast generation failed. Please retry.'],
    [
      'IDEMPOTENCY_KEY_CONFLICT',
      'This scenario request was already submitted with different inputs.',
    ],
  ])('maps %s to its specified message', (code, expected) => {
    expect(friendlyErrorMessage(code)).toBe(expected);
  });

  it('covers every code in the API taxonomy', () => {
    for (const code of scenarioErrorCodeSchema.options) {
      expect(ERROR_MESSAGES[code]).toBeTruthy();
    }
  });

  it('falls back to a generic message for an unknown code', () => {
    expect(friendlyErrorMessage('WHO_KNOWS')).toBe(GENERIC_ERROR_MESSAGE);
    expect(friendlyErrorMessage(undefined)).toBe(GENERIC_ERROR_MESSAGE);
  });

  it('maps client-side codes too', () => {
    expect(friendlyErrorMessage(CLIENT_ERROR_CODES.NETWORK_ERROR)).toContain(
      'Could not reach',
    );
    expect(friendlyErrorMessage(CLIENT_ERROR_CODES.INVALID_RESPONSE)).toContain(
      'unexpected response',
    );
  });
});

describe('retryability', () => {
  it.each(['MLFLOW_UNAVAILABLE', 'DATABASE_UNAVAILABLE', 'PREDICTION_FAILED'])(
    '%s is retryable',
    (code) => {
      expect(new ScenarioApiError({ code }).isRetryable).toBe(true);
    },
  );

  it.each(['SCENARIO_VALIDATION_ERROR', 'MODEL_NOT_FOUND', 'IDEMPOTENCY_KEY_CONFLICT'])(
    '%s is not retryable',
    (code) => {
      expect(new ScenarioApiError({ code }).isRetryable).toBe(false);
    },
  );

  it('treats any unmapped 5xx as retryable', () => {
    expect(new ScenarioApiError({ code: 'ODD' }, 502).isRetryable).toBe(true);
    expect(new ScenarioApiError({ code: 'ODD' }, 400).isRetryable).toBe(false);
  });

  it('ignores non-api errors', () => {
    expect(isRetryableError(new Error('nope'))).toBe(false);
  });
});

describe('presentation', () => {
  it('marks input errors as warnings with a distinct title', () => {
    const presentation = new ScenarioApiError({
      code: 'SCENARIO_VALIDATION_ERROR',
      field: 'history',
    }).toPresentation();

    expect(presentation.severity).toBe('warning');
    expect(presentation.title).toBe('Check your scenario');
    expect(presentation.field).toBe('history');
    expect(presentation.retryable).toBe(false);
  });

  it('marks infrastructure errors as retryable errors', () => {
    const presentation = toErrorPresentation(
      new ScenarioApiError({ code: 'MLFLOW_UNAVAILABLE', request_id: 'req_9' }, 503),
    );

    expect(presentation.severity).toBe('error');
    expect(presentation.retryable).toBe(true);
    expect(presentation.requestId).toBe('req_9');
  });

  it('builds field errors for inline form display', () => {
    const errors = toFieldErrors(
      new ScenarioApiError({ code: 'SCENARIO_NOT_SUPPORTED', field: 'scenario.scenario_type' }),
    );

    expect(errors).toEqual({
      'scenario.scenario_type': 'This scenario is not supported by the selected model.',
    });
  });

  it('returns no field errors when the API blamed nothing', () => {
    expect(toFieldErrors(new ScenarioApiError({ code: 'PREDICTION_FAILED' }))).toEqual({});
  });
});

describe('normalisation', () => {
  it('passes through an existing api error', () => {
    const error = new ScenarioApiError({ code: 'MODEL_NOT_FOUND' });
    expect(toScenarioApiError(error)).toBe(error);
  });

  it('maps a TypeError to a network error', () => {
    expect(toScenarioApiError(new TypeError('fetch failed')).code).toBe(
      CLIENT_ERROR_CODES.NETWORK_ERROR,
    );
  });

  it('maps an AbortError to REQUEST_ABORTED', () => {
    expect(
      toScenarioApiError(new DOMException('stop', 'AbortError')).code,
    ).toBe(CLIENT_ERROR_CODES.REQUEST_ABORTED);
  });

  it('maps anything else to an internal error', () => {
    const error = toScenarioApiError({ weird: true });
    expect(error.code).toBe('INTERNAL_SCENARIO_ERROR');
    expect(error.message).toBe(GENERIC_ERROR_MESSAGE);
  });
});

describe('parsing error envelopes', () => {
  it('reads the documented envelope', () => {
    const error = parseErrorResponse(errorEnvelope('MODEL_NOT_FOUND'), 404);

    expect(error.code).toBe('MODEL_NOT_FOUND');
    expect(error.status).toBe(404);
    expect(error.requestId).toBe('req_server');
  });

  it('falls back to the response header request id', () => {
    const error = parseErrorResponse(
      errorEnvelope('PREDICTION_FAILED', { request_id: null }),
      500,
      'req_header',
    );

    expect(error.requestId).toBe('req_header');
  });

  it.each([
    [404, 'SCENARIO_NOT_FOUND'],
    [410, 'SCENARIO_EXPIRED'],
    [422, 'SCENARIO_VALIDATION_ERROR'],
    [503, 'MLFLOW_UNAVAILABLE'],
    [500, 'INTERNAL_SCENARIO_ERROR'],
  ])('derives a code from status %s when the body is unusable', (status, expected) => {
    expect(parseErrorResponse(null, status as number).code).toBe(expected);
  });
});

describe('toast sink', () => {
  it('emits presentations to a registered sink', () => {
    const sink = vi.fn();
    setToastSink(sink);

    reportError(new ScenarioApiError({ code: 'PREDICTION_FAILED' }));

    expect(sink).toHaveBeenCalledWith(
      expect.objectContaining({
        code: 'PREDICTION_FAILED',
        message: 'Forecast generation failed. Please retry.',
        retryable: true,
      }),
    );
    setToastSink(null);
  });

  it('logs instead of throwing when no sink is registered', () => {
    setToastSink(null);
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const presentation = reportError(new ScenarioApiError({ code: 'MODEL_NOT_FOUND' }));

    expect(presentation.code).toBe('MODEL_NOT_FOUND');
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });
});
