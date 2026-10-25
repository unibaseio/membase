/** The Membase side: clients, time budgets, error wording and how recall reads to a model. */
// The SDK is installed under the alias `membase-sdk` (npm:@unibaseio/membase): this plugin's own
// package name on ClawHub is @unibaseio/membase, and a package cannot import itself by name.
import {
  APIConnectionError,
  APITimeoutError,
  AuthenticationError,
  BadRequestError,
  Membase,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  UnprocessableEntityError,
  type SearchResult,
} from "membase-sdk";
import type { MembasePluginConfig } from "./config.js";

/** How the plugin gets its Membase client. */
export interface MembaseClientOptions {
  /** A ready client (one person's key). Automatic recall still runs under the recall budget. */
  client?: Membase;
  /** A `fetch` for the clients the plugin builds itself (tests, proxies). */
  fetch?: typeof fetch;
}

export class NotConfiguredError extends Error {
  constructor() {
    super(
      "Membase is not configured: set plugins.entries.membase.config.apiKey (or the MEMBASE_API_KEY " +
        "environment variable) to a developer key from Connect > Developer keys in the Membase app.",
    );
    this.name = "NotConfiguredError";
  }
}

/**
 * Two clients over the same key: `main` for tool calls (SDK default timeout, 90 s, with retries,
 * because the first search after a quiet spell waits for the memory to wake) and `recall` for
 * automatic per-turn recall (one attempt, bounded by `recallBudgetMs`). Built lazily so a missing
 * key surfaces as an actionable error on use, not as a failed plugin load.
 */
export class MembaseClients {
  private mainClient?: Membase;
  private recallClient?: Membase;

  constructor(
    private readonly config: MembasePluginConfig,
    private readonly options: MembaseClientOptions = {},
  ) {}

  main(): Membase {
    if (this.options.client) return this.options.client;
    this.mainClient ??= this.build({});
    return this.mainClient;
  }

  recall(): Membase {
    if (this.options.client) return this.options.client;
    this.recallClient ??= this.build({ timeoutMs: this.config.recallBudgetMs, maxRetries: 0 });
    return this.recallClient;
  }

  private build(extra: { timeoutMs?: number; maxRetries?: number }): Membase {
    try {
      return new Membase({
        ...(this.config.apiKey ? { apiKey: this.config.apiKey } : {}),
        ...(this.config.baseUrl ? { baseUrl: this.config.baseUrl } : {}),
        ...(this.options.fetch ? { fetch: this.options.fetch } : {}),
        ...extra,
      });
    } catch {
      // The SDK throws only when no key is passed and MEMBASE_API_KEY is unset.
      throw new NotConfiguredError();
    }
  }
}

export type Budgeted<T> = { ok: true; value: T } | { ok: false; reason: "timeout" };

/** Resolves within `ms`; the work keeps running in the background (it also wakes the memory). */
export async function withBudget<T>(work: Promise<T>, ms: number): Promise<Budgeted<T>> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<Budgeted<T>>((resolve) => {
    timer = setTimeout(() => resolve({ ok: false, reason: "timeout" }), ms);
  });
  // A late rejection after the budget expired must not become an unhandled rejection.
  work.catch(() => undefined);
  try {
    return await Promise.race([work.then((value) => ({ ok: true as const, value })), timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export type MembaseOperation = "search" | "save" | "profile" | "status";

/** One actionable sentence for a failed Membase call. Never contains the key. */
export function describeError(error: unknown, op: MembaseOperation): string {
  if (error instanceof NotConfiguredError) return error.message;
  const detail = error instanceof Error && error.message ? ` (${error.message})` : "";
  if (error instanceof PermissionDeniedError) {
    if (op === "profile") {
      return (
        "Membase refused the profile read (403): this key does not include Profile access, or its owner " +
        `narrowed or revoked it. Tick Profile on the key under Connect > Developer keys${detail}.`
      );
    }
    if (op === "save") {
      return (
        "Membase refused the save (403): saving needs a Read & write key that reaches this Memory; " +
        `this key was narrowed or revoked, or is Read only. Do not retry with this key${detail}.`
      );
    }
    return (
      "Membase refused this key (403): its owner narrowed or revoked it, or the Memory is outside its " +
      `reach. Do not retry with this key; mint or widen one under Connect > Developer keys${detail}.`
    );
  }
  if (error instanceof AuthenticationError) {
    return `Membase received no key (401). Set the plugin's apiKey or MEMBASE_API_KEY${detail}.`;
  }
  if (error instanceof BadRequestError) {
    return `Membase rejected the request (400)${detail}.`;
  }
  if (error instanceof NotFoundError) {
    return `Membase does not know that id (404)${detail}.`;
  }
  if (error instanceof UnprocessableEntityError) {
    return (
      "The Membase account's memory cannot run a turn right now (422): usually no working model under " +
      `AI Setup in the Membase app, or the free turns are spent${detail}.`
    );
  }
  if (error instanceof RateLimitError) {
    return `Membase is busy for this account (429): two turns are already running; try again shortly${detail}.`;
  }
  if (error instanceof APITimeoutError) {
    return (
      "Membase did not answer in time: the memory may be waking after a quiet spell (up to a minute). " +
      `Try again shortly${detail}.`
    );
  }
  if (error instanceof APIConnectionError) {
    return `Could not reach Membase${detail}.`;
  }
  return `Membase ${op} failed${detail}.`;
}

/** Search hits as the model reads them: each passage names the Memory it came from. */
export function formatSearch(result: SearchResult, opts: { maxChars?: number } = {}): string {
  const maxChars = opts.maxChars ?? Number.POSITIVE_INFINITY;
  const lines: string[] = [];
  let used = 0;
  for (const hit of result.results ?? []) {
    const where = hit.container_name || hit.container || "Membase";
    const content = String(hit.content ?? "").trim();
    if (!content) continue;
    const line = `- [${where}] ${content}`;
    if (used + line.length > maxChars) break;
    lines.push(line);
    used += line.length;
  }
  const failed = (result.containers ?? []).filter((c) => c.error);
  const notes = failed.map(
    (c) => `- Memory "${c.name || c.id}" could not answer: ${c.error}. An empty answer from it is not evidence of no memory.`,
  );
  if (!lines.length && !notes.length) return "No matching memories in Membase.";
  const parts: string[] = [];
  if (lines.length) parts.push(lines.join("\n"));
  if (notes.length) parts.push(notes.join("\n"));
  return parts.join("\n");
}
