/**
 * Typed client for the forecast API.
 *
 * The only way the browser reaches a model. Every response is parsed with Zod
 * before it is returned, and every failure is normalised into `ApiError` so the
 * UI has one error shape to render rather than three (network, HTTP, schema).
 *
 * Requests carry an `X-Request-ID`, which the API echoes and includes in error
 * bodies. The console surfaces it so a user can quote one identifier that is
 * greppable in the server log.
 */
import {
  apiErrorSchema,
  forecastResponseSchema,
  healthSchema,
  modelDetailSchema,
  modelListSchema,
  type ForecastRequestBody,
  type ForecastResponse,
  type HealthReport,
  type ModelDetail,
  type ModelList,
} from '@/lib/types';

/**
 * Generous because the call runs model inference across up to 90 recursive
 * steps, but bounded: without a timeout a stalled request leaves the console
 * spinning with no way out.
 */
export const FORECAST_TIMEOUT_MS = 60_000;

/** Short, because these are metadata reads. */
export const DEFAULT_TIMEOUT_MS = 15_000;

/** Classifies a failure so the UI can choose between retry and guidance. */
export type ApiErrorKind = 'network' | 'timeout' | 'http' | 'schema';

/** A failure from any layer of a request, in one shape. */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly code?: string;
  readonly field?: string | null;
  readonly details?: unknown;
  readonly requestId?: string;

  constructor(
    message: string,
    options: {
      kind: ApiErrorKind;
      status?: number;
      code?: string;
      field?: string | null;
      details?: unknown;
      requestId?: string;
      cause?: unknown;
    },
  ) {
    super(message);
    this.name = 'ApiError';
    this.kind = options.kind;
    this.status = options.status;
    this.code = options.code;
    this.field = options.field;
    this.details = options.details;
    this.requestId = options.requestId;
    if (options.cause !== undefined) this.cause = options.cause;
  }

  /**
   * Whether retrying unaided could plausibly succeed.
   *
   * Only transport failures and an unavailable upstream. A 404 or a 422 will
   * not fix itself, and retrying it just delays the error the user needs.
   */
  get isRetryable(): boolean {
    if (this.kind === 'network' || this.kind === 'timeout') return true;
    return this.status === 502 || this.status === 503 || this.status === 504;
  }
}

/**
 * Resolve the API origin.
 *
 * Empty means same-origin, which is what a reverse-proxy deployment wants.
 */
export function apiBaseUrl(): string {
  const fromEnv =
    typeof process !== 'undefined'
      ? process.env.NEXT_PUBLIC_API_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL
      : undefined;
  return (fromEnv ?? '').replace(/\/+$/, '');
}

/** Generate a request id, matching the API's `req_` convention. */
export function generateRequestId(): string {
  const cryptoApi = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (cryptoApi?.randomUUID) return `req_${cryptoApi.randomUUID().replace(/-/g, '')}`;
  return `req_${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`;
}

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  timeoutMs?: number;
  signal?: AbortSignal;
}

/**
 * Human-readable text for an error code.
 *
 * Deliberately a closed set. Echoing an unrecognised server message into the
 * UI risks surfacing internal wording, so an unknown code gets a neutral
 * sentence and the raw text stays on `ApiError.message` for the detail view.
 */
export const ERROR_MESSAGES: Record<string, string> = {
  MODEL_NOT_FOUND:
    'That model version does not exist. Pick another version, or train a model first.',
  MODEL_NOT_IN_PRODUCTION:
    'That version is not promoted to Production, so it cannot serve forecasts.',
  MLFLOW_UNAVAILABLE:
    'The model registry is unavailable right now. Try again in a moment.',
  ARTIFACT_DOWNLOAD_FAILED:
    'The model files could not be retrieved from storage. Try again in a moment.',
  INSUFFICIENT_HISTORY:
    'Not enough history to forecast. Add more days and try again.',
  FEATURE_MISMATCH:
    'This model needs inputs that cannot be built from a sales history.',
  SCENARIO_VALIDATION_ERROR: 'Check the highlighted field and try again.',
  PREDICTION_FAILED: 'The model could not produce a forecast for this request.',
  DATABASE_UNAVAILABLE: 'A required service is unavailable right now.',
  INTERNAL_SCENARIO_ERROR: 'Something went wrong on the server.',
};

/** The sentence to show a user for a given failure. */
export function friendlyMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return 'Something went wrong. Try again.';
  }
  if (error.kind === 'timeout') {
    return 'The forecast took longer than 60 seconds and was cancelled. Try a shorter horizon.';
  }
  if (error.kind === 'network') {
    return 'Could not reach the forecast API. Check that it is running and try again.';
  }
  if (error.kind === 'schema') {
    return 'The API returned a response this app does not understand.';
  }
  if (error.code && ERROR_MESSAGES[error.code]) {
    return ERROR_MESSAGES[error.code] as string;
  }
  return 'The request could not be completed.';
}

async function request<T>(
  path: string,
  schema: { parse: (value: unknown) => T },
  options: RequestOptions = {},
): Promise<T> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const requestId = generateRequestId();

  // Two abort sources: the caller's signal (component unmount) and the
  // timeout. They are combined so whichever fires first wins.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs);
  const onExternalAbort = () => controller.abort();
  options.signal?.addEventListener('abort', onExternalAbort);

  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        'X-Request-ID': requestId,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      signal: controller.signal,
    });
  } catch (cause) {
    const aborted = cause instanceof Error && cause.name.includes('Abort');
    const timedOut =
      controller.signal.aborted &&
      (controller.signal.reason as { name?: string } | undefined)?.name === 'TimeoutError';
    throw new ApiError(
      timedOut ? 'The request timed out.' : 'The API could not be reached.',
      {
        kind: timedOut ? 'timeout' : aborted ? 'timeout' : 'network',
        requestId,
        cause,
      },
    );
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', onExternalAbort);
  }

  const payload: unknown = await response.json().catch(() => undefined);

  if (!response.ok) {
    const parsed = apiErrorSchema.safeParse(payload);
    if (parsed.success) {
      const { error } = parsed.data;
      throw new ApiError(error.message, {
        kind: 'http',
        status: response.status,
        code: error.code,
        field: error.field ?? null,
        details: error.details,
        requestId: error.request_id ?? response.headers.get('X-Request-ID') ?? requestId,
      });
    }
    // FastAPI's own 422 for a malformed body does not use the envelope.
    throw new ApiError(`Request failed with status ${response.status}.`, {
      kind: 'http',
      status: response.status,
      details: payload,
      requestId: response.headers.get('X-Request-ID') ?? requestId,
    });
  }

  try {
    return schema.parse(payload);
  } catch (cause) {
    throw new ApiError('The API response did not match the expected shape.', {
      kind: 'schema',
      status: response.status,
      requestId,
      cause,
    });
  }
}

/** API health, dependency state, and the current Production model. */
export function getHealth(signal?: AbortSignal): Promise<HealthReport> {
  return request('/health', healthSchema, signal ? { signal } : {});
}

/** The model version serving each configured model. */
export function getModels(signal?: AbortSignal): Promise<ModelList> {
  return request('/v1/models', modelListSchema, signal ? { signal } : {});
}

/** Detail for one model version. */
export function getModel(
  version: string,
  model?: string,
  signal?: AbortSignal,
): Promise<ModelDetail> {
  const query = model ? `?model=${encodeURIComponent(model)}` : '';
  return request(
    `/v1/models/${encodeURIComponent(version)}${query}`,
    modelDetailSchema,
    signal ? { signal } : {},
  );
}

/** Generate a forecast. */
export function createForecast(
  body: ForecastRequestBody,
  signal?: AbortSignal,
): Promise<ForecastResponse> {
  return request('/v1/forecast', forecastResponseSchema, {
    method: 'POST',
    body,
    timeoutMs: FORECAST_TIMEOUT_MS,
    ...(signal ? { signal } : {}),
  });
}
