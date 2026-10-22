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
import {
  configureTrainingDataset,
  registryAction,
  sealTrainingDataset,
  startTrainingRun,
  uploadTrainingDataset,
} from '@/lib/api';
import { datasetDetailSchema, runDetailSchema } from '@/lib/types/training';

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
  it('uploads several files as multipart with progress and no forced content type', async () => {
    const sent: { headers: Record<string, string>; body?: FormData; url?: string } = { headers: {} };
    class FakeXhr {
      status = 202;
      responseText = JSON.stringify({ ...DATASET, status: 'processing' });
      upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      ontimeout: (() => void) | null = null;
      withCredentials = false;
      timeout = 0;
      open(_method: string, url: string) {
        sent.url = url;
      }
      setRequestHeader(name: string, value: string) {
        sent.headers[name] = value;
      }
      send(body: FormData) {
        sent.body = body;
        this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
        this.onload?.();
      }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXhr);
    const progress: number[] = [];
    const train = new File(['Store,Date,Sales\n'], 'train.csv', { type: 'text/csv' });
    const meta = new File(['Store,StoreType\n'], 'store.csv', { type: 'text/csv' });
    const result = await uploadTrainingDataset(
      {
        files: [train, meta],
        name: 'Rossmann',
        mode: 'append',
        sourceType: 'real',
        baseDatasetId: 'b'.repeat(32),
        conflictPolicy: 'prefer_new',
        idempotencyKey: 'upload-key-1',
      },
      (value) => progress.push(value),
    );
    vi.unstubAllGlobals();
    expect(result.status).toBe('processing');
    expect(sent.url).toMatch(/\/v1\/training\/datasets$/);
    expect(sent.body?.getAll('files')).toHaveLength(2);
    expect(sent.body?.get('mode')).toBe('append');
    expect(sent.body?.get('conflict_policy')).toBe('prefer_new');
    // Currency was not chosen, so none is sent: the server asks for it.
    expect(sent.body?.get('currency')).toBeNull();
    expect(sent.headers['Content-Type']).toBeUndefined();
    expect(sent.headers['Idempotency-Key']).toBe('upload-key-1');
    expect(progress).toEqual([0.5]);
  });

  it('sends the reviewed mapping version and acknowledgments when sealing', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ ...DATASET, status: 'ready' }));
    await sealTrainingDataset('a'.repeat(32), { expectedMappingVersion: 3, acknowledgedWarnings: ['STORES_EXCLUDED'] });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      expected_mapping_version: 3,
      acknowledged_warnings: ['STORES_EXCLUDED'],
    });
  });

  it('re-configures with the expected mapping version so stale edits are refused', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(DATASET));
    await configureTrainingDataset('a'.repeat(32), { currency: 'EUR' }, 2);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/configuration$/);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({ config: { currency: 'EUR' }, expected_mapping_version: 2 });
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

describe('dataset report parsing', () => {
  it('keeps row counts null when row validation never ran', () => {
    const parsed = datasetDetailSchema.parse({
      ...DATASET,
      status: 'invalid',
      validation_report: { row_validation_performed: false, rows_valid: null, rows_invalid: null, errors: [], warnings: [] },
    });
    expect(parsed.validation_report.rows_valid).toBeNull();
    expect(parsed.validation_report.rows_invalid).toBeNull();
    expect(parsed.validation_report.row_validation_performed).toBe(false);
  });
});

describe('polling backoff', () => {
  it('starts quickly, grows, and is capped', () => {
    expect(pollDelay(0)).toBe(POLL_BASE_MS);
    expect(pollDelay(1)).toBeGreaterThan(pollDelay(0));
    expect(pollDelay(50)).toBe(POLL_MAX_MS);
  });
});
