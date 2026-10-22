/**
 * The training workspace client: what goes over the wire, and when polling
 * stops.
 *
 * The upload must be multipart with no JSON content type (the browser sets the
 * boundary), a training submission must carry its idempotency key, and an
 * approval must carry the exact identity the reviewer confirmed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { pollDelay, POLL_BASE_MS, POLL_MAX_MS } from '@/hooks/use-training';
import { registryAction, startTrainingRun, uploadTrainingDataset } from '@/lib/api';
import { runDetailSchema } from '@/lib/types/training';

const DATASET = {
  id: 'a'.repeat(32),
  name: 'Store sales',
  version: 1,
  status: 'validated',
  mode: 'independent',
  schema_version: 'store_day_v1',
  target: 'sales',
  currency: 'USD',
  frequency: 'D',
  source_types: ['real'],
  file_format: 'csv',
  raw_sha256: 'f'.repeat(64),
  raw_size_bytes: 10,
  stores: ['CA_1'],
  created_at: '2026-01-01T00:00:00Z',
  error_count: 0,
  warning_count: 0,
  validation_report: { errors: [], warnings: [] },
};

const RUN = {
  id: 'r1',
  dataset_id: 'a'.repeat(32),
  dataset_name: 'Store sales',
  dataset_version: 1,
  dataset_content_sha256: 'c'.repeat(64),
  training_config_id: 'standard_v1',
  training_config: {},
  deployment_policy: 'manual_review',
  dag_id: 'sales_forecast_training',
  dag_run_id: 'training__r1',
  status: 'queued',
  created_at: '2026-01-01T00:00:00Z',
  candidates: [],
  deployment_state: 'not_ready',
  is_terminal: false,
  can_deploy: false,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => vi.restoreAllMocks());

describe('training client', () => {
  it('uploads as multipart without forcing a JSON content type', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(DATASET, 201));
    const file = new File(['date,store_id,sales\n'], 'sales.csv', { type: 'text/csv' });
    await uploadTrainingDataset({
      file,
      name: 'Store sales',
      mode: 'append',
      currency: 'USD',
      sourceType: 'real',
      baseDatasetId: 'b'.repeat(32),
      conflictPolicy: 'prefer_new',
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/v1\/training\/datasets$/);
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect(form.get('mode')).toBe('append');
    expect(form.get('base_dataset_id')).toBe('b'.repeat(32));
    expect(form.get('conflict_policy')).toBe('prefer_new');
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
  });

  it('sends the idempotency key with a training request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(RUN, 201));
    await startTrainingRun({ datasetId: 'a'.repeat(32), trainingConfigId: 'standard_v1' }, 'train-key-1');
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('train-key-1');
    expect(JSON.parse(init.body as string)).toEqual({
      dataset_id: 'a'.repeat(32),
      training_config_id: 'standard_v1',
    });
  });

  it('sends the confirmed identity with an approval', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({
        success: true,
        action: 'promote',
        model: 'ensemble',
        version: '3',
        previous_stage: 'None',
        new_stage: 'Production',
        archived_versions: ['2'],
        verified: true,
        message: 'ok',
        models: [],
      }),
    );
    await registryAction('promote', '3', { model: 'ensemble', reason: 'Better', expectedIdentity: 'ensemble:3' });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toMatchObject({
      confirm: true,
      model: 'ensemble',
      expected_identity: 'ensemble:3',
    });
  });

  it('parses a run without inventing progress', () => {
    const parsed = runDetailSchema.parse(RUN);
    expect(parsed.task_states).toEqual([]);
    expect(parsed.candidates).toEqual([]);
  });
});

describe('polling backoff', () => {
  it('starts quickly, grows, and is capped', () => {
    expect(pollDelay(0)).toBe(POLL_BASE_MS);
    expect(pollDelay(1)).toBeGreaterThan(pollDelay(0));
    expect(pollDelay(50)).toBe(POLL_MAX_MS);
  });
});
