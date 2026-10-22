/**
 * Runtime-validated types for authentication, admin, monitoring, and the
 * registry.
 *
 * Mirrors `api/schemas/{auth,admin,monitoring,models}.py`. Every numeric
 * monitoring field is nullable on purpose: the API returns `null` when nothing
 * has been scored, and typing those as `number` would force the UI to invent a
 * value for "unknown".
 */
import { z } from 'zod';

import { roleSchema } from '@/lib/rbac';

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

export const userProfileSchema = z.object({
  id: z.string(),
  email: z.string(),
  full_name: z.string(),
  role: z.string(),
  role_description: z.string(),
  is_active: z.boolean(),
  must_change_password: z.boolean(),
  permissions: z.array(z.string()),
  created_at: z.string(),
  updated_at: z.string(),
  last_login_at: z.string().nullable().optional(),
});

export const tokenResponseSchema = z.object({
  success: z.boolean(),
  access_token: z.string(),
  token_type: z.string(),
  expires_in: z.number(),
  user: userProfileSchema,
  refresh_token: z.string().nullable().optional(),
});

export const authConfigSchema = z.object({
  success: z.boolean(),
  self_registration_enabled: z.boolean(),
  password_min_length: z.number(),
  roles: z.array(z.string()),
  /** Roles the sign-up form may offer, least privileged first. */
  self_registration_roles: z.array(z.string()).default([]),
  default_self_registration_role: z.string().nullish(),
});

export const logoutResponseSchema = z.object({
  success: z.boolean(),
  sessions_revoked: z.number(),
});

export const sessionSummarySchema = z.object({
  id: z.string(),
  created_at: z.string(),
  expires_at: z.string(),
  ip_address: z.string().nullable().optional(),
  user_agent: z.string().nullable().optional(),
  current: z.boolean(),
});

// ---------------------------------------------------------------------------
// Admin: users
// ---------------------------------------------------------------------------

export const adminUserSchema = z.object({
  id: z.string(),
  email: z.string(),
  full_name: z.string(),
  role: z.string(),
  is_active: z.boolean(),
  must_change_password: z.boolean(),
  is_locked: z.boolean(),
  failed_login_count: z.number(),
  created_at: z.string(),
  updated_at: z.string(),
  last_login_at: z.string().nullable().optional(),
});

export const adminUserListSchema = z.object({
  success: z.boolean(),
  users: z.array(adminUserSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const createdUserSchema = z.object({
  success: z.boolean(),
  user: adminUserSchema,
  /** Present only when the server generated the password. Shown once. */
  temporary_password: z.string().nullable().optional(),
  message: z.string(),
});

export const resetPasswordSchema = z.object({
  success: z.boolean(),
  user: adminUserSchema,
  temporary_password: z.string(),
  sessions_revoked: z.number(),
  message: z.string(),
});

export const roleDescriptionSchema = z.object({
  role: z.string(),
  description: z.string(),
  permissions: z.array(z.string()),
});

export const permissionMatrixSchema = z.object({
  success: z.boolean(),
  roles: z.array(roleDescriptionSchema),
  matrix: z.array(z.record(z.unknown())),
});

// ---------------------------------------------------------------------------
// Admin: audit log
// ---------------------------------------------------------------------------

export const auditLogRowSchema = z.object({
  id: z.string(),
  action: z.string(),
  resource_type: z.string(),
  resource_id: z.string().nullable().optional(),
  outcome: z.string(),
  user_id: z.string().nullable().optional(),
  user_email: z.string().nullable().optional(),
  user_role: z.string().nullable().optional(),
  metadata: z.record(z.unknown()),
  ip_address: z.string().nullable().optional(),
  request_id: z.string().nullable().optional(),
  created_at: z.string(),
});

export const auditLogListSchema = z.object({
  success: z.boolean(),
  entries: z.array(auditLogRowSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
  available_actions: z.record(z.number()),
  unrestricted: z.boolean(),
});

// ---------------------------------------------------------------------------
// Monitoring
// ---------------------------------------------------------------------------

const nullableNumber = z.number().nullable().optional();

export const accuracyMetricsSchema = z.object({
  sample_size: z.number(),
  wape: nullableNumber,
  mae: nullableNumber,
  rmse: nullableNumber,
  bias: nullableNumber,
  mean_error_pct: nullableNumber,
  mape: nullableNumber,
  interval_coverage: nullableNumber,
  r2: nullableNumber,
});

export const storeCoverageSchema = z.object({
  store_id: z.string(),
  latest_date: z.string().nullable().optional(),
  days: z.number(),
});

export const freshnessSchema = z.object({
  success: z.boolean().optional(),
  status: z.string(),
  has_data: z.boolean(),
  latest_actual_date: z.string().nullable().optional(),
  latest_forecast_date: z.string().nullable().optional(),
  latest_forecast_created_at: z.string().nullable().optional(),
  days_since_actual: z.number().nullable().optional(),
  actual_count: z.number(),
  forecast_count: z.number(),
  coverage: z.array(storeCoverageSchema),
  no_data_message: z.string().nullable().optional(),
});

export const accuracyResponseSchema = z.object({
  success: z.boolean().optional(),
  has_data: z.boolean(),
  window_days: z.number(),
  window_start: z.string(),
  window_end: z.string(),
  model_version: z.string().nullable().optional(),
  store_id: z.string().nullable().optional(),
  current: accuracyMetricsSchema,
  previous: accuracyMetricsSchema.nullable().optional(),
  trend: z.record(z.number().nullable()),
  series: z.array(z.record(z.unknown())),
  no_data_message: z.string().nullable().optional(),
});

export const driftRowSchema = z.object({
  feature_name: z.string(),
  drift_method: z.string(),
  drift_score: z.number(),
  threshold: z.number(),
  status: z.string(),
  model_version: z.string().nullable().optional(),
  reference_size: z.number().nullable().optional(),
  current_size: z.number().nullable().optional(),
  evaluation_date: z.string().nullable().optional(),
});

export const driftResponseSchema = z.object({
  success: z.boolean().optional(),
  has_data: z.boolean(),
  model_version: z.string().nullable().optional(),
  features: z.array(driftRowSchema),
  drifting_features: z.number(),
  warning_features: z.number(),
  no_data_message: z.string().nullable().optional(),
});

export const alertSchema = z.object({
  id: z.string(),
  alert_type: z.string(),
  severity: z.string(),
  status: z.string(),
  title: z.string(),
  message: z.string(),
  recommended_action: z.string().nullable().optional(),
  model_version: z.string().nullable().optional(),
  store_id: z.string().nullable().optional(),
  occurrence_count: z.number(),
  details: z.record(z.unknown()),
  created_at: z.string(),
  last_seen_at: z.string().nullable().optional(),
  acknowledged_at: z.string().nullable().optional(),
  resolved_at: z.string().nullable().optional(),
  resolution_note: z.string().nullable().optional(),
});

export const alertListSchema = z.object({
  success: z.boolean(),
  alerts: z.array(alertSchema),
  total: z.number(),
  open_count: z.number(),
  critical_count: z.number(),
  warning_count: z.number(),
});

export const retrainingSchema = z.object({
  recommendation: z.string(),
  reason: z.string(),
  model_version: z.string().nullable().optional(),
  last_training_date: z.string().nullable().optional(),
  wape: nullableNumber,
  wape_trend: nullableNumber,
  drift_features: z.number(),
});

export const monitoringSummarySchema = z.object({
  success: z.boolean(),
  status: z.string(),
  has_data: z.boolean(),
  window_days: z.number(),
  window_start: z.string(),
  window_end: z.string(),
  model_version: z.string().nullable().optional(),
  freshness: freshnessSchema,
  accuracy: accuracyResponseSchema,
  drift_summary: z.record(z.number()),
  alert_summary: z.record(z.number()),
  retraining: retrainingSchema,
  no_data_message: z.string().nullable().optional(),
  can_manage_alerts: z.boolean(),
  can_view_detail: z.boolean(),
});

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

export const metricDeltaSchema = z.object({
  metric: z.string(),
  version_a: nullableNumber,
  version_b: nullableNumber,
  delta: nullableNumber,
  delta_pct: nullableNumber,
  verdict: z.string(),
  lower_is_better: z.boolean(),
});

export const registryActionSchema = z.object({
  success: z.boolean(),
  action: z.string(),
  model: z.string(),
  version: z.string(),
  previous_stage: z.string(),
  new_stage: z.string(),
  archived_versions: z.array(z.string()),
  /** False means the registry accepted the call but the stage did not change. */
  verified: z.boolean(),
  message: z.string(),
  models: z.array(z.record(z.unknown())),
});

export const lineageSchema = z.object({
  success: z.boolean(),
  model: z.string(),
  version: z.string(),
  stage: z.string(),
  run_id: z.string().nullable().optional(),
  training_date: z.string().nullable().optional(),
  dataset: z.record(z.unknown()),
  metrics: z.record(z.number()),
  mlflow_run_url: z.string().nullable().optional(),
});

export const artifactSummarySchema = z.object({
  name: z.string(),
  kind: z.string(),
  description: z.string(),
  available: z.boolean(),
});

export const artifactsResponseSchema = z.object({
  success: z.boolean(),
  model: z.string(),
  version: z.string(),
  run_id: z.string().nullable().optional(),
  artifacts: z.array(artifactSummarySchema),
  feature_count: z.number().nullable().optional(),
  warnings: z.array(z.string()),
});

// ---------------------------------------------------------------------------
// Inferred types
// ---------------------------------------------------------------------------

export type UserProfile = z.infer<typeof userProfileSchema>;
export type TokenResponse = z.infer<typeof tokenResponseSchema>;
export type AuthConfig = z.infer<typeof authConfigSchema>;
export type SessionSummary = z.infer<typeof sessionSummarySchema>;
export type AdminUser = z.infer<typeof adminUserSchema>;
export type AdminUserList = z.infer<typeof adminUserListSchema>;
export type CreatedUser = z.infer<typeof createdUserSchema>;
export type ResetPasswordResult = z.infer<typeof resetPasswordSchema>;
export type PermissionMatrix = z.infer<typeof permissionMatrixSchema>;
export type AuditLogRow = z.infer<typeof auditLogRowSchema>;
export type AuditLogList = z.infer<typeof auditLogListSchema>;
export type AccuracyMetrics = z.infer<typeof accuracyMetricsSchema>;
export type FreshnessReport = z.infer<typeof freshnessSchema>;
export type AccuracyReport = z.infer<typeof accuracyResponseSchema>;
export type DriftRow = z.infer<typeof driftRowSchema>;
export type DriftReport = z.infer<typeof driftResponseSchema>;
export type MonitoringAlert = z.infer<typeof alertSchema>;
export type AlertList = z.infer<typeof alertListSchema>;
export type RetrainingRecommendation = z.infer<typeof retrainingSchema>;
export type MonitoringSummary = z.infer<typeof monitoringSummarySchema>;
export type MetricDelta = z.infer<typeof metricDeltaSchema>;
export type RegistryActionResult = z.infer<typeof registryActionSchema>;
export type ModelLineage = z.infer<typeof lineageSchema>;
export type ModelArtifacts = z.infer<typeof artifactsResponseSchema>;
