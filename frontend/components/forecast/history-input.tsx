'use client';

import {
  AlertCircle,
  CheckCircle2,
  Info,
  Plus,
  Sparkles,
  Trash2,
  Upload,
} from 'lucide-react';
import { useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { StatusBadge } from '@/components/shared/status-badge';
import { addDays, formatDateRange, formatSales, toIsoDate } from '@/lib/format';
import {
  MIN_HISTORY_DAYS,
  RECOMMENDED_HISTORY_DAYS,
  buildManualTemplate,
  buildSampleHistory,
  parseHistoryCsv,
  validateHistory,
  type HistoryValidation,
} from '@/lib/sample-history';
import type { HistoryPoint, HistorySource } from '@/lib/types';
import { cn } from '@/lib/utils';

export interface HistoryInputProps {
  readonly rows: readonly HistoryPoint[];
  readonly source: HistorySource | null;
  readonly onChange: (rows: HistoryPoint[], source: HistorySource) => void;
  readonly onClear: () => void;
  readonly validation: HistoryValidation;
}

const MODES: readonly { id: HistorySource; label: string; icon: typeof Sparkles }[] = [
  { id: 'sample', label: 'Demo data', icon: Sparkles },
  { id: 'upload', label: 'Upload CSV', icon: Upload },
  { id: 'manual', label: 'Manual entry', icon: Plus },
];

/**
 * Where the history comes from.
 *
 * Three modes, one output: whichever is used, the result is the same array of
 * `{date, sales}` rows that `validateHistory` judges, so the enable/disable
 * rule for Generate is identical in all three.
 */
export function HistoryInput({
  rows,
  source,
  onChange,
  onClear,
  validation,
}: HistoryInputProps) {
  const [mode, setMode] = useState<HistorySource>(
    source && source !== 'dataset' ? source : 'sample',
  );
  const [csvError, setCsvError] = useState<string | null>(null);
  const [csvHeaders, setCsvHeaders] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadSample = () => {
    setCsvError(null);
    onChange(buildSampleHistory(RECOMMENDED_HISTORY_DAYS), 'sample');
  };

  const loadManualTemplate = () => {
    setCsvError(null);
    onChange(buildManualTemplate(MIN_HISTORY_DAYS), 'manual');
  };

  const handleFile = async (file: File) => {
    setCsvError(null);
    try {
      const text = await file.text();
      const parsed = parseHistoryCsv(text);
      setCsvHeaders(parsed.headers);
      if (parsed.rows.length === 0) {
        setCsvError(
          parsed.issues[0]?.message ?? 'No usable rows were found in that file.',
        );
        return;
      }
      onChange(parsed.rows, 'upload');
    } catch {
      setCsvError('That file could not be read.');
    }
  };

  const updateManualRow = (index: number, patch: Partial<HistoryPoint>) => {
    const next = rows.map((row, position) =>
      position === index ? { ...row, ...patch } : row,
    );
    onChange(next, 'manual');
  };

  const appendManualRow = () => {
    const last = rows[rows.length - 1];
    const nextDate = last ? addDays(last.date, 1) : toIsoDate(new Date());
    onChange([...rows, { date: nextDate, sales: 0 }], 'manual');
  };

  const errors = validation.issues.filter((issue) => issue.severity === 'error');
  const warnings = validation.issues.filter((issue) => issue.severity === 'warning');

  return (
    <Card>
      <CardHeader>
        <CardTitle>Historical sales</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div role="tablist" aria-label="History source" className="flex flex-wrap gap-2">
          {MODES.map((option) => {
            const Icon = option.icon;
            const isActive = mode === option.id;
            return (
              <Button
                key={option.id}
                role="tab"
                aria-selected={isActive}
                size="sm"
                variant={isActive ? 'secondary' : 'ghost'}
                onClick={() => setMode(option.id)}
              >
                <Icon aria-hidden="true" className="h-3.5 w-3.5" />
                {option.label}
              </Button>
            );
          })}
        </div>

        {mode === 'sample' ? (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {RECOMMENDED_HISTORY_DAYS} days of synthetic sales with weekly
              seasonality and a mild upward trend, ending yesterday. Generated
              from a fixed seed, so it is the same every time.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={loadSample}>
                <Sparkles aria-hidden="true" className="h-3.5 w-3.5" />
                Load demo history
              </Button>
              <StatusBadge tone="warning">Demo data — not real sales</StatusBadge>
            </div>
          </div>
        ) : null}

        {mode === 'upload' ? (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              A CSV with a <span className="font-mono text-xs">date</span> column and a{' '}
              <span className="font-mono text-xs">sales</span> column.{' '}
              <span className="font-mono text-xs">store_id</span>,{' '}
              <span className="font-mono text-xs">quantity_sold</span>,{' '}
              <span className="font-mono text-xs">profit</span>, and{' '}
              <span className="font-mono text-xs">customer_traffic</span> are used
              if present.
            </p>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void handleFile(file);
                // Reset so selecting the same file again re-triggers a change.
                event.target.value = '';
              }}
            />
            <Button size="sm" variant="outline" onClick={() => fileInput.current?.click()}>
              <Upload aria-hidden="true" className="h-3.5 w-3.5" />
              Choose CSV file
            </Button>
            {csvError ? (
              <p className="flex items-start gap-2 text-sm text-destructive">
                <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                {csvError}
              </p>
            ) : null}
            {csvHeaders.length > 0 ? (
              <p className="text-xs text-muted-foreground">
                Columns detected: <span className="font-mono">{csvHeaders.join(', ')}</span>
              </p>
            ) : null}
          </div>
        ) : null}

        {mode === 'manual' ? (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              At least {MIN_HISTORY_DAYS} days are required. Start from a dated
              template and fill in the sales figures.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={loadManualTemplate}>
                <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                Start {MIN_HISTORY_DAYS}-day template
              </Button>
              {rows.length > 0 ? (
                <Button size="sm" variant="ghost" onClick={appendManualRow}>
                  <Plus aria-hidden="true" className="h-3.5 w-3.5" />
                  Add a day
                </Button>
              ) : null}
            </div>
            {rows.length > 0 ? (
              <div className="max-h-72 overflow-y-auto rounded-md border">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-muted/80 backdrop-blur">
                    <tr>
                      <th scope="col" className="px-3 py-2 text-left font-medium">
                        Date
                      </th>
                      <th scope="col" className="px-3 py-2 text-right font-medium">
                        Sales
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, index) => (
                      <tr key={row.date} className="border-t">
                        <td className="px-3 py-1.5">
                          <Label className="sr-only" htmlFor={`date-${index}`}>
                            Date for row {index + 1}
                          </Label>
                          <Input
                            id={`date-${index}`}
                            type="date"
                            value={row.date}
                            className="h-8"
                            onChange={(event) =>
                              updateManualRow(index, { date: event.target.value })
                            }
                          />
                        </td>
                        <td className="px-3 py-1.5">
                          <Label className="sr-only" htmlFor={`sales-${index}`}>
                            Sales for row {index + 1}
                          </Label>
                          <Input
                            id={`sales-${index}`}
                            type="number"
                            min={0}
                            step="any"
                            value={row.sales}
                            className="h-8 text-right"
                            onChange={(event) =>
                              updateManualRow(index, {
                                sales: Number(event.target.value) || 0,
                              })
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        ) : null}

        {rows.length > 0 ? (
          <div className="space-y-3 rounded-md border bg-muted/30 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="flex items-center gap-2 text-sm font-medium">
                {validation.isValid ? (
                  <CheckCircle2
                    aria-hidden="true"
                    className="h-4 w-4 text-emerald-500"
                  />
                ) : (
                  <AlertCircle aria-hidden="true" className="h-4 w-4 text-destructive" />
                )}
                {validation.stats.rowCount} day
                {validation.stats.rowCount === 1 ? '' : 's'} loaded
              </p>
              <div className="flex items-center gap-2">
                {source === 'sample' ? (
                  <StatusBadge tone="warning">Demo</StatusBadge>
                ) : null}
                {source === 'dataset' ? (
                  <StatusBadge tone="success">Training dataset</StatusBadge>
                ) : null}
                <Button size="sm" variant="ghost" onClick={onClear}>
                  <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                  Clear
                </Button>
              </div>
            </div>

            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
              <div>
                <dt className="text-muted-foreground">Range</dt>
                <dd className="font-medium">
                  {validation.stats.startDate && validation.stats.endDate
                    ? formatDateRange(
                        validation.stats.startDate,
                        validation.stats.endDate,
                      )
                    : '—'}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Average</dt>
                <dd className="font-medium tabular-nums">
                  {formatSales(validation.stats.mean)}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Range of values</dt>
                <dd className="font-medium tabular-nums">
                  {formatSales(validation.stats.min)} – {formatSales(validation.stats.max)}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Missing days</dt>
                <dd
                  className={cn(
                    'font-medium tabular-nums',
                    validation.stats.missingDays > 0 ? 'text-amber-500' : '',
                  )}
                >
                  {validation.stats.missingDays}
                </dd>
              </div>
            </dl>

            {errors.length > 0 ? (
              <ul className="space-y-1">
                {errors.map((issue) => (
                  <li
                    key={issue.message}
                    className="flex items-start gap-2 text-xs text-destructive"
                  >
                    <AlertCircle aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {issue.message}
                  </li>
                ))}
              </ul>
            ) : null}

            {warnings.length > 0 ? (
              <ul className="space-y-1">
                {warnings.map((issue) => (
                  <li
                    key={issue.message}
                    className="flex items-start gap-2 text-xs text-muted-foreground"
                  >
                    <Info aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {issue.message}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
