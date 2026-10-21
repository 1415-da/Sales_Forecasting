import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

/** Semantic tone, so a caller never picks raw colours. */
export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral';

const TONE_CLASSES: Record<StatusTone, string> = {
  success: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400',
  warning: 'border-amber-500/30 bg-amber-500/10 text-amber-400',
  danger: 'border-red-500/30 bg-red-500/10 text-red-400',
  info: 'border-sky-500/30 bg-sky-500/10 text-sky-400',
  neutral: 'border-border bg-muted text-muted-foreground',
};

export interface StatusBadgeProps {
  readonly children: React.ReactNode;
  readonly tone?: StatusTone;
  readonly className?: string;
}

/** A small, colour-coded state label. */
export function StatusBadge({ children, tone = 'neutral', className }: StatusBadgeProps) {
  return (
    <Badge variant="outline" className={cn('font-medium', TONE_CLASSES[tone], className)}>
      {children}
    </Badge>
  );
}

/**
 * Tone for a model lifecycle stage.
 *
 * Only Production is green: a Staging model answering a forecast request is
 * something the user should notice, not something that blends in.
 */
export function stageTone(stage: string): StatusTone {
  const normalised = stage.toLowerCase();
  if (normalised === 'production') return 'success';
  if (normalised === 'staging') return 'warning';
  if (normalised === 'archived') return 'neutral';
  return 'info';
}

/** Tone for a health component status. */
export function healthTone(status: string): StatusTone {
  switch (status) {
    case 'ok':
      return 'success';
    case 'degraded':
      return 'warning';
    case 'unavailable':
      return 'danger';
    default:
      return 'neutral';
  }
}
