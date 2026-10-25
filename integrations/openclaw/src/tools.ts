/** Agent tools: membase_search, membase_save, membase_profile. */
import { createHash } from "node:crypto";
import { Type, type Static } from "typebox";
import { BadRequestError, type Membase, type Profile } from "membase-sdk";
import type { AnyAgentTool, OpenClawPluginToolContext } from "openclaw/plugin-sdk/plugin-entry";
import type { MembasePluginConfig } from "./config.js";
import { describeError, formatSearch, type MembaseClients } from "./membase.js";
import { toolAllowed } from "./policy.js";

export const TOOL_NAMES = ["membase_search", "membase_save", "membase_profile"] as const;

const SearchParams = Type.Object({
  query: Type.String({
    minLength: 1,
    description: "What to look for, as a natural-language question.",
  }),
  container: Type.Optional(
    Type.String({ description: "One Memory (container id, mv-...) to search; omit to search every Memory in reach." }),
  ),
  limit: Type.Optional(
    Type.Integer({ minimum: 1, maximum: 50, description: "How many passages in all (1-50)." }),
  ),
});

const SaveParams = Type.Object({
  content: Type.String({
    minLength: 1,
    description: "What to remember, in the user's own words. One fact or note per call.",
  }),
  container: Type.Optional(
    Type.String({ description: "The Memory (container id, mv-...) to save into; defaults to the configured one." }),
  ),
  static: Type.Optional(
    Type.Boolean({
      description:
        "True for a lasting fact about the user themself (identity, standing preference): it goes to their profile instead of a Memory.",
    }),
  ),
  title: Type.Optional(Type.String({ description: "A short title for the note." })),
});

const ProfileParams = Type.Object({
  query: Type.Optional(
    Type.String({ description: "A topic, to also return profile passages relevant to it." }),
  ),
});

export interface ToolDeps {
  config: MembasePluginConfig;
  clients: MembaseClients;
}

type ToolFactory = (ctx: OpenClawPluginToolContext) => AnyAgentTool | null;

function text(value: string, details: unknown) {
  return { content: [{ type: "text" as const, text: value }], details };
}

function assertCurrent(ctx: OpenClawPluginToolContext, signal?: AbortSignal): void {
  signal?.throwIfAborted();
  ctx.assertInvocationCurrent?.();
  ctx.assertMemoryAudienceCurrent?.();
}

export function searchToolFactory(deps: ToolDeps): ToolFactory {
  return (ctx) => {
    if (!toolAllowed(ctx, "read", deps.config)) return null;
    return {
      name: "membase_search",
      label: "Membase search",
      description:
        "Search the user's long-term memory in Membase: past decisions, projects, preferences and notes. " +
        "Use it before answering anything that depends on what the user told you or did before. " +
        "Each passage names the Memory it came from; cite it.",
      parameters: SearchParams,
      resultContentSource: "network",
      async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
        const params = rawParams as Static<typeof SearchParams>;
        assertCurrent(ctx, signal);
        let result;
        try {
          result = await deps.clients.main().search({
            q: params.query,
            ...(params.container ? { container: params.container } : {}),
            ...(params.limit ? { limit: params.limit } : {}),
          });
        } catch (error) {
          throw new Error(describeError(error, "search"));
        }
        assertCurrent(ctx, signal);
        return text(formatSearch(result), { results: result.results, containers: result.containers });
      },
    };
  };
}

async function containerHint(client: Membase): Promise<string> {
  try {
    const { containers } = await client.containers.list();
    if (!containers?.length) return "";
    return ` Memories in reach: ${containers.map((c) => `${c.name} (${c.id})`).join(", ")}.`;
  } catch {
    return "";
  }
}

export function saveToolFactory(deps: ToolDeps): ToolFactory {
  // Pre-compaction flush retries share a flushId; the same note is not saved twice in one cycle.
  const flushed = new Set<string>();
  return (ctx) => {
    if (!toolAllowed(ctx, "write", deps.config)) return null;
    return {
      name: "membase_save",
      label: "Membase save",
      description:
        "Save something to the user's long-term memory in Membase. Call it only when the user asked you " +
        "to remember something or plainly meant it to be kept, and save their own words, one fact per call. " +
        "Never save secrets, credentials or what other people said. A saved note is learned on the next run, " +
        "so search may not find it at once.",
      parameters: SaveParams,
      executionMode: "sequential",
      async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
        const params = rawParams as Static<typeof SaveParams>;
        const content = params.content.trim();
        if (!content) throw new Error("Nothing to save: content is empty.");
        const container = params.container || deps.config.container;
        const flushId = ctx.memoryFlush?.flushId;
        const dedupeKey = flushId
          ? `${flushId}:${createHash("sha256").update(`${container ?? ""}\n${content}`).digest("hex")}`
          : undefined;
        if (dedupeKey && flushed.has(dedupeKey)) {
          return text("Already saved in this flush.", { status: "duplicate" });
        }
        assertCurrent(ctx, signal);
        const client = deps.clients.main();
        let saved: Record<string, unknown>;
        try {
          saved = await client.memories.add({
            content,
            ...(container ? { container } : {}),
            ...(params.static ? { static: true } : {}),
            ...(params.title ? { title: params.title } : {}),
          });
        } catch (error) {
          const hint = error instanceof BadRequestError && !container ? await containerHint(client) : "";
          throw new Error(describeError(error, "save") + hint);
        }
        if (dedupeKey) flushed.add(dedupeKey);
        assertCurrent(ctx, signal);
        if (saved?.static) {
          return text("Recorded as a standing fact in the user's Membase profile.", saved);
        }
        const where = typeof saved?.container === "string" ? ` in ${saved.container}` : "";
        const doc = typeof saved?.document_id === "string" ? ` (document ${saved.document_id})` : "";
        return text(
          `Saved to Membase${where}${doc}; status ${String(saved?.status ?? "queued")}. ` +
            "It is learned on the next run, so a search right now may not find it yet.",
          saved,
        );
      },
    };
  };
}

function formatProfile(profile: Profile): string {
  const parts: string[] = [];
  if (profile.static?.length) parts.push(["Standing facts:", ...profile.static.map((s) => `- ${s}`)].join("\n"));
  if (profile.dynamic?.length) {
    parts.push(["Recently changed:", ...profile.dynamic.map((d) => `- ${d.content}`)].join("\n"));
  }
  if (profile.results?.length) {
    parts.push(
      ["Relevant:", ...profile.results.map((h) => `- [${h.container_name || h.container || "profile"}] ${h.content}`)].join("\n"),
    );
  }
  return parts.length ? parts.join("\n\n") : "The user's Membase profile is empty.";
}

export function profileToolFactory(deps: ToolDeps): ToolFactory {
  return (ctx) => {
    if (!toolAllowed(ctx, "read", deps.config)) return null;
    return {
      name: "membase_profile",
      label: "Membase profile",
      description:
        "Read who the user is from Membase: standing facts and recently changed ones. Read it once near " +
        "the start of a conversation, not every turn. Needs a key with Profile access.",
      parameters: ProfileParams,
      resultContentSource: "network",
      async execute(_toolCallId: string, rawParams: unknown, signal?: AbortSignal) {
        const params = (rawParams ?? {}) as Static<typeof ProfileParams>;
        assertCurrent(ctx, signal);
        let profile: Profile;
        try {
          profile = await deps.clients.main().profile(params.query ? { q: params.query } : {});
        } catch (error) {
          throw new Error(describeError(error, "profile"));
        }
        assertCurrent(ctx, signal);
        return text(formatProfile(profile), profile);
      },
    };
  };
}
