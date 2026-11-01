/** The Membase client: one developer key, the account's memory. */

import { APIConnectionError, APITimeoutError, errorFor } from "./errors.js";

export const VERSION = "0.4.0";
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

/** A Memory that could not answer (`error`) or whose passages all lost to `limit` (`truncated`); the others still answered. */
export interface PartialMemory { memory_name: string; reason: "error" | "truncated" | string }
/** One passage: a learned fact (`type: "memory"`, with its `mem_` id) or a passage from a document. */
export interface SearchItem { type: "memory" | "document" | string; id?: string; content: string; score?: number | null; memory_name: string; title?: string; [k: string]: unknown }
export interface SearchResult { items: SearchItem[]; partial: PartialMemory[] }
/** Every list: the newest `limit`, whether more were left out, and the Memories that could not answer. */
export interface Page<T> { items: T[]; has_more: boolean; partial: PartialMemory[] }
/** A fact a Memory learned (`mem_…`). */
export interface Memory { id: string; content: string; title?: string; memory_name: string; source_time?: string | null; recorded_at?: string | null; [k: string]: unknown }
export type LearningStatus = "succeeded" | "processing" | "pending" | "failed";
export interface Learning { memory_name: string; status: LearningStatus | string }
/** Material handed in (`doc_…`): a file or page (`kind: "file"`) or the note a fact was written as (`kind: "note"`). */
export interface Document { id: string; kind: "file" | "note" | string; title: string; memory_name: string; custom_id?: string; created_at?: string | null; updated_at?: string | null; learning?: Learning[]; [k: string]: unknown }
/** A write's answer (HTTP 202): the document it landed as, and where its learning stands. */
export interface Receipt { id: string | null; kind: "file" | "note" | string; status: LearningStatus | string; learning: Learning[] }
export interface MediaMoment { t: number; end?: number | null; kind: string; text?: string; speaker?: string | null; thumb_url?: string | null; [k: string]: unknown }
/** A photo or video (`med_…`). `moments` is a count in lists, the moments themselves from `media.get` and `media.search`. */
export interface MediaItem { id: string; kind: string; filename?: string | null; memory_name: string; summary: string; duration?: number | null; created_at?: string | null; file_url?: string | null; thumb_url?: string | null; moments?: MediaMoment[] | number | null; [k: string]: unknown }
export interface MediaSearchResult { items: MediaItem[]; partial: PartialMemory[] }
/** A carried-out removal. */
export interface Removed { id: string; status: "forgotten" | "deleted" | string }
/** One standing statement about the user (`pf_…`): the account's, not a Memory's. */
export interface ProfileFact { id: string; content: string; source_kind: "user_stated" | "imported" | "model_generated" | string; recorded_at?: string | null; [k: string]: unknown }
/** `profileFacts.add`'s answer: the statement kept, and whether it is new (`false`: the same one was already kept). */
export interface ProfileFactAdded extends ProfileFact { created: boolean }
/** The user's standing rules for using their memory. */
export interface Rules { citation_required: boolean; retention_notice: string; version: string; [k: string]: unknown }
/** One answer from the memory, the Memory it came from, and the Memories that could not answer. */
export interface Answer { answer: string; citations: Array<{ memory_name: string }>; partial: PartialMemory[] }
/** What this key may do. Not permission: every call is still checked when made. */
export interface Capabilities { actions: string[]; memories: Array<{ name: string; kind: string }>; rules: Rules; features: string[]; [k: string]: unknown }
export type Json = Record<string, unknown>;

export interface AddParams {
  /** The document's full text. */
  content?: string;
  /** A public web address to fetch instead of `content`. */
  url?: string;
  title?: string;
  /** Your own id; adding the same customId again is a no-op. */
  customId?: string;
}

export interface SearchParams {
  /** The question, in natural language. */
  q: string;
  /** How many passages in all (1–50). */
  limit?: number;
  /** `full` (default): the engine's whole retrieval. `fast`: one round, no model call. */
  mode?: "full" | "fast";
}

const item = (collection: string, id: string) => `/v1/${collection}/${encodeURIComponent(id)}`;
const flag = (confirm?: boolean) => (confirm ? "true" : "false");

export class Membase {
  readonly baseUrl: string;
  readonly maxRetries: number;
  readonly timeoutMs: number;
  private readonly apiKey: string;
  private readonly fetchImpl: Fetch;

  /** The facts the Memories in reach learned (`mem_…`). */
  readonly memories = {
    /** The newest facts (`limit` 1–1000, default 100). */
    list: (params: { limit?: number } = {}): Promise<Page<Memory>> =>
      this.request("GET", "/v1/memories", { query: { limit: params.limit } }),
    /** One fact, at any age. */
    get: (memoryId: string): Promise<Memory> => this.request("GET", item("memories", memoryId)),
    /** Save one fact into this key's write target; the receipt is the note it was written as. */
    add: (params: { content: string; title?: string }): Promise<Receipt> =>
      this.request("POST", "/v1/memories/add", { body: { content: params.content, title: params.title ?? "" } }),
    /** Forget one learned fact. Needs Full access and `confirm: true`; otherwise `ConfirmationRequiredError` (428) and nothing changes. */
    forget: (memoryId: string, params: { confirm?: boolean } = {}): Promise<Removed> =>
      this.request("POST", `${item("memories", memoryId)}/forget`, { query: { confirm: flag(params.confirm) } }),
  };

  /** The material handed in (`doc_…`): files and pages, and the notes facts were written as. */
  readonly documents = {
    /** The documents, newest first; `kind` narrows to `file` or `note`. */
    list: (params: { kind?: "file" | "note"; limit?: number } = {}): Promise<Page<Document>> =>
      this.request("GET", "/v1/documents", { query: { kind: params.kind, limit: params.limit } }),
    /** One document, with `learning`: how far each Memory got in learning it. */
    get: (documentId: string): Promise<Document> => this.request("GET", item("documents", documentId)),
    /** Hand in a document (its text, or a public `url`) to this key's write target. */
    add: (params: AddParams): Promise<Receipt> =>
      this.request("POST", "/v1/documents/add", {
        body: { content: params.content, url: params.url, title: params.title ?? "", custom_id: params.customId ?? "" },
      }),
    /** Withdraw a document and what was learned from it. Needs Full access and `confirm: true`; otherwise `ConfirmationRequiredError` (428). */
    delete: (documentId: string, params: { confirm?: boolean } = {}): Promise<Removed> =>
      this.request("POST", `${item("documents", documentId)}/delete`, { query: { confirm: flag(params.confirm) } }),
  };

  /** Who the user is (`pf_…`): standing statements, the account's. Each needs the key's profile tick. */
  readonly profileFacts = {
    /** The statements, newest first (`limit` 1–1000, default 100), each with its `source_kind`. */
    list: (params: { limit?: number } = {}): Promise<Page<ProfileFact>> =>
      this.request("GET", "/v1/profile-facts", { query: { limit: params.limit } }),
    /** Keep one statement about the user; `created: false` when the same one was already kept. */
    add: (params: { content: string }): Promise<ProfileFactAdded> =>
      this.request("POST", "/v1/profile-facts/add", { body: { content: params.content } }),
    /** Forget one statement. Needs Full access and `confirm: true`; otherwise `ConfirmationRequiredError` (428) and nothing changes. */
    forget: (factId: string, params: { confirm?: boolean } = {}): Promise<Removed> =>
      this.request("POST", `${item("profile-facts", factId)}/forget`, { query: { confirm: flag(params.confirm) } }),
  };

  /** Photos and videos (`med_…`) in the media Memories in reach. */
  readonly media = {
    /** Newest first, each with its `summary`, `file_url` and `thumb_url`. */
    list: (params: { limit?: number } = {}): Promise<Page<MediaItem>> =>
      this.request("GET", "/v1/media", { query: { limit: params.limit } }),
    /** One item with its `moments`: a video's shots and spoken lines. */
    get: (mediaId: string): Promise<MediaItem> => this.request("GET", item("media", mediaId)),
    /** Items matching what was seen or said in `q`, best first; `kind` is `image` or `video`. */
    search: (params: { q: string; kind?: "image" | "video"; limit?: number }): Promise<MediaSearchResult> =>
      this.request("POST", "/v1/media/search", { body: { q: params.q, kind: params.kind, limit: params.limit } }),
    /** Forget one item and remove its file. Needs Full access and `confirm: true`; otherwise `ConfirmationRequiredError` (428). */
    delete: (mediaId: string, params: { confirm?: boolean } = {}): Promise<Removed> =>
      this.request("POST", `${item("media", mediaId)}/delete`, { query: { confirm: flag(params.confirm) } }),
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

  /** Hand in a document; the same as `documents.add`. */
  add(params: AddParams): Promise<Receipt> {
    return this.documents.add(params);
  }

  /** The passages the Memories in reach hold on `q`, most relevant first, each naming its `memory_name`. */
  search(params: SearchParams): Promise<SearchResult> {
    return this.request("POST", "/v1/search", { body: { q: params.q, limit: params.limit, mode: params.mode ?? "full" } });
  }

  /** One answer to `message`, written by the memory from the Memories in reach; the passages are not returned. Needs a credential that holds `ask` (a marketplace buyer's). */
  ask(params: { message: string }): Promise<Answer> {
    return this.request("POST", "/v1/ask", { body: { message: params.message } });
  }

  /** What this key may do (`actions`: `search`, `memories.add`, …), the Memories it reaches (by name and kind) and the user's standing `rules`. */
  capabilities(): Promise<Capabilities> {
    return this.request("GET", "/v1/capabilities");
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
