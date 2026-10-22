'use client';

import { SearchCheck } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { useConfigureDataset } from '@/hooks/use-training';
import { friendlyMessage } from '@/lib/api';
import type { DetectionCandidate, TrainingContract, TrainingDatasetDetail } from '@/lib/types/training';

type Config = Record<string, string | boolean | undefined>;

const MATCH_LABEL: Record<string, string> = {
  exact: 'Recognised',
  partial: 'Possible match: confirm',
  manual: 'Manual mapping',
};

function OptionSelect({
  id,
  label,
  value,
  onChange,
  options,
  placeholder = 'Choose…',
  help,
}: {
  id: string;
  label: string;
  value: string | undefined;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  placeholder?: string;
  help?: string | null;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Select id={id} value={value ?? ''} onChange={(event) => onChange(event.target.value)}>
        <option value="">{placeholder}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </Select>
      {help ? <p className="text-xs text-muted-foreground">{help}</p> : null}
    </div>
  );
}

/** Step 2: which adapter reads these files, and what must be confirmed. */
export function MappingPanel({
  dataset,
  contract,
}: {
  dataset: TrainingDatasetDetail;
  contract: TrainingContract;
}) {
  const configure = useConfigureDataset();
  const detection = dataset.validation_report.detection ?? null;
  const candidates = detection?.candidates ?? [];
  const initialAdapter =
    (dataset.ingestion_config.adapter_id as string | undefined) ??
    detection?.selected ??
    candidates.find((item) => item.match === 'exact')?.adapter_id ??
    'mapped_tabular';
  const [adapterId, setAdapterId] = useState<string>(initialAdapter);
  const [config, setConfig] = useState<Config>({});
  // Open whenever something must change: a confirmation is missing or the data failed a rule.
  const [open, setOpen] = useState(dataset.status === 'needs_mapping' || dataset.status === 'invalid');

  const candidate: DetectionCandidate | undefined = candidates.find((item) => item.adapter_id === adapterId);

  // Reset the form from the server whenever the session's mapping changes.
  useEffect(() => {
    const proposed = candidate?.proposed_mapping ?? {};
    const fromServer = dataset.ingestion_config as Config;
    const seeded: Config = { ...fromServer };
    if (adapterId === 'mapped_tabular') {
      for (const key of ['file', 'date', 'store_id', 'sales', 'open', 'is_closed']) {
        if (seeded[key] == null && typeof proposed[key] === 'string') seeded[key] = proposed[key] as string;
      }
    }
    setConfig(seeded);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [adapterId]);

  const set = (key: string, value: string | boolean) => setConfig((current) => ({ ...current, [key]: value === '' ? undefined : value }));
  const needs = new Set(detection?.needs_input ?? []);
  const confirmations = candidate?.confirmations ?? [];
  const confirmation = (key: string) => confirmations.find((item) => item.key === key);
  const fileNames = Object.keys(candidate?.columns ?? {});
  const selectedFile = (config.file as string | undefined) ?? fileNames[0];
  const columns = useMemo(
    () => (candidate?.columns?.[selectedFile ?? ''] ?? []).map((name) => ({ value: name, label: name })),
    [candidate, selectedFile],
  );
  const currencyOptions = [
    ...Array.from(new Set([...contract.serving_currencies, 'USD', 'EUR', 'GBP', 'CAD', 'AUD', 'INR'])).map((code) => ({
      value: code,
      label: contract.serving_currencies.includes(code) ? `${code} (serving model currency)` : code,
    })),
    { value: 'UNKNOWN', label: 'UNKNOWN (not stated by the source)' },
  ];

  if (dataset.status === 'processing' || dataset.status === 'ready' || !detection) return null;

  const apply = () => {
    const payload: Record<string, unknown> = { adapter_id: adapterId };
    for (const [key, value] of Object.entries(config)) {
      if (key === 'adapter_id') continue;
      if (value !== undefined && value !== '') payload[key] = value;
    }
    configure.mutate({ datasetId: dataset.id, config: payload, expectedMappingVersion: dataset.mapping_version });
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <SearchCheck className="h-4 w-4" aria-hidden /> 2. Detect and map
            </CardTitle>
            <CardDescription>{detection.reason}</CardDescription>
          </div>
          {dataset.status !== 'needs_mapping' ? (
            <Button size="sm" variant="outline" onClick={() => setOpen((value) => !value)}>
              {open ? 'Hide mapping' : 'Edit mapping'}
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <ul className="space-y-2">
          {candidates.map((item) => (
            <li key={item.adapter_id} className="rounded-md border p-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{item.label}</span>
                <Badge variant={item.match === 'exact' ? 'success' : 'warning'}>{MATCH_LABEL[item.match] ?? item.match}</Badge>
                {item.adapter_id === detection.selected ? <Badge variant="secondary">selected</Badge> : null}
              </div>
              {item.evidence.length ? <p className="text-xs text-muted-foreground">{item.evidence.join(' ')}</p> : null}
              {item.files_used.length ? (
                <p className="text-xs">
                  Uses: {item.files_used.map((file) => `${file.name} (${file.role})`).join(', ')}
                </p>
              ) : null}
              {item.files_excluded.length ? (
                <p className="text-xs text-muted-foreground">
                  Excluded: {item.files_excluded.map((file) => `${file.name}: ${file.reason}`).join('; ')}
                </p>
              ) : null}
              {[...item.missing_files.map((text) => `Missing ${text}`), ...item.ambiguities].map((text) => (
                <p key={text} className="text-xs text-destructive">
                  {text}
                </p>
              ))}
            </li>
          ))}
        </ul>

        {needs.size > 0 ? (
          <p role="status" className="rounded-md border border-amber-500/40 bg-amber-500/5 p-2 text-sm text-amber-300">
            Needed before validation: {Array.from(needs).map((key) => confirmation(key)?.label ?? key).join('; ')}.
          </p>
        ) : null}

        {open ? (
          <div className="space-y-4 rounded-md border p-3">
            <OptionSelect
              id="adapter"
              label="Adapter"
              value={adapterId}
              onChange={(value) => setAdapterId(value || 'mapped_tabular')}
              options={candidates.filter((item) => item.match !== 'none').map((item) => ({ value: item.adapter_id, label: item.label }))}
              placeholder="Choose an adapter…"
            />

            <div className="grid gap-4 sm:grid-cols-2">
              <OptionSelect
                id="map-currency"
                label="Currency of the sales figures"
                value={config.currency as string | undefined}
                onChange={(value) => set('currency', value)}
                options={currencyOptions}
                help="Never inferred from a country. UNKNOWN can be trained but cannot replace the serving model."
              />
              <OptionSelect
                id="map-missing-dates"
                label="Stores with days missing inside their history"
                value={(config.missing_dates as string | undefined) ?? 'reject'}
                onChange={(value) => set('missing_dates', value || 'reject')}
                options={[
                  { value: 'reject', label: 'Reject the dataset (default)' },
                  { value: 'exclude_stores', label: 'Exclude those stores and list them' },
                ]}
                placeholder="Reject the dataset (default)"
              />
            </div>

            {confirmation('closure') ? (
              <OptionSelect
                id="map-closure"
                label={confirmation('closure')!.label}
                value={config.closure as string | undefined}
                onChange={(value) => set('closure', value)}
                options={confirmation('closure')!.options}
                help={confirmation('closure')!.help}
              />
            ) : null}
            {confirmation('confirm_source') ? (
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={config.confirm_source === 'yes'}
                  onChange={(event) => set('confirm_source', event.target.checked ? 'yes' : '')}
                />
                {confirmation('confirm_source')!.label}
              </label>
            ) : null}

            {adapterId === 'mapped_tabular' ? (
              <div className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-3">
                  <OptionSelect id="map-file" label="File" value={selectedFile} onChange={(value) => set('file', value)} options={fileNames.map((name) => ({ value: name, label: name }))} />
                  <OptionSelect id="map-date" label="Date column" value={config.date as string | undefined} onChange={(value) => set('date', value)} options={columns} />
                  <OptionSelect id="map-sales" label="Target column" value={config.sales as string | undefined} onChange={(value) => set('sales', value)} options={columns} />
                  <OptionSelect id="map-store" label="Store column" value={config.store_id as string | undefined} onChange={(value) => set('store_id', value)} options={columns} placeholder="None: the file is one store" />
                  <OptionSelect id="map-open" label="Opening-status column (1 = open)" value={config.open as string | undefined} onChange={(value) => set('open', value)} options={columns} placeholder="None" />
                  <OptionSelect id="map-closed" label="Closed-flag column (1 = closed)" value={config.is_closed as string | undefined} onChange={(value) => set('is_closed', value)} options={columns} placeholder="None" />
                </div>
                {!config.store_id ? (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="map-single-store">Store identifier for the whole file</Label>
                      <Input id="map-single-store" value={(config.single_store_id as string | undefined) ?? ''} onChange={(event) => set('single_store_id', event.target.value)} placeholder="e.g. shop_1" />
                    </div>
                    <label className="flex items-center gap-2 self-end text-sm">
                      <input type="checkbox" checked={config.confirm_single_store === true} onChange={(event) => set('confirm_single_store', event.target.checked)} />
                      I confirm every row is the same single store
                    </label>
                  </div>
                ) : null}
                <div className="grid gap-4 sm:grid-cols-3">
                  {(['target_type', 'frequency', 'source_grain', 'date_format', 'closure'] as const).map((key) =>
                    confirmation(key) ? (
                      <OptionSelect
                        key={key}
                        id={`map-${key}`}
                        label={confirmation(key)!.label}
                        value={config[key] as string | undefined}
                        onChange={(value) => set(key, value)}
                        options={confirmation(key)!.options}
                        help={confirmation(key)!.help}
                      />
                    ) : null,
                  )}
                </div>
                <p className="text-xs text-muted-foreground">
                  Suggestions are pre-filled from column names only; confirm what each column means. A column named
                  &ldquo;sales&rdquo; may be units, not money.
                </p>
              </div>
            ) : null}

            {configure.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {configure.error.message || friendlyMessage(configure.error)}
              </p>
            ) : null}
            <div className="flex items-center gap-3">
              <Button onClick={apply} disabled={configure.isPending}>
                {configure.isPending ? 'Applying…' : 'Apply and validate'}
              </Button>
              <span className="text-xs text-muted-foreground">
                Applying replaces any earlier validation result (mapping v{dataset.mapping_version ?? 1} →{' '}
                v{(dataset.mapping_version ?? 1) + 1}).
              </span>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
