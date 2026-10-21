'use client';

import { Archive, RotateCcw, Upload } from 'lucide-react';
import { useMemo, useState } from 'react';

import { RequireAuth } from '@/components/auth/require-auth';
import { ComparisonPanel } from '@/components/registry/comparison-panel';
import { DetailDrawer } from '@/components/registry/detail-drawer';
import { ProductionModelCard } from '@/components/registry/production-card';
import {
  RegistryActionDialog,
  type RegistryActionKind,
} from '@/components/registry/registry-action-dialog';
import { VersionTable } from '@/components/registry/version-table';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { KpiSkeleton, TableSkeleton } from '@/components/shared/loading-skeleton';
import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/use-auth';
import { useModels } from '@/hooks/use-models';
import {
  useModelArtifacts,
  useModelComparison,
  useModelLineage,
  useRegistryMutation,
} from '@/hooks/use-registry';
import { NO_PERMISSION_MESSAGE, PERMISSIONS } from '@/lib/rbac';
import type { ModelInfo } from '@/lib/types';

/**
 * Model Registry.
 *
 * Reads are open to every role; mutations require the matching capability and
 * go through a confirmation that shows the before/after. The API enforces the
 * same rules, so hiding a button here is a courtesy rather than the control.
 */
export default function ModelsPage() {
  return (
    <RequireAuth permission={PERMISSIONS.viewModelRegistry}>
      <ModelRegistry />
    </RequireAuth>
  );
}

function ModelRegistry() {
  const { can } = useAuth();
  const models = useModels();
  const mutation = useRegistryMutation();

  const [detailTarget, setDetailTarget] = useState<ModelInfo | null>(null);
  const [actionTarget, setActionTarget] = useState<
    { model: ModelInfo; action: RegistryActionKind } | null
  >(null);
  const [versionA, setVersionA] = useState('');
  const [versionB, setVersionB] = useState('');
  const [compareModel, setCompareModel] = useState<string | undefined>();
  const [lastResult, setLastResult] = useState<string | null>(null);

  const canDetail = can(PERMISSIONS.viewModelRegistryDetail);
  const canPromote = can(PERMISSIONS.promoteModel);
  const canArchive = can(PERMISSIONS.archiveModel);
  const canRollback = can(PERMISSIONS.rollbackModel);
  const canMutate = canPromote || canArchive || canRollback;

  const production = models.productionModels[0];

  // Every version the registry knows about, for the comparison selectors.
  const versions = useMemo(
    () =>
      Array.from(
        new Set((models.data?.models ?? []).map((model) => model.version)),
      ).sort((left, right) => Number(right) - Number(left)),
    [models.data],
  );

  const comparison = useModelComparison({
    ...(versionA ? { versionA } : {}),
    ...(versionB ? { versionB } : {}),
    ...(compareModel ? { model: compareModel } : {}),
  });

  const lineage = useModelLineage(
    detailTarget?.version,
    detailTarget?.model,
    Boolean(detailTarget) && canDetail,
  );
  const artifacts = useModelArtifacts(
    detailTarget?.version,
    detailTarget?.model,
    Boolean(detailTarget) && canDetail,
  );

  const applyAction = (reason: string) => {
    if (!actionTarget) return;
    mutation.mutate(
      {
        action: actionTarget.action,
        version: actionTarget.model.version,
        model: actionTarget.model.model,
        ...(reason ? { reason } : {}),
      },
      {
        onSuccess: (result) => {
          setLastResult(result.message);
          setActionTarget(null);
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">Model Registry</h1>
        <p className="text-sm text-muted-foreground">
          Registered versions, what is serving, and how to change it.
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

      {lastResult ? (
        <div className="flex items-start justify-between gap-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4">
          <p className="text-sm">{lastResult}</p>
          <Button size="sm" variant="ghost" onClick={() => setLastResult(null)}>
            Dismiss
          </Button>
        </div>
      ) : null}

      {models.isLoading ? (
        <>
          <KpiSkeleton count={2} />
          <TableSkeleton rows={3} />
        </>
      ) : models.isEmpty ? (
        <EmptyState
          title="No registered models"
          description="Trigger the sales_forecast_training DAG in Airflow. It trains, registers, and promotes a model; this page picks it up automatically."
        />
      ) : (
        <>
          <ProductionModelCard model={production} />

          <VersionTable
            models={models.data?.models ?? []}
            canViewDetail={canDetail}
            onInspect={(model) => setDetailTarget(model)}
            onCompare={(model) => {
              setCompareModel(model.model);
              setVersionB(model.version);
              setVersionA(production?.version ?? '');
            }}
          />

          {canMutate ? (
            <div className="space-y-3 rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold">Registry actions</h2>
                <StatusBadge tone="warning">Audited</StatusBadge>
              </div>
              <p className="text-sm text-muted-foreground">
                Promoting changes the model behind every forecast. Each action
                asks for confirmation and records who did it and why.
              </p>
              <div className="flex flex-wrap gap-2">
                {(models.data?.models ?? []).map((model) => (
                  <div
                    key={`${model.model}-actions`}
                    className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2"
                  >
                    <span className="text-sm font-medium">
                      {model.label} v{model.version}
                    </span>
                    {canPromote && !model.is_production ? (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          setActionTarget({ model, action: 'promote' })
                        }
                      >
                        <Upload aria-hidden="true" className="h-3.5 w-3.5" />
                        Promote
                      </Button>
                    ) : null}
                    {canRollback && !model.is_production && production ? (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          setActionTarget({ model, action: 'rollback' })
                        }
                      >
                        <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
                        Roll back to this
                      </Button>
                    ) : null}
                    {canArchive && !model.is_production ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          setActionTarget({ model, action: 'archive' })
                        }
                      >
                        <Archive aria-hidden="true" className="h-3.5 w-3.5" />
                        Archive
                      </Button>
                    ) : null}
                    {model.is_production ? (
                      <span className="text-xs text-muted-foreground">
                        Serving — promote a replacement to change it
                      </span>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
              {NO_PERMISSION_MESSAGE} Promoting, archiving, and rolling back
              require an ML Engineer or Administrator role.
            </p>
          )}

          {canDetail ? (
            <ComparisonPanel
              versions={versions}
              versionA={versionA}
              versionB={versionB}
              onChange={(next) => {
                if (next.versionA !== undefined) setVersionA(next.versionA);
                if (next.versionB !== undefined) setVersionB(next.versionB);
              }}
              comparison={comparison.data}
              isLoading={comparison.isLoading && comparison.isFetching}
              error={comparison.error}
            />
          ) : null}
        </>
      )}

      {detailTarget ? (
        <DetailDrawer
          model={detailTarget}
          lineage={lineage.data}
          artifacts={artifacts.data}
          isLoading={lineage.isLoading || artifacts.isLoading}
          error={lineage.error ?? artifacts.error}
          onClose={() => setDetailTarget(null)}
        />
      ) : null}

      {actionTarget ? (
        <RegistryActionDialog
          action={actionTarget.action}
          target={actionTarget.model}
          currentProduction={production}
          isPending={mutation.isPending}
          error={mutation.error}
          onConfirm={applyAction}
          onCancel={() => {
            setActionTarget(null);
            mutation.reset();
          }}
        />
      ) : null}
    </div>
  );
}
