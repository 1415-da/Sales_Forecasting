'use client';

/**
 * Scenario Planner.
 *
 * This file owns the left setup column only. The right column is a placeholder
 * until the results panel lands.
 *
 * Wiring note: the store list and the history series are placeholders. There is
 * no store-directory or history endpoint yet, and `POST /v1/scenario/compare`
 * needs at least 7 days of history, so running a scenario will report a history
 * error until that data source exists.
 */

import { useCallback, useMemo, useState } from 'react';

import { ModelSupportCard } from '@/components/scenario/model-support-card';
import { RunScenarioCard } from '@/components/scenario/run-scenario-card';
import { ScenarioAssumptionsCard } from '@/components/scenario/scenario-assumptions-card';
import { ScenarioContextCard } from '@/components/scenario/scenario-context-card';
import { ScenarioPageHeader } from '@/components/scenario/scenario-page-header';
import { ScenarioTypeSelector } from '@/components/scenario/scenario-type-selector';
import { useScenarioCapabilities } from '@/hooks/use-scenario-capabilities';
import { useScenarioForecast } from '@/hooks/use-scenario-forecast';
import { useScenarioForm } from '@/hooks/use-scenario-form';
import type { HistoryPoint, ScenarioType } from '@/lib/types/scenario';

/** Placeholder store list; replace with the store-directory endpoint. */
const STORE_OPTIONS = [
  { id: 'CA_1', label: 'CA_1 — California 1' },
  { id: 'CA_2', label: 'CA_2 — California 2' },
  { id: 'TX_1', label: 'TX_1 — Texas 1' },
  { id: 'WI_1', label: 'WI_1 — Wisconsin 1' },
];

const MODEL_OPTIONS = [{ version: 'latest', label: 'Latest production model' }];

export default function ScenarioPlannerPage() {
  const [history] = useState<HistoryPoint[]>([]);
  const [modelVersion, setPendingModelVersion] = useState('latest');

  const capabilities = useScenarioCapabilities({ modelVersion });
  const forecast = useScenarioForecast();

  const availableFeatures = capabilities.data?.features ?? [];

  // Only gate on support once capabilities have actually loaded; before that,
  // nothing is known and the form should not block the user.
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

  // Keep the capabilities query key in step with the selected model.
  const handleModelChange = useCallback(
    (value: string) => {
      setPendingModelVersion(value);
      form.setModelVersion(value);
    },
    [form],
  );

  const scenarioSupported = isScenarioTypeSupported(form.state.scenarioType);
  const errors = form.visibleErrors;
  const canRun =
    form.isValid && (!capabilities.isSuccess || scenarioSupported) && !forecast.isPending;

  const handleRun = useCallback(() => {
    // One key per distinct request: a retry of the same inputs replays the
    // original result instead of paying for another inference.
    const idempotencyKey = form.claimIdempotencyKey();
    forecast
      .mutateAsync({ request: form.request, idempotencyKey })
      .then(() => form.markRunSucceeded())
      .catch(() => {
        /* surfaced through forecast.errorPresentation */
      });
  }, [forecast, form]);

  const handleReset = useCallback(() => {
    form.reset();
    forecast.reset();
  }, [form, forecast]);

  const handleExport = useCallback(() => {
    const comparison = forecast.data;
    if (!comparison) return;
    const blob = new Blob([JSON.stringify(comparison, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${comparison.scenario_id}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }, [forecast.data]);

  const capabilityList = useMemo(
    () => capabilities.data?.capabilities ?? [],
    [capabilities.data],
  );

  return (
    <main className="mx-auto w-full max-w-[1400px] space-y-5 p-4 pb-24 lg:p-6 lg:pb-6">
      <ScenarioPageHeader
        onReset={handleReset}
        onExport={handleExport}
        hasResult={Boolean(forecast.data)}
        isResetDisabled={forecast.isPending}
      />

      <div className="flex flex-col gap-5 lg:flex-row lg:items-start">
        {/* Setup column: fixed ~380px on desktop, full width when stacked. */}
        <div className="w-full space-y-4 lg:w-[380px] lg:shrink-0">
          <ScenarioContextCard
            stores={STORE_OPTIONS}
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
            /* Stage and data freshness are not on the capabilities response;
               they arrive with a comparison (model_stage and dataset_lineage),
               so they read as unknown until the first run rather than being
               invented here. */
            modelStage={forecast.data?.model_stage ?? null}
            dataFreshness={forecast.data?.dataset_lineage?.train_end_date ?? null}
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

          {/* Desktop: inline with the column. */}
          <RunScenarioCard
            className="hidden lg:block"
            onRun={handleRun}
            onRetry={handleRun}
            isRunning={forecast.isPending}
            resultStatus={form.resultStatus}
            canRun={canRun}
            errors={errors}
            failure={forecast.errorPresentation}
          />
        </div>

        {/* Results column: implemented in a later task. */}
        <section
          aria-label="Scenario results"
          className="min-h-[320px] flex-1 rounded-lg border border-dashed p-6"
        >
          <p className="text-sm text-muted-foreground">
            Results appear here once a scenario has been run.
          </p>
        </section>
      </div>

      {/* Mobile: sticky action bar, so Run is always reachable. */}
      <div className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 p-3 backdrop-blur lg:hidden">
        <RunScenarioCard
          className="border-0 shadow-none"
          onRun={handleRun}
          onRetry={handleRun}
          isRunning={forecast.isPending}
          resultStatus={form.resultStatus}
          canRun={canRun}
          errors={errors}
          failure={forecast.errorPresentation}
        />
      </div>
    </main>
  );
}
