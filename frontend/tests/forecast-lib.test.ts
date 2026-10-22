/**
 * The pure logic behind the Forecast Console: CSV parsing, history validation,
 * demo generation, CSV export, and the API client's error handling.
 *
 * These are the parts that decide whether the Generate button enables and what
 * the user is told when it does not, so they are tested directly rather than
 * through the components that render them.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, friendlyMessage, getModels, createForecast, loadAuthConfig } from '@/lib/api';
import { addDays, formatSales, inclusiveDayCount, toIsoDate } from '@/lib/format';
import {
  MIN_HISTORY_DAYS,
  buildSampleHistory,
  firstForecastDate,
  parseHistoryCsv,
  validateHistory,
} from '@/lib/sample-history';
import { buildForecastCsv, forecastFilename } from '@/components/forecast/export-button';
import { forecastResponseSchema, modelListSchema } from '@/lib/types';

// ---------------------------------------------------------------------------
// Demo history
// ---------------------------------------------------------------------------

describe('buildSampleHistory', () => {
  it('is deterministic, so a demo forecast is reproducible', () => {
    expect(buildSampleHistory(40)).toEqual(buildSampleHistory(40));
  });

  it('produces contiguous days ending yesterday', () => {
    const rows = buildSampleHistory(45);
    expect(rows).toHaveLength(45);
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    expect(rows[rows.length - 1]!.date).toBe(toIsoDate(yesterday));
    for (let index = 1; index < rows.length; index += 1) {
      expect(rows[index]!.date).toBe(addDays(rows[index - 1]!.date, 1));
    }
  });

  it('passes its own validation, so the demo path is never blocked', () => {
    expect(validateHistory(buildSampleHistory()).isValid).toBe(true);
  });

  it('has a weekly pattern rather than a flat line', () => {
    const values = buildSampleHistory(60).map((row) => row.sales);
    expect(Math.max(...values)).toBeGreaterThan(Math.min(...values) * 1.05);
  });
});

// ---------------------------------------------------------------------------
// CSV parsing
// ---------------------------------------------------------------------------

describe('parseHistoryCsv', () => {
  it('reads the required columns', () => {
    const { rows, issues } = parseHistoryCsv(
      'date,sales\n2026-01-01,100\n2026-01-02,120\n',
    );
    expect(issues).toEqual([]);
    expect(rows).toEqual([
      { date: '2026-01-01', sales: 100 },
      { date: '2026-01-02', sales: 120 },
    ]);
  });

  it('accepts the documented column aliases', () => {
    const { rows } = parseHistoryCsv('day,revenue\n2026-01-01,100\n');
    expect(rows[0]).toEqual({ date: '2026-01-01', sales: 100 });
  });

  it('carries through the optional columns the model can use', () => {
    const { rows } = parseHistoryCsv(
      'date,sales,store_id,profit,customer_traffic\n2026-01-01,100,CA_1,30,500\n',
    );
    expect(rows[0]).toEqual({
      date: '2026-01-01',
      sales: 100,
      store_id: 'CA_1',
      profit: 30,
      customer_traffic: 500,
    });
  });

  it('names the columns it found when a required one is missing', () => {
    const { rows, issues } = parseHistoryCsv('date,units\n2026-01-01,100\n');
    expect(rows).toEqual([]);
    // Telling the user what was found is what makes the error actionable.
    expect(issues[0]!.message).toContain('units');
  });

  it('honours quoted fields containing commas', () => {
    const { rows } = parseHistoryCsv('date,sales,store_id\n2026-01-01,"1,250","CA, 1"\n');
    expect(rows[0]!.sales).toBe(1250);
    expect(rows[0]!.store_id).toBe('CA, 1');
  });

  it('handles CRLF line endings', () => {
    const { rows } = parseHistoryCsv('date,sales\r\n2026-01-01,100\r\n2026-01-02,110\r\n');
    expect(rows).toHaveLength(2);
  });

  it('sorts rows chronologically, because lag features are positional', () => {
    const { rows } = parseHistoryCsv('date,sales\n2026-01-03,130\n2026-01-01,100\n');
    expect(rows.map((row) => row.date)).toEqual(['2026-01-01', '2026-01-03']);
  });

  it('reports skipped rows instead of dropping them silently', () => {
    const { rows, issues } = parseHistoryCsv(
      'date,sales\n2026-01-01,100\nnot-a-date,120\n2026-01-03,oops\n',
    );
    expect(rows).toHaveLength(1);
    expect(issues.map((issue) => issue.message).join(' ')).toMatch(/unreadable date/);
    expect(issues.map((issue) => issue.message).join(' ')).toMatch(/unreadable sales/);
  });

  it('accepts US slash dates', () => {
    const { rows } = parseHistoryCsv('date,sales\n03/04/2026,100\n');
    expect(rows[0]!.date).toBe('2026-03-04');
  });

  it('rejects a file with no data rows', () => {
    const { rows, issues } = parseHistoryCsv('date,sales\n');
    expect(rows).toEqual([]);
    expect(issues[0]!.severity).toBe('error');
  });
});

// ---------------------------------------------------------------------------
// History validation
// ---------------------------------------------------------------------------

describe('validateHistory', () => {
  const build = (days: number, sales = 1000) =>
    buildSampleHistory(days).map((row) => ({ ...row, sales }));

  it('blocks a history shorter than the model requires', () => {
    const result = validateHistory(build(10));
    expect(result.isValid).toBe(false);
    expect(result.issues.some((issue) => issue.severity === 'error')).toBe(true);
  });

  it('accepts exactly the minimum', () => {
    expect(validateHistory(build(MIN_HISTORY_DAYS)).isValid).toBe(true);
  });

  it('warns rather than blocks between the minimum and the recommendation', () => {
    const result = validateHistory(build(40));
    expect(result.isValid).toBe(true);
    expect(result.issues.every((issue) => issue.severity === 'warning')).toBe(true);
  });

  it('counts missing calendar days as a warning', () => {
    const rows = build(60);
    rows.splice(20, 3);
    const result = validateHistory(rows);
    expect(result.stats.missingDays).toBe(3);
    expect(result.isValid).toBe(true);
  });

  it('treats a duplicate date as an error', () => {
    const rows = build(60);
    rows[5] = { ...rows[5]!, date: rows[4]!.date };
    const result = validateHistory(rows);
    expect(result.isValid).toBe(false);
    expect(result.issues.some((issue) => issue.message.includes('duplicate'))).toBe(true);
  });

  it('refuses an all-zero history, which has no level to project', () => {
    const result = validateHistory(build(60, 0));
    expect(result.isValid).toBe(false);
  });

  it('reports the statistics the preview shows', () => {
    const rows = build(60, 500);
    const result = validateHistory(rows);
    expect(result.stats.rowCount).toBe(60);
    expect(result.stats.mean).toBe(500);
    expect(result.stats.min).toBe(500);
    expect(result.stats.max).toBe(500);
    expect(inclusiveDayCount(result.stats.startDate!, result.stats.endDate!)).toBe(60);
  });
});

describe('firstForecastDate', () => {
  it('is the day after the history ends', () => {
    const rows = buildSampleHistory(30);
    expect(firstForecastDate(rows)).toBe(addDays(rows[rows.length - 1]!.date, 1));
  });

  it('is null without history', () => {
    expect(firstForecastDate([])).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// CSV export
// ---------------------------------------------------------------------------

const FORECAST = forecastResponseSchema.parse({
  forecast_id: 'fc_abc123',
  store_id: 'CA_1',
  model: 'ensemble',
  model_version: '1',
  model_stage: 'Production',
  generated_at: '2026-10-21T09:30:00Z',
  horizon_days: 2,
  confidence_level: 0.95,
  summary: {
    total_forecast_sales: 6000,
    average_daily_sales: 3000,
    lower_bound_total: 5600,
    upper_bound_total: 6400,
  },
  forecasts: [
    { date: '2026-10-22', predicted_sales: 3000, lower_bound: 2800, upper_bound: 3200 },
    { date: '2026-10-23', predicted_sales: 3000, lower_bound: 2780, upper_bound: 3220 },
  ],
  metadata: {
    history_start_date: '2026-08-23',
    history_end_date: '2026-10-21',
    history_days: 60,
    feature_schema_version: 'v1',
  },
  warnings: ['a caveat'],
});

describe('buildForecastCsv', () => {
  it('writes the documented columns, in order', () => {
    const lines = buildForecastCsv(FORECAST).trim().split('\n');
    expect(lines[0]).toBe(
      'date,predicted_sales,lower_bound,upper_bound,model,model_version,generated_at',
    );
    expect(lines).toHaveLength(3);
    expect(lines[1]).toBe('2026-10-22,3000,2800,3200,ensemble,1,2026-10-21T09:30:00Z');
  });

  it('ends with a newline, so POSIX tools do not see a truncated file', () => {
    expect(buildForecastCsv(FORECAST).endsWith('\n')).toBe(true);
  });

  it('quotes a value containing a comma rather than gaining a column', () => {
    const csv = buildForecastCsv({ ...FORECAST, model: 'ensemble, v2' });
    expect(csv).toContain('"ensemble, v2"');
    const columnCount = csv.trim().split('\n')[1]!.split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/).length;
    expect(columnCount).toBe(7);
  });
});

describe('forecastFilename', () => {
  it('identifies the forecast without needing the file opened', () => {
    expect(forecastFilename(FORECAST)).toBe(
      'forecast_CA_1_ensemble_v1_2d_2026-10-21.csv',
    );
  });
});

// ---------------------------------------------------------------------------
// Response validation
// ---------------------------------------------------------------------------

describe('response schemas', () => {
  it('rejects a forecast with no days, which would render an empty chart', () => {
    expect(() =>
      forecastResponseSchema.parse({ ...FORECAST, forecasts: [] }),
    ).toThrow();
  });

  it('rejects a non-ISO forecast date', () => {
    expect(() =>
      forecastResponseSchema.parse({
        ...FORECAST,
        forecasts: [{ ...FORECAST.forecasts[0]!, date: '21/10/2026' }],
      }),
    ).toThrow();
  });

  it('accepts an empty model list, which is a state and not an error', () => {
    const parsed = modelListSchema.parse({
      success: true,
      models: [],
      default_model: null,
      production_available: false,
      supported_stores: [],
      warnings: ['Run the training DAG.'],
    });
    expect(parsed.models).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

describe('ApiError', () => {
  it('retries only transport failures and an unavailable upstream', () => {
    expect(new ApiError('x', { kind: 'network' }).isRetryable).toBe(true);
    expect(new ApiError('x', { kind: 'timeout' }).isRetryable).toBe(true);
    expect(new ApiError('x', { kind: 'http', status: 503 }).isRetryable).toBe(true);
    expect(new ApiError('x', { kind: 'http', status: 404 }).isRetryable).toBe(false);
    expect(new ApiError('x', { kind: 'http', status: 422 }).isRetryable).toBe(false);
    expect(new ApiError('x', { kind: 'schema' }).isRetryable).toBe(false);
  });
});

describe('friendlyMessage', () => {
  it('maps a known code to guidance', () => {
    const message = friendlyMessage(
      new ApiError('raw server text', {
        kind: 'http',
        status: 422,
        code: 'INSUFFICIENT_HISTORY',
      }),
    );
    expect(message).toContain('Add more days');
  });

  it('never echoes raw server text for an unknown code', () => {
    const message = friendlyMessage(
      new ApiError('Traceback: /usr/local/lib/mlflow failed', {
        kind: 'http',
        status: 500,
        code: 'SOMETHING_NEW',
      }),
    );
    expect(message).not.toContain('/usr/local');
    expect(message).not.toContain('Traceback');
  });

  it('explains a timeout in terms of what to do next', () => {
    expect(friendlyMessage(new ApiError('t', { kind: 'timeout' }))).toContain(
      'shorter horizon',
    );
  });

  it('says a timeout may mean the service is unreachable', () => {
    expect(friendlyMessage(new ApiError('t', { kind: 'timeout' }))).toContain(
      'briefly unavailable',
    );
  });

  it('explains a gateway error without an envelope as the service being unreachable', () => {
    for (const status of [502, 503, 504, 530]) {
      const error = new ApiError('x', { kind: 'http', status });
      expect(error.isRetryable).toBe(true);
      expect(friendlyMessage(error)).toContain('temporarily unreachable');
    }
    expect(friendlyMessage(new ApiError('x', { kind: 'http', status: 500 }))).toBe(
      'The request could not be completed.',
    );
  });
});

describe('request handling', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('parses the error envelope into a typed ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            success: false,
            error: {
              code: 'INSUFFICIENT_HISTORY',
              message: 'At least 30 days are required.',
              field: 'history',
              details: { required_days: 30 },
              request_id: 'req_deadbeef',
            },
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );

    await expect(
      createForecast({
        store_id: 'CA_1',
        model: 'ensemble',
        model_version: 'latest',
        horizon_days: 7,
        confidence_level: 0.95,
        history: [{ date: '2026-01-01', sales: 1 }],
      }),
    ).rejects.toMatchObject({
      kind: 'http',
      status: 422,
      code: 'INSUFFICIENT_HISTORY',
      field: 'history',
      requestId: 'req_deadbeef',
    });
  });

  it('retries the auth config after a gateway error', async () => {
    const config = {
      success: true,
      self_registration_enabled: true,
      password_min_length: 12,
      roles: ['VIEWER'],
      self_registration_roles: ['VIEWER'],
      default_self_registration_role: 'VIEWER',
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('bad gateway', { status: 502 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(config), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadAuthConfig([0])).resolves.toMatchObject({
      self_registration_roles: ['VIEWER'],
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('gives up on the auth config once the retries are spent', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 530 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadAuthConfig([0, 0])).rejects.toMatchObject({ status: 530 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('classifies a transport failure as retryable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await expect(getModels()).rejects.toMatchObject({ kind: 'network' });
  });

  it('fails loudly when the response does not match the schema', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ unexpected: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    await expect(getModels()).rejects.toMatchObject({ kind: 'schema' });
  });

  it('sends a request id the API can echo back', async () => {
    // Typed with the real fetch signature so the recorded call can be read
    // back; a zero-arg mock records an empty tuple.
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            success: true,
            models: [],
            default_model: null,
            production_available: false,
            supported_stores: [],
            warnings: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await getModels();
    const [, init] = fetchMock.mock.calls[0]!;
    const headers = init?.headers as Record<string, string>;
    expect(headers['X-Request-ID']).toMatch(/^req_/);
  });
});

describe('formatSales', () => {
  it('renders a dash for a value that is not a number', () => {
    expect(formatSales(Number.NaN)).toBe('—');
    expect(formatSales(null)).toBe('—');
  });
});
