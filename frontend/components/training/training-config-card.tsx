'use client';

import { Play } from 'lucide-react';
import { useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { RadioGroup } from '@/components/ui/radio-group';
import { Select } from '@/components/ui/select';
import { newIdempotencyKey, useStartTraining } from '@/hooks/use-training';
import { friendlyMessage } from '@/lib/api';
import type { TrainingConfig, TrainingDataset, TrainingRunDetail } from '@/lib/types/training';

export interface TrainingConfigCardProps {
  datasets: TrainingDataset[];
  configs: TrainingConfig[];
  selectedDatasetId: string;
  onSelectDataset: (id: string) => void;
  onStarted: (run: TrainingRunDetail) => void;
}

/** Step 3: an approved configuration on a sealed version, confirmed. */
export function TrainingConfigCard({
  datasets,
  configs,
  selectedDatasetId,
  onSelectDataset,
  onStarted,
}: TrainingConfigCardProps) {
  const start = useStartTraining();
  const ready = datasets.filter((item) => item.status === 'ready');
  const allowed = configs.filter((config) => config.allowed);
  const [configId, setConfigId] = useState(allowed[0]?.id ?? '');
  const [confirming, setConfirming] = useState(false);
  // One key per confirmation: a double click or a network retry reuses it and
  // gets the same request back instead of a second run.
  const keyRef = useRef<string>('');
  const dataset = ready.find((item) => item.id === selectedDatasetId);
  const config = configs.find((item) => item.id === configId);

  const open = () => {
    keyRef.current = newIdempotencyKey();
    start.reset();
    setConfirming(true);
  };

  const confirm = () => {
    if (!dataset || !config) return;
    start.mutate(
      { datasetId: dataset.id, trainingConfigId: config.id, idempotencyKey: keyRef.current },
      {
        onSuccess: (run) => {
          setConfirming(false);
          onStarted(run);
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">3. Training configuration</CardTitle>
        <CardDescription>
          Only sealed versions and approved configurations can be trained. Candidates are
          registered for review and never deployed automatically.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="train-dataset">Dataset version</Label>
          <Select id="train-dataset" value={selectedDatasetId} onChange={(event) => onSelectDataset(event.target.value)}>
            <option value="">Select a sealed version…</option>
            {ready.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} v{item.version} · {item.row_count?.toLocaleString()} rows · {item.date_min} to {item.date_max}
              </option>
            ))}
          </Select>
          {dataset ? (
            <p className="break-all font-mono text-xs text-muted-foreground">sha256 {dataset.content_sha256}</p>
          ) : null}
        </div>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium">Approved configuration</legend>
          <RadioGroup
            name="training-config"
            value={configId}
            onValueChange={setConfigId}
            options={configs.map((item) => ({
              value: item.id,
              label: item.label,
              hint: item.allowed
                ? `${item.description} About ${item.expected_duration}.`
                : `${item.description} Requires an ML Engineer or Administrator.`,
              disabled: !item.allowed,
            }))}
          />
        </fieldset>

        {config ? (
          <div className="space-y-1 rounded-md border bg-muted/40 p-3 text-xs">
            <p>
              <span className="font-medium">Models:</span> {config.models.join(', ')}
            </p>
            <p>
              <span className="font-medium">Evaluation policy:</span> {config.evaluation_policy} Minimum{' '}
              {config.min_test_rows} scored test rows.
            </p>
            <p>
              <span className="font-medium">Deployment policy:</span> manual review. An ML Engineer or
              Administrator approves (or rejects) each candidate.
            </p>
          </div>
        ) : null}

        <Button onClick={open} disabled={!dataset || !config || !config.allowed}>
          <Play className="mr-1 h-4 w-4" aria-hidden /> Start training
        </Button>

        <Dialog
          open={confirming}
          onOpenChange={setConfirming}
          title="Start training?"
          description="This starts the Airflow training DAG on the exact dataset version below."
          footer={
            <>
              <Button variant="outline" onClick={() => setConfirming(false)} disabled={start.isPending}>
                Cancel
              </Button>
              <Button onClick={confirm} disabled={start.isPending}>
                {start.isPending ? 'Submitting…' : 'Confirm and start'}
              </Button>
            </>
          }
        >
          {dataset && config ? (
            <dl className="space-y-1 text-sm">
              <div>
                <dt className="inline font-medium">Dataset: </dt>
                <dd className="inline">
                  {dataset.name} v{dataset.version} ({dataset.row_count?.toLocaleString()} rows, {dataset.store_count} stores)
                </dd>
              </div>
              <div>
                <dt className="inline font-medium">Configuration: </dt>
                <dd className="inline">{config.label}</dd>
              </div>
              <div>
                <dt className="inline font-medium">Deployment: </dt>
                <dd className="inline">manual review; nothing changes in Production.</dd>
              </div>
            </dl>
          ) : null}
          {start.isError ? (
            <p role="alert" className="mt-2 text-sm text-destructive">
              {start.error.message || friendlyMessage(start.error)}
            </p>
          ) : null}
        </Dialog>
      </CardContent>
    </Card>
  );
}
