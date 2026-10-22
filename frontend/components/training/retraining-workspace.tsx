'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useMemo } from 'react';

import { ErrorState } from '@/components/shared/error-state';
import { KpiSkeleton } from '@/components/shared/loading-skeleton';
import { MappingPanel } from '@/components/training/mapping-panel';
import { ResultsPanel } from '@/components/training/results-panel';
import { RunProgressPanel } from '@/components/training/run-progress-panel';
import { TrainingConfigCard } from '@/components/training/training-config-card';
import { TrainingHistoryTable } from '@/components/training/training-history-table';
import { UploadCard } from '@/components/training/upload-card';
import { ValidationPanel } from '@/components/training/validation-panel';
import {
  useTrainingContract,
  useTrainingDataset,
  useTrainingDatasets,
  useTrainingRun,
  useTrainingRuns,
} from '@/hooks/use-training';

/**
 * The "Data & Retraining" tab.
 *
 * The selected dataset and request live in the URL (`?dataset=`, `?run=`), so
 * a refresh -- or a link sent to a reviewer -- reopens the same request and
 * resumes polling it.
 */
export function RetrainingWorkspace() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const datasetId = params.get('dataset');
  const runId = params.get('run');

  const setParam = useCallback(
    (updates: Record<string, string | null>) => {
      const next = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(updates)) {
        if (value) next.set(key, value);
        else next.delete(key);
      }
      router.replace(`${pathname}?${next.toString()}`, { scroll: false });
    },
    [params, pathname, router],
  );

  const contract = useTrainingContract();
  const datasets = useTrainingDatasets();
  const dataset = useTrainingDataset(datasetId);
  const runs = useTrainingRuns();
  const run = useTrainingRun(runId);

  const ready = useMemo(() => (datasets.data ?? []).filter((item) => item.status === 'ready'), [datasets.data]);

  if (contract.isError) {
    return <ErrorState error={contract.error} onRetry={() => void contract.refetch()} />;
  }
  if (contract.isLoading || !contract.data) return <KpiSkeleton count={2} />;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 xl:grid-cols-2">
        <UploadCard
          contract={contract.data}
          readyDatasets={ready}
          onUploaded={(item) => setParam({ dataset: item.id })}
        />
        {dataset.data ? (
          <div className="space-y-4">
            {/* Remounted per status/mapping version: its form starts from the server's current result. */}
            <MappingPanel
              key={`${dataset.data.id}-${dataset.data.status}-${dataset.data.mapping_version}`}
              dataset={dataset.data}
              contract={contract.data}
            />
            <ValidationPanel dataset={dataset.data} onSealed={(item) => setParam({ dataset: item.id })} />
          </div>
        ) : dataset.isError ? (
          <ErrorState error={dataset.error} onRetry={() => void dataset.refetch()} />
        ) : (
          <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
            Upload a file to see its validation report here. Earlier versions:{' '}
            {(datasets.data ?? []).length === 0 ? 'none yet.' : null}
            <ul className="mt-2 space-y-1">
              {(datasets.data ?? []).slice(0, 8).map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className="text-left text-primary underline-offset-4 hover:underline"
                    onClick={() => setParam({ dataset: item.id })}
                  >
                    {item.name} v{item.version}
                  </button>{' '}
                  · {item.status} · {item.row_count?.toLocaleString() ?? '—'} rows
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <TrainingConfigCard
        datasets={datasets.data ?? []}
        configs={contract.data.configs}
        selectedDatasetId={datasetId && ready.some((item) => item.id === datasetId) ? datasetId : ''}
        onSelectDataset={(id) => setParam({ dataset: id || null })}
        onStarted={(item) => setParam({ run: item.id })}
      />

      {run.data ? (
        <div className="space-y-4">
          <RunProgressPanel run={run.data} />
          <ResultsPanel run={run.data} />
        </div>
      ) : run.isError ? (
        <ErrorState error={run.error} onRetry={() => void run.refetch()} />
      ) : null}

      <TrainingHistoryTable
        runs={runs.data ?? []}
        selectedId={runId}
        onSelect={(id) => setParam({ run: id })}
      />
    </div>
  );
}
