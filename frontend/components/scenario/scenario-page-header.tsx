'use client';

import { Download, RotateCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';

export interface ScenarioPageHeaderProps {
  onReset: () => void;
  onExport: () => void;
  /** Export stays disabled until there is a comparison to export. */
  hasResult: boolean;
  isResetDisabled?: boolean;
}

export function ScenarioPageHeader({
  onReset,
  onExport,
  hasResult,
  isResetDisabled = false,
}: ScenarioPageHeaderProps) {
  return (
    <header className="flex flex-col gap-3 border-b pb-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Scenario Planner</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Compare business assumptions against the current baseline forecast.
        </p>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={onReset}
          disabled={isResetDisabled}
          aria-label="Reset scenario inputs"
        >
          <RotateCcw aria-hidden="true" className="h-4 w-4" />
          Reset
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onExport}
          disabled={!hasResult}
          aria-label="Export comparison"
          /* Explains the disabled state to assistive tech rather than leaving
             the user to guess why the control is inert. */
          title={hasResult ? 'Export comparison' : 'Run a scenario to enable export'}
        >
          <Download aria-hidden="true" className="h-4 w-4" />
          Export comparison
        </Button>
      </div>
    </header>
  );
}
