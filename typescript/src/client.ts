/** The Membase client: one developer key, the account's memory. */

import { APIConnectionError, APITimeoutError, errorFor } from "./errors.js";

export const VERSION = "0.3.2";
export const DEFAULT_BASE_URL = "https://api.app.membase.com";
/** The first search after a quiet spell can take up to a minute while the memory wakes. */
export const DEFAULT_TIMEOUT_MS = 90_000;
export const DEFAULT_MAX_RETRIES = 2;
const RETRY_STATUSES = new Set([408, 409, 429]);

type Fetch = typeof fetch;

export interface MembaseOptions {
  /** Defaults to `process.env.MEMBASE_API_KEY`. */
  apiKey?: string;
  /** Defaults to `process.env.MEMBASE_BASE_URL`, then `https://api.app.membase.com`. */
  baseUrl?: string;
  /** Per-request timeout. Default 90 s. */
  timeoutMs?: number;
  /** Retries on 408/409/429/5xx and connection errors. Default 2. */
  maxRetries?: number;
  /** A `fetch` to use instead of the global one (tests, custom agents). */
  fetch?: Fetch;
}

export interface Container { id: string; name: string; description?: string; [k: string]: unknown }
export interface ContainerList { containers: Container[] }
export interface SearchHit { content: string; score?: number; container: string; container_name?: string; source?: string; [k: string]: unknown }
export interface SearchResult { query: string; results: SearchHit[]; containers: Array<{ id: string; name: string; error?: string }> }
export interface Profile { available?: boolean; ready?: boolean; static: string[]; dynamic: Array<{ id: number | string; content: string; updated_at?: string }>; results?: SearchHit[] }
export interface Document { id: string; container: string; title?: string; learned?: boolean; [k: string]: unknown }
export interface DocumentList { documents: Document[] }
export interface AddDocumentResult { document_id: string | null; container: string; status: string; path?: string; learning_run?: { id: string; status: string } | null; [k: string]: unknown }
export interface ConfirmationRequired { status: "confirmation_required"; verb: string; how: string; [k: string]: unknown }
export type Json = Record<string, unknown>;

export interface AddParams {
  /** Which container reads it. */
  container: string;
  /** The document's full text. */
  content?: string;
  /** A public web address to fetch instead of `content`. */
  url?: string;
  title?: string;
  /** Your own tags, as an object. */
  metadata?: Record<string, unknown>;
  /** Your own id; adding the same customId again is a no-op. */
  customId?: string;
}

export interface SearchParams {
  /** The question, in natural language. */
  q: string;
  /** One container id; omit to search every container in reach. */
  container?: string;
  /** How many passages in all, not per container (1–50). */
  limit?: number;
}

export class Membase {
  readonly baseUrl: string;
  readonly maxRetries: number;
  readonly timeoutMs: number;
  private readonly apiKey: string;
  private readonly fetchImpl: Fetch;

  readonly containers = {
    /** The containers this key may use — every one, for the account's owner. */
    list: (): Promise<ContainerList> => this.request("GET", "/v1/containers"),
  };

  readonly documents = {
    /** The documents the containers in reach have read, newest first, each with `learned`. */
    list: (params: { container?: string } = {}): Promise<DocumentList> =>
      this.request("GET", "/v1/documents", { query: { container: params.container } }),
    /** One document, with whether its container has learned it yet. */
    get: (documentId: string): Promise<Document> =>
      this.request("GET", `/v1/documents/${encodeURIComponent(documentId)}`),
    /** Remove one document everywhere; needs Full access and `confirm: true`. */
    delete: (documentId: string, params: { confirm?: boolean } = {}): Promise<Json | ConfirmationRequired> =>
      this.request("DELETE", `/v1/documents/${encodeURIComponent(documentId)}`, {
        query: { confirm: params.confirm ? "true" : "false" },
      }),
  };

  readonly memories = {
    /** Save one fact; `static: true` records a standing fact in the user's profile. */
    add: (params: { content: string; container?: string; static?: boolean; title?: string }): Promise<Json> =>
      this.request("POST", "/v1/memories", {
        body: { content: params.content, container: params.container, static: params.static ?? false, title: params.title ?? "" },
      }),
    /** Forget one learned fact; needs `confirm: true`, like `documents.delete`. */
    forget: (memoryId: string, params: { container?: string; confirm?: boolean } = {}): Promise<Json | ConfirmationRequired> =>
      this.request("DELETE", `/v1/memories/${encodeURIComponent(memoryId)}`, {
        query: { container: params.container, confirm: params.confirm ? "true" : "false" },
      }),
  };

  constructor(opts: MembaseOptions = {}) {
    const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};
    const apiKey = opts.apiKey ?? env.MEMBASE_API_KEY;
    if (!apiKey) {
      throw new Error("no API key: pass apiKey or set MEMBASE_API_KEY (Connect › Developer keys in the Membase app)");
    }
    this.apiKey = apiKey;
    this.baseUrl = (opts.baseUrl ?? env.MEMBASE_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = Math.max(0, opts.maxRetries ?? DEFAULT_MAX_RETRIES);
    this.fetchImpl = opts.fetch ?? fetch;
  }

  /** Hand a document (its text, or a public `url` to fetch) to a container. */
  add(params: AddParams): Promise<AddDocumentResult> {
    return this.request("POST", "/v1/documents", {
      body: {
        container: params.container,
        content: params.content,
        url: params.url,
        title: params.title ?? "",
        metadata: params.metadata,
        custom_id: params.customId ?? "",
      },
    });
  }

  /** Passages the containers hold on `q`, most relevant first, each naming its container. */
  search(params: SearchParams): Promise<SearchResult> {
    return this.request("POST", "/v1/search", { body: { q: params.q, container: params.container, limit: params.limit } });
  }

  /** Who the user is: `static` (standing facts), `dynamic` (recently changed facts) and, with `q`, `results` relevant to the topic. */
  profile(params: { q?: string } = {}): Promise<Profile> {
    return this.request("GET", "/v1/profile", { query: { q: params.q } });
  }

  /** The exposed agent's answer to `message` (an agent-endpoint credential). */
  ask(params: { message: string; model?: string }): Promise<Json> {
    return this.request("POST", "/v1/ask", { body: { message: params.message, model: params.model } });
  }

  /** The user's standing rules for how their memory is used by this credential. */
  rules(): Promise<Json> {
    return this.request("GET", "/v1/rules");
  }

  private async request<T>(
    method: string,
    path: string,
    opts: { body?: Record<string, unknown>; query?: Record<string, string | number | undefined> } = {},
  ): Promise<T> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(opts.query ?? {})) if (v !== undefined) url.searchParams.set(k, String(v));
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      Accept: "application/json",
      "User-Agent": `unibaseio-membase-typescript/${VERSION}`,
    };
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(Object.fromEntries(Object.entries(opts.body).filter(([, v]) => v !== undefined)));
    }
    let attempt = 0;
    for (;;) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      let res: Response;
      try {
        res = await this.fetchImpl(url.toString(), { method, headers, body, signal: controller.signal });
      } catch (e) {
        clearTimeout(timer);
        if (attempt < this.maxRetries) {
          attempt += 1;
          await sleep(backoff(attempt));
          continue;
        }
        const aborted = (e as { name?: string })?.name === "AbortError";
        throw aborted
          ? new APITimeoutError(`${method} ${path} timed out after ${this.timeoutMs} ms`)
          : new APIConnectionError(`${method} ${path}: ${(e as Error)?.message ?? String(e)}`);
      }
      clearTimeout(timer);
      if (res.ok) {
        const text = await res.text();
        return (text ? JSON.parse(text) : null) as T;
      }
      if ((RETRY_STATUSES.has(res.status) || res.status >= 500) && attempt < this.maxRetries) {
        attempt += 1;
        await sleep(backoff(attempt, res.headers.get("Retry-After")));
        continue;
      }
      let parsed: unknown;
      const text = await res.text();
      try {
        parsed = text ? JSON.parse(text) : {};
      } catch {
        parsed = { error: { message: text } };
      }
      throw errorFor(res.status, parsed);
    }
  }
}

function backoff(attempt: number, retryAfter?: string | null): number {
  if (retryAfter) {
    const s = Number(retryAfter);
    if (Number.isFinite(s)) return Math.min(s, 30) * 1000;
  }
  return Math.min(500 * 2 ** (attempt - 1), 8000) * (0.5 + Math.random());
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
