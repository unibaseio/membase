/** Errors, one class per HTTP status class, all carrying the platform's error envelope. */

export interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
    retryable?: boolean;
    trace_id?: string;
  };
  detail?: unknown;
}

export class MembaseError extends Error {
  readonly status: number | undefined;
  readonly body: unknown;
  readonly code: string;
  readonly details: unknown;
  readonly retryable: boolean;
  readonly traceId: string;

  constructor(message: string, opts: { status?: number; body?: unknown } = {}) {
    super(message);
    this.name = new.target.name;
    this.status = opts.status;
    this.body = opts.body;
    const err = (opts.body as ErrorEnvelope | undefined)?.error ?? {};
    this.code = String(err.code ?? "");
    this.details = err.details;
    this.retryable = Boolean(err.retryable ?? false);
    this.traceId = String(err.trace_id ?? "");
  }
}

/** The request never got an answer (DNS, connection refused, reset). */
export class APIConnectionError extends MembaseError {}
/** The request ran past the client's timeout. */
export class APITimeoutError extends APIConnectionError {}

/** A non-2xx answer. */
export class APIStatusError extends MembaseError {}
/** 400 — a malformed request: an empty `q`, both `content` and `url`, a list `limit` outside 1–1000. */
export class BadRequestError extends APIStatusError {}
/** 401 — the request carried no bearer at all. */
export class AuthenticationError extends APIStatusError {}
/** 403 (`code: unauthorized`) — an unknown, expired or revoked key, or a verb above its access level. */
export class PermissionDeniedError extends APIStatusError {}
/** 404 — an id that is not this key's to read: unknown, deleted, outside its reach, or of the wrong type. */
export class NotFoundError extends APIStatusError {}
/** 409. */
export class ConflictError extends APIStatusError {}
/** 422 — a request the API cannot take (`validation`: an unknown field such as `container`, a value out of range), or the account's memory cannot run a turn on this deployment (`capability_unavailable`). */
export class UnprocessableEntityError extends APIStatusError {}
/** 422 `write_target_required` — a write from a key that reaches several Memories and has no write target. Nothing was written; the key's owner sets where its writes go. */
export class WriteTargetRequiredError extends UnprocessableEntityError {}
/** 428 `confirmation_required` — a removal without `confirm: true`, or one this key may not carry out on its own. Nothing changed; `details` names the target (`id`, `verb`, …). */
export class ConfirmationRequiredError extends APIStatusError {}
/** 429 — the account's turn budget is spent for now. */
export class RateLimitError extends APIStatusError {}
/** 5xx. */
export class InternalServerError extends APIStatusError {}

const BY_STATUS: Record<number, new (m: string, o: { status?: number; body?: unknown }) => APIStatusError> = {
  400: BadRequestError,
  401: AuthenticationError,
  403: PermissionDeniedError,
  404: NotFoundError,
  409: ConflictError,
  422: UnprocessableEntityError,
  428: ConfirmationRequiredError,
  429: RateLimitError,
};

const BY_CODE: Record<string, new (m: string, o: { status?: number; body?: unknown }) => APIStatusError> = {
  write_target_required: WriteTargetRequiredError,
};

/** The exception for a non-2xx answer. */
export function errorFor(status: number, body: unknown, fallback = ""): APIStatusError {
  const env = (body ?? {}) as ErrorEnvelope;
  let message = env.error?.message;
  if (!message && env.detail !== undefined) message = typeof env.detail === "string" ? env.detail : JSON.stringify(env.detail);
  message = message || fallback || `HTTP ${status}`;
  const Cls = BY_CODE[String(env.error?.code ?? "")] ?? BY_STATUS[status] ?? (status >= 500 ? InternalServerError : APIStatusError);
  return new Cls(message, { status, body });
}
