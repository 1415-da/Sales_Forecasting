'use client';

import { Copy, Loader2 } from 'lucide-react';
import { useState } from 'react';

import { ErrorState } from '@/components/shared/error-state';
import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { ROLE_DESCRIPTIONS, ROLE_LABELS, ROLES, type Role } from '@/lib/rbac';
import type { AdminUser } from '@/lib/types/auth';

/** Shared modal frame. */
function Modal({
  title,
  description,
  children,
  onClose,
}: {
  readonly title: string;
  readonly description?: string;
  readonly children: React.ReactNode;
  readonly onClose: () => void;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg space-y-4 rounded-lg border bg-card p-6 shadow-xl">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">{title}</h2>
          {description ? (
            <p className="text-sm text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {children}
      </div>
    </div>
  );
}

/** A role selector that explains what each role can do. */
function RoleSelect({
  id,
  value,
  onChange,
}: {
  readonly id: string;
  readonly value: Role;
  readonly onChange: (role: Role) => void;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>Role</Label>
      <Select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value as Role)}
      >
        {ROLES.map((role) => (
          <option key={role} value={role}>
            {ROLE_LABELS[role]}
          </option>
        ))}
      </Select>
      {/* The description sits under the selector rather than in a tooltip:
          assigning a role is a security decision and the consequence should be
          readable without hovering. */}
      <p className="text-xs text-muted-foreground">{ROLE_DESCRIPTIONS[value]}</p>
    </div>
  );
}

/** A one-time secret, with a copy button. */
export function OneTimePassword({
  password,
  message,
}: {
  readonly password: string;
  readonly message: string;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <div className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-4">
      <p className="text-sm">{message}</p>
      <div className="flex items-center gap-2">
        <code className="flex-1 break-all rounded bg-background px-3 py-2 font-mono text-sm">
          {password}
        </code>
        <Button
          size="sm"
          variant="outline"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(password);
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            } catch {
              setCopied(false);
            }
          }}
        >
          <Copy aria-hidden="true" className="h-3.5 w-3.5" />
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        This is shown once and is not stored in readable form. Share it over a
        trusted channel.
      </p>
    </div>
  );
}

export interface CreateUserDialogProps {
  readonly isPending: boolean;
  readonly error: unknown;
  readonly result: { password: string | null; message: string } | null;
  readonly onSubmit: (payload: {
    email: string;
    full_name: string;
    role: Role;
  }) => void;
  readonly onClose: () => void;
}

/**
 * Create an account.
 *
 * No password field: the server generates one and returns it once. An
 * administrator inventing a password is both worse entropy and one more secret
 * to communicate.
 */
export function CreateUserDialog({
  isPending,
  error,
  result,
  onSubmit,
  onClose,
}: CreateUserDialogProps) {
  const [email, setEmail] = useState('');
  const [fullName, setFullName] = useState('');
  const [role, setRole] = useState<Role>('VIEWER');

  if (result) {
    return (
      <Modal title="Account created" onClose={onClose}>
        {result.password ? (
          <OneTimePassword password={result.password} message={result.message} />
        ) : (
          <p className="text-sm">{result.message}</p>
        )}
        <div className="flex justify-end">
          <Button onClick={onClose}>Done</Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      title="Create a user"
      description="The server generates a password and shows it once. The account must change it at first sign-in."
      onClose={onClose}
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit({ email: email.trim(), full_name: fullName.trim(), role });
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="new-email">Email</Label>
          <Input
            id="new-email"
            type="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="new-name">Full name</Label>
          <Input
            id="new-name"
            required
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
          />
        </div>
        <RoleSelect id="new-role" value={role} onChange={setRole} />

        {error ? <ErrorState error={error} /> : null}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={isPending || !email.trim() || !fullName.trim()}>
            {isPending ? (
              <>
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                Creating…
              </>
            ) : (
              'Create user'
            )}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export interface EditUserDialogProps {
  readonly user: AdminUser;
  readonly isPending: boolean;
  readonly error: unknown;
  readonly onSubmit: (payload: { full_name?: string; role?: Role }) => void;
  readonly onClose: () => void;
}

/**
 * Edit an account.
 *
 * A role change requires a second confirmation: it takes effect immediately by
 * revoking that user's sessions, so it is not something to do by misclicking a
 * dropdown.
 */
export function EditUserDialog({
  user,
  isPending,
  error,
  onSubmit,
  onClose,
}: EditUserDialogProps) {
  const [fullName, setFullName] = useState(user.full_name);
  const [role, setRole] = useState<Role>(user.role as Role);
  const [confirmed, setConfirmed] = useState(false);

  const roleChanged = role !== user.role;

  return (
    <Modal title={`Edit ${user.email}`} onClose={onClose}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          const payload: { full_name?: string; role?: Role } = {};
          if (fullName.trim() !== user.full_name) payload.full_name = fullName.trim();
          if (roleChanged) payload.role = role;
          onSubmit(payload);
        }}
      >
        <div className="space-y-2">
          <Label htmlFor="edit-name">Full name</Label>
          <Input
            id="edit-name"
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
          />
        </div>

        <RoleSelect id="edit-role" value={role} onChange={setRole} />

        {roleChanged ? (
          <label className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="mt-0.5"
            />
            <span>
              Change the role from{' '}
              <StatusBadge tone="neutral">{ROLE_LABELS[user.role as Role]}</StatusBadge>{' '}
              to <StatusBadge tone="info">{ROLE_LABELS[role]}</StatusBadge>. This
              takes effect immediately and signs the user out of every session.
            </span>
          </label>
        ) : null}

        {error ? <ErrorState error={error} /> : null}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={
              isPending ||
              (roleChanged && !confirmed) ||
              (!roleChanged && fullName.trim() === user.full_name)
            }
          >
            {isPending ? (
              <>
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                Saving…
              </>
            ) : (
              'Save changes'
            )}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export interface ConfirmDialogProps {
  readonly title: string;
  readonly body: React.ReactNode;
  readonly confirmLabel: string;
  readonly destructive?: boolean;
  readonly isPending: boolean;
  readonly error: unknown;
  readonly onConfirm: () => void;
  readonly onClose: () => void;
}

/** Generic confirmation, for deactivate / reactivate / reset. */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  destructive = false,
  isPending,
  error,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-4">
        <div className="text-sm">{body}</div>
        {error ? <ErrorState error={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <Button
            variant={destructive ? 'destructive' : 'default'}
            onClick={onConfirm}
            disabled={isPending}
          >
            {isPending ? (
              <>
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
                Working…
              </>
            ) : (
              confirmLabel
            )}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
