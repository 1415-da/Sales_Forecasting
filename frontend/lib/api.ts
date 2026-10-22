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
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from '@/lib/auth-store';
import {
  accuracyResponseSchema,
  adminUserListSchema,
  adminUserSchema,
  alertListSchema,
  alertSchema,
  artifactsResponseSchema,
  auditLogListSchema,
  authConfigSchema,
  createdUserSchema,
  driftResponseSchema,
  freshnessSchema,
  lineageSchema,
  logoutResponseSchema,
  monitoringSummarySchema,
  permissionMatrixSchema,
  registryActionSchema,
  resetPasswordSchema,
  sessionSummarySchema,
  tokenResponseSchema,
  userProfileSchema,
  type AccuracyReport,
  type AdminUser,
  type AdminUserList,
  type AlertList,
  type AuditLogList,
  type AuthConfig,
  type CreatedUser,
  type DriftReport,
  type FreshnessReport,
  type ModelArtifacts,
  type ModelLineage,
  type MonitoringAlert,
  type MonitoringSummary,
  type PermissionMatrix,
  type RegistryActionResult,
  type ResetPasswordResult,
  type SessionSummary,
  type TokenResponse,
  type UserProfile,
} from '@/lib/types/auth';
import {
  apiErrorSchema,
  forecastResponseSchema,
  healthSchema,
  modelComparisonSchema,
  modelDetailSchema,
  modelListSchema,
  type ForecastRequestBody,
  type ForecastResponse,
  type HealthReport,
  type ModelComparison,
  type ModelDetail,
  type ModelList,
} from '@/lib/types';
import {
  contractSchema,
  datasetDetailSchema,
  datasetHistorySchema,
  datasetListSchema,
  runDetailSchema,
  runListSchema,
  type DatasetHistory,
  type TrainingContract,
  type TrainingDataset,
  type TrainingDatasetDetail,
  type TrainingRun,
  type TrainingRunDetail,
} from '@/lib/types/training';

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
  method?: 'GET' | 'POST' | 'PATCH';
  body?: unknown;
  /** Multipart body (a file upload). Mutually exclusive with `body`. */
  formData?: FormData;
  /** Extra request headers, e.g. `Idempotency-Key`. */
  headers?: Record<string, string>;
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
  NOT_AUTHENTICATED: 'Please sign in to continue.',
  INVALID_CREDENTIALS: 'Incorrect email or password.',
  TOKEN_EXPIRED: 'Your session expired. Please sign in again.',
  TOKEN_INVALID: 'Your session is no longer valid. Please sign in again.',
  ACCOUNT_INACTIVE:
    'This account has been deactivated. Ask an administrator to re-enable it.',
  ACCOUNT_LOCKED:
    'Too many failed sign-in attempts. Wait a few minutes and try again.',
  PERMISSION_DENIED: 'You do not have permission to perform this action.',
  REGISTRATION_DISABLED:
    'Self-registration is disabled. Ask an administrator for an account.',
  ROLE_NOT_SELF_ASSIGNABLE:
    'That role cannot be chosen at sign-up. Pick another, or ask an administrator.',
  EMAIL_ALREADY_REGISTERED: 'That email address is already registered.',
  WEAK_PASSWORD: 'That password does not meet the requirements.',
  RATE_LIMITED: 'Too many attempts. Wait a moment and try again.',
  USER_NOT_FOUND: 'That user no longer exists.',
  ALERT_NOT_FOUND: 'That alert no longer exists.',
  MONITORING_DATA_UNAVAILABLE: 'Monitoring data is unavailable right now.',
  REGISTRY_OPERATION_FAILED:
    'The model registry rejected the operation. Check MLflow and try again.',
  INVALID_STATE_TRANSITION: 'That change is not valid from the current state.',
  PREDICTION_FAILED: 'The model could not produce a forecast for this request.',
  DATABASE_UNAVAILABLE: 'A required service is unavailable right now.',
  INTERNAL_SCENARIO_ERROR: 'Something went wrong on the server.',
  DATASET_NOT_FOUND: 'That training dataset no longer exists.',
  DATASET_INVALID: 'The dataset did not pass validation. See the report for details.',
  DATASET_NOT_READY: 'Seal the dataset version before training on it.',
  UPLOAD_TOO_LARGE: 'The file is larger than the upload limit.',
  STORAGE_UNAVAILABLE: 'Dataset storage is unavailable right now. Try again in a moment.',
  TRAINING_REQUEST_NOT_FOUND: 'That training request no longer exists.',
  ORCHESTRATOR_UNAVAILABLE: 'Airflow is not reachable right now. Try again in a moment.',
  ORCHESTRATOR_REJECTED: 'Airflow refused to start the training run.',
  DEPLOYMENT_BLOCKED: 'This candidate did not pass a deployment check.',
  IDEMPOTENCY_KEY_CONFLICT: 'That submission key was already used for a different request.',
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

/** Hooks the React layer installs so the client can react to session loss. */
interface AuthHooks {
  /** Called after every failed refresh, so the app can route to /login. */
  readonly onSessionLost?: () => void;
}

let authHooks: AuthHooks = {};

/** Install session-loss handling. Called once by the auth provider. */
export function configureAuth(hooks: AuthHooks): void {
  authHooks = hooks;
}

/**
 * Paths that must never trigger an automatic refresh.
 *
 * `/auth/refresh` would recurse; `/auth/login` returning 401 means the
 * credentials were wrong, and retrying it after a refresh would both fail
 * again and hide the real error from the user.
 */
const NO_REFRESH_PATHS = ['/auth/refresh', '/auth/login', '/auth/register'];

/**
 * A single in-flight refresh, shared by every caller.
 *
 * Without this, a page that fires five queries on mount and gets five
 * simultaneous 401s would start five refreshes. Each rotates the token and
 * revokes the previous one, so four of them would present an
 * already-rotated token -- which the server correctly treats as replay and
 * answers by revoking the whole session. The user would be logged out by the
 * act of loading the page.
 */
let refreshInFlight: Promise<boolean> | null = null;

/** Exchange the refresh cookie for a new access token. Returns success. */
export async function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${apiBaseUrl()}/auth/refresh`, {
        method: 'POST',
        headers: { Accept: 'application/json' },
        // The refresh token is an HttpOnly cookie, so it only travels if
        // credentials are included.
        credentials: 'include',
      });
      if (!response.ok) return false;
      const parsed = tokenResponseSchema.safeParse(await response.json());
      if (!parsed.success) return false;
      setAccessToken(parsed.data.access_token, parsed.data.expires_in);
      return true;
    } catch {
      return false;
    } finally {
      // Cleared in a microtask so concurrent callers awaiting this promise all
      // observe the same result before a new attempt can start.
      queueMicrotask(() => {
        refreshInFlight = null;
      });
    }
  })();

  return refreshInFlight;
}

async function request<T>(
  path: string,
  schema: { parse: (value: unknown) => T },
  options: RequestOptions = {},
): Promise<T> {
  const attempt = async (): Promise<Response> => {
    const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
    const requestId = generateRequestId();

    // Two abort sources: the caller's signal (component unmount) and the
    // timeout. Combined so whichever fires first wins.
    const controller = new AbortController();
    const timer = setTimeout(
      () => controller.abort(new DOMException('timeout', 'TimeoutError')),
      timeoutMs,
    );
    const onExternalAbort = () => controller.abort();
    options.signal?.addEventListener('abort', onExternalAbort);

    const token = getAccessToken();

    try {
      return await fetch(`${apiBaseUrl()}${path}`, {
        method,
        headers: {
          Accept: 'application/json',
          'X-Request-ID': requestId,
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
          ...(options.headers ?? {}),
        },
        // A FormData body sets its own multipart boundary header.
        ...(options.formData !== undefined
          ? { body: options.formData }
          : body !== undefined
            ? { body: JSON.stringify(body) }
            : {}),
        // Always included: the refresh cookie must reach /auth/*, and sending
        // it elsewhere is harmless since it is scoped to that path.
        credentials: 'include',
        signal: controller.signal,
      });
    } catch (cause) {
      const timedOut =
        controller.signal.aborted &&
        (controller.signal.reason as { name?: string } | undefined)?.name ===
          'TimeoutError';
      const aborted = cause instanceof Error && cause.name.includes('Abort');
      throw new ApiError(
        timedOut ? 'The request timed out.' : 'The API could not be reached.',
        {
          kind: timedOut || aborted ? 'timeout' : 'network',
          requestId,
          cause,
        },
      );
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', onExternalAbort);
    }
  };

  let response = await attempt();

  // One transparent refresh-and-retry on a 401. Beyond one attempt the session
  // is genuinely gone and retrying only delays telling the user.
  if (response.status === 401 && !NO_REFRESH_PATHS.includes(path)) {
    const refreshed = await refreshSession();
    if (refreshed) {
      response = await attempt();
    } else {
      clearAccessToken();
      authHooks.onSessionLost?.();
    }
  }

  const payload: unknown = await response.json().catch(() => undefined);

  if (!response.ok) {
    const parsed = apiErrorSchema.safeParse(payload);
    if (parsed.success) {
      const { error } = parsed.data;
      if (response.status === 401 && !NO_REFRESH_PATHS.includes(path)) {
        clearAccessToken();
        authHooks.onSessionLost?.();
      }
      throw new ApiError(error.message, {
        kind: 'http',
        status: response.status,
        code: error.code,
        field: error.field ?? null,
        details: error.details,
        requestId: error.request_id ?? response.headers.get('X-Request-ID') ?? undefined,
      });
    }
    // FastAPI's own 422 for a malformed body does not use the envelope.
    throw new ApiError(`Request failed with status ${response.status}.`, {
      kind: 'http',
      status: response.status,
      details: payload,
      requestId: response.headers.get('X-Request-ID') ?? undefined,
    });
  }

  try {
    return schema.parse(payload);
  } catch (cause) {
    throw new ApiError('The API response did not match the expected shape.', {
      kind: 'schema',
      status: response.status,
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

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

/** What the login page needs before anyone has signed in. */
export function getAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  return request('/auth/config', authConfigSchema, signal ? { signal } : {});
}

/**
 * Sign in, storing the access token for subsequent calls.
 *
 * The refresh token arrives as an HttpOnly cookie, so nothing about it is
 * handled here -- which is the point.
 */
export async function login(
  email: string,
  password: string,
): Promise<TokenResponse> {
  const result = await request('/auth/login', tokenResponseSchema, {
    method: 'POST',
    body: { email, password },
  });
  setAccessToken(result.access_token, result.expires_in);
  return result;
}

/** Register and sign in, when self-registration is enabled. */
export async function register(payload: {
  email: string;
  full_name: string;
  password: string;
  /** Requested role; the server grants it only if it is self-assignable. */
  role?: string;
}): Promise<TokenResponse> {
  const result = await request('/auth/register', tokenResponseSchema, {
    method: 'POST',
    body: payload,
  });
  setAccessToken(result.access_token, result.expires_in);
  return result;
}

/**
 * Sign out.
 *
 * The local token is cleared whatever the server says: a logout that leaves
 * the client believing it is signed in is worse than one the server did not
 * record.
 */
export async function logout(allSessions = false): Promise<void> {
  try {
    await request('/auth/logout', logoutResponseSchema, {
      method: 'POST',
      body: { all_sessions: allSessions },
    });
  } finally {
    clearAccessToken();
  }
}

/** The signed-in user and the permissions their role grants. */
export function getCurrentUser(signal?: AbortSignal): Promise<UserProfile> {
  return request('/auth/me', userProfileSchema, signal ? { signal } : {});
}

/** Change your own password. Every session is revoked, including this one. */
export async function changePassword(payload: {
  current_password: string;
  new_password: string;
}): Promise<UserProfile> {
  const result = await request('/auth/change-password', userProfileSchema, {
    method: 'POST',
    body: payload,
  });
  clearAccessToken();
  return result;
}

/** Live sessions for the signed-in user. */
export function getSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
  return request(
    '/auth/sessions',
    { parse: (value: unknown) => sessionSummarySchema.array().parse(value) },
    signal ? { signal } : {},
  );
}

// ---------------------------------------------------------------------------
// Monitoring
// ---------------------------------------------------------------------------

/** Health summary for the monitoring dashboard. */
export function getMonitoringSummary(
  params: { windowDays?: number; modelVersion?: string } = {},
  signal?: AbortSignal,
): Promise<MonitoringSummary> {
  const query = new URLSearchParams();
  if (params.windowDays) query.set('window_days', String(params.windowDays));
  if (params.modelVersion) query.set('model_version', params.modelVersion);
  return request(
    `/v1/monitoring/summary${query.size ? `?${query}` : ''}`,
    monitoringSummarySchema,
    signal ? { signal } : {},
  );
}

/** Accuracy over the recent window, plus the stored trend series. */
export function getAccuracy(
  params: {
    windowDays?: number;
    modelVersion?: string;
    storeId?: string;
    start?: string;
    end?: string;
  } = {},
  signal?: AbortSignal,
): Promise<AccuracyReport> {
  const query = new URLSearchParams();
  if (params.windowDays) query.set('window_days', String(params.windowDays));
  if (params.modelVersion) query.set('model_version', params.modelVersion);
  if (params.storeId) query.set('store_id', params.storeId);
  if (params.start) query.set('start', params.start);
  if (params.end) query.set('end', params.end);
  return request(
    `/v1/monitoring/accuracy${query.size ? `?${query}` : ''}`,
    accuracyResponseSchema,
    signal ? { signal } : {},
  );
}

/** How current the actuals and forecasts are. */
export function getDataFreshness(signal?: AbortSignal): Promise<FreshnessReport> {
  return request(
    '/v1/monitoring/data-freshness',
    freshnessSchema,
    signal ? { signal } : {},
  );
}

/** Per-feature drift. Requires detailed monitoring access. */
export function getDrift(
  modelVersion?: string,
  signal?: AbortSignal,
): Promise<DriftReport> {
  const query = modelVersion
    ? `?model_version=${encodeURIComponent(modelVersion)}`
    : '';
  return request(
    `/v1/monitoring/drift${query}`,
    driftResponseSchema,
    signal ? { signal } : {},
  );
}

/** Alerts, newest first. Defaults to those still needing attention. */
export function getAlerts(
  params: { status?: string; severity?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AlertList> {
  const query = new URLSearchParams();
  if (params.status) query.set('status', params.status);
  if (params.severity) query.set('severity', params.severity);
  if (params.limit) query.set('limit', String(params.limit));
  return request(
    `/v1/monitoring/alerts${query.size ? `?${query}` : ''}`,
    alertListSchema,
    signal ? { signal } : {},
  );
}

/** Mark an alert as seen. */
export function acknowledgeAlert(alertId: string): Promise<MonitoringAlert> {
  return request(
    `/v1/monitoring/alerts/${encodeURIComponent(alertId)}/acknowledge`,
    alertSchema,
    { method: 'POST' },
  );
}

/** Close an alert, optionally recording what was done. */
export function resolveAlert(
  alertId: string,
  note?: string,
): Promise<MonitoringAlert> {
  return request(
    `/v1/monitoring/alerts/${encodeURIComponent(alertId)}/resolve`,
    alertSchema,
    { method: 'POST', body: { note: note ?? null } },
  );
}

// ---------------------------------------------------------------------------
// Model registry
// ---------------------------------------------------------------------------

/** Compare two versions of one model. */
export function compareModels(
  params: { versionA: string; versionB: string; model?: string },
  signal?: AbortSignal,
): Promise<ModelComparison> {
  const query = new URLSearchParams({
    version_a: params.versionA,
    version_b: params.versionB,
  });
  if (params.model) query.set('model', params.model);
  return request(
    `/v1/models/compare?${query}`,
    modelComparisonSchema,
    signal ? { signal } : {},
  );
}

/** Where a model version came from. */
export function getModelLineage(
  version: string,
  model?: string,
  signal?: AbortSignal,
): Promise<ModelLineage> {
  const query = model ? `?model=${encodeURIComponent(model)}` : '';
  return request(
    `/v1/models/${encodeURIComponent(version)}/lineage${query}`,
    lineageSchema,
    signal ? { signal } : {},
  );
}

/** Artifact metadata for a version. No storage paths are exposed. */
export function getModelArtifacts(
  version: string,
  model?: string,
  signal?: AbortSignal,
): Promise<ModelArtifacts> {
  const query = model ? `?model=${encodeURIComponent(model)}` : '';
  return request(
    `/v1/models/${encodeURIComponent(version)}/artifacts${query}`,
    artifactsResponseSchema,
    signal ? { signal } : {},
  );
}

/** A registry mutation: promote, archive, or roll back. */
export function registryAction(
  action: 'promote' | 'archive' | 'rollback',
  version: string,
  payload: {
    model?: string;
    reason?: string;
    archiveExisting?: boolean;
    /** `<model>:<version>`, required when promoting a training candidate. */
    expectedIdentity?: string;
  } = {},
): Promise<RegistryActionResult> {
  return request(
    `/v1/models/${encodeURIComponent(version)}/${action}`,
    registryActionSchema,
    {
      method: 'POST',
      body: {
        // Always true from the UI: the confirmation happened in the modal, and
        // the flag exists so an empty POST cannot change what serves.
        confirm: true,
        model: payload.model ?? null,
        reason: payload.reason ?? null,
        archive_existing: payload.archiveExisting ?? true,
        ...(payload.expectedIdentity ? { expected_identity: payload.expectedIdentity } : {}),
      },
    },
  );
}

// ---------------------------------------------------------------------------
// Training workspace
// ---------------------------------------------------------------------------

/** Uploads can be large; parsing and validation run server-side. */
export const UPLOAD_TIMEOUT_MS = 120_000;

/** Accepted training schema, validation policy, and approved configurations. */
export function getTrainingContract(signal?: AbortSignal): Promise<TrainingContract> {
  return request('/v1/training/contract', contractSchema, signal ? { signal } : {});
}

export function getTrainingDatasets(signal?: AbortSignal): Promise<TrainingDataset[]> {
  return request('/v1/training/datasets', datasetListSchema, signal ? { signal } : {}).then(
    (body) => body.datasets,
  );
}

export function getTrainingDataset(
  datasetId: string,
  signal?: AbortSignal,
): Promise<TrainingDatasetDetail> {
  return request(
    `/v1/training/datasets/${encodeURIComponent(datasetId)}`,
    datasetDetailSchema,
    signal ? { signal } : {},
  );
}

export interface DatasetUpload {
  file: File;
  name: string;
  mode: 'independent' | 'append';
  currency: string;
  sourceType: string;
  baseDatasetId?: string;
  conflictPolicy?: string;
}

/** Upload a CSV/Parquet file; the response carries the validation report. */
export function uploadTrainingDataset(upload: DatasetUpload): Promise<TrainingDatasetDetail> {
  const form = new FormData();
  form.append('file', upload.file);
  form.append('name', upload.name);
  form.append('mode', upload.mode);
  form.append('currency', upload.currency);
  form.append('source_type', upload.sourceType);
  if (upload.mode === 'append' && upload.baseDatasetId) {
    form.append('base_dataset_id', upload.baseDatasetId);
    form.append('conflict_policy', upload.conflictPolicy ?? 'reject');
  }
  return request('/v1/training/datasets', datasetDetailSchema, {
    method: 'POST',
    formData: form,
    timeoutMs: UPLOAD_TIMEOUT_MS,
  });
}

/** Re-validate from storage and seal an immutable version. */
export function sealTrainingDataset(datasetId: string): Promise<TrainingDatasetDetail> {
  return request(
    `/v1/training/datasets/${encodeURIComponent(datasetId)}/validate`,
    datasetDetailSchema,
    { method: 'POST', timeoutMs: UPLOAD_TIMEOUT_MS },
  );
}

/** The validation report as a downloaded JSON file. */
export async function downloadValidationReport(datasetId: string): Promise<void> {
  const token = getAccessToken();
  const response = await fetch(
    `${apiBaseUrl()}/v1/training/datasets/${encodeURIComponent(datasetId)}/report`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      credentials: 'include',
    },
  );
  if (!response.ok) {
    throw new ApiError('The report could not be downloaded.', {
      kind: 'http',
      status: response.status,
    });
  }
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? 'validation-report.json';
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** Real recorded days of one store from a sealed dataset. */
export function getDatasetHistory(
  datasetId: string,
  storeId: string,
  days = 90,
  signal?: AbortSignal,
): Promise<DatasetHistory> {
  const query = new URLSearchParams({ store_id: storeId, days: String(days) });
  return request(
    `/v1/training/datasets/${encodeURIComponent(datasetId)}/history?${query}`,
    datasetHistorySchema,
    signal ? { signal } : {},
  );
}

/** Start training. The key makes a retry return the same request. */
export function startTrainingRun(
  payload: { datasetId: string; trainingConfigId: string },
  idempotencyKey: string,
): Promise<TrainingRunDetail> {
  return request('/v1/training/runs', runDetailSchema, {
    method: 'POST',
    body: { dataset_id: payload.datasetId, training_config_id: payload.trainingConfigId },
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export function getTrainingRuns(signal?: AbortSignal): Promise<TrainingRun[]> {
  return request('/v1/training/runs', runListSchema, signal ? { signal } : {}).then(
    (body) => body.runs,
  );
}

/** One request, refreshed from Airflow by the server. */
export function getTrainingRun(runId: string, signal?: AbortSignal): Promise<TrainingRunDetail> {
  return request(
    `/v1/training/runs/${encodeURIComponent(runId)}`,
    runDetailSchema,
    signal ? { signal } : {},
  );
}

export function rejectCandidate(
  runId: string,
  payload: { model: string; version: string; reason: string },
): Promise<TrainingRunDetail> {
  return request(
    `/v1/training/runs/${encodeURIComponent(runId)}/reject`,
    runDetailSchema,
    { method: 'POST', body: payload },
  );
}

// ---------------------------------------------------------------------------
// Administration
// ---------------------------------------------------------------------------

/** A page of user accounts. */
export function getUsers(
  params: {
    role?: string;
    isActive?: boolean;
    search?: string;
    limit?: number;
    offset?: number;
  } = {},
  signal?: AbortSignal,
): Promise<AdminUserList> {
  const query = new URLSearchParams();
  if (params.role) query.set('role', params.role);
  if (params.isActive !== undefined) query.set('is_active', String(params.isActive));
  if (params.search) query.set('search', params.search);
  if (params.limit) query.set('limit', String(params.limit));
  if (params.offset) query.set('offset', String(params.offset));
  return request(
    `/v1/admin/users${query.size ? `?${query}` : ''}`,
    adminUserListSchema,
    signal ? { signal } : {},
  );
}

/** Create an account. Omit the password to have one generated. */
export function createUser(payload: {
  email: string;
  full_name: string;
  role: string;
  password?: string;
  is_active?: boolean;
}): Promise<CreatedUser> {
  return request('/v1/admin/users', createdUserSchema, {
    method: 'POST',
    body: {
      email: payload.email,
      full_name: payload.full_name,
      role: payload.role,
      password: payload.password ?? null,
      is_active: payload.is_active ?? true,
    },
  });
}

/**
 * Edit an account. Only the supplied fields are applied.
 *
 * PATCH rather than POST: a partial update must not blank out the fields it
 * did not mention, and the verb is what says so.
 */
export function updateUser(
  userId: string,
  payload: { full_name?: string; role?: string; is_active?: boolean },
): Promise<AdminUser> {
  return request(
    `/v1/admin/users/${encodeURIComponent(userId)}`,
    adminUserSchema,
    { method: 'PATCH', body: payload },
  );
}

/** Deactivate or reactivate an account. */
export function setUserActive(
  userId: string,
  active: boolean,
): Promise<AdminUser> {
  return request(
    `/v1/admin/users/${encodeURIComponent(userId)}/${active ? 'activate' : 'deactivate'}`,
    adminUserSchema,
    { method: 'POST' },
  );
}

/** Issue a new temporary password. Returned once; all sessions are revoked. */
export function resetUserPassword(userId: string): Promise<ResetPasswordResult> {
  return request(
    `/v1/admin/users/${encodeURIComponent(userId)}/reset-password`,
    resetPasswordSchema,
    { method: 'POST' },
  );
}

/** The role and permission matrix the API enforces. */
export function getPermissionMatrix(
  signal?: AbortSignal,
): Promise<PermissionMatrix> {
  return request(
    '/v1/admin/permissions',
    permissionMatrixSchema,
    signal ? { signal } : {},
  );
}

/** A page of audit records. */
export function getAuditLogs(
  params: {
    actions?: string[];
    userId?: string;
    outcome?: string;
    resourceType?: string;
    limit?: number;
    offset?: number;
  } = {},
  signal?: AbortSignal,
): Promise<AuditLogList> {
  const query = new URLSearchParams();
  for (const action of params.actions ?? []) query.append('action', action);
  if (params.userId) query.set('user_id', params.userId);
  if (params.outcome) query.set('outcome', params.outcome);
  if (params.resourceType) query.set('resource_type', params.resourceType);
  if (params.limit) query.set('limit', String(params.limit));
  if (params.offset) query.set('offset', String(params.offset));
  return request(
    `/v1/admin/audit-logs${query.size ? `?${query}` : ''}`,
    auditLogListSchema,
    signal ? { signal } : {},
  );
}
