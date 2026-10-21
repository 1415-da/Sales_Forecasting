/**
 * Runtime-validated types for the forecast API.
 *
 * Zod rather than bare TypeScript interfaces because a type assertion is a
 * promise, not a check: if the API changes shape the UI would render
 * `undefined` into a chart and fail far from the cause. Parsing at the boundary
 * turns that into one legible error.
 *
 * Every schema mirrors a Pydantic model in `api/schemas/`. The two are kept in
 * step by hand, so each schema names its source.
 */
import { z } from 'zod';

/** ISO date, `YYYY-MM-DD`. */
const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'expected a YYYY-MM-DD date');

// ---------------------------------------------------------------------------
// Models  (api/schemas/models.py)
// ---------------------------------------------------------------------------

/**
 * Training metrics. Every field is optional: training omits MAPE when the
 * actuals contain zeros, and an older run may never have logged WAPE or bias.
 */
export const modelMetricsSchema = z.object({
  rmse: z.number().nullable().optional(),
  mae: z.number().nullable().optional(),
  r2: z.number().nullable().optional(),
  mape: z.number().nullable().optional(),
  wape: z.number().nullable().optional(),
  bias: z.number().nullable().optional(),
});

export const modelInfoSchema = z.object({
  model: z.string(),
  label: z.string(),
  registered_model_name: z.string(),
  version: z.string(),
  stage: z.string(),
  run_id: z.string().nullable().optional(),
  is_production: z.boolean(),
  is_default: z.boolean(),
  metrics: modelMetricsSchema,
  training_date: z.string().nullable().optional(),
  supported_stores: z.array(z.string()),
  feature_count: z.number().nullable().optional(),
  data_source: z.string().nullable().optional(),
  dataset_version: z.string().nullable().optional(),
});

export const modelListSchema = z.object({
  success: z.boolean(),
  models: z.array(modelInfoSchema),
  default_model: z.string().nullable().optional(),
  production_available: z.boolean(),
  supported_stores: z.array(z.string()),
  warnings: z.array(z.string()),
});

export const modelDetailSchema = z.object({
  success: z.boolean(),
  model: modelInfoSchema,
  features: z.array(z.string()),
  lineage: z.record(z.unknown()),
  warnings: z.array(z.string()),
});

// ---------------------------------------------------------------------------
// Forecast  (api/schemas/forecast.py)
// ---------------------------------------------------------------------------

export const dailyForecastSchema = z.object({
  date: isoDate,
  predicted_sales: z.number(),
  lower_bound: z.number(),
  upper_bound: z.number(),
});

export const forecastSummarySchema = z.object({
  total_forecast_sales: z.number(),
  average_daily_sales: z.number(),
  lower_bound_total: z.number(),
  upper_bound_total: z.number(),
});

export const forecastMetadataSchema = z.object({
  history_start_date: z.string(),
  history_end_date: z.string(),
  history_days: z.number(),
  feature_schema_version: z.string(),
  feature_count: z.number().optional(),
  uncertainty_method: z.string().optional(),
  residual_std: z.number().nullable().optional(),
  imputed_features: z.array(z.string()).optional(),
  run_id: z.string().nullable().optional(),
  data_source: z.string().nullable().optional(),
  dataset_version: z.string().nullable().optional(),
});

export const forecastResponseSchema = z.object({
  forecast_id: z.string(),
  store_id: z.string(),
  model: z.string(),
  model_version: z.string(),
  model_stage: z.string(),
  generated_at: z.string(),
  horizon_days: z.number(),
  confidence_level: z.number(),
  summary: forecastSummarySchema,
  // A forecast with no days is not a forecast; an empty array is a server bug
  // and should fail here rather than render an empty chart.
  forecasts: z.array(dailyForecastSchema).min(1),
  metadata: forecastMetadataSchema,
  warnings: z.array(z.string()).default([]),
});

/** One observed day sent to the API. */
export const historyPointSchema = z.object({
  date: isoDate,
  sales: z.number().nonnegative(),
  store_id: z.string().optional(),
  quantity_sold: z.number().nonnegative().optional(),
  profit: z.number().optional(),
  customer_traffic: z.number().nonnegative().optional(),
  has_promotion: z.boolean().optional(),
});

export const forecastRequestSchema = z.object({
  store_id: z.string().min(1),
  model: z.string().min(1),
  model_version: z.string().min(1),
  horizon_days: z.number().int().min(1).max(90),
  confidence_level: z.union([z.literal(0.8), z.literal(0.95)]),
  history: z.array(historyPointSchema).min(1),
});

// ---------------------------------------------------------------------------
// Health  (api/routers/health.py)
// ---------------------------------------------------------------------------

export const componentHealthSchema = z.object({
  status: z.string(),
  latency_ms: z.number().optional(),
  detail: z.string().optional(),
});

export const productionModelSchema = z.object({
  model: z.string(),
  registered_model_name: z.string(),
  version: z.string(),
  stage: z.string(),
  run_id: z.string().nullable().optional(),
  training_date: z.string().nullable().optional(),
  metrics: z.record(z.number()).optional(),
});

export const healthSchema = z.object({
  success: z.boolean(),
  status: z.string(),
  version: z.string(),
  components: z.record(componentHealthSchema),
  production_model: productionModelSchema.nullable().optional(),
});

// ---------------------------------------------------------------------------
// Error envelope  (api/core/errors.py)
// ---------------------------------------------------------------------------

export const apiErrorSchema = z.object({
  success: z.literal(false),
  error: z.object({
    code: z.string(),
    message: z.string(),
    field: z.string().nullable().optional(),
    details: z.unknown().optional(),
    request_id: z.string().optional(),
    timestamp: z.string().optional(),
  }),
});

// ---------------------------------------------------------------------------
// Inferred types
// ---------------------------------------------------------------------------

export type ModelMetrics = z.infer<typeof modelMetricsSchema>;
export type ModelInfo = z.infer<typeof modelInfoSchema>;
export type ModelList = z.infer<typeof modelListSchema>;
export type ModelDetail = z.infer<typeof modelDetailSchema>;
export type DailyForecast = z.infer<typeof dailyForecastSchema>;
export type ForecastSummary = z.infer<typeof forecastSummarySchema>;
export type ForecastMetadata = z.infer<typeof forecastMetadataSchema>;
export type ForecastResponse = z.infer<typeof forecastResponseSchema>;
export type HistoryPoint = z.infer<typeof historyPointSchema>;
export type ForecastRequestBody = z.infer<typeof forecastRequestSchema>;
export type HealthReport = z.infer<typeof healthSchema>;
export type ProductionModel = z.infer<typeof productionModelSchema>;
export type ApiErrorBody = z.infer<typeof apiErrorSchema>;

/** Horizon presets the console offers. */
export const HORIZON_OPTIONS = [7, 14, 30, 60, 90] as const;
export type HorizonOption = (typeof HORIZON_OPTIONS)[number];

/** Confidence levels the API's uncertainty band is calibrated for. */
export const CONFIDENCE_OPTIONS = [0.8, 0.95] as const;
export type ConfidenceOption = (typeof CONFIDENCE_OPTIONS)[number];

/** How history reaches the console. */
export type HistorySource = 'sample' | 'upload' | 'manual';
