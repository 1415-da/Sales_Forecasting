/**
 * Typed client for the Scenario Planner API.
 *
 * Every response is validated with Zod before it reaches a caller, so a backend
 * contract change surfaces as one `INVALID_RESPONSE` error at the boundary
 * rather than as `undefined` deep inside a chart component.
 *
 * Headers:
 *  - `X-Request-ID` is sent on every call and echoed by the API, so a UI error
 *    and a server log line can be correlated.
 *  - `Idempotency-Key` is sent only for `createScenarioComparison`, the one
 *    non-idempotent operation; reusing a key replays the original result, and
 *    reusing it with different inputs returns 409.
 */

import type { z } from 'zod';

import {
  type ScenarioAuditListResponse,
  type ScenarioCapabilitiesResponse,
  type ScenarioComparisonResponse,
  type ScenarioRequest,
  type ScenarioRequestInput,
  type ScenarioRollbackResponse,
  SCENARIO_LIMITS,
  scenarioAuditListResponseSchema,
  scenarioCapabilitiesResponseSchema,
  scenarioComparisonResponseSchema,
  scenarioRequestSchema,
  scenarioRollbackResponseSchema,
} from '@/lib/types/scenario';
import { ScenarioApiError, parseErrorResponse } from '@/lib/utils/errors';

export const REQUEST_ID_HEADER = 'X-Request-ID';
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key';

/**
 * Deadline for a scenario comparison.
 *
 * Generous because the call runs model inference, but bounded: without it a
 * stalled request leaves the UI spinning with no way out.
 */
export const COMPARISON_TIMEOUT_MS = 60_000;

const SCENARIO_BASE_PATH = '/v1/scenario';

/** Resolve the API origin from the environment, defaulting to a same-origin proxy. */
function defaultBaseUrl(): string {
  const fromEnv =
    typeof process !== 'undefined'
      ? process.env?.NEXT_PUBLIC_API_BASE_URL ?? process.env?.API_BASE_URL
      : undefined;
  return (fromEnv ?? '').replace(/\/+$/, '');
}

/** Generate a request id, matching the API's `req_` convention. */
export function generateRequestId(): string {
  const globalCrypto = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (globalCrypto?.randomUUID) {
    return `req_${globalCrypto.randomUUID().replace(/-/g, '')}`;
  }
  const random = Math.random().toString(16).slice(2);
  return `req_${Date.now().toString(16)}${random}`;
}

/** Generate an idempotency key for a scenario submission. */
export function generateIdempotencyKey(): string {
  const globalCrypto = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (globalCrypto?.randomUUID) return globalCrypto.randomUUID();
  return `idem-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

export interface ScenarioApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  /** Called with the request id used for each call; useful for logging. */
  onRequestId?: (requestId: string) => void;
}

export interface RequestOptions {
  /** Reuse an upstream request id instead of generating one. */
  requestId?: string;
  signal?: AbortSignal;
  /** Client-side deadline in milliseconds. Omit or pass 0 to disable. */
  timeoutMs?: number;
}

export interface CreateComparisonOptions extends RequestOptions {
  idempotencyKey?: string;
}

interface InternalRequest extends RequestOptions {
  method: 'GET' | 'POST';
  path: string;
  body?: unknown;
  idempotencyKey?: string;
}

/**
 * Combine a caller's signal with a timeout.
 *
 * `AbortSignal.any` would be tidier but is not available everywhere this may
 * run, so the controller is wired up by hand. The returned `timedOut` flag is
 * what lets an abort be reported as a timeout rather than a cancellation.
 */
function withTimeout(
  signal: AbortSignal | undefined,
  timeoutMs: number | undefined,
): { signal: AbortSignal | undefined; cleanup: () => void; timedOut: () => boolean } {
  if (!timeoutMs || timeoutMs <= 0) {
    return { signal, cleanup: () => {}, timedOut: () => false };
  }

  const controller = new AbortController();
  let didTimeOut = false;

  const timer = setTimeout(() => {
    didTimeOut = true;
    controller.abort();
  }, timeoutMs);

  const onCallerAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onCallerAbort, { once: true });
  }

  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onCallerAbort);
    },
    timedOut: () => didTimeOut,
  };
}

/**
 * HTTP client for the Scenario Planner.
 *
 * `fetchImpl` is injectable so tests exercise the real header, parsing, and
 * error paths without a network or a mock framework.
 */
export class ScenarioApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly onRequestId?: (requestId: string) => void;

  constructor(options: ScenarioApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? defaultBaseUrl()).replace(/\/+$/, '');
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch;
    this.onRequestId = options.onRequestId;
  }

  /** Scenario types the given model version can support. */
  async getScenarioCapabilities(
    modelVersion: string = SCENARIO_LIMITS.defaultModelVersion,
    options: RequestOptions = {},
  ): Promise<ScenarioCapabilitiesResponse> {
    const query = new URLSearchParams({ model_version: modelVersion });
    return this.request(
      { method: 'GET', path: `${SCENARIO_BASE_PATH}/capabilities?${query}`, ...options },
      scenarioCapabilitiesResponseSchema,
    );
  }

  /**
   * Run a what-if comparison.
   *
   * The request is validated locally first, so an obviously invalid payload
   * fails immediately with field-level messages instead of a round trip.
   */
  async createScenarioComparison(
    request: ScenarioRequestInput,
    options: CreateComparisonOptions = {},
  ): Promise<ScenarioComparisonResponse> {
    const validated = this.validateRequest(request);
    return this.request(
      {
        method: 'POST',
        path: `${SCENARIO_BASE_PATH}/compare`,
        body: validated,
        idempotencyKey: options.idempotencyKey,
        requestId: options.requestId,
        signal: options.signal,
        timeoutMs: options.timeoutMs ?? COMPARISON_TIMEOUT_MS,
      },
      scenarioComparisonResponseSchema,
    );
  }

  /** A previously computed comparison. */
  async getScenarioById(
    scenarioId: string,
    options: RequestOptions = {},
  ): Promise<ScenarioComparisonResponse> {
    return this.request(
      {
        method: 'GET',
        path: `${SCENARIO_BASE_PATH}/${encodeURIComponent(scenarioId)}`,
        ...options,
      },
      scenarioComparisonResponseSchema,
    );
  }

  /** Withdraw a scenario, preserving its audit history. */
  async rollbackScenario(
    scenarioId: string,
    reason: string,
    options: RequestOptions = {},
  ): Promise<ScenarioRollbackResponse> {
    return this.request(
      {
        method: 'POST',
        path: `${SCENARIO_BASE_PATH}/${encodeURIComponent(scenarioId)}/rollback`,
        body: { reason },
        ...options,
      },
      scenarioRollbackResponseSchema,
    );
  }

  /** Audit trail for a scenario. */
  async getScenarioAudit(
    scenarioId: string,
    options: RequestOptions = {},
  ): Promise<ScenarioAuditListResponse> {
    return this.request(
      {
        method: 'GET',
        path: `${SCENARIO_BASE_PATH}/${encodeURIComponent(scenarioId)}/audit`,
        ...options,
      },
      scenarioAuditListResponseSchema,
    );
  }

  /**
   * Validate a request payload client-side.
   *
   * Exposed so a form can check inputs before enabling submit.
   */
  validateRequest(request: ScenarioRequestInput): ScenarioRequest {
    const parsed = scenarioRequestSchema.safeParse(request);
    if (parsed.success) return parsed.data;

    const first = parsed.error.issues[0];
    throw new ScenarioApiError(
      {
        code: 'SCENARIO_VALIDATION_ERROR',
        message: first?.message ?? '',
        field: first ? first.path.join('.') : null,
        details: {
          issues: parsed.error.issues.map((issue) => ({
            path: issue.path.join('.'),
            message: issue.message,
          })),
        },
      },
      null,
    );
  }

  private async request<TSchema extends z.ZodTypeAny>(
    config: InternalRequest,
    schema: TSchema,
  ): Promise<z.infer<TSchema>> {
    const requestId = config.requestId ?? generateRequestId();
    this.onRequestId?.(requestId);

    const headers: Record<string, string> = {
      Accept: 'application/json',
      [REQUEST_ID_HEADER]: requestId,
    };
    if (config.body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    if (config.idempotencyKey) {
      headers[IDEMPOTENCY_KEY_HEADER] = config.idempotencyKey;
    }

    const deadline = withTimeout(config.signal, config.timeoutMs);
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${config.path}`, {
        method: config.method,
        headers,
        body: config.body === undefined ? undefined : JSON.stringify(config.body),
        signal: deadline.signal,
      });
    } catch (cause) {
      if (deadline.timedOut()) {
        throw ScenarioApiError.timeout(config.timeoutMs ?? 0);
      }
      if (cause instanceof DOMException && cause.name === 'AbortError') {
        throw ScenarioApiError.aborted();
      }
      throw ScenarioApiError.network(cause);
    } finally {
      deadline.cleanup();
    }

    // Prefer the server's id: it is what appears in the server's own logs.
    const responseRequestId = response.headers?.get?.(REQUEST_ID_HEADER) ?? requestId;
    const body = await this.readJson(response);

    if (!response.ok) {
      throw parseErrorResponse(body, response.status, responseRequestId);
    }

    const parsed = schema.safeParse(body);
    if (!parsed.success) {
      throw ScenarioApiError.invalidResponse(
        parsed.error.issues,
        response.status,
        responseRequestId,
      );
    }
    return parsed.data;
  }

  /** Read a JSON body, tolerating an empty or non-JSON one. */
  private async readJson(response: Response): Promise<unknown> {
    try {
      const text = await response.text();
      return text ? JSON.parse(text) : null;
    } catch {
      // A malformed body on a 2xx becomes INVALID_RESPONSE; on an error status
      // `parseErrorResponse` falls back to the HTTP status.
      return null;
    }
  }
}

/** Shared client instance for app code. */
export const scenarioApi = new ScenarioApiClient();

/** Build a client with explicit options (tests, server components, proxies). */
export function createScenarioApiClient(
  options: ScenarioApiClientOptions = {},
): ScenarioApiClient {
  return new ScenarioApiClient(options);
}

/* -------------------------------------------------------------------------- */
/* Query keys                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * TanStack Query keys.
 *
 * Hierarchical so `invalidateQueries({ queryKey: scenarioKeys.all })` clears
 * everything scenario-related after a rollback.
 */
export const scenarioKeys = {
  all: ['scenario'] as const,
  capabilities: (modelVersion: string) =>
    [...scenarioKeys.all, 'capabilities', modelVersion] as const,
  detail: (scenarioId: string) => [...scenarioKeys.all, 'detail', scenarioId] as const,
  audit: (scenarioId: string) => [...scenarioKeys.all, 'audit', scenarioId] as const,
} as const;
