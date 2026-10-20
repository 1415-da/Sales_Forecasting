/**
 * Error handling for the Scenario Planner data layer.
 *
 * One error class, one message map, and one presentation descriptor. API
 * failures, response-shape failures, and network failures all surface as a
 * `ScenarioApiError`, so callers never branch on `instanceof TypeError`.
 *
 * The backend already redacts paths, connection strings, and stack traces, and
 * replaces unexpected-error messages with a generic one. This module adds the
 * second half: a user-facing message chosen by error *code*, never by splicing
 * the raw server string into the UI.
 */

import { z } from 'zod';

import {
  type ScenarioApiErrorPayload,
  type ScenarioErrorCode,
  scenarioErrorEnvelopeSchema,
} from '@/lib/types/scenario';

/** Friendly messages, keyed by API error code. */
export const ERROR_MESSAGES: Record<ScenarioErrorCode, string> = {
  SCENARIO_VALIDATION_ERROR: 'Please correct the highlighted inputs.',
  SCENARIO_NOT_SUPPORTED: 'This scenario is not supported by the selected model.',
  FEATURE_NOT_AVAILABLE: 'The selected model does not include the required features.',
  MODEL_NOT_FOUND: 'Selected model version is unavailable.',
  MLFLOW_UNAVAILABLE: 'Model registry is temporarily unavailable. Try again shortly.',
  DATABASE_UNAVAILABLE: 'Scenario history is temporarily unavailable.',
  PREDICTION_FAILED: 'Forecast generation failed. Please retry.',
  IDEMPOTENCY_KEY_CONFLICT:
    'This scenario request was already submitted with different inputs.',
  // Remaining codes from the API taxonomy. Without these a real failure would
  // fall through to the generic message and tell the user nothing actionable.
  FEATURE_SCHEMA_MISSING:
    'The selected model is missing its feature definitions, so scenarios cannot run against it.',
  MODEL_NOT_IN_PRODUCTION:
    'The selected model version is not promoted to production yet.',
  ARTIFACT_DOWNLOAD_FAILED:
    'A required model file could not be retrieved. Try again shortly.',
  FORECAST_ALIGNMENT_ERROR:
    'The baseline and scenario forecasts did not line up. Please retry.',
  BASELINE_NOT_FOUND: 'That baseline forecast no longer exists.',
  BASELINE_EXPIRED: 'That baseline forecast has expired. Generate a new one.',
  SCENARIO_ALREADY_RUNNING:
    'A scenario for this store is already running. Wait for it to finish.',
  SCENARIO_NOT_FOUND: 'That scenario could not be found.',
  SCENARIO_EXPIRED: 'That scenario has expired and is no longer available.',
  INTERNAL_SCENARIO_ERROR: 'Something went wrong. Please try again.',
};

export const GENERIC_ERROR_MESSAGE = 'Something went wrong. Please try again.';
export const NETWORK_ERROR_MESSAGE =
  'Could not reach the forecasting service. Check your connection and try again.';
export const INVALID_RESPONSE_MESSAGE =
  'The forecasting service returned an unexpected response. Please try again.';
export const TIMEOUT_MESSAGE =
  'Scenario generation is taking longer than expected. You can retry or check the model registry.';

/** Client-side codes for failures that never reached the API. */
export const CLIENT_ERROR_CODES = {
  NETWORK_ERROR: 'NETWORK_ERROR',
  INVALID_RESPONSE: 'INVALID_RESPONSE',
  REQUEST_ABORTED: 'REQUEST_ABORTED',
  /** Distinct from REQUEST_ABORTED: the client gave up, the user did not. */
  TIMEOUT: 'TIMEOUT',
} as const;

export type ClientErrorCode = (typeof CLIENT_ERROR_CODES)[keyof typeof CLIENT_ERROR_CODES];

/** Codes worth offering a retry for: transient or infrastructure-side. */
const RETRYABLE_CODES = new Set<string>([
  'MLFLOW_UNAVAILABLE',
  'DATABASE_UNAVAILABLE',
  'ARTIFACT_DOWNLOAD_FAILED',
  'PREDICTION_FAILED',
  'FORECAST_ALIGNMENT_ERROR',
  'INTERNAL_SCENARIO_ERROR',
  CLIENT_ERROR_CODES.NETWORK_ERROR,
  CLIENT_ERROR_CODES.TIMEOUT,
]);

/**
 * Failures safe to retry *automatically*, without the user asking.
 *
 * Narrower than `isRetryableError`, which governs whether to offer a Retry
 * button. A 500 may be deterministic -- retrying it silently just burns another
 * inference and delays the error -- so only transport-level and
 * upstream-unavailable failures qualify.
 */
const AUTO_RETRYABLE_CODES = new Set<string>([
  CLIENT_ERROR_CODES.NETWORK_ERROR,
  CLIENT_ERROR_CODES.TIMEOUT,
]);

const AUTO_RETRYABLE_STATUSES = new Set<number>([502, 503]);

/** Codes the user can fix by changing their inputs. */
const INPUT_CODES = new Set<string>([
  'SCENARIO_VALIDATION_ERROR',
  'SCENARIO_NOT_SUPPORTED',
  'FEATURE_NOT_AVAILABLE',
  'IDEMPOTENCY_KEY_CONFLICT',
]);

export type ErrorSeverity = 'warning' | 'error';

/** A toast-ready or inline-ready description of a failure. */
export interface ErrorPresentation {
  title: string;
  message: string;
  code: string;
  severity: ErrorSeverity;
  retryable: boolean;
  /** Field path the API blamed, e.g. `scenario.scenario_type`. */
  field: string | null;
  /** Correlation id to show in a support footer. */
  requestId: string | null;
  /** Extra context, already redacted server-side. Safe to log, not to show raw. */
  details: Record<string, unknown>;
}

/**
 * A failure from the Scenario Planner API, or from trying to reach it.
 *
 * Carries the API's structured error payload when there was one, and a
 * synthesised payload when the failure happened before a response could be
 * parsed.
 */
export class ScenarioApiError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly field: string | null;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  readonly timestamp: string | null;

  /** Raw server-authored text. For logs and diagnostics only, never the UI. */
  readonly serverMessage: string | null;

  constructor(
    payload: Partial<ScenarioApiErrorPayload> & { code: string },
    status: number | null = null,
    options?: { cause?: unknown },
  ) {
    // `message` is always the mapped, user-safe string, so logs and UI tell the
    // same story and server-authored text can never reach a user by accident.
    super(friendlyErrorMessage(payload.code));
    this.name = 'ScenarioApiError';
    this.serverMessage = payload.message?.trim() ? payload.message : null;
    this.code = payload.code;
    this.status = status;
    this.field = payload.field ?? null;
    this.details = payload.details ?? {};
    this.requestId = payload.request_id ?? null;
    this.timestamp = payload.timestamp ?? null;
    if (options?.cause !== undefined) {
      this.cause = options.cause;
    }
  }

  /** Whether offering a retry button makes sense. */
  get isRetryable(): boolean {
    return isRetryableError(this);
  }

  /** Whether the user can fix this by editing their inputs. */
  get isInputError(): boolean {
    return INPUT_CODES.has(this.code);
  }

  toPresentation(): ErrorPresentation {
    return toErrorPresentation(this);
  }

  /** A network or DNS failure: no response was ever received. */
  static network(cause?: unknown): ScenarioApiError {
    return new ScenarioApiError(
      {
        code: CLIENT_ERROR_CODES.NETWORK_ERROR,
        message: NETWORK_ERROR_MESSAGE,
        details: {},
      },
      null,
      { cause },
    );
  }

  /** The response arrived but did not match the expected schema. */
  static invalidResponse(
    issues: z.ZodIssue[],
    status: number | null = null,
    requestId: string | null = null,
  ): ScenarioApiError {
    return new ScenarioApiError(
      {
        code: CLIENT_ERROR_CODES.INVALID_RESPONSE,
        message: INVALID_RESPONSE_MESSAGE,
        request_id: requestId,
        // Issue paths are developer diagnostics, never rendered to a user.
        details: {
          issues: issues.map((issue) => ({
            path: issue.path.join('.'),
            message: issue.message,
          })),
        },
      },
      status,
    );
  }

  /** The client's own deadline elapsed before the server answered. */
  static timeout(timeoutMs: number): ScenarioApiError {
    return new ScenarioApiError(
      {
        code: CLIENT_ERROR_CODES.TIMEOUT,
        message: TIMEOUT_MESSAGE,
        details: { timeout_ms: timeoutMs },
      },
      null,
    );
  }

  /** The caller aborted the request. */
  static aborted(): ScenarioApiError {
    return new ScenarioApiError(
      { code: CLIENT_ERROR_CODES.REQUEST_ABORTED, message: 'Request cancelled.' },
      null,
    );
  }
}

/**
 * Map an error code to a user-facing message.
 *
 * Strict by design: an unrecognised code yields the generic message rather than
 * echoing the server's text. A message written for an API consumer is not
 * necessarily fit for an end user, and a future backend code must not be able
 * to put unreviewed text on screen. The raw text stays on
 * `ScenarioApiError.serverMessage` for logs.
 */
export function friendlyErrorMessage(code: string | undefined): string {
  if (code && code in ERROR_MESSAGES) {
    return ERROR_MESSAGES[code as ScenarioErrorCode];
  }
  if (code === CLIENT_ERROR_CODES.NETWORK_ERROR) return NETWORK_ERROR_MESSAGE;
  if (code === CLIENT_ERROR_CODES.INVALID_RESPONSE) return INVALID_RESPONSE_MESSAGE;
  if (code === CLIENT_ERROR_CODES.REQUEST_ABORTED) return 'Request cancelled.';
  if (code === CLIENT_ERROR_CODES.TIMEOUT) return TIMEOUT_MESSAGE;
  return GENERIC_ERROR_MESSAGE;
}

/**
 * Whether a failure may be retried automatically.
 *
 * 422, 404, and 409 are explicitly excluded: a validation problem, a missing
 * model, and an unsupported scenario are all deterministic.
 */
export function isAutoRetryableError(error: unknown): boolean {
  if (!(error instanceof ScenarioApiError)) return false;
  if (error.code === CLIENT_ERROR_CODES.REQUEST_ABORTED) return false;
  if (AUTO_RETRYABLE_CODES.has(error.code)) return true;
  return error.status !== null && AUTO_RETRYABLE_STATUSES.has(error.status);
}

/** Whether a failure is worth offering a manual retry for. */
export function isRetryableError(error: unknown): boolean {
  if (error instanceof ScenarioApiError) {
    if (error.code === CLIENT_ERROR_CODES.REQUEST_ABORTED) return false;
    if (RETRYABLE_CODES.has(error.code)) return true;
    return error.status !== null && error.status >= 500;
  }
  return false;
}

/** Build the descriptor a toast or inline banner renders. */
export function toErrorPresentation(error: unknown): ErrorPresentation {
  const normalised = toScenarioApiError(error);
  const isInput = normalised.isInputError;

  return {
    title: isInput
      ? 'Check your scenario'
      : normalised.code === CLIENT_ERROR_CODES.TIMEOUT
        ? 'Still working on it'
        : 'Scenario could not be completed',
    message: friendlyErrorMessage(normalised.code),
    code: normalised.code,
    severity: isInput ? 'warning' : 'error',
    retryable: isRetryableError(normalised),
    field: normalised.field,
    requestId: normalised.requestId,
    details: normalised.details,
  };
}

/** Coerce anything thrown into a `ScenarioApiError`. */
export function toScenarioApiError(error: unknown): ScenarioApiError {
  if (error instanceof ScenarioApiError) return error;

  if (error instanceof DOMException && error.name === 'AbortError') {
    return ScenarioApiError.aborted();
  }
  if (error instanceof TypeError) {
    // `fetch` rejects with a TypeError for network-level failures.
    return ScenarioApiError.network(error);
  }
  return new ScenarioApiError(
    {
      code: 'INTERNAL_SCENARIO_ERROR',
      message: error instanceof Error ? error.message : '',
    },
    null,
    { cause: error },
  );
}

/**
 * Parse an error response body into a `ScenarioApiError`.
 *
 * A body that is not the documented envelope still yields a usable error,
 * derived from the HTTP status, rather than throwing while handling an error.
 */
export function parseErrorResponse(
  body: unknown,
  status: number,
  requestId: string | null = null,
): ScenarioApiError {
  const parsed = scenarioErrorEnvelopeSchema.safeParse(body);
  if (parsed.success) {
    const payload = parsed.data.error;
    return new ScenarioApiError(
      { ...payload, request_id: payload.request_id ?? requestId },
      status,
    );
  }

  return new ScenarioApiError(
    {
      code: codeForStatus(status),
      message: '',
      request_id: requestId,
      details: {},
    },
    status,
  );
}

/** Best-effort code for a response that did not carry the documented envelope. */
function codeForStatus(status: number): string {
  if (status === 404) return 'SCENARIO_NOT_FOUND';
  if (status === 409) return 'SCENARIO_NOT_SUPPORTED';
  if (status === 410) return 'SCENARIO_EXPIRED';
  if (status === 422) return 'SCENARIO_VALIDATION_ERROR';
  if (status === 503) return 'MLFLOW_UNAVAILABLE';
  return 'INTERNAL_SCENARIO_ERROR';
}

/**
 * Field-keyed messages for inline form errors.
 *
 * The API blames one field per error, so this yields at most one entry; it
 * exists so form code has a stable shape to merge into.
 */
export function toFieldErrors(error: unknown): Record<string, string> {
  const normalised = toScenarioApiError(error);
  if (!normalised.field) return {};
  return { [normalised.field]: friendlyErrorMessage(normalised.code) };
}

/** A sink the app wires to its own toast system. */
export type ToastSink = (presentation: ErrorPresentation) => void;

let toastSink: ToastSink | null = null;

/**
 * Register the app's toast implementation.
 *
 * The data layer must not depend on a specific UI library, so it emits
 * `ErrorPresentation` descriptors and the app decides how to show them. Until a
 * sink is registered, errors are logged with their request id and nothing else.
 */
export function setToastSink(sink: ToastSink | null): void {
  toastSink = sink;
}

/** Emit a failure to the registered toast sink. */
export function reportError(error: unknown): ErrorPresentation {
  const presentation = toErrorPresentation(error);
  if (toastSink) {
    toastSink(presentation);
  } else if (typeof console !== 'undefined') {
    console.error(
      `[scenario] ${presentation.code}: ${presentation.message}`,
      presentation.requestId ? `(request ${presentation.requestId})` : '',
    );
  }
  return presentation;
}
