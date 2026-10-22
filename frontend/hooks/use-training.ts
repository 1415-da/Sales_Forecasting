'use client';

/**
 * Training workspace: dataset versions and training requests.
 *
 * Run status is polled with backoff -- quickly while a run has just started,
 * more slowly as it goes on -- and not at all once it is terminal. Progress
 * comes only from the task states Airflow reports; nothing here invents a
 * percentage.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  ApiError,
  getTrainingContract,
  getTrainingDataset,
  getTrainingDatasets,
  getTrainingRun,
  getTrainingRuns,
  rejectCandidate,
  sealTrainingDataset,
  startTrainingRun,
  uploadTrainingDataset,
  type DatasetUpload,
} from '@/lib/api';
import {
  TERMINAL_STATUSES,
  type TrainingContract,
  type TrainingDataset,
  type TrainingDatasetDetail,
  type TrainingRun,
  type TrainingRunDetail,
} from '@/lib/types/training';

/** First poll interval, its growth factor, and the ceiling. */
export const POLL_BASE_MS = 3_000;
export const POLL_FACTOR = 1.5;
export const POLL_MAX_MS = 30_000;

/** Delay before the n-th poll of a running request (n from 0). */
export function pollDelay(polls: number): number {
  return Math.min(POLL_MAX_MS, Math.round(POLL_BASE_MS * POLL_FACTOR ** Math.max(0, polls)));
}

function retry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  return error instanceof ApiError ? error.isRetryable : false;
}

export const trainingKeys = {
  contract: ['training', 'contract'] as const,
  datasets: ['training', 'datasets'] as const,
  dataset: (id: string) => ['training', 'dataset', id] as const,
  runs: ['training', 'runs'] as const,
  run: (id: string) => ['training', 'run', id] as const,
};

export function useTrainingContract(enabled = true): UseQueryResult<TrainingContract, Error> {
  return useQuery<TrainingContract, Error>({
    queryKey: trainingKeys.contract,
    queryFn: ({ signal }) => getTrainingContract(signal),
    staleTime: 5 * 60_000,
    enabled,
    retry,
  });
}

export function useTrainingDatasets(enabled = true): UseQueryResult<TrainingDataset[], Error> {
  return useQuery<TrainingDataset[], Error>({
    queryKey: trainingKeys.datasets,
    queryFn: ({ signal }) => getTrainingDatasets(signal),
    staleTime: 15_000,
    enabled,
    retry,
  });
}

export function useTrainingDataset(
  datasetId: string | null,
): UseQueryResult<TrainingDatasetDetail, Error> {
  return useQuery<TrainingDatasetDetail, Error>({
    queryKey: trainingKeys.dataset(datasetId ?? ''),
    queryFn: ({ signal }) => getTrainingDataset(datasetId as string, signal),
    enabled: Boolean(datasetId),
    staleTime: 15_000,
    retry,
  });
}

export function useUploadDataset() {
  const queryClient = useQueryClient();
  return useMutation<TrainingDatasetDetail, Error, DatasetUpload>({
    mutationFn: uploadTrainingDataset,
    retry: false,
    onSuccess: (dataset) => {
      queryClient.setQueryData(trainingKeys.dataset(dataset.id), dataset);
      queryClient.invalidateQueries({ queryKey: trainingKeys.datasets });
    },
  });
}

export function useSealDataset() {
  const queryClient = useQueryClient();
  return useMutation<TrainingDatasetDetail, Error, string>({
    mutationFn: sealTrainingDataset,
    retry: false,
    onSuccess: (dataset) => {
      queryClient.setQueryData(trainingKeys.dataset(dataset.id), dataset);
      queryClient.invalidateQueries({ queryKey: trainingKeys.datasets });
    },
  });
}

export function useTrainingRuns(enabled = true): UseQueryResult<TrainingRun[], Error> {
  return useQuery<TrainingRun[], Error>({
    queryKey: trainingKeys.runs,
    queryFn: ({ signal }) => getTrainingRuns(signal),
    enabled,
    retry,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((run) => !run.is_terminal) ? POLL_MAX_MS : false,
  });
}

/** One request, polled with backoff until terminal. */
export function useTrainingRun(runId: string | null): UseQueryResult<TrainingRunDetail, Error> {
  return useQuery<TrainingRunDetail, Error>({
    queryKey: trainingKeys.run(runId ?? ''),
    queryFn: ({ signal }) => getTrainingRun(runId as string, signal),
    enabled: Boolean(runId),
    retry,
    refetchInterval: (query) => {
      const run = query.state.data;
      if (!run) return POLL_BASE_MS;
      // A succeeded run whose candidates are not collected yet is still
      // settling on the server, so keep asking.
      const settled =
        TERMINAL_STATUSES.has(run.status) && (run.status === 'failed' || run.candidates.length > 0);
      return settled ? false : pollDelay(query.state.dataUpdateCount);
    },
  });
}

export function useStartTraining() {
  const queryClient = useQueryClient();
  return useMutation<
    TrainingRunDetail,
    Error,
    { datasetId: string; trainingConfigId: string; idempotencyKey: string }
  >({
    mutationFn: ({ idempotencyKey, ...payload }) => startTrainingRun(payload, idempotencyKey),
    // Safe to retry a transport failure: the same key returns the same request.
    retry: (count, error) => count < 1 && error instanceof ApiError && error.kind === 'network',
    onSuccess: (run) => {
      queryClient.setQueryData(trainingKeys.run(run.id), run);
      queryClient.invalidateQueries({ queryKey: trainingKeys.runs });
    },
  });
}

export function useRejectCandidate(runId: string) {
  const queryClient = useQueryClient();
  return useMutation<TrainingRunDetail, Error, { model: string; version: string; reason: string }>({
    mutationFn: (payload) => rejectCandidate(runId, payload),
    retry: false,
    onSuccess: (run) => {
      queryClient.setQueryData(trainingKeys.run(run.id), run);
      queryClient.invalidateQueries({ queryKey: trainingKeys.runs });
    },
  });
}

/** A fresh idempotency key for one confirmed submission. */
export function newIdempotencyKey(): string {
  return `train-${crypto.randomUUID()}`;
}
