'use client';

import { KeyRound, Pencil, Search, UserPlus } from 'lucide-react';
import { useState } from 'react';

import { RequireAuth } from '@/components/auth/require-auth';
import {
  ConfirmDialog,
  CreateUserDialog,
  EditUserDialog,
  OneTimePassword,
} from '@/components/admin/user-dialogs';
import { EmptyState } from '@/components/shared/empty-state';
import { ErrorState } from '@/components/shared/error-state';
import { TableSkeleton } from '@/components/shared/loading-skeleton';
import { StatusBadge } from '@/components/shared/status-badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';
import { useAuth } from '@/hooks/use-auth';
import { useUserMutations, useUsers } from '@/hooks/use-admin';
import { formatRelative, formatTimestamp } from '@/lib/format';
import { PERMISSIONS, ROLE_LABELS, ROLE_TONE, ROLES, type Role } from '@/lib/rbac';
import type { AdminUser } from '@/lib/types/auth';

const PAGE_SIZE = 25;

/**
 * User administration. ADMIN only.
 *
 * Deactivating and role changes revoke the affected user's sessions server-side,
 * so a change takes effect at once rather than whenever their token happens to
 * expire. The dialogs say so, because "I removed their access an hour ago and
 * they are still in" is otherwise a reasonable thing to worry about.
 */
export default function AdminUsersPage() {
  return (
    <RequireAuth permission={PERMISSIONS.manageUsers}>
      <UserAdministration />
    </RequireAuth>
  );
}

type Dialog =
  | { kind: 'create' }
  | { kind: 'edit'; user: AdminUser }
  | { kind: 'deactivate'; user: AdminUser }
  | { kind: 'activate'; user: AdminUser }
  | { kind: 'reset'; user: AdminUser }
  | null;

function UserAdministration() {
  const { user: currentUser } = useAuth();
  const [role, setRole] = useState('');
  const [activeFilter, setActiveFilter] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [createResult, setCreateResult] = useState<
    { password: string | null; message: string } | null
  >(null);
  const [resetResult, setResetResult] = useState<
    { email: string; password: string; message: string } | null
  >(null);

  const filters = {
    ...(role ? { role } : {}),
    ...(activeFilter ? { isActive: activeFilter === 'active' } : {}),
    ...(search.trim() ? { search: search.trim() } : {}),
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  };

  const users = useUsers(filters);
  const { create, update, setActive, resetPassword } = useUserMutations();

  const totalPages = Math.max(1, Math.ceil((users.data?.total ?? 0) / PAGE_SIZE));

  const closeDialog = () => {
    setDialog(null);
    create.reset();
    update.reset();
    setActive.reset();
    resetPassword.reset();
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight">Users</h1>
          <p className="text-sm text-muted-foreground">
            Accounts, roles, and access. Every change is recorded in the audit
            log.
          </p>
        </div>
        <Button onClick={() => setDialog({ kind: 'create' })}>
          <UserPlus aria-hidden="true" className="h-4 w-4" />
          Create user
        </Button>
      </div>

      {resetResult ? (
        <Card>
          <CardHeader className="space-y-1">
            <CardTitle>Password reset for {resetResult.email}</CardTitle>
            <CardDescription>
              Every session for that account has been revoked.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <OneTimePassword
              password={resetResult.password}
              message={resetResult.message}
            />
            <Button size="sm" variant="ghost" onClick={() => setResetResult(null)}>
              Dismiss
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="grid gap-4 p-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label htmlFor="filter-search">Search</Label>
            <div className="relative">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              />
              <Input
                id="filter-search"
                value={search}
                placeholder="Name or email"
                className="pl-9"
                onChange={(event) => {
                  setSearch(event.target.value);
                  setPage(0);
                }}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="filter-role">Role</Label>
            <Select
              id="filter-role"
              value={role}
              onChange={(event) => {
                setRole(event.target.value);
                setPage(0);
              }}
            >
              <option value="">All roles</option>
              {ROLES.map((item) => (
                <option key={item} value={item}>
                  {ROLE_LABELS[item]}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="filter-status">Status</Label>
            <Select
              id="filter-status"
              value={activeFilter}
              onChange={(event) => {
                setActiveFilter(event.target.value);
                setPage(0);
              }}
            >
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="inactive">Deactivated</option>
            </Select>
          </div>
        </CardContent>
      </Card>

      {users.isError ? (
        <ErrorState error={users.error} onRetry={() => void users.refetch()} />
      ) : users.isLoading ? (
        <TableSkeleton rows={6} />
      ) : (users.data?.users.length ?? 0) === 0 ? (
        <EmptyState
          title="No users match"
          description="Adjust the filters, or create an account."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">User accounts</caption>
                <thead className="bg-muted/60">
                  <tr>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Name
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Email
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Role
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Status
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Last sign-in
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      Created
                    </th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {users.data?.users.map((row) => {
                    const isSelf = row.id === currentUser?.id;
                    return (
                      <tr key={row.id} className="border-t hover:bg-muted/40">
                        <td className="px-4 py-2">
                          <span className="font-medium">{row.full_name}</span>
                          {isSelf ? (
                            <span className="ml-2 text-xs text-muted-foreground">
                              you
                            </span>
                          ) : null}
                        </td>
                        <td className="px-4 py-2 text-xs">{row.email}</td>
                        <td className="px-4 py-2">
                          <StatusBadge tone={ROLE_TONE[row.role as Role] ?? 'neutral'}>
                            {ROLE_LABELS[row.role as Role] ?? row.role}
                          </StatusBadge>
                        </td>
                        <td className="px-4 py-2">
                          <div className="flex flex-wrap gap-1">
                            <StatusBadge tone={row.is_active ? 'success' : 'danger'}>
                              {row.is_active ? 'Active' : 'Deactivated'}
                            </StatusBadge>
                            {row.is_locked ? (
                              <StatusBadge tone="warning">Locked</StatusBadge>
                            ) : null}
                            {row.must_change_password ? (
                              <StatusBadge tone="info">Must reset</StatusBadge>
                            ) : null}
                          </div>
                        </td>
                        <td className="px-4 py-2 text-xs text-muted-foreground">
                          {row.last_login_at
                            ? formatRelative(row.last_login_at)
                            : 'never'}
                        </td>
                        <td className="px-4 py-2 text-xs text-muted-foreground">
                          {formatTimestamp(row.created_at)}
                        </td>
                        <td className="px-4 py-2">
                          <div className="flex flex-wrap justify-end gap-1">
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => setDialog({ kind: 'edit', user: row })}
                            >
                              <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
                              Edit
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => setDialog({ kind: 'reset', user: row })}
                            >
                              <KeyRound aria-hidden="true" className="h-3.5 w-3.5" />
                              Reset
                            </Button>
                            {/* Self-deactivation is refused by the API; the
                                button is hidden so the refusal is never a
                                surprise. */}
                            {!isSelf ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() =>
                                  setDialog({
                                    kind: row.is_active ? 'deactivate' : 'activate',
                                    user: row,
                                  })
                                }
                              >
                                {row.is_active ? 'Deactivate' : 'Reactivate'}
                              </Button>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {totalPages > 1 ? (
        <div className="flex items-center justify-between">
          <p className="text-xs text-muted-foreground">
            Showing {page * PAGE_SIZE + 1}–
            {Math.min((page + 1) * PAGE_SIZE, users.data?.total ?? 0)} of{' '}
            {users.data?.total ?? 0}
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={page === 0}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
            >
              Previous
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      ) : null}

      {dialog?.kind === 'create' ? (
        <CreateUserDialog
          isPending={create.isPending}
          error={create.error}
          result={createResult}
          onSubmit={(payload) =>
            create.mutate(payload, {
              onSuccess: (result) =>
                setCreateResult({
                  password: result.temporary_password ?? null,
                  message: result.message,
                }),
            })
          }
          onClose={() => {
            setCreateResult(null);
            closeDialog();
          }}
        />
      ) : null}

      {dialog?.kind === 'edit' ? (
        <EditUserDialog
          user={dialog.user}
          isPending={update.isPending}
          error={update.error}
          onSubmit={(payload) =>
            update.mutate(
              { userId: dialog.user.id, ...payload },
              { onSuccess: closeDialog },
            )
          }
          onClose={closeDialog}
        />
      ) : null}

      {dialog?.kind === 'deactivate' || dialog?.kind === 'activate' ? (
        <ConfirmDialog
          title={
            dialog.kind === 'deactivate' ? 'Deactivate account' : 'Reactivate account'
          }
          destructive={dialog.kind === 'deactivate'}
          confirmLabel={dialog.kind === 'deactivate' ? 'Deactivate' : 'Reactivate'}
          isPending={setActive.isPending}
          error={setActive.error}
          body={
            dialog.kind === 'deactivate' ? (
              <>
                <strong>{dialog.user.email}</strong> will be unable to sign in, and
                every session they currently hold is revoked immediately — their
                existing token stops working rather than lasting until it expires.
              </>
            ) : (
              <>
                <strong>{dialog.user.email}</strong> will be able to sign in again
                with their existing password.
              </>
            )
          }
          onConfirm={() =>
            setActive.mutate(
              { userId: dialog.user.id, active: dialog.kind === 'activate' },
              { onSuccess: closeDialog },
            )
          }
          onClose={closeDialog}
        />
      ) : null}

      {dialog?.kind === 'reset' ? (
        <ConfirmDialog
          title="Reset password"
          confirmLabel="Reset password"
          isPending={resetPassword.isPending}
          error={resetPassword.error}
          body={
            <>
              A new temporary password will be generated for{' '}
              <strong>{dialog.user.email}</strong> and shown once. All of their
              sessions are revoked and any lockout is cleared.
            </>
          }
          onConfirm={() =>
            resetPassword.mutate(dialog.user.id, {
              onSuccess: (result) => {
                setResetResult({
                  email: result.user.email,
                  password: result.temporary_password,
                  message: result.message,
                });
                closeDialog();
              },
            })
          }
          onClose={closeDialog}
        />
      ) : null}
    </div>
  );
}
