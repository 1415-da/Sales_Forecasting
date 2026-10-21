/**
 * Opt-in smoke test against a running API.
 *
 * Skipped unless `LIVE_API_URL` is set, so the default suite stays hermetic:
 *
 *   LIVE_API_URL=http://localhost:8000 npm test
 *
 * What it adds over the mocked tests is the one thing those cannot check --
 * that the contract the frontend parses is the contract the server actually
 * sends. It walks the real user path: discover models, build demo history,
 * validate it, request 7-day and 30-day forecasts, and build the CSV.
 */
import { beforeAll, describe, expect, it } from 'vitest';

import { buildForecastCsv, forecastFilename } from '@/components/forecast/export-button';
import { ApiError, createForecast, getHealth, getModels } from '@/lib/api';
import { buildSampleHistory, validateHistory } from '@/lib/sample-history';
import type { ModelList } from '@/lib/types';

const LIVE_API_URL = process.env.LIVE_API_URL;
const describeLive = LIVE_API_URL ? describe : describe.skip;

describeLive('live API', () => {
  let models: ModelList;

  beforeAll(() => {
    // `lib/api` reads this at call time, so setting it here is enough.
    process.env.NEXT_PUBLIC_API_URL = LIVE_API_URL;
  });

  it('reports health and a Production model', async () => {
    const health = await getHealth();
    expect(health.success).toBe(true);
    expect(health.components.api?.status).toBe('ok');
    expect(health.components.mlflow?.status).toBe('ok');
    expect(health.production_model?.stage).toBe('Production');
  });

  it('lists the serving model versions', async () => {
    models = await getModels();
    expect(models.production_available).toBe(true);
    expect(models.models.length).toBeGreaterThan(0);
    expect(models.supported_stores.length).toBeGreaterThan(0);
    const serving = models.models.find((model) => model.is_default) ?? models.models[0]!;
    expect(serving.feature_count).toBeGreaterThan(0);
    expect(serving.run_id).toBeTruthy();
  });

  it.each([7, 30])('generates a %i-day forecast the UI can render', async (horizon) => {
    models ??= await getModels();
    const serving = models.models.find((model) => model.is_default) ?? models.models[0]!;
    const store = serving.supported_stores[0] ?? models.supported_stores[0]!;

    const history = buildSampleHistory(60);
    expect(validateHistory(history).isValid).toBe(true);

    const forecast = await createForecast({
      store_id: store,
      model: serving.model,
      model_version: 'latest',
      horizon_days: horizon,
      confidence_level: 0.95,
      history,
    });

    expect(forecast.forecasts).toHaveLength(horizon);
    expect(forecast.model_stage).toBe('Production');
    expect(forecast.metadata.history_days).toBe(60);

    // The first forecast day is the day after history ends.
    expect(forecast.forecasts[0]!.date).toBe(
      new Date(
        new Date(`${history[history.length - 1]!.date}T00:00:00Z`).getTime() + 86_400_000,
      )
        .toISOString()
        .slice(0, 10),
    );

    // Every point is inside its own band, and the band only widens.
    let previousWidth = -1;
    for (const point of forecast.forecasts) {
      expect(point.lower_bound).toBeLessThanOrEqual(point.predicted_sales);
      expect(point.predicted_sales).toBeLessThanOrEqual(point.upper_bound);
      expect(point.lower_bound).toBeGreaterThanOrEqual(0);
      const width = point.upper_bound - point.lower_bound;
      expect(width).toBeGreaterThanOrEqual(previousWidth);
      previousWidth = width;
    }

    // The summary must equal the series it summarises.
    const total = forecast.forecasts.reduce(
      (sum, point) => sum + point.predicted_sales,
      0,
    );
    expect(forecast.summary.total_forecast_sales).toBeCloseTo(total, 1);

    // The caveats are never empty: the trained model's reliance on same-day
    // columns is disclosed on every response.
    expect(forecast.warnings.length).toBeGreaterThan(0);

    const csv = buildForecastCsv(forecast);
    expect(csv.trim().split('\n')).toHaveLength(horizon + 1);
    expect(forecastFilename(forecast)).toContain(store);
  }, 70_000);

  it('rejects too little history with actionable guidance', async () => {
    models ??= await getModels();
    const serving = models.models.find((model) => model.is_default) ?? models.models[0]!;
    const store = serving.supported_stores[0] ?? models.supported_stores[0]!;

    await expect(
      createForecast({
        store_id: store,
        model: serving.model,
        model_version: 'latest',
        horizon_days: 7,
        confidence_level: 0.95,
        history: buildSampleHistory(5),
      }),
    ).rejects.toSatisfy(
      (error: unknown) =>
        error instanceof ApiError &&
        error.status === 422 &&
        error.code === 'INSUFFICIENT_HISTORY' &&
        error.isRetryable === false,
    );
  });

  it('rejects a store the model does not know', async () => {
    models ??= await getModels();
    const serving = models.models.find((model) => model.is_default) ?? models.models[0]!;

    await expect(
      createForecast({
        store_id: 'ZZ_NOT_A_STORE',
        model: serving.model,
        model_version: 'latest',
        horizon_days: 7,
        confidence_level: 0.95,
        history: buildSampleHistory(60),
      }),
    ).rejects.toSatisfy(
      (error: unknown) => error instanceof ApiError && error.status === 422,
    );
  });
});
