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
const LIVE_ADMIN_EMAIL = process.env.LIVE_ADMIN_EMAIL ?? 'admin@example.com';
const LIVE_ADMIN_PASSWORD = process.env.LIVE_ADMIN_PASSWORD;

// Every `/v1` route requires a token, so these need credentials as well as a
// URL. Without both there is nothing meaningful to assert.
const describeLive =
  LIVE_API_URL && LIVE_ADMIN_PASSWORD ? describe : describe.skip;

describeLive('live API', () => {
  let models: ModelList;

  beforeAll(async () => {
    // `lib/api` reads this at call time, so setting it here is enough.
    process.env.NEXT_PUBLIC_API_URL = LIVE_API_URL;
    const { login } = await import('@/lib/api');
    await login(LIVE_ADMIN_EMAIL, LIVE_ADMIN_PASSWORD as string);
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

/**
 * Authentication and RBAC against a running API.
 *
 * Requires the bootstrap admin's credentials, so it is skipped unless both
 * `LIVE_API_URL` and `LIVE_ADMIN_PASSWORD` are set:
 *
 *   LIVE_API_URL=http://localhost:8000 \
 *   LIVE_ADMIN_PASSWORD='...' npm test
 */
describe('live authentication', () => {
  const password = LIVE_ADMIN_PASSWORD;
  const email = LIVE_ADMIN_EMAIL;
  const enabled = Boolean(LIVE_API_URL && password);
  const maybe = enabled ? it : it.skip;

  beforeAll(() => {
    process.env.NEXT_PUBLIC_API_URL = LIVE_API_URL;
  });

  maybe('refuses a /v1 call without a token', async () => {
    const { clearAccessToken } = await import('@/lib/auth-store');
    clearAccessToken();
    await expect(getModels()).rejects.toSatisfy(
      (error: unknown) =>
        error instanceof ApiError &&
        error.status === 401 &&
        error.code === 'NOT_AUTHENTICATED',
    );
  });

  maybe('refuses the wrong password with the same error as an unknown email', async () => {
    const { login } = await import('@/lib/api');
    const failures = await Promise.all(
      [email, 'definitely-not-registered@example.com'].map(async (address) => {
        try {
          await login(address, 'wrong-password-entirely');
          return null;
        } catch (error) {
          return error as ApiError;
        }
      }),
    );
    // Identical responses: distinguishing them would turn the login form into
    // an account-enumeration oracle.
    expect(failures[0]?.code).toBe('INVALID_CREDENTIALS');
    expect(failures[1]?.code).toBe('INVALID_CREDENTIALS');
    expect(failures[0]?.message).toBe(failures[1]?.message);
  });

  maybe('signs in and reads the permission list the UI renders from', async () => {
    const { getCurrentUser, login } = await import('@/lib/api');
    const result = await login(email, password as string);

    expect(result.user.role).toBe('ADMIN');
    expect(result.access_token).toBeTruthy();
    // Not in the body: it is an HttpOnly cookie, which is the point.
    expect(result.refresh_token ?? null).toBeNull();

    const profile = await getCurrentUser();
    expect(profile.email).toBe(email);
    expect(profile.permissions).toContain('manage_users');
    expect(profile.permissions.length).toBeGreaterThan(15);
  });

  maybe('reads monitoring, registry, and admin as an admin', async () => {
    const { getAlerts, getMonitoringSummary, getUsers } = await import('@/lib/api');

    const summary = await getMonitoringSummary({ windowDays: 30 });
    expect(['healthy', 'warning', 'degraded', 'no_data']).toContain(summary.status);
    // A monitoring summary must always be able to say "nothing yet".
    if (!summary.has_data) {
      expect(summary.no_data_message).toBeTruthy();
    }

    const alerts = await getAlerts({});
    expect(Array.isArray(alerts.alerts)).toBe(true);

    const users = await getUsers({ limit: 5 });
    expect(users.total).toBeGreaterThan(0);
  });
});
