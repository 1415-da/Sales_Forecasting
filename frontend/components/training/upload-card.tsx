'use client';

import { Upload } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup } from '@/components/ui/radio-group';
import { Select } from '@/components/ui/select';
import { useUploadDataset } from '@/hooks/use-training';
import { friendlyMessage } from '@/lib/api';
import type {
  TrainingContract,
  TrainingDataset,
  TrainingDatasetDetail,
} from '@/lib/types/training';

export interface UploadCardProps {
  contract: TrainingContract;
  readyDatasets: TrainingDataset[];
  onUploaded: (dataset: TrainingDatasetDetail) => void;
}

/** Step 1: choose a file and how it relates to existing data. */
export function UploadCard({ contract, readyDatasets, onUploaded }: UploadCardProps) {
  const upload = useUploadDataset();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [mode, setMode] = useState<'independent' | 'append'>('independent');
  const [baseId, setBaseId] = useState('');
  const [conflictPolicy, setConflictPolicy] = useState('reject');
  const [currency, setCurrency] = useState(
    String((contract.contract.policy.allowed_currencies as string[] | undefined)?.[0] ?? 'USD'),
  );
  const [sourceType, setSourceType] = useState('real');

  const maxBytes = Number(contract.contract.policy.max_bytes ?? 0);
  const base = readyDatasets.find((item) => item.id === baseId);
  const tooLarge = file !== null && maxBytes > 0 && file.size > maxBytes;
  const canSubmit =
    file !== null && !tooLarge && name.trim().length > 0 && (mode === 'independent' || baseId !== '');

  const submit = () => {
    if (!file) return;
    upload.mutate(
      {
        file,
        name: name.trim(),
        mode,
        currency,
        sourceType,
        ...(mode === 'append' ? { baseDatasetId: baseId, conflictPolicy } : {}),
      },
      { onSuccess: onUploaded },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Upload className="h-4 w-4" aria-hidden /> 1. Upload training data
        </CardTitle>
        <CardDescription>
          Historical store-day sales to train a new model on. This is not the history for a
          single forecast, and not the actual sales monitoring scores against.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-md border bg-muted/40 p-3 text-xs">
          <p className="font-medium">
            Accepted schema ({contract.contract.schema_version}): {contract.contract.grain}
          </p>
          <ul className="mt-1 space-y-0.5">
            {contract.contract.required_columns.map((column) => (
              <li key={column.name}>
                <code className="font-semibold">{column.name}</code> ({column.type}) - {column.description}
              </li>
            ))}
            {contract.contract.optional_columns.map((column) => (
              <li key={column.name}>
                <code>{column.name}</code> (optional, {column.type}) - {contract.contract.derivations[column.name] ?? column.description}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-muted-foreground">Rejected: {contract.contract.rejected}</p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="training-file">File (CSV or Parquet)</Label>
            <Input
              id="training-file"
              type="file"
              accept=".csv,.parquet"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
            {tooLarge ? (
              <p role="alert" className="text-xs text-destructive">
                The file is larger than the {Math.round(maxBytes / 1024 / 1024)} MB limit.
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="dataset-name">Dataset name</Label>
            <Input
              id="dataset-name"
              value={name}
              maxLength={80}
              placeholder="e.g. Store sales 2014-2016"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1.5">
              <Label htmlFor="dataset-currency">Currency</Label>
              <Select id="dataset-currency" value={currency} onChange={(event) => setCurrency(event.target.value)}>
                {((contract.contract.policy.allowed_currencies as string[] | undefined) ?? ['USD']).map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="dataset-source">Source</Label>
              <Select id="dataset-source" value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
                {contract.source_types.map((type) => (
                  <option key={type} value={type}>
                    {type === 'real' ? 'Real sales' : 'Synthetic'}
                  </option>
                ))}
              </Select>
            </div>
          </div>
        </div>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium">Mode</legend>
          <RadioGroup
            name="dataset-mode"
            value={mode}
            onValueChange={setMode}
            options={[
              {
                value: 'independent',
                label: 'Independent dataset',
                hint: 'Train on this file alone; nothing else is added.',
              },
              {
                value: 'append',
                label: 'Append to an approved dataset',
                hint: 'Combine with a sealed version after compatibility checks.',
                disabled: readyDatasets.length === 0,
              },
            ]}
          />
        </fieldset>

        {mode === 'append' ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="base-dataset">Append to</Label>
              <Select id="base-dataset" value={baseId} onChange={(event) => setBaseId(event.target.value)}>
                <option value="">Select a sealed version…</option>
                {readyDatasets.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} v{item.version} ({item.date_min} to {item.date_max})
                  </option>
                ))}
              </Select>
              {base ? (
                <p className="text-xs text-muted-foreground">
                  {base.source_types.join(', ')} data, {base.currency}, {base.store_count} stores. Real
                  and synthetic data are never combined.
                </p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="conflict-policy">Overlapping days with different values</Label>
              <Select
                id="conflict-policy"
                value={conflictPolicy}
                onChange={(event) => setConflictPolicy(event.target.value)}
              >
                <option value="reject">Reject the upload (default)</option>
                <option value="prefer_new">Correct: the upload replaces them</option>
              </Select>
              <p className="text-xs text-muted-foreground">
                Identical overlapping days are always de-duplicated. The base version is never changed.
              </p>
            </div>
          </div>
        ) : null}

        {upload.isError ? (
          <p role="alert" className="text-sm text-destructive">
            {upload.error.message || friendlyMessage(upload.error)}
          </p>
        ) : null}

        <Button onClick={submit} disabled={!canSubmit || upload.isPending}>
          {upload.isPending ? 'Uploading and validating…' : 'Upload and validate'}
        </Button>
      </CardContent>
    </Card>
  );
}
