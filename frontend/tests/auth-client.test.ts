/**
 * The API client's authentication behaviour.
 *
 * The two properties that matter most here are hard to get right and easy to
 * get silently wrong: a 401 triggers exactly one refresh-and-retry, and
 * concurrent 401s share a single refresh. Without the second, a page that
 * fires several queries on mount would start several refreshes, each rotating
 * the token and revoking the previous one — and the server would correctly
 * read the losers as replay and revoke the whole session. The user would be
 * logged out by the act of loading the page.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  configureAuth,
  friendlyMessage,
  getModels,
  login,
  logout,
  refreshSession,
} from '@/lib/api';
import {
  clearAccessToken,
  getAccessToken,
  isExpiring,
  setAccessToken,
} from '@/lib/auth-store';

const PROFILE = {
  id: 'u1',
  email: 'ada@example.com',
  full_name: 'Ada',
  role: 'ANALYST',
  role_description: 'Analyst',
  is_active: true,
  must_change_password: false,
  permissions: ['view_forecast'],
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  last_login_at: null,
};

const EMPTY_MODELS = {
  success: true,
  models: [],
  default_model: null,
  production_available: false,
  supported_stores: [],
  warnings: [],
};

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function envelope(code: string, status: number) {
  return json(
    { success: false, error: { code, message: 'nope', request_id: 'req_x' } },
    status,
  );
}

beforeEach(() => {
  clearAccessToken();
  configureAuth({});
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearAccessToken();
  configureAuth({});
});

describe('the token store', () => {
  it('keeps the token in memory and loses it when cleared', () => {
    setAccessToken('abc', 3600);
    expect(getAccessToken()).toBe('abc');

    // In memory means exactly this: nothing outlives the clear. There is no
    // persistence call to make, which is the point -- a token in
    // `localStorage` would be readable by any script on the page, and that is
    // the exposure the HttpOnly refresh cookie exists to avoid.
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
  });

  it('exposes no persistence API', async () => {
    // A future change that started writing the token to storage would have to
    // add an export here, which this assertion makes visible in review.
    const store = await import('@/lib/auth-store');
    expect(Object.keys(store).sort()).toEqual([
      'REFRESH_MARGIN_MS',
      'clearAccessToken',
      'getAccessToken',
      'isExpiring',
      'millisecondsUntilRefresh',
      'onAccessTokenChange',
      'setAccessToken',
    ]);
  });

  it('treats an absent token as expiring', () => {
    clearAccessToken();
    expect(isExpiring()).toBe(true);
  });

  it('treats a fresh token as not expiring', () => {
    setAccessToken('abc', 3600);
    expect(isExpiring()).toBe(false);
  });

  it('treats a token inside the refresh margin as expiring', () => {
    // Shorter than the margin, so it should already be due for refresh.
    setAccessToken('abc', 5);
    expect(isExpiring()).toBe(true);
  });

  it('does not guess when the lifetime is unknown', () => {
    // Guessing would cause a refresh storm.
    setAccessToken('abc');
    expect(isExpiring()).toBe(false);
  });
});

describe('request authentication', () => {
  it('attaches the bearer token and includes credentials', async () => {
    setAccessToken('tok-123', 3600);
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => json(EMPTY_MODELS),
    );
    vi.stubGlobal('fetch', fetchMock);

    await getModels();

    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer tok-123');
    // Required for the HttpOnly refresh cookie to reach /auth/*.
    expect(init.credentials).toBe('include');
  });

  it('omits the header when there is no session', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => json(EMPTY_MODELS),
    );
    vi.stubGlobal('fetch', fetchMock);

    await getModels();

    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it('stores the token after a successful login', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          success: true,
          access_token: 'fresh',
          token_type: 'bearer',
          expires_in: 2700,
          user: PROFILE,
        }),
      ),
    );

    const result = await login('ada@example.com', 'pw');
    expect(result.user.email).toBe('ada@example.com');
    expect(getAccessToken()).toBe('fresh');
  });

  it('clears the token on logout even when the call fails', async () => {
    setAccessToken('tok', 3600);
    vi.stubGlobal('fetch', vi.fn(async () => envelope('INTERNAL_SCENARIO_ERROR', 500)));

    await expect(logout()).rejects.toBeInstanceOf(ApiError);
    // A logout that leaves the client believing it is signed in is worse than
    // one the server did not record.
    expect(getAccessToken()).toBeNull();
  });
});

describe('refresh on 401', () => {
  it('refreshes once and retries the original request', async () => {
    setAccessToken('stale', 3600);
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith('/auth/refresh')) {
        return json({
          success: true,
          access_token: 'renewed',
          token_type: 'bearer',
          expires_in: 2700,
          user: PROFILE,
        });
      }
      // First attempt 401s; the retry succeeds.
      return calls.filter((item) => item.endsWith('/v1/models')).length === 1
        ? envelope('TOKEN_EXPIRED', 401)
        : json(EMPTY_MODELS);
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await getModels();

    expect(result.models).toEqual([]);
    expect(calls.filter((url) => url.endsWith('/auth/refresh'))).toHaveLength(1);
    expect(calls.filter((url) => url.endsWith('/v1/models'))).toHaveLength(2);
    expect(getAccessToken()).toBe('renewed');
  });

  it('gives up and reports session loss when the refresh fails', async () => {
    setAccessToken('stale', 3600);
    const lost = vi.fn();
    configureAuth({ onSessionLost: lost });

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).endsWith('/auth/refresh')
          ? envelope('TOKEN_INVALID', 401)
          : envelope('NOT_AUTHENTICATED', 401),
      ),
    );

    await expect(getModels()).rejects.toMatchObject({ status: 401 });
    expect(lost).toHaveBeenCalled();
    expect(getAccessToken()).toBeNull();
  });

  it('shares one refresh between concurrent 401s', async () => {
    setAccessToken('stale', 3600);
    let refreshes = 0;
    let modelCalls = 0;

    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/auth/refresh')) {
          refreshes += 1;
          // A real refresh is not instantaneous; the delay is what makes the
          // de-duplication observable.
          await new Promise((resolve) => setTimeout(resolve, 10));
          return json({
            success: true,
            access_token: `renewed-${refreshes}`,
            token_type: 'bearer',
            expires_in: 2700,
            user: PROFILE,
          });
        }
        modelCalls += 1;
        return modelCalls <= 3 ? envelope('TOKEN_EXPIRED', 401) : json(EMPTY_MODELS);
      }),
    );

    await Promise.all([getModels(), getModels(), getModels()]);

    // One refresh, not three. Three would rotate the token three times and the
    // server would read two of them as replay and kill the session.
    expect(refreshes).toBe(1);
  });

  it('never refreshes on the login route', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return envelope('INVALID_CREDENTIALS', 401);
      }),
    );

    await expect(login('ada@example.com', 'wrong')).rejects.toMatchObject({
      code: 'INVALID_CREDENTIALS',
    });
    // Retrying a wrong password after a refresh would fail again and hide the
    // real error from the user.
    expect(calls.filter((url) => url.endsWith('/auth/refresh'))).toHaveLength(0);
  });

  it('reports a failed refresh without throwing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => envelope('TOKEN_INVALID', 401)));
    await expect(refreshSession()).resolves.toBe(false);
  });
});

describe('friendlyMessage for the auth codes', () => {
  it.each([
    ['INVALID_CREDENTIALS', 'Incorrect email or password.'],
    ['ACCOUNT_LOCKED', 'Too many failed sign-in attempts'],
    ['PERMISSION_DENIED', 'You do not have permission'],
    ['REGISTRATION_DISABLED', 'Self-registration is disabled'],
    ['TOKEN_EXPIRED', 'session expired'],
  ])('maps %s to actionable guidance', (code, fragment) => {
    const message = friendlyMessage(
      new ApiError('raw server text', { kind: 'http', status: 401, code }),
    );
    expect(message).toContain(fragment);
  });

  it('never echoes raw server text for an unknown code', () => {
    const message = friendlyMessage(
      new ApiError('Traceback: /usr/local/lib/secret', {
        kind: 'http',
        status: 500,
        code: 'SOMETHING_NEW',
      }),
    );
    expect(message).not.toContain('/usr/local');
    expect(message).not.toContain('Traceback');
  });
});
