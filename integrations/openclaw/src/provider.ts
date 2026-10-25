/**
 * The memory slot: OpenClaw's provider-neutral memory runtime over the Membase account.
 *
 * Host consumers (Gateway `memory.search` v2 / `memory.get` / `memory.status`, doctor, voice fast
 * context, Memory Wiki) call `open()` with the caller's authority and use the returned handle.
 * Search runs under the recall budget and fails open on a slow (waking) memory: it answers with no
 * hits, `coverage: "partial"` and a warning instead of holding the caller.
 */
import { createHash } from "node:crypto";
import type { MemoryPluginCapability } from "openclaw/plugin-sdk/memory-host-core";
import type { MembasePluginConfig } from "./config.js";
import { APITimeoutError, AuthenticationError, PermissionDeniedError } from "membase-sdk";
import { describeError, NotConfiguredError, withBudget, type MembaseClients } from "./membase.js";
import { audienceAllows } from "./policy.js";

export type MemoryProviderRuntime = NonNullable<MemoryPluginCapability["providerRuntime"]>;
type OpenParams = Parameters<MemoryProviderRuntime["open"]>[0];
type OpenResult = Awaited<ReturnType<MemoryProviderRuntime["open"]>>;
export type MemoryProviderHandle = NonNullable<OpenResult["provider"]>;
type SearchRequest = Parameters<MemoryProviderHandle["search"]>[0];
type SearchPage = Awaited<ReturnType<MemoryProviderHandle["search"]>>;
type GetRequest = Parameters<MemoryProviderHandle["get"]>[0];
type GetResult = Awaited<ReturnType<MemoryProviderHandle["get"]>>;
type Health = Awaited<ReturnType<MemoryProviderHandle["health"]>>;
type Authority = OpenParams["context"]["authority"];

export const PROVIDER_ID = "membase";
const MAX_CACHED_PASSAGES = 500;

interface CachedPassage {
  text: string;
  container: string;
  containerName: string;
}

/** Search hits are passages, not records with ids; `get` resolves passages this process returned. */
class PassageCache {
  private readonly entries = new Map<string, CachedPassage>();

  put(passage: CachedPassage): string {
    const digest = createHash("sha256").update(`${passage.container}\n${passage.text}`).digest("hex").slice(0, 24);
    const id = `${passage.container}:${digest}`;
    this.entries.delete(id);
    this.entries.set(id, passage);
    while (this.entries.size > MAX_CACHED_PASSAGES) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.entries.delete(oldest);
    }
    return id;
  }

  get(id: string): CachedPassage | undefined {
    return this.entries.get(id);
  }
}

function canRead(authority: Authority, agentId: string, config: MembasePluginConfig): boolean {
  switch (authority.kind) {
    case "operator":
      // An authenticated operator of this Gateway: the person whose key this is.
      return true;
    case "session":
      return audienceAllows(authority.audience, "read", config, agentId);
    default:
      // Host authority is a named host operation; it grants no private memory access.
      return false;
  }
}

function denied(): Error {
  return new Error(
    "Membase memory is not available to this caller: it is read only in the key owner's own " +
      "conversations (enable allowSharedConversations to also read in shared ones).",
  );
}

export function createProviderRuntime(deps: {
  config: MembasePluginConfig;
  clients: MembaseClients;
  logger?: { warn: (message: string) => void };
}): MemoryProviderRuntime {
  const { config, clients } = deps;
  const cache = new PassageCache();

  return {
    async open({ agentId, context }: OpenParams): Promise<OpenResult> {
      const readable = canRead(context.authority, agentId, config);

      const provider: MemoryProviderHandle = {
        capabilities: { sources: ["memory"], pagination: false, candidates: [], projectFilter: false },

        async search(request: SearchRequest): Promise<SearchPage> {
          context.assertCurrent();
          if (!readable) throw denied();
          const query = request.query?.trim();
          if (!query) return { hits: [], coverage: "complete" };
          const limit = Math.min(50, Math.max(1, Math.floor(request.maxResults ?? 8)));
          let outcome;
          try {
            outcome = await withBudget(
              clients.recall().search({ q: query, limit, ...(config.container ? { container: config.container } : {}) }),
              config.recallBudgetMs,
            );
          } catch (error) {
            if (!(error instanceof APITimeoutError)) throw new Error(describeError(error, "search"));
            outcome = { ok: false as const, reason: "timeout" as const };
          }
          context.assertCurrent();
          if (!outcome.ok) {
            deps.logger?.warn(`membase: search exceeded the ${config.recallBudgetMs} ms budget; answering without memory`);
            return {
              hits: [],
              coverage: "partial",
              warning: `Membase did not answer within ${config.recallBudgetMs} ms (the memory may be waking); no memory this time.`,
            };
          }
          const result = outcome.value;
          const hits: SearchPage["hits"] = [];
          for (const hit of result.results ?? []) {
            const text = String(hit.content ?? "").trim();
            if (!text) continue;
            const score = typeof hit.score === "number" ? hit.score : undefined;
            // Membase ranks passages and may not score them; a passage without a score is kept.
            if (request.minScore !== undefined && score !== undefined && score < request.minScore) continue;
            const containerName = hit.container_name || hit.container || "Membase";
            const id = cache.put({ text, container: hit.container, containerName });
            const reference = { providerId: PROVIDER_ID, id };
            hits.push({
              reference,
              excerpt: text,
              ...(score !== undefined ? { score } : {}),
              source: "memory",
              citations: [{ label: `Membase: ${containerName}`, reference }],
            });
          }
          const failed = (result.containers ?? []).filter((c) => c.error);
          if (failed.length) {
            return {
              hits,
              coverage: "partial",
              warning: failed.map((c) => `Memory "${c.name || c.id}" could not answer: ${c.error}`).join("; "),
            };
          }
          return { hits, coverage: "complete" };
        },

        async get(request: GetRequest): Promise<GetResult> {
          context.assertCurrent();
          if (!readable) throw denied();
          if (request.reference.providerId !== PROVIDER_ID) return { status: "not_found" };
          const passage = cache.get(request.reference.id);
          if (!passage) return { status: "not_found" };
          const lines = passage.text.split("\n");
          const from = Math.max(1, Math.floor(request.from ?? 1));
          const count = request.lines !== undefined ? Math.max(0, Math.floor(request.lines)) : lines.length;
          const slice = lines.slice(from - 1, from - 1 + count);
          const end = from - 1 + slice.length;
          return {
            status: "ok",
            reference: request.reference,
            text: slice.join("\n"),
            citations: [{ label: `Membase: ${passage.containerName}`, reference: request.reference }],
            ...(end < lines.length ? { truncated: true, nextFrom: end + 1 } : {}),
            ...(request.from !== undefined ? { from } : {}),
            ...(request.lines !== undefined ? { lines: slice.length } : {}),
          };
        },

        async health(): Promise<Health> {
          // Readiness only; never partition content. Works for every authenticated caller.
          let outcome;
          try {
            outcome = await withBudget(clients.recall().containers.list(), config.recallBudgetMs);
          } catch (error) {
            const unavailable =
              error instanceof NotConfiguredError ||
              error instanceof PermissionDeniedError ||
              error instanceof AuthenticationError;
            return { status: unavailable ? "unavailable" : "degraded", message: describeError(error, "status") };
          }
          if (!outcome.ok) {
            return { status: "degraded", message: `Membase did not answer within ${config.recallBudgetMs} ms.` };
          }
          return { status: "ready" };
        },

        async close(): Promise<void> {
          // The clients are shared by every caller; a lease holds nothing of its own.
        },
      };
      return { provider };
    },
  };
}
