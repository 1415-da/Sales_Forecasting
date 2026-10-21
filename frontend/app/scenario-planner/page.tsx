'use client';

/**
 * Scenario Planner.
 *
 * Owns the full flow: validate locally, claim an idempotency key, call
 * `POST /v1/scenario/compare`, then render the comparison, a partial-scenario
 * warning, an error state, or the empty state.
 *
 * Store list and history are both real now. Stores come from
 * `GET /v1/models` -- the identifiers the selected model's encoder actually
 * recognises, so an option the model would reject is never offered. History
 * comes from the same seeded demo generator the Forecast Console uses, which
 * keeps a scenario reproducible and clearly labelled as demo input.
 */

import { useCallback, useMemo, useState } from 'react';
import { AlertTriangle, Layers, Undo2 } from 'lucide-react';

import { RequireAuth } from '@/components/auth/require-auth';
import { ModelSupportCard } from '@/components/scenario/model-support-card';
import { RunScenarioCard } from '@/components/scenario/run-scenario-card';
import { ScenarioComparisonChartCardSkeleton } from '@/components/scenario/scenario-comparison-chart-card';
import { ScenarioEmptyState } from '@/components/scenario/scenario-empty-state';
import { ScenarioErrorState } from '@/components/scenario/scenario-error-state';
import { ScenarioExportActions } from '@/components/scenario/scenario-export-actions';
import {
  ScenarioImpactCards,
  ScenarioImpactCardsSkeleton,
} from '@/components/scenario/scenario-impact-cards';
import { ScenarioImpactTabsCard } from '@/components/scenario/scenario-impact-tabs-card';
import {
  ScenarioInsightCard,
  ScenarioInsightCardSkeleton,
} from '@/components/scenario/scenario-insight-card';
import { ScenarioLimitationsCard } from '@/components/scenario/scenario-limitations-card';
import { ScenarioPageHeader } from '@/components/scenario/scenario-page-header';
import { ScenarioRollbackDialog } from '@/components/scenario/scenario-rollback-dialog';
import { ScenarioTypeSelector } from '@/components/scenario/scenario-type-selector';
import { ScenarioAssumptionsCard } from '@/components/scenario/scenario-assumptions-card';
import { ScenarioContextCard } from '@/components/scenario/scenario-context-card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/ui/toast';
import { useAuth } from '@/hooks/use-auth';
import { useModels } from '@/hooks/use-models';
import {
  RECOMMENDED_HISTORY_DAYS,
  buildSampleHistory,
} from '@/lib/sample-history';
import { NO_PERMISSION_MESSAGE, PERMISSIONS } from '@/lib/rbac';
import { useScenarioCapabilities } from '@/hooks/use-scenario-capabilities';
import { useScenarioForecast } from '@/hooks/use-scenario-forecast';
import { useScenarioForm } from '@/hooks/use-scenario-form';
import { useScenarioRollback } from '@/hooks/use-scenario-rollback';
import type {
  HistoryPoint,
  ScenarioComparisonResponse,
  ScenarioType,
} from '@/lib/types/scenario';
import { humanizeIdentifier } from '@/lib/utils/format';
import { buildComparisonCsv, comparisonFileName } from '@/lib/utils/scenario-results';

/** Placeholder store list; replace with the store-directory endpoint. */
const STORE_OPTIONS = [
  { id: 'CA_1', label: 'CA_1 — California 1' },
  { id: 'CA_2', label: 'CA_2 — California 2' },
  { id: 'TX_1', label: 'TX_1 — Texas 1' },
  { id: 'WI_1', label: 'WI_1 — Wisconsin 1' },
];

const MODEL_OPTIONS = [{ version: 'latest', label: 'Latest production model' }];

/** Banner for a scenario that ran, but could not apply every override. */
function PartialScenarioBanner({
  comparison,
}: {
  comparison: ScenarioComparisonResponse;
}) {
  if (comparison.unsupported_overrides.length === 0) return null;

  return (
    <Alert variant="warning">
      <AlertTriangle aria-hidden="true" className="h-4 w-4" />
      <AlertTitle className="text-sm">Partial scenario</AlertTitle>
      <AlertDescription className="space-y-1.5 text-xs">
        <p>
          The comparison ran, but some assumptions could not be applied to this
          model. Treat the impact as a lower bound on what you described.
        </p>
        <ul className="space-y-0.5">
          {comparison.unsupported_overrides.map((override) => (
            <li key={override.feature_name}>
              <span className="font-medium">
                {humanizeIdentifier(override.feature_name)}
              </span>
              : {override.reason}
            </li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  );
}

export default function ScenarioPlannerPage() {
  return (
    <RequireAuth permission={PERMISSIONS.viewScenario}>
      <ScenarioPlanner />
    </RequireAuth>
  );
}

function ScenarioPlanner() {
  const { can } = useAuth();
  const isPermittedToRun = can(PERMISSIONS.runScenario);
  const models = useModels();

  // The identifiers the selected model's encoder recognises. Offering anything
  // else would produce a confident forecast for the wrong store, which the API
  // refuses anyway.
  const storeOptions = useMemo(
    () =>
      models.stores.length > 0
        ? models.stores.map((store) => ({ id: store, label: store }))
        : STORE_OPTIONS,
    [models.stores],
  );

  // The same seeded demo series the Forecast Console uses: a scenario a user
  // shows a colleague should be reproducible, and a random series would not be.
  const history = useMemo<HistoryPoint[]>(
    () =>
      buildSampleHistory(RECOMMENDED_HISTORY_DAYS).map((point) => ({
        date: point.date,
        sales: point.sales,
      })),
    [],
  );
  const [modelVersion, setPendingModelVersion] = useState('latest');
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [rolledBackIds, setRolledBackIds] = useState<string[]>([]);

  const { toast } = useToast();
  const capabilities = useScenarioCapabilities({ modelVersion });
  const forecast = useScenarioForecast();
  const rollback = useScenarioRollback();

  const availableFeatures = capabilities.data?.features ?? [];

  // Only gate on support once capabilities have loaded; before that nothing is
  // known and the form should not block the user.
  const isScenarioTypeSupported = useCallback(
    (type: ScenarioType) =>
      capabilities.isSuccess ? capabilities.isScenarioSupported(type) : true,
    [capabilities.isSuccess, capabilities.isScenarioSupported],
  );

  const form = useScenarioForm({
    modelVersion,
    history,
    isScenarioTypeSupported,
    availableFeatures: capabilities.isSuccess ? availableFeatures : undefined,
  });

  const handleModelChange = useCallback(
    (value: string) => {
      setPendingModelVersion(value);
      form.setModelVersion(value);
    },
    [form],
  );

  /**
   * The comparison on screen.
   *
   * Falls back to the retained result while a re-run is in flight, so updating
   * a scenario does not blank the panel the user is reading.
   */
  const comparison = forecast.data ?? forecast.lastComparison;
  const isRolledBack = Boolean(
    comparison && rolledBackIds.includes(comparison.scenario_id),
  );

  /**
   * Reuse the baseline across runs.
   *
   * `resultStatus` is 'none' exactly when the store, model, or horizon changed
   * -- a different baseline question -- so reuse is skipped there and applied
   * when only the assumptions moved.
   */
  const reusableBaselineId =
    form.resultStatus !== 'none' && comparison ? comparison.baseline_forecast_id : null;

  const scenarioSupported = isScenarioTypeSupported(form.state.scenarioType);
  const errors = form.visibleErrors;
  const canRun =
    isPermittedToRun &&
    form.isValid &&
    scenarioSupported &&
    !forecast.isPending &&
    !isRolledBack;

  const handleRun = useCallback(() => {
    // One key per distinct request: an automatic retry or a double-click
    // replays the original result instead of paying for another inference.
    const idempotencyKey = form.claimIdempotencyKey();
    forecast
      .mutateAsync({
        request: { ...form.request, baseline_forecast_id: reusableBaselineId },
        idempotencyKey,
      })
      .then(() => form.markRunSucceeded())
      .catch(() => {
        /* surfaced through forecast.errorPresentation */
      });
  }, [forecast, form, reusableBaselineId]);

  const handleReset = useCallback(() => {
    form.reset();
    forecast.reset();
    forecast.clearLastComparison();
    setRolledBackIds([]);
  }, [form, forecast]);

  const handleExport = useCallback(() => {
    if (!comparison || isRolledBack) return;
    const blob = new Blob([buildComparisonCsv(comparison)], {
      type: 'text/csv;charset=utf-8;',
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = comparisonFileName(comparison, 'csv');
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }, [comparison, isRolledBack]);

  const handleRollbackConfirm = useCallback(
    (reason: string) => {
      if (!comparison) return;
      rollback
        .rollback(comparison.scenario_id, reason)
        .then(() => {
          setRolledBackIds((current) => [...current, comparison.scenario_id]);
          setRollbackOpen(false);
          toast({
            title: 'Scenario rolled back. Audit history preserved.',
            variant: 'success',
          });
        })
        .catch(() => {
          /* surfaced inside the dialog */
        });
    },
    [comparison, rollback, toast],
  );

  const focusModelSelector = useCallback(() => {
    document.getElementById('scenario-model')?.focus();
  }, []);

  const capabilityList = useMemo(
    () => capabilities.data?.capabilities ?? [],
    [capabilities.data],
  );

  // A first run shows skeletons; an update keeps the previous result on screen.
  const showSkeletons = forecast.isPending && !comparison;
  const showError = Boolean(forecast.errorPresentation) && !forecast.isPending;

  const runCardProps = {
    onRun: handleRun,
    onRetry: handleRun,
    isRunning: forecast.isPending,
    isRetrying: forecast.isRetrying,
    resultStatus: form.resultStatus,
    canRun,
    errors,
    failure: forecast.errorPresentation,
    isReusingBaseline: Boolean(reusableBaselineId),
    isRolledBack,
  };

  return (
    <main className="mx-auto w-full max-w-[1400px] space-y-5 p-4 pb-40 lg:p-6 lg:pb-6">
      <ScenarioPageHeader
        onReset={handleReset}
        onExport={handleExport}
        hasResult={Boolean(comparison) && !isRolledBack}
        isResetDisabled={forecast.isPending}
      />

      <div className="flex flex-col gap-5 lg:flex-row lg:items-start">
        <div className="w-full space-y-4 lg:w-[380px] lg:shrink-0">
          <ScenarioContextCard
            stores={storeOptions}
            models={MODEL_OPTIONS}
            storeId={form.state.storeId}
            horizonDays={form.state.horizonDays}
            modelVersion={form.state.modelVersion}
            confidenceLevel={form.state.confidenceLevel}
            onStoreChange={form.setStoreId}
            onHorizonChange={form.setHorizonDays}
            onModelChange={handleModelChange}
            onConfidenceChange={form.setConfidenceLevel}
            errors={errors}
            resolvedModelVersion={capabilities.data?.model_version ?? null}
            modelStage={comparison?.model_stage ?? null}
            dataFreshness={comparison?.dataset_lineage?.train_end_date ?? null}
            isLoading={capabilities.isLoading}
          />

          <ScenarioTypeSelector
            value={form.state.scenarioType}
            onChange={form.setScenarioType}
            supportedTypes={
              capabilities.isSuccess ? capabilities.supportedScenarioTypes : undefined
            }
            unsupportedReason={capabilities.unsupportedReason}
            isLoading={capabilities.isLoading}
          />

          <ScenarioAssumptionsCard
            scenarioType={form.state.scenarioType}
            assumptions={form.state.assumptions}
            onChange={form.setAssumption}
            onBlur={form.touch}
            errors={errors}
            availableFeatures={availableFeatures}
            maxDurationDays={form.state.horizonDays}
          />

          <ModelSupportCard
            capabilities={capabilityList}
            selectedType={form.state.scenarioType}
            isLoading={capabilities.isLoading}
            errorMessage={capabilities.error ? capabilities.error.message : null}
            warnings={capabilities.data?.warnings ?? []}
          />

          <RunScenarioCard className="hidden lg:block" {...runCardProps} />
        </div>

        <section aria-label="Scenario results" className="min-w-0 flex-1 space-y-4">
          {showSkeletons ? (
            <>
              <ScenarioImpactCardsSkeleton />
              <ScenarioComparisonChartCardSkeleton />
              <ScenarioInsightCardSkeleton />
            </>
          ) : showError ? (
            <ScenarioErrorState
              presentation={forecast.errorPresentation!}
              onRetry={handleRun}
              isRetrying={forecast.isPending}
              onChangeModel={focusModelSelector}
            />
          ) : comparison ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  {comparison.baseline_reused ? (
                    <Badge variant="secondary" className="gap-1">
                      <Layers aria-hidden="true" className="h-3 w-3" />
                      Reusing baseline forecast
                    </Badge>
                  ) : null}
                  {isRolledBack ? (
                    <Badge variant="destructive">Rolled back</Badge>
                  ) : form.isStale ? (
                    <Badge variant="warning">Stale</Badge>
                  ) : null}
                  {forecast.isPending ? (
                    <span role="status" className="text-xs text-muted-foreground">
                      Updating…
                    </span>
                  ) : null}
                </div>

                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setRollbackOpen(true)}
                  disabled={isRolledBack || forecast.isPending}
                  title={
                    isRolledBack
                      ? 'This scenario has already been rolled back'
                      : 'Roll back this scenario'
                  }
                >
                  <Undo2 aria-hidden="true" className="h-4 w-4" />
                  Rollback scenario
                </Button>
              </div>

              {form.isStale && !isRolledBack ? (
                <p
                  role="status"
                  className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200"
                >
                  These results reflect the previous assumptions. Update the scenario
                  to refresh them.
                </p>
              ) : null}

              {isRolledBack ? (
                <Alert variant="destructive">
                  <Undo2 aria-hidden="true" className="h-4 w-4" />
                  <AlertDescription className="text-xs">
                    This scenario was rolled back. Its audit history is preserved and
                    the baseline forecast is unaffected.
                  </AlertDescription>
                </Alert>
              ) : null}

              <PartialScenarioBanner comparison={comparison} />

              <ScenarioImpactCards comparison={comparison} />
              <ScenarioImpactTabsCard comparison={comparison} history={history} />
              <ScenarioInsightCard comparison={comparison} />
              <ScenarioLimitationsCard comparison={comparison} />
              <ScenarioExportActions
                comparison={comparison}
                isRolledBack={isRolledBack}
                isStale={form.isStale}
              />
            </>
          ) : (
            <ScenarioEmptyState
              isSampleAvailable={false}
              sampleUnavailableReason={undefined}
            />
          )}
        </section>
      </div>

      <div className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 p-3 backdrop-blur lg:hidden">
        <RunScenarioCard className="border-0 shadow-none" {...runCardProps} />
      </div>

      {comparison ? (
        <ScenarioRollbackDialog
          open={rollbackOpen}
          onOpenChange={setRollbackOpen}
          scenarioId={comparison.scenario_id}
          onConfirm={handleRollbackConfirm}
          isPending={rollback.isPending}
          errorMessage={rollback.errorPresentation?.message ?? null}
        />
      ) : null}
    </main>
  );
}
