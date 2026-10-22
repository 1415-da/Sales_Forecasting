'use client';

import { FileArchive, Upload, X } from 'lucide-react';
import { useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup } from '@/components/ui/radio-group';
import { Select } from '@/components/ui/select';
import { newIdempotencyKey, useUploadDataset } from '@/hooks/use-training';
import { friendlyMessage } from '@/lib/api';
import type { TrainingContract, TrainingDataset, TrainingDatasetDetail } from '@/lib/types/training';

/** Common ISO codes; the serving currencies are always listed first. */
const COMMON_CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'INR', 'JPY', 'CHF'];

export function formatBytes(bytes: number | undefined | null): string {
  if (bytes == null) return '—';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(bytes >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

const ACCEPTED = ['.csv', '.parquet', '.pq', '.zip'];

export interface UploadCardProps {
  contract: TrainingContract;
  readyDatasets: TrainingDataset[];
  onUploaded: (dataset: TrainingDatasetDetail) => void;
}

/** Step 1: choose files and how they relate to existing data. */
export function UploadCard({ contract, readyDatasets, onUploaded }: UploadCardProps) {
  const upload = useUploadDataset();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState('');
  const [mode, setMode] = useState<'independent' | 'append'>('independent');
  const [baseId, setBaseId] = useState('');
  const [conflictPolicy, setConflictPolicy] = useState('reject');
  const [currency, setCurrency] = useState('');
  const [sourceType, setSourceType] = useState('real');
  const limits = contract.limits;
  const currencies = Array.from(new Set([...contract.serving_currencies, ...COMMON_CURRENCIES]));

  const total = files.reduce((sum, file) => sum + file.size, 0);
  const problems: string[] = [];
  if (limits.max_files != null && files.length > limits.max_files)
    problems.push(`${files.length} files selected; the limit is ${limits.max_files}. Put related files in one ZIP.`);
  for (const file of files) {
    if (limits.max_file_bytes != null && file.size > limits.max_file_bytes)
      problems.push(`${file.name} is ${formatBytes(file.size)}; the per-file limit is ${formatBytes(limits.max_file_bytes)}.`);
    if (!ACCEPTED.some((ext) => file.name.toLowerCase().endsWith(ext)))
      problems.push(`${file.name}: only CSV, Parquet and ZIP files are accepted.`);
  }
  if (limits.max_upload_bytes != null && total > limits.max_upload_bytes)
    problems.push(`The selection is ${formatBytes(total)}; the upload limit is ${formatBytes(limits.max_upload_bytes)}.`);

  // Mirrors the server's rule, so a bad name is explained before uploading.
  const nameOk = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$/.test(name.trim());
  const canSubmit =
    files.length > 0 && problems.length === 0 && nameOk && (mode === 'independent' || baseId !== '');

  const addFiles = (list: FileList | null) => {
    if (!list) return;
    const incoming = Array.from(list);
    setFiles((current) => [
      ...current.filter((file) => !incoming.some((item) => item.name === file.name)),
      ...incoming,
    ]);
    if (inputRef.current) inputRef.current.value = '';
  };

  const submit = () => {
    upload.mutate(
      {
        files,
        name: name.trim(),
        mode,
        sourceType,
        idempotencyKey: newIdempotencyKey(),
        ...(currency ? { currency } : {}),
        ...(mode === 'append' ? { baseDatasetId: baseId, conflictPolicy } : {}),
      },
      {
        onSuccess: (dataset) => {
          setFiles([]);
          onUploaded(dataset);
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Upload className="h-4 w-4" aria-hidden /> 1. Upload training data
        </CardTitle>
        <CardDescription>
          Historical store-day sales to train a new model on. Not the history for a single forecast,
          and not the actual sales monitoring scores against.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-md border bg-muted/40 p-3 text-xs">
          <p className="font-medium">
            Trains on {contract.contract.schema_version}: {contract.contract.grain} Daily revenue only.
          </p>
          <p className="mt-1 text-muted-foreground">
            Recognised automatically:{' '}
            {contract.adapters.map((adapter) => adapter.label).join('; ')}. Anything else can be mapped
            column by column after upload. Not every dataset can be used: product-level, transaction-level and
            unit (quantity) data are refused with an explanation.
          </p>
          <p className="mt-1 text-muted-foreground">
            Limits: {formatBytes(limits.max_upload_bytes)} per upload, {formatBytes(limits.max_file_bytes)} per file,{' '}
            {limits.max_files} files, ZIPs up to {formatBytes(limits.max_expanded_bytes)} expanded,{' '}
            {limits.max_rows?.toLocaleString()} rows.
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="training-files">Files (CSV, Parquet, or a ZIP of them; several related files allowed)</Label>
          <Input
            id="training-files"
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPTED.join(',')}
            onChange={(event) => addFiles(event.target.files)}
          />
          {files.length > 0 ? (
            <ul className="space-y-1 text-xs">
              {files.map((file) => (
                <li key={file.name} className="flex items-center justify-between gap-2 rounded border px-2 py-1">
                  <span className="flex items-center gap-1 truncate">
                    {file.name.toLowerCase().endsWith('.zip') ? <FileArchive className="h-3.5 w-3.5" aria-hidden /> : null}
                    {file.name} <span className="text-muted-foreground">({formatBytes(file.size)})</span>
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => setFiles((current) => current.filter((item) => item !== file))}
                  >
                    <X className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {problems.map((problem) => (
            <p key={problem} role="alert" className="text-xs text-destructive">
              {problem}
            </p>
          ))}
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label htmlFor="dataset-name">Dataset name</Label>
            <Input
              id="dataset-name"
              value={name}
              maxLength={80}
              placeholder="e.g. Rossmann 2013-2015"
              onChange={(event) => setName(event.target.value)}
            />
            {name.trim() && !nameOk ? (
              <p role="alert" className="text-xs text-destructive">
                Use letters, digits, spaces, &lsquo;.&rsquo;, &lsquo;_&rsquo; or &lsquo;-&rsquo; (up to 80 characters).
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="dataset-currency">Currency of the sales figures</Label>
            <Select id="dataset-currency" value={currency} onChange={(event) => setCurrency(event.target.value)}>
              <option value="">Not confirmed yet (asked before validation)</option>
              {currencies.map((code) => (
                <option key={code} value={code}>
                  {code}
                  {contract.serving_currencies.includes(code) ? ' (serving model currency)' : ''}
                </option>
              ))}
              <option value="UNKNOWN">UNKNOWN (not stated by the source)</option>
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

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium">Mode</legend>
          <RadioGroup
            name="dataset-mode"
            value={mode}
            onValueChange={setMode}
            options={[
              { value: 'independent', label: 'Independent dataset', hint: 'Train on these files alone; nothing else is added.' },
              {
                value: 'append',
                label: 'Append to an approved dataset',
                hint: 'Combine with a sealed version of the same source after compatibility checks.',
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
                    {item.name} v{item.version} ({item.source_namespace ?? 'canonical'}, {item.date_min} to {item.date_max})
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="conflict-policy">Overlapping days with different values</Label>
              <Select id="conflict-policy" value={conflictPolicy} onChange={(event) => setConflictPolicy(event.target.value)}>
                <option value="reject">Reject the upload (default)</option>
                <option value="prefer_new">Correct: the upload replaces them</option>
              </Select>
              <p className="text-xs text-muted-foreground">
                Identical overlapping days are de-duplicated. The base version is never changed. Different sources,
                currencies or real/synthetic data are never combined.
              </p>
            </div>
          </div>
        ) : null}

        {upload.isError ? (
          <div role="alert" className="rounded-md border border-destructive/50 p-3 text-sm text-destructive">
            <p className="font-medium">The upload was not accepted.</p>
            <p>{upload.error.message || friendlyMessage(upload.error)}</p>
          </div>
        ) : null}

        {upload.progress != null ? (
          <div className="space-y-1">
            <div className="h-2 w-full overflow-hidden rounded bg-muted">
              <div className="h-full bg-primary transition-all" style={{ width: `${Math.round(upload.progress * 100)}%` }} />
            </div>
            <p className="text-xs text-muted-foreground">
              {upload.progress < 1
                ? `Uploading ${formatBytes(total)}… ${Math.round(upload.progress * 100)}%`
                : 'Uploaded. Storing the originals and starting processing…'}
            </p>
          </div>
        ) : null}

        <Button onClick={submit} disabled={!canSubmit || upload.isPending}>
          {upload.isPending ? 'Uploading…' : 'Upload'}
        </Button>
      </CardContent>
    </Card>
  );
}
