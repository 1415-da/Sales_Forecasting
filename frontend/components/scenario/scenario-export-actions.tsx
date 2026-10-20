'use client';

import { useCallback } from 'react';
import { FileDown, FileJson } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  buildComparisonCsv,
  comparisonFileName,
} from '@/lib/utils/scenario-results';
import type { ScenarioComparisonResponse } from '@/lib/types/scenario';

export interface ScenarioExportActionsProps {
  comparison: ScenarioComparisonResponse;
  /** Disables export once the scenario has been withdrawn. */
  isRolledBack?: boolean;
  /** Warns that the figures no longer match the current inputs. */
  isStale?: boolean;
  className?: string;
}

/** Trigger a client-side download without leaving the page. */
function downloadFile(contents: string, fileName: string, mimeType: string): void {
  const blob = new Blob([contents], { type: `${mimeType};charset=utf-8;` });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking immediately can cancel the download in some browsers, so the URL
  // is released on the next tick instead.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function ScenarioExportActions({
  comparison,
  isRolledBack = false,
  isStale = false,
  className,
}: ScenarioExportActionsProps) {
  const handleCsv = useCallback(() => {
    downloadFile(
      buildComparisonCsv(comparison),
      comparisonFileName(comparison, 'csv'),
      'text/csv',
    );
  }, [comparison]);

  const handleJson = useCallback(() => {
    downloadFile(
      JSON.stringify(comparison, null, 2),
      comparisonFileName(comparison, 'json'),
      'application/json',
    );
  }, [comparison]);

  return (
    <div className={className}>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={handleCsv}
          disabled={isRolledBack}
          title={
            isRolledBack
              ? 'This scenario was rolled back and cannot be exported'
              : 'Download the daily comparison as CSV'
          }
        >
          <FileDown aria-hidden="true" className="h-4 w-4" />
          Download CSV
        </Button>
        <Button variant="ghost" size="sm" onClick={handleJson} disabled={isRolledBack}>
          <FileJson aria-hidden="true" className="h-4 w-4" />
          Download JSON
        </Button>
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        {isRolledBack
          ? 'Export is unavailable for a rolled-back scenario.'
          : isStale
            ? 'These figures reflect the previous assumptions; update the scenario before sharing them.'
            : 'CSV includes daily baseline and scenario medians with their P10 and P90 bounds.'}
      </p>
    </div>
  );
}
