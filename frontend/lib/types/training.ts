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
  severity: z.enum(['error', 'warning', 'info']).default('error'),
  stage: z.string().nullable().optional(),
  file: z.string().nullable().optional(),
  column: z.string().nullable().optional(),
  action: z.string().nullable().optional(),
  count: z.number().nullable().default(0),
  examples: z.array(z.record(z.unknown())).default([]),
  requires_ack: z.boolean().default(false),
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

export const stageNames = [
  'file',
  'detection',
  'mapping',
  'schema',
  'rows',
  'readiness',
  'append',
  'deployment',
] as const;

const confirmationSchema = z.object({
  key: z.string(),
  label: z.string(),
  options: z.array(z.object({ value: z.string(), label: z.string() })).default([]),
  required: z.boolean().default(true),
  help: z.string().nullable().optional(),
});
export type Confirmation = z.infer<typeof confirmationSchema>;

export const detectionCandidateSchema = z.object({
  adapter_id: z.string(),
  adapter_version: z.string(),
  label: z.string(),
  match: z.enum(['exact', 'partial', 'manual', 'none']),
  evidence: z.array(z.string()).default([]),
  files_used: z.array(z.record(z.string())).default([]),
  files_excluded: z.array(z.record(z.string())).default([]),
  missing_files: z.array(z.string()).default([]),
  missing_fields: z.array(z.string()).default([]),
  ambiguities: z.array(z.string()).default([]),
  confirmations: z.array(confirmationSchema).default([]),
  proposed_mapping: z.record(z.unknown()).default({}),
  columns: z.record(z.array(z.string())).default({}),
  usable: z.boolean().default(false),
});
export type DetectionCandidate = z.infer<typeof detectionCandidateSchema>;

export const detectionSchema = z.object({
  candidates: z.array(detectionCandidateSchema).default([]),
  selected: z.string().nullable().optional(),
  reason: z.string().default(''),
  needs_input: z.array(z.string()).default([]),
});
export type Detection = z.infer<typeof detectionSchema>;

export const validationReportSchema = z
  .object({
    status: z.string().optional(),
    row_validation_performed: z.boolean().optional(),
    rows_accepted: z.number().nullable().optional(),
    stores_excluded: z.array(z.string()).default([]),
    stages: z.record(z.object({ status: z.string(), issues: z.number().default(0) })).default({}),
    detection: detectionSchema.nullable().optional(),
    files: z
      .object({
        data: z.array(z.record(z.unknown())).default([]),
        excluded: z.array(z.record(z.unknown())).default([]),
      })
      .nullable()
      .optional(),
    transformations: z.array(z.record(z.string())).nullable().optional(),
    retained_columns: z.array(z.record(z.string())).nullable().optional(),
    store_metadata: z.record(z.unknown()).nullable().optional(),
    adapter_message: z.string().nullable().optional(),
    deployment_compatibility: z
      .object({
        compatible: z.boolean(),
        production_model: z.string().nullable().optional(),
        reasons: z.array(z.string()).default([]),
        note: z.string().nullable().optional(),
      })
      .nullable()
      .optional(),
    mapping_version: z.number().nullable().optional(),
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
  status: z.enum(['processing', 'needs_mapping', 'invalid', 'validated', 'ready', 'failed']),
  mode: z.enum(['independent', 'append']),
  base_dataset_id: nullableString,
  conflict_policy: nullableString,
  schema_version: z.string(),
  target: z.string(),
  target_type: nullableString,
  currency: nullableString,
  frequency: z.string(),
  source_types: z.array(z.string()),
  source_namespace: nullableString,
  source_grain: nullableString,
  adapter_id: nullableString,
  adapter_version: nullableString,
  mapping_version: z.number().nullable().optional(),
  source_files: z.array(z.record(z.unknown())).default([]),
  processing_error: nullableString,
  processing_started_at: nullableString,
  processing_finished_at: nullableString,
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
  ingestion_config: z.record(z.unknown()).default({}),
  feature_options: z.record(z.unknown()).nullable().optional(),
  acknowledged_warnings: z.array(z.string()).default([]),
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
  limits: z
    .object({
      max_upload_bytes: z.number(),
      max_file_bytes: z.number(),
      max_files: z.number(),
      max_expanded_bytes: z.number(),
      max_compression_ratio: z.number(),
      max_rows: z.number(),
      processing_timeout_seconds: z.number(),
      max_concurrent_jobs: z.number(),
      preview_rows: z.number(),
      inline_processing_bytes: z.number(),
    })
    .partial()
    .default({}),
  adapters: z.array(z.object({ id: z.string(), version: z.string(), label: z.string() })).default([]),
  serving_currencies: z.array(z.string()).default([]),
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

/** Dataset statuses that are still changing on the server. */
export const DATASET_BUSY_STATUSES: ReadonlySet<string> = new Set(['processing']);

/** Statuses after which polling stops. */
export const TERMINAL_STATUSES: ReadonlySet<TrainingStatus> = new Set(['succeeded', 'failed']);
