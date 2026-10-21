'use client';

import { LineChart, ServerCrash } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { RequireAuth } from '@/components/auth/require-auth';
import { ExportButton } from '@/components/forecast/export-button';
import { ForecastChart } from '@/components/forecast/forecast-chart';
import {
  ForecastControls,
  type ForecastControlsValue,
} from '@/components/forecast/forecast-controls';
import { ForecastKpiCards } from '@/components/forecast/forecast-kpi-cards';
import { ForecastNotes } from '@/components/forecast/forecast-notes';
import { ForecastTable } from '@/components/forecast/forecast-table';
import { HistoryInput } from '@/components/forecast/history-input';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { ForecastResultsSkeleton, KpiSkeleton } from '@/components/shared/loading-skeleton';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/use-auth';
import { useForecast } from '@/hooks/use-forecast';
import { useModels } from '@/hooks/use-models';
import { NO_PERMISSION_MESSAGE, PERMISSIONS } from '@/lib/rbac';
import { MIN_HISTORY_DAYS, validateHistory } from '@/lib/sample-history';
import type { HistoryPoint, HistorySource } from '@/lib/types';

const INITIAL_CONTROLS: ForecastControlsValue = {
  storeId: '',
  model: 'ensemble',
  modelVersion: 'latest',
  horizonDays: 30,
  confidenceLevel: 0.95,
};

/**
 * The Forecast Console.
 *
 * Setup on the left, results on the right. State is held here rather than in a
 * store: it is one screen's worth of form state with a single consumer, and a
 * store would add indirection without removing anything.
 */
export default function ForecastPage() {
  return (
    <RequireAuth permission={PERMISSIONS.viewForecast}>
      <ForecastConsole />
    </RequireAuth>
  );
}

function ForecastConsole() {
  const { can } = useAuth();
  const canGenerate = can(PERMISSIONS.generateForecast);
  const canExport = can(PERMISSIONS.exportForecast);
  const models = useModels();
  const forecastRun = useForecast();

  const [controls, setControls] = useState<ForecastControlsValue>(INITIAL_CONTROLS);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [historySource, setHistorySource] = useState<HistorySource | null>(null);

  // Default the selectors once the registry answers. Guarded on the current
  // value so a user's choice is never overwritten by a background refetch.
  useEffect(() => {
    const preferred = models.defaultModel;
    if (!preferred) return;
    setControls((current) => ({
      ...current,
      model: current.model === INITIAL_CONTROLS.model ? preferred.model : current.model,
      storeId:
        current.storeId === ''
          ? (preferred.supported_stores[0] ?? models.stores[0] ?? '')
          : current.storeId,
    }));
  }, [models.defaultModel, models.stores]);

  const validation = useMemo(() => validateHistory(history), [history]);

  const blockedReason = (() => {
    // Checked first: a VIEWER should be told their role is the reason, not
    // sent to load history and only then find out.
    if (!canGenerate)
      return `${NO_PERMISSION_MESSAGE} Generating a forecast requires a Business User role or above.`;
    if (models.isLoading) return 'Loading models…';
    if (models.isError) return 'The API is unreachable.';
    if (models.isEmpty) return 'No model has been trained yet.';
    if (!models.data?.production_available)
      return 'No Production model is available to serve a forecast.';
    if (history.length === 0) return 'Load historical sales to continue.';
    if (!validation.isValid)
      return (
        validation.issues.find((issue) => issue.severity === 'error')?.message ??
        `At least ${MIN_HISTORY_DAYS} days of history are required.`
      );
    if (!controls.storeId) return 'Select a store.';
    return undefined;
  })();

  const submit = () => {
    if (blockedReason) return;
    forecastRun.generate({
      store_id: controls.storeId,
      model: controls.model,
      model_version: controls.modelVersion,
      horizon_days: controls.horizonDays,
      confidence_level: controls.confidenceLevel,
      history: history.map((point) => ({ ...point, store_id: controls.storeId })),
    });
  };

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Forecast Console</h1>
        <p className="text-sm text-muted-foreground">
          Generate a multi-day sales projection from the Production model.
        </p>
      </div>

      {models.isError ? (
        <ErrorState error={models.error} onRetry={() => void models.refetch()} />
      ) : null}

      {models.data?.warnings.length ? (
        <ul className="space-y-1 rounded-lg border border-amber-500/30 bg-amber-500/5 p-4">
          {models.data.warnings.map((warning) => (
            <li key={warning} className="text-sm text-amber-500/90">
              {warning}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(340px,400px)_1fr]">
        <div className="space-y-6">
          <ForecastControls
            value={controls}
            onChange={(patch) => setControls((current) => ({ ...current, ...patch }))}
            models={models.data?.models ?? []}
            stores={
              models.data?.models.find((model) => model.model === controls.model)
                ?.supported_stores ?? models.stores
            }
            isLoadingModels={models.isLoading}
            onSubmit={submit}
            isSubmitting={forecastRun.isPending}
            {...(blockedReason ? { blockedReason } : {})}
          />

          <HistoryInput
            rows={history}
            source={historySource}
            validation={validation}
            onChange={(rows, source) => {
              setHistory(rows);
              setHistorySource(source);
            }}
            onClear={() => {
              setHistory([]);
              setHistorySource(null);
              forecastRun.reset();
            }}
          />
        </div>

        <div className="min-w-0 space-y-6">
          {forecastRun.error ? (
            <ErrorState error={forecastRun.error} onRetry={submit} />
          ) : null}

          {forecastRun.isPending && !forecastRun.forecast ? (
            <ForecastResultsSkeleton />
          ) : null}

          {forecastRun.forecast ? (
            <>
              {forecastRun.isPending ? <KpiSkeleton /> : null}
              {!forecastRun.isPending ? (
                <ForecastKpiCards forecast={forecastRun.forecast} />
              ) : null}

              {canExport ? (
                <div className="flex items-center justify-end">
                  <ExportButton forecast={forecastRun.forecast} />
                </div>
              ) : null}

              <ForecastChart
                forecast={forecastRun.forecast}
                history={forecastRun.submitted?.history ?? history}
              />
              <ForecastTable forecast={forecastRun.forecast} />
              <ForecastNotes forecast={forecastRun.forecast} />
            </>
          ) : null}

          {!forecastRun.forecast && !forecastRun.isPending && !forecastRun.error ? (
            models.isError ? (
              <EmptyState
                icon={<ServerCrash aria-hidden="true" className="h-8 w-8" />}
                title="The forecast API is unreachable"
                description="Start the API service and reload. The console talks to nothing else."
                action={
                  <Button size="sm" variant="outline" onClick={() => void models.refetch()}>
                    Retry
                  </Button>
                }
              />
            ) : models.isLoading ? (
              <ForecastResultsSkeleton />
            ) : models.isEmpty || !models.data?.production_available ? (
              <EmptyState
                icon={<ServerCrash aria-hidden="true" className="h-8 w-8" />}
                title="No Production model available"
                description="Trigger the sales_forecast_training DAG in Airflow. It trains, registers, and promotes a model; this page picks it up automatically."
              />
            ) : (
              <EmptyState
                icon={<LineChart aria-hidden="true" className="h-8 w-8" />}
                title="No forecast yet"
                description="Load historical sales on the left, choose a horizon, and select Generate Forecast."
              />
            )
          ) : null}
        </div>
      </div>
    </div>
  );
}
