/**
 * Runtime-validated types for the training workspace.
 *
 * Mirrors `api/schemas/training.py`. Reports and evaluations are passed through
 * as records: their shape is owned by the shared contract module and rendered
 * defensively, so a new report field never breaks parsing.
 */
import { z } from 'zod';

const nullableString = z.string().nullable().optional();

export const issueSchema = z.object({
  code: z.string(),
  message: z.string(),
  count: z.number().default(0),
  examples: z.array(z.record(z.unknown())).default([]),
});
export type ValidationIssue = z.infer<typeof issueSchema>;

export const storeReportSchema = z.object({
  store_id: z.string(),
  days: z.number(),
  first_date: z.string(),
  last_date: z.string(),
  missing_dates: z.number(),
  zero_sales_days: z.number(),
  total_sales: z.number().optional(),
  sufficient: z.boolean(),
  reason: z.string().nullable().optional(),
});
export type StoreReport = z.infer<typeof storeReportSchema>;

export const validationReportSchema = z
  .object({
    status: z.string().optional(),
    rows_received: z.number().nullable().optional(),
    rows_valid: z.number().nullable().optional(),
    rows_invalid: z.number().nullable().optional(),
    exact_duplicates_removed: z.number().nullable().optional(),
    conflicting_duplicates: z.number().nullable().optional(),
    date_min: nullableString,
    date_max: nullableString,
    total_dates: z.number().nullable().optional(),
    store_count: z.number().nullable().optional(),
    errors: z.array(issueSchema).default([]),
    warnings: z.array(issueSchema).default([]),
    stores: z.array(storeReportSchema).default([]),
    preview: z.array(z.record(z.unknown())).default([]),
    context_features: z.record(z.string()).default({}),
    split: z.record(z.unknown()).nullable().optional(),
    combination: z.record(z.unknown()).nullable().optional(),
    upload: z.record(z.unknown()).nullable().optional(),
  })
  .passthrough();
export type ValidationReport = z.infer<typeof validationReportSchema>;

export const datasetSchema = z.object({
  id: z.string(),
  name: z.string(),
  version: z.number(),
  status: z.enum(['invalid', 'validated', 'ready']),
  mode: z.enum(['independent', 'append']),
  base_dataset_id: nullableString,
  conflict_policy: nullableString,
  schema_version: z.string(),
  target: z.string(),
  currency: z.string(),
  frequency: z.string(),
  source_types: z.array(z.string()),
  original_filename: nullableString,
  file_format: z.string(),
  raw_sha256: z.string(),
  raw_size_bytes: z.number(),
  content_sha256: nullableString,
  manifest_sha256: nullableString,
  manifest_uri: nullableString,
  date_min: nullableString,
  date_max: nullableString,
  row_count: z.number().nullable().optional(),
  store_count: z.number().nullable().optional(),
  stores: z.array(z.string()).default([]),
  created_at: z.string(),
  created_by: nullableString,
  sealed_at: nullableString,
  error_count: z.number().default(0),
  warning_count: z.number().default(0),
});
export type TrainingDataset = z.infer<typeof datasetSchema>;

export const datasetDetailSchema = datasetSchema.extend({
  validation_report: validationReportSchema,
});
export type TrainingDatasetDetail = z.infer<typeof datasetDetailSchema>;

export const datasetListSchema = z.object({ datasets: z.array(datasetSchema) });

export const trainingConfigSchema = z.object({
  id: z.string(),
  label: z.string(),
  description: z.string(),
  use_optuna: z.boolean(),
  optuna_trials: z.number(),
  models: z.array(z.string()),
  evaluation_policy: z.string(),
  deployment_policy: z.string(),
  expected_duration: z.string(),
  min_test_rows: z.number(),
  allowed: z.boolean(),
});
export type TrainingConfig = z.infer<typeof trainingConfigSchema>;

export const contractSchema = z.object({
  contract: z.object({
    schema_version: z.string(),
    grain: z.string(),
    target: z.string(),
    target_description: z.string(),
    frequency: z.string(),
    required_columns: z.array(
      z.object({ name: z.string(), type: z.string(), description: z.string() }),
    ),
    optional_columns: z.array(
      z.object({ name: z.string(), type: z.string(), description: z.string() }),
    ),
    derivations: z.record(z.string()),
    rejected: z.string(),
    file_types: z.array(z.string()),
    policy: z.record(z.unknown()),
  }),
  configs: z.array(trainingConfigSchema),
  source_types: z.array(z.string()),
  conflict_policies: z.array(z.string()),
  workflows: z.record(z.string()),
});
export type TrainingContract = z.infer<typeof contractSchema>;

export const candidateSchema = z
  .object({
    model: z.string(),
    version: z.string(),
    registered_name: z.string().optional(),
    gate_passed: z.boolean().optional(),
  })
  .passthrough();
export type Candidate = z.infer<typeof candidateSchema>;

export const taskStateSchema = z.object({
  task_id: z.string(),
  state: z.string().nullable(),
  try_number: z.number().nullable().optional(),
  start_date: nullableString,
  end_date: nullableString,
  duration: z.number().nullable().optional(),
});
export type TaskState = z.infer<typeof taskStateSchema>;

export const trainingStatusSchema = z.enum([
  'pending',
  'submission_uncertain',
  'queued',
  'running',
  'succeeded',
  'failed',
]);
export type TrainingStatus = z.infer<typeof trainingStatusSchema>;

export const runSchema = z.object({
  id: z.string(),
  dataset_id: z.string(),
  dataset_name: z.string(),
  dataset_version: z.number(),
  dataset_content_sha256: z.string(),
  training_config_id: z.string(),
  training_config: z.record(z.unknown()),
  deployment_policy: z.string(),
  dag_id: z.string(),
  dag_run_id: z.string(),
  status: trainingStatusSchema,
  airflow_state: nullableString,
  failure_reason: nullableString,
  created_at: z.string(),
  created_by: nullableString,
  created_by_role: nullableString,
  submitted_at: nullableString,
  started_at: nullableString,
  finished_at: nullableString,
  last_synced_at: nullableString,
  mlflow_run_id: nullableString,
  candidates: z.array(candidateSchema).default([]),
  deployment_state: z.string(),
  deployed_model: nullableString,
  deployed_version: nullableString,
  headline_wape: z.number().nullable().optional(),
  is_terminal: z.boolean(),
});
export type TrainingRun = z.infer<typeof runSchema>;

export const decisionSchema = z.object({
  decision: z.string(),
  model: z.string(),
  version: z.string(),
  reason: z.string(),
  previous_production_version: nullableString,
  checks: z.record(z.unknown()).default({}),
  decided_at: z.string(),
  decided_by: nullableString,
  decided_by_role: nullableString,
});
export type DeploymentDecision = z.infer<typeof decisionSchema>;

export const productionModelSchema = z.object({
  model: z.string(),
  version: z.string(),
  stage: z.string(),
  run_id: nullableString,
  metrics: z.record(z.number()).default({}),
  dataset_version: nullableString,
});
export type ProductionModel = z.infer<typeof productionModelSchema>;

export const runDetailSchema = runSchema.extend({
  task_states: z.array(taskStateSchema).default([]),
  evaluation: z.record(z.unknown()).default({}),
  decisions: z.array(decisionSchema).default([]),
  production: z.array(productionModelSchema).default([]),
  airflow_url: nullableString,
  mlflow_url: nullableString,
  sync_warning: nullableString,
  can_deploy: z.boolean(),
});
export type TrainingRunDetail = z.infer<typeof runDetailSchema>;

export const runListSchema = z.object({ runs: z.array(runSchema) });

export const datasetHistorySchema = z.object({
  dataset_id: z.string(),
  dataset_label: z.string(),
  store_id: z.string(),
  history: z.array(z.object({ date: z.string(), sales: z.number() })),
});
export type DatasetHistory = z.infer<typeof datasetHistorySchema>;

/** Statuses after which polling stops. */
export const TERMINAL_STATUSES: ReadonlySet<TrainingStatus> = new Set(['succeeded', 'failed']);
