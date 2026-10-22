/**
 * Typed client for the unknown-api terminal API (ADR-0013).
 *
 * - Reads: `get(endpoint, params)` for every GET route in server.py, typed through `EndpointResponse`.
 * - Auth: `session`, `login`, `reauth`, `logout`. The session cookie is HttpOnly; the CSRF token from
 *   login/session is kept in memory only and echoed in `X-CSRF-Token` on every POST.
 * - Commands: `sendCommand(kind, payload)`. The client chooses the durable command id before sending,
 *   so a retry after a transport failure or a step-up reuses it and the core returns the stored result
 *   instead of acting twice. A 428 runs the step-up handler (passphrase re-entry) and retries once.
 * - 401 on anything except a passphrase check calls `onUnauthorized` (the app shows the login screen).
 */
import type {
  AuthSession,
  CommandKind,
  CommandPayload,
  CommandResponse,
  CommandResult,
  Endpoint,
  LoginResponse,
  LogoutResponse,
  OrderDetail,
  ParamsOf,
  ReauthResponse,
  ResponseOf,
  SessionDetail,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly method: string;
  readonly endpoint: string;
  /** The server's `{"error": ...}` message when present. */
  readonly serverMessage: string | null;
  readonly body: unknown;

  constructor(method: string, endpoint: string, status: number, serverMessage: string | null, body: unknown) {
    super(serverMessage ? `${serverMessage} (HTTP ${status})` : `HTTP ${status} from ${method} ${endpoint}`);
    this.name = 'ApiError';
    this.method = method;
    this.endpoint = endpoint;
    this.status = status;
    this.serverMessage = serverMessage;
    this.body = body;
  }
}

/** 401: no valid session. The app returns to the login screen. */
export class UnauthorizedError extends ApiError {
  constructor(method: string, endpoint: string, serverMessage: string | null, body: unknown) {
    super(method, endpoint, 401, serverMessage, body);
    this.name = 'UnauthorizedError';
  }
}

/** 401 from a passphrase check (login or step-up): the passphrase was wrong; the session is unaffected. */
export class InvalidPassphraseError extends ApiError {
  constructor(method: string, endpoint: string, serverMessage: string | null, body: unknown) {
    super(method, endpoint, 401, serverMessage, body);
    this.name = 'InvalidPassphraseError';
  }
}

/** 429: failed-passphrase backoff is active. */
export class RateLimitedError extends ApiError {
  constructor(method: string, endpoint: string, serverMessage: string | null, body: unknown) {
    super(method, endpoint, 429, serverMessage, body);
    this.name = 'RateLimitedError';
  }
}

/** 428 that could not be satisfied (no handler, or the server still refused after re-authentication). */
export class StepUpRequiredError extends ApiError {
  readonly commandId: string;
  constructor(endpoint: string, serverMessage: string | null, body: unknown, commandId: string) {
    super('POST', endpoint, 428, serverMessage, body);
    this.name = 'StepUpRequiredError';
    this.commandId = commandId;
  }
}

/** The owner dismissed the passphrase prompt; the command was not sent again. */
export class StepUpCancelledError extends Error {
  readonly commandId: string;
  constructor(commandId: string) {
    super('Re-authentication was cancelled; the command was not executed.');
    this.name = 'StepUpCancelledError';
    this.commandId = commandId;
  }
}

/** The request never produced an HTTP response (API process down, network error, aborted). */
export class NetworkError extends Error {
  readonly method: string;
  readonly endpoint: string;
  constructor(method: string, endpoint: string, cause: unknown) {
    super(`API unreachable for ${method} ${endpoint}: ${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = 'NetworkError';
    this.method = method;
    this.endpoint = endpoint;
  }
}

/**
 * Command-path failure that carries the durable command id so the UI can show it and retry safely.
 * `outcomeUnknown` is true when the command may have reached the core (network failure, 5xx): a retry
 * must reuse the same id so the core returns the recorded result instead of acting twice. It is false
 * when the API refused the request before forwarding it (4xx), so nothing was recorded.
 */
export class CommandTransportError extends Error {
  readonly commandId: string;
  readonly kind: CommandKind;
  readonly outcomeUnknown: boolean;
  readonly status: number | null;
  override readonly cause: unknown;
  constructor(kind: CommandKind, commandId: string, cause: unknown) {
    const status = cause instanceof ApiError ? cause.status : null;
    const outcomeUnknown = status === null || status >= 500;
    const detail = cause instanceof ApiError ? (cause.serverMessage ?? cause.message) : cause instanceof Error ? cause.message : String(cause);
    super(
      outcomeUnknown
        ? `${kind} ${commandId}: ${detail}. Outcome unknown; retrying with the same command id is safe (the core records each id once).`
        : `${kind} ${commandId} refused before reaching the core: ${detail}`,
    );
    this.name = 'CommandTransportError';
    this.kind = kind;
    this.commandId = commandId;
    this.outcomeUnknown = outcomeUnknown;
    this.status = status;
    this.cause = cause;
  }
}

export interface StepUpRequest {
  kind: CommandKind;
  commandId: string;
  /** Server message, e.g. "re-authentication required for this command". */
  reason: string;
}

/** Shows the passphrase prompt and calls `client.reauth`. Resolves true when re-authentication succeeded. */
export type StepUpHandler = (request: StepUpRequest) => Promise<boolean>;

export type QueryValue = string | number | boolean | null | undefined;

export interface ApiClientOptions {
  /** Prefix for every request; '' means same origin (production static server or Vite proxy). */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  onUnauthorized?: (error: UnauthorizedError) => void;
  stepUp?: StepUpHandler;
  newCommandId?: (kind: CommandKind) => string;
}

export interface SendCommandOptions {
  /** Reuse an id to retry a command whose outcome is unknown. Defaults to a fresh `ui.<kind>.<hex>`. */
  commandId?: string;
  signal?: AbortSignal;
}

function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** Matches backend domain/ids.py: `ui.<kind>.<24 hex>` is always a valid command id. */
export function defaultCommandId(kind: CommandKind): string {
  return `ui.${kind}.${randomHex(12)}`;
}

/** Stable query string: keys sorted, empty values dropped (also used as the cache key). */
export function buildQuery(params: Record<string, QueryValue> | undefined): string {
  if (!params) return '';
  const keys = Object.keys(params)
    .filter((k) => {
      const v = params[k];
      return v !== undefined && v !== null && v !== '';
    })
    .sort();
  if (keys.length === 0) return '';
  return '?' + keys.map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(String(params[k]))}`).join('&');
}

/**
 * Encode one path segment. The API splits the path on "/" and then percent-decodes each segment, so "/"
 * inside an id travels as "%2F"; characters that are legal in a segment (":", "@", "+") are left as-is.
 */
export function encodePathSegment(id: string): string {
  return encodeURIComponent(id).replace(/%3A/gi, ':').replace(/%40/gi, '@').replace(/%2B/gi, '+');
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function serverMessage(body: unknown): string | null {
  if (body && typeof body === 'object' && typeof (body as { error?: unknown }).error === 'string') {
    return (body as { error: string }).error;
  }
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 300);
  return null;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private csrfToken: string | null = null;
  private onUnauthorized: ((error: UnauthorizedError) => void) | undefined;
  private stepUp: StepUpHandler | undefined;
  private readonly newCommandId: (kind: CommandKind) => string;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = options.baseUrl ?? '';
    // Bind so a bare `fetch` keeps its global receiver.
    const f = options.fetchImpl ?? globalThis.fetch;
    this.fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => f(input, init)) as typeof fetch;
    this.onUnauthorized = options.onUnauthorized;
    this.stepUp = options.stepUp;
    this.newCommandId = options.newCommandId ?? defaultCommandId;
  }

  setUnauthorizedHandler(handler: ((error: UnauthorizedError) => void) | undefined): void {
    this.onUnauthorized = handler;
  }

  setStepUpHandler(handler: StepUpHandler | undefined): void {
    this.stepUp = handler;
  }

  get csrf(): string | null {
    return this.csrfToken;
  }

  setCsrfToken(token: string | null): void {
    this.csrfToken = token;
  }

  // ---------------------------------------------------------------- transport

  private async request(
    method: 'GET' | 'POST',
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<{ status: number; body: unknown }> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (method === 'POST') {
      headers['Content-Type'] = 'application/json';
      if (this.csrfToken) headers['X-CSRF-Token'] = this.csrfToken;
    }
    let res: Response;
    try {
      const init: RequestInit = { method, headers, credentials: 'same-origin', cache: 'no-store' };
      if (body !== undefined) init.body = JSON.stringify(body);
      if (signal) init.signal = signal;
      res = await this.fetchImpl(this.baseUrl + path, init);
    } catch (err) {
      throw new NetworkError(method, path, err);
    }
    return { status: res.status, body: await readBody(res) };
  }

  private fail(method: string, path: string, status: number, body: unknown): never {
    const msg = serverMessage(body);
    if (status === 401) {
      const err = new UnauthorizedError(method, path, msg, body);
      this.onUnauthorized?.(err);
      throw err;
    }
    if (status === 429) throw new RateLimitedError(method, path, msg, body);
    throw new ApiError(method, path, status, msg, body);
  }

  private isCsrfRefusal(status: number, body: unknown): boolean {
    return status === 403 && /csrf/i.test(serverMessage(body) ?? '');
  }

  /** POST with one CSRF refresh: a 403 CSRF refusal happens before the core sees anything. */
  private async post(path: string, body: unknown, signal?: AbortSignal): Promise<{ status: number; body: unknown }> {
    let res = await this.request('POST', path, body, signal);
    if (this.isCsrfRefusal(res.status, res.body)) {
      const s = await this.session();
      if (s.authenticated) res = await this.request('POST', path, body, signal);
    }
    return res;
  }

  // ---------------------------------------------------------------- reads

  /** Typed GET for every read endpoint. Throws ApiError / UnauthorizedError / NetworkError. */
  async get<E extends Endpoint>(endpoint: E, params?: ParamsOf<E>, signal?: AbortSignal): Promise<ResponseOf<E>> {
    const path = endpoint + buildQuery(params as Record<string, QueryValue> | undefined);
    const res = await this.request('GET', path, undefined, signal);
    if (res.status < 200 || res.status >= 300) this.fail('GET', path, res.status, res.body);
    return res.body as ResponseOf<E>;
  }

  sessionDetail(sessionId: string, signal?: AbortSignal): Promise<SessionDetail> {
    return this.get(`/api/sessions/${encodePathSegment(sessionId)}`, undefined, signal);
  }

  orderDetail(orderId: string, signal?: AbortSignal): Promise<OrderDetail> {
    return this.get(`/api/orders/${encodePathSegment(orderId)}`, undefined, signal);
  }

  /** The stored result of a command whose outcome is unknown (404 COMMAND_NOT_FOUND: it was not recorded). */
  commandResult(commandId: string, signal?: AbortSignal): Promise<CommandResult> {
    return this.get(`/api/commands/${encodePathSegment(commandId)}`, undefined, signal);
  }

  // ---------------------------------------------------------------- auth

  /** GET /api/auth/session; never 401. Stores the CSRF token when authenticated. */
  async session(): Promise<AuthSession> {
    const res = await this.request('GET', '/api/auth/session');
    if (res.status !== 200) this.fail('GET', '/api/auth/session', res.status, res.body);
    const s = res.body as AuthSession;
    this.csrfToken = s.authenticated ? s.csrf_token : null;
    return s;
  }

  async login(passphrase: string): Promise<LoginResponse> {
    const path = '/api/auth/login';
    const res = await this.request('POST', path, { passphrase });
    if (res.status === 401) throw new InvalidPassphraseError('POST', path, serverMessage(res.body), res.body);
    if (res.status !== 200) this.fail('POST', path, res.status, res.body);
    const body = res.body as LoginResponse;
    this.csrfToken = body.csrf_token;
    return body;
  }

  /** Step-up: re-enter the passphrase within the current session. */
  async reauth(passphrase: string): Promise<ReauthResponse> {
    const path = '/api/auth/reauth';
    const res = await this.post(path, { passphrase });
    if (res.status === 401 && serverMessage(res.body) === 'invalid passphrase') {
      throw new InvalidPassphraseError('POST', path, serverMessage(res.body), res.body);
    }
    if (res.status !== 200) this.fail('POST', path, res.status, res.body);
    return res.body as ReauthResponse;
  }

  async logout(): Promise<LogoutResponse> {
    const path = '/api/auth/logout';
    const res = await this.post(path, {});
    this.csrfToken = null;
    if (res.status !== 200) this.fail('POST', path, res.status, res.body);
    return res.body as LogoutResponse;
  }

  // ---------------------------------------------------------------- commands

  /**
   * Send an owner command. Resolves with the core's decision record (accepted or a deterministic
   * rejection); throws `CommandTransportError` when the outcome is unknown or the command was refused
   * before reaching the core. Never applies anything locally: callers wait for the state stream.
   */
  async sendCommand<K extends CommandKind>(
    kind: K,
    payload: CommandPayload<K>,
    options: SendCommandOptions = {},
  ): Promise<CommandResponse> {
    const commandId = options.commandId ?? this.newCommandId(kind);
    const path = '/api/commands';
    const send = () => this.post(path, { kind, payload, command_id: commandId }, options.signal);
    try {
      let res = await send();
      if (res.status === 428) {
        const reason = serverMessage(res.body) ?? 're-authentication required for this command';
        if (!this.stepUp) throw new StepUpRequiredError(path, reason, res.body, commandId);
        let ok = false;
        try {
          ok = await this.stepUp({ kind, commandId, reason });
        } catch {
          ok = false; // a failed prompt is a cancellation: the 428 means nothing reached the core
        }
        if (!ok) throw new StepUpCancelledError(commandId);
        res = await send();
        if (res.status === 428) {
          throw new StepUpRequiredError(path, serverMessage(res.body), res.body, commandId);
        }
      }
      if (res.status !== 200) this.fail('POST', path, res.status, res.body);
      return res.body as CommandResponse;
    } catch (err) {
      if (err instanceof StepUpCancelledError || err instanceof UnauthorizedError) throw err;
      if (err instanceof StepUpRequiredError || err instanceof InvalidPassphraseError) throw err;
      if (err instanceof RateLimitedError) throw err;
      throw new CommandTransportError(kind, commandId, err);
    }
  }
}

/** Human-readable message for any error thrown by the client. */
export function describeError(err: unknown): string {
  if (err instanceof CommandTransportError) return err.message;
  if (err instanceof ApiError) {
    if (err.status === 503) return `${err.serverMessage ?? 'service unavailable'}`;
    return err.serverMessage ?? err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
