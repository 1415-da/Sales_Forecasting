'use client';

/**
 * Administration: users, roles, and the audit trail.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  ApiError,
  createUser,
  getAuditLogs,
  getPermissionMatrix,
  getUsers,
  resetUserPassword,
  setUserActive,
  updateUser,
} from '@/lib/api';
import type {
  AdminUserList,
  AuditLogList,
  CreatedUser,
  PermissionMatrix,
  ResetPasswordResult,
} from '@/lib/types/auth';

function retry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  return error instanceof ApiError ? error.isRetryable : false;
}

export interface UserFilters {
  readonly role?: string;
  readonly isActive?: boolean;
  readonly search?: string;
  readonly limit?: number;
  readonly offset?: number;
}

/** A page of accounts. */
export function useUsers(filters: UserFilters): UseQueryResult<AdminUserList, Error> {
  return useQuery<AdminUserList, Error>({
    queryKey: ['admin', 'users', filters],
    queryFn: ({ signal }) => getUsers(filters, signal),
    // Short: an admin who just changed a role expects the table to reflect it.
    staleTime: 10_000,
    retry,
  });
}

/** The role and permission matrix the API enforces. */
export function usePermissionMatrix(
  enabled = true,
): UseQueryResult<PermissionMatrix, Error> {
  return useQuery<PermissionMatrix, Error>({
    queryKey: ['admin', 'permissions'],
    queryFn: ({ signal }) => getPermissionMatrix(signal),
    enabled,
    // Static for a deployment's lifetime.
    staleTime: 60 * 60_000,
    retry,
  });
}

/** A page of audit records. */
export function useAuditLogs(params: {
  actions?: string[];
  outcome?: string;
  limit?: number;
  offset?: number;
}): UseQueryResult<AuditLogList, Error> {
  return useQuery<AuditLogList, Error>({
    queryKey: ['admin', 'audit-logs', params],
    queryFn: ({ signal }) => getAuditLogs(params, signal),
    staleTime: 15_000,
    retry,
  });
}

/**
 * The four account mutations.
 *
 * Grouped so a page gets them in one call and they share one invalidation
 * rule. None retries: creating a user twice because of a retry would be a
 * genuine problem.
 */
export function useUserMutations() {
  const queryClient = useQueryClient();

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['admin', 'users'] });
    // A role change can affect the signed-in user's own permissions.
    queryClient.invalidateQueries({ queryKey: ['admin', 'audit-logs'] });
  };

  const create = useMutation<
    CreatedUser,
    Error,
    {
      email: string;
      full_name: string;
      role: string;
      password?: string;
      is_active?: boolean;
    }
  >({
    mutationFn: (payload) => createUser(payload),
    onSuccess: invalidate,
    retry: false,
  });

  const update = useMutation({
    mutationFn: ({
      userId,
      ...payload
    }: {
      userId: string;
      full_name?: string;
      role?: string;
      is_active?: boolean;
    }) => updateUser(userId, payload),
    onSuccess: invalidate,
    retry: false,
  });

  const setActive = useMutation({
    mutationFn: ({ userId, active }: { userId: string; active: boolean }) =>
      setUserActive(userId, active),
    onSuccess: invalidate,
    retry: false,
  });

  const resetPassword = useMutation<ResetPasswordResult, Error, string>({
    mutationFn: (userId) => resetUserPassword(userId),
    onSuccess: invalidate,
    retry: false,
  });

  return { create, update, setActive, resetPassword };
}
