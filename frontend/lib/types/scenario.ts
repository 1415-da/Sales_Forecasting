/**
 * Scenario Planner wire contract.
 *
 * Zod schemas are the single source of truth: every TypeScript type here is
 * inferred from one, so a schema and its type can never drift apart.
 *
 * Backend name mapping. The API's authoritative comparison contract lives in
 * `api/schemas/scenario_comparison.py`, where the per-day row is `DailyImpact`
 * and the aggregate is `ScenarioImpact`. This module exposes them under the
 * frontend-facing names `ScenarioForecastPoint` and `ScenarioImpactSummary`;
 * the field names match the JSON exactly.
 *
 * Unknown keys are stripped rather than rejected (Zod's default for `z.object`),
 * so the backend can add a field without breaking a deployed frontend. Required
 * fields are still enforced.
 */

import { z } from 'zod';

/* -------------------------------------------------------------------------- */
/* Primitives                                                                  */
/* -------------------------------------------------------------------------- */

/** ISO calendar date, e.g. `2026-02-01`. */
export const isoDateSchema = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/, 'Expected an ISO date (YYYY-MM-DD)');

/** ISO timestamp, e.g. `2026-02-01T10:30:00Z`. */
export const isoDateTimeSchema = z.string().min(1);

export const scenarioTypeSchema = z.enum([
  'promotion',
  'traffic',
  'closure',
  'holiday',
  'price',
  'custom',
]);
export type ScenarioType = z.infer<typeof scenarioTypeSchema>;

export const promotionIntensitySchema = z.enum(['none', 'low', 'medium', 'high']);
export type PromotionIntensity = z.infer<typeof promotionIntensitySchema>;

export const uncertaintyMethodSchema = z.enum(['quantile_model', 'residual_based']);
export type UncertaintyMethod = z.infer<typeof uncertaintyMethodSchema>;

export const requirementModeSchema = z.enum(['any', 'all']);
export type RequirementMode = z.infer<typeof requirementModeSchema>;

export const lineageStatusSchema = z.enum(['available', 'partial', 'unavailable']);
export type LineageStatus = z.infer<typeof lineageStatusSchema>;

/** The only forecast kind this API produces. */
export const forecastTypeSchema = z.literal('predictive_what_if');
export type ForecastType = z.infer<typeof forecastTypeSchema>;

export const auditStatusSchema = z.enum([
  'received',
  'validation_failed',
  'capability_check_failed',
  'baseline_created',
  'baseline_failed',
  'scenario_completed',
  'scenario_failed',
  'scenario_cancelled',
  'rolled_back',
]);
export type AuditStatus = z.infer<typeof auditStatusSchema>;

/* -------------------------------------------------------------------------- */
/* Errors                                                                      */
/* -------------------------------------------------------------------------- */

/** Every error code the API can return. Mirrors `api/core/errors.py`. */
export const scenarioErrorCodeSchema = z.enum([
  'SCENARIO_VALIDATION_ERROR',
  'SCENARIO_NOT_SUPPORTED',
  'FEATURE_SCHEMA_MISSING',
  'FEATURE_NOT_AVAILABLE',
  'MODEL_NOT_FOUND',
  'MODEL_NOT_IN_PRODUCTION',
  'MLFLOW_UNAVAILABLE',
  'ARTIFACT_DOWNLOAD_FAILED',
  'PREDICTION_FAILED',
  'FORECAST_ALIGNMENT_ERROR',
  'DATABASE_UNAVAILABLE',
  'BASELINE_NOT_FOUND',
  'BASELINE_EXPIRED',
  'SCENARIO_ALREADY_RUNNING',
  'INTERNAL_SCENARIO_ERROR',
  'IDEMPOTENCY_KEY_CONFLICT',
  'SCENARIO_NOT_FOUND',
  'SCENARIO_EXPIRED',
]);
export type ScenarioErrorCode = z.infer<typeof scenarioErrorCodeSchema>;

/**
 * The error body the API returns.
 *
 * `code` is a plain string rather than the enum so an unrecognised code from a
 * newer backend still parses; map it through `friendlyErrorMessage` for a safe
 * fallback.
 */
export const scenarioApiErrorPayloadSchema = z.object({
  code: z.string().min(1),
  message: z.string().default(''),
  field: z.string().nullable().default(null),
  details: z.record(z.unknown()).default({}),
  request_id: z.string().nullable().default(null),
  timestamp: z.string().nullable().default(null),
});
export type ScenarioApiErrorPayload = z.infer<typeof scenarioApiErrorPayloadSchema>;

/** The full failure envelope: `{ success: false, error: {...} }`. */
export const scenarioErrorEnvelopeSchema = z.object({
  success: z.literal(false),
  error: scenarioApiErrorPayloadSchema,
});
export type ScenarioErrorEnvelope = z.infer<typeof scenarioErrorEnvelopeSchema>;

/* -------------------------------------------------------------------------- */
/* Request                                                                     */
/* -------------------------------------------------------------------------- */

export const historyPointSchema = z.object({
  date: isoDateSchema,
  sales: z.number().nonnegative(),
  store_id: z.string().optional(),
  customer_traffic: z.number().nonnegative().optional(),
  is_promotion: z.boolean().optional(),
  promotion_intensity: promotionIntensitySchema.optional(),
  price: z.number().positive().optional(),
  is_closed: z.boolean().optional(),
});
export type HistoryPoint = z.infer<typeof historyPointSchema>;

export const scenarioAssumptionsSchema = z.object({
  scenario_type: scenarioTypeSchema,
  start_date: isoDateSchema.nullish(),
  end_date: isoDateSchema.nullish(),
  duration_days: z.number().int().positive().nullish(),
  promotion_intensity: promotionIntensitySchema.default('none'),
  discount_depth: z.number().min(0).max(1).nullish(),
  traffic_multiplier: z.number().min(0.5).max(2).nullish(),
  closure_dates: z.array(isoDateSchema).default([]),
  custom_overrides: z.record(z.union([z.number(), z.string()])).default({}),
});
export type ScenarioAssumptions = z.infer<typeof scenarioAssumptionsSchema>;

/** Mirrors the API's request limits so the client can fail fast. */
export const SCENARIO_LIMITS = {
  minHistoryRows: 7,
  minHorizonDays: 1,
  maxHorizonDays: 90,
  defaultHorizonDays: 30,
  defaultModelVersion: 'latest',
  defaultConfidenceLevels: [0.1, 0.5, 0.9] as const,
} as const;

export const scenarioRequestSchema = z.object({
  store_id: z.string().min(1),
  model_version: z.string().nullish().default(SCENARIO_LIMITS.defaultModelVersion),
  horizon_days: z
    .number()
    .int()
    .min(SCENARIO_LIMITS.minHorizonDays)
    .max(SCENARIO_LIMITS.maxHorizonDays)
    .default(SCENARIO_LIMITS.defaultHorizonDays),
  confidence_levels: z
    .array(z.number().gt(0).lt(1))
    .nonempty()
    .default([...SCENARIO_LIMITS.defaultConfidenceLevels])
    .refine(
      (levels) => levels.every((level, index) => index === 0 || level > levels[index - 1]!),
      { message: 'confidence_levels must be unique and sorted ascending' },
    ),
  history: z
    .array(historyPointSchema)
    .min(SCENARIO_LIMITS.minHistoryRows, {
      message: `At least ${SCENARIO_LIMITS.minHistoryRows} days of history are required`,
    })
    .refine(
      (rows) => rows.every((row, index) => index === 0 || row.date > rows[index - 1]!.date),
      { message: 'history must have unique dates in ascending order' },
    ),
  scenario: scenarioAssumptionsSchema,
  baseline_forecast_id: z.string().nullish(),
});
export type ScenarioRequest = z.infer<typeof scenarioRequestSchema>;

/** The shape a caller supplies; defaults are filled in by the schema. */
export type ScenarioRequestInput = z.input<typeof scenarioRequestSchema>;

/* -------------------------------------------------------------------------- */
/* Capabilities                                                                */
/* -------------------------------------------------------------------------- */

export const scenarioCapabilityStatusSchema = z.object({
  scenario_type: scenarioTypeSchema,
  supported: z.boolean(),
  required_features: z.array(z.string()).default([]),
  available_features: z.array(z.string()).default([]),
  missing_features: z.array(z.string()).default([]),
  requirement_mode: requirementModeSchema.default('any'),
  reason: z.string().nullable().default(null),
});
export type ScenarioCapabilityStatus = z.infer<typeof scenarioCapabilityStatusSchema>;

export const scenarioCapabilitiesResponseSchema = z.object({
  model_version: z.string(),
  feature_schema_version: z.string().default('v1'),
  run_id: z.string().nullable().default(null),
  feature_count: z.number().int().nonnegative().default(0),
  features: z.array(z.string()).default([]),
  capabilities: z.array(scenarioCapabilityStatusSchema).default([]),
  warnings: z.array(z.string()).default([]),
  schema_version: z.string().default('v1'),
  min_history_rows: z.number().int().positive().default(SCENARIO_LIMITS.minHistoryRows),
  min_horizon_days: z.number().int().positive().default(SCENARIO_LIMITS.minHorizonDays),
  max_horizon_days: z.number().int().positive().default(SCENARIO_LIMITS.maxHorizonDays),
  default_horizon_days: z
    .number()
    .int()
    .positive()
    .default(SCENARIO_LIMITS.defaultHorizonDays),
  default_model_version: z.string().default(SCENARIO_LIMITS.defaultModelVersion),
  default_confidence_levels: z
    .array(z.number())
    .default([...SCENARIO_LIMITS.defaultConfidenceLevels]),
  generated_at: isoDateTimeSchema.optional(),
});
export type ScenarioCapabilitiesResponse = z.infer<typeof scenarioCapabilitiesResponseSchema>;

/* -------------------------------------------------------------------------- */
/* Comparison                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * One forecast day of the comparison.
 *
 * Backend type: `DailyImpact`. Quantile fields are null when the model does not
 * produce native quantiles.
 */
export const scenarioForecastPointSchema = z.object({
  date: isoDateSchema,
  baseline_forecast: z.number(),
  scenario_forecast: z.number(),
  daily_difference: z.number(),
  daily_difference_pct: z.number().nullable().default(null),
  cumulative_difference: z.number().default(0),
  baseline_p10: z.number().nullable().default(null),
  baseline_p50: z.number().nullable().default(null),
  baseline_p90: z.number().nullable().default(null),
  scenario_p10: z.number().nullable().default(null),
  scenario_p50: z.number().nullable().default(null),
  scenario_p90: z.number().nullable().default(null),
  difference_p10: z.number().nullable().default(null),
  difference_p50: z.number().nullable().default(null),
  difference_p90: z.number().nullable().default(null),
});
export type ScenarioForecastPoint = z.infer<typeof scenarioForecastPointSchema>;

export const quantileImpactSchema = z.object({
  quantile: z.number().gt(0).lt(1),
  total_baseline_forecast: z.number(),
  total_scenario_forecast: z.number(),
  total_difference: z.number(),
  total_difference_pct: z.number().nullable().default(null),
});
export type QuantileImpact = z.infer<typeof quantileImpactSchema>;

export const impactRangeSchema = z.object({
  total_difference_p10: z.number().nullable().default(null),
  total_difference_p50: z.number().nullable().default(null),
  total_difference_p90: z.number().nullable().default(null),
});
export type ImpactRange = z.infer<typeof impactRangeSchema>;

/**
 * Aggregate impact across the horizon.
 *
 * Backend type: `ScenarioImpact`. `*_pct` fields are null whenever the baseline
 * is zero or negative -- the API refuses to divide by it rather than returning
 * Infinity, so the UI must render a dash, not a number.
 */
export const scenarioImpactSummarySchema = z.object({
  horizon_days: z.number().int().nonnegative(),
  dates: z.array(isoDateSchema).default([]),
  daily_difference: z.array(z.number()).default([]),
  daily_difference_pct: z.array(z.number().nullable()).default([]),
  cumulative_difference: z.array(z.number()).default([]),
  total_baseline_forecast: z.number().default(0),
  total_scenario_forecast: z.number().default(0),
  total_difference: z.number().default(0),
  total_difference_pct: z.number().nullable().default(null),
  positive_days: z.number().int().nonnegative().default(0),
  negative_days: z.number().int().nonnegative().default(0),
  neutral_days: z.number().int().nonnegative().default(0),
  peak_impact_date: isoDateSchema.nullable().default(null),
  peak_impact_value: z.number().nullable().default(null),
  quantile_impacts: z.array(quantileImpactSchema).default([]),
  impact_range: impactRangeSchema.default({
    total_difference_p10: null,
    total_difference_p50: null,
    total_difference_p90: null,
  }),
  daily_impacts: z.array(scenarioForecastPointSchema).default([]),
  notes: z.array(z.string()).default([]),
});
export type ScenarioImpactSummary = z.infer<typeof scenarioImpactSummarySchema>;

export const forecastSeriesSummarySchema = z.object({
  forecast_id: z.string().nullable().default(null),
  horizon_days: z.number().int().nonnegative(),
  start_date: isoDateSchema.nullable().default(null),
  end_date: isoDateSchema.nullable().default(null),
  total_forecast: z.number().default(0),
  average_daily_forecast: z.number().default(0),
  min_daily_forecast: z.number().default(0),
  max_daily_forecast: z.number().default(0),
  uncertainty_method: uncertaintyMethodSchema.nullable().default(null),
  has_quantiles: z.boolean().default(false),
});
export type ForecastSeriesSummary = z.infer<typeof forecastSeriesSummarySchema>;

export const valueSummarySchema = z.object({
  count: z.number().int().nonnegative(),
  dtype: z.string(),
  min: z.number().nullable().default(null),
  max: z.number().nullable().default(null),
  mean: z.number().nullable().default(null),
  distinct: z.array(z.unknown()).nullable().default(null),
});
export type ValueSummary = z.infer<typeof valueSummarySchema>;

export const appliedOverrideSchema = z.object({
  feature_name: z.string(),
  override_type: z.enum(['set_constant', 'multiply', 'recompute', 'custom']),
  affected_date_count: z.number().int().nonnegative(),
  original_value_summary: valueSummarySchema,
  new_value_summary: valueSummarySchema,
  affected_dates: z.array(isoDateSchema).default([]),
  detail: z.string().nullable().default(null),
});
export type AppliedOverride = z.infer<typeof appliedOverrideSchema>;

export const unsupportedOverrideSchema = z.object({
  feature_name: z.string(),
  reason: z.string(),
  error_code: z.string().default('FEATURE_NOT_AVAILABLE'),
});
export type UnsupportedOverride = z.infer<typeof unsupportedOverrideSchema>;

export const datasetLineageSchema = z.object({
  model_version: z.string().nullable().default(null),
  data_source: z.string().nullable().default(null),
  dataset_version: z.string().nullable().default(null),
  dataset_hash: z.string().nullable().default(null),
  raw_data_path: z.string().nullable().default(null),
  processed_data_path: z.string().nullable().default(null),
  train_start_date: isoDateSchema.nullable().default(null),
  train_end_date: isoDateSchema.nullable().default(null),
  feature_schema_version: z.string().nullable().default(null),
  ingestion_run_id: z.string().nullable().default(null),
  lineage_status: lineageStatusSchema.default('unavailable'),
  source_of_record: z.string().default('none'),
  missing_fields: z.array(z.string()).default([]),
  warnings: z.array(z.string()).default([]),
  resolved_at: isoDateTimeSchema.optional(),
});
export type DatasetLineage = z.infer<typeof datasetLineageSchema>;

export const scenarioComparisonResponseSchema = z.object({
  scenario_id: z.string(),
  baseline_forecast_id: z.string(),
  scenario_forecast_id: z.string(),
  model_version: z.string(),
  model_stage: z.string(),
  scenario_type: scenarioTypeSchema,
  forecast_type: forecastTypeSchema,
  baseline: forecastSeriesSummarySchema,
  scenario: forecastSeriesSummarySchema,
  impact: scenarioImpactSummarySchema,
  daily_forecasts: z.array(scenarioForecastPointSchema).default([]),
  applied_overrides: z.array(appliedOverrideSchema).default([]),
  unsupported_overrides: z.array(unsupportedOverrideSchema).default([]),
  modified_features: z.array(z.string()).default([]),
  modified_dates: z.array(isoDateSchema).default([]),
  /** Always present, and always shown: the API attaches these unconditionally. */
  limitations: z.array(z.string()).default([]),
  generated_at: isoDateTimeSchema,
  store_id: z.string().nullable().default(null),
  assumptions: scenarioAssumptionsSchema.nullable().default(null),
  baseline_reused: z.boolean().default(false),
  warnings: z.array(z.string()).default([]),
  schema_version: z.string().default('v1'),
  dataset_lineage: datasetLineageSchema.nullable().default(null),
});
export type ScenarioComparisonResponse = z.infer<typeof scenarioComparisonResponseSchema>;

/* -------------------------------------------------------------------------- */
/* Rollback and audit                                                          */
/* -------------------------------------------------------------------------- */

export const scenarioRollbackResponseSchema = z.object({
  rolled_back: z.boolean(),
  scenario_id: z.string().nullable().default(null),
  request_id: z.string().nullable().default(null),
  status: auditStatusSchema,
  stage: z.string().nullable().default(null),
  reason: z.string(),
  error_code: z.string().nullable().default(null),
  baseline_forecast_id: z.string().nullable().default(null),
  baseline_preserved: z.boolean().default(false),
  baseline_status: z.string().nullable().default(null),
  audit_event_recorded: z.boolean().default(false),
  audit_history_preserved: z.boolean().default(true),
  partial_results_discarded: z.boolean().default(false),
  locks_released: z.boolean().default(false),
  rolled_back_at: isoDateTimeSchema,
  notes: z.array(z.string()).default([]),
});
export type ScenarioRollbackResponse = z.infer<typeof scenarioRollbackResponseSchema>;

export const scenarioAuditEventSchema = z.object({
  request_id: z.string(),
  scenario_id: z.string().nullable().default(null),
  baseline_forecast_id: z.string().nullable().default(null),
  store_id: z.string(),
  model_version: z.string(),
  scenario_type: z.string(),
  status: auditStatusSchema,
  error_code: z.string().nullable().default(null),
  error_message: z.string().nullable().default(null),
  duration_ms: z.number().int().nullable().default(null),
  created_at: isoDateTimeSchema,
});
export type ScenarioAuditEvent = z.infer<typeof scenarioAuditEventSchema>;

export const scenarioAuditListResponseSchema = z.object({
  scenario_id: z.string(),
  event_count: z.number().int().nonnegative(),
  events: z.array(scenarioAuditEventSchema).default([]),
});
export type ScenarioAuditListResponse = z.infer<typeof scenarioAuditListResponseSchema>;
