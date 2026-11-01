/** Agent tools: membase_search, membase_save, membase_profile. */
import { createHash } from "node:crypto";
import { Type, type Static } from "typebox";
import type { Page, ProfileFact, Receipt } from "membase-sdk";
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
  limit: Type.Optional(
    Type.Integer({ minimum: 1, maximum: 50, description: "How many passages in all (1-50)." }),
  ),
});

const SaveParams = Type.Object({
  content: Type.String({
    minLength: 1,
    description: "What to remember, in the user's own words. One fact or note per call.",
  }),
  title: Type.Optional(Type.String({ description: "A short title for the note." })),
});

const ProfileParams = Type.Object({});

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
            ...(params.limit ? { limit: params.limit } : {}),
          });
        } catch (error) {
          throw new Error(describeError(error, "search"));
        }
        assertCurrent(ctx, signal);
        return text(formatSearch(result), { items: result.items, partial: result.partial });
      },
    };
  };
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
        const flushId = ctx.memoryFlush?.flushId;
        const dedupeKey = flushId
          ? `${flushId}:${createHash("sha256").update(content).digest("hex")}`
          : undefined;
        if (dedupeKey && flushed.has(dedupeKey)) {
          return text("Already saved in this flush.", { status: "duplicate" });
        }
        assertCurrent(ctx, signal);
        let saved: Receipt;
        try {
          saved = await deps.clients.main().memories.add({
            content,
            ...(params.title ? { title: params.title } : {}),
          });
        } catch (error) {
          throw new Error(describeError(error, "save"));
        }
        if (dedupeKey) flushed.add(dedupeKey);
        assertCurrent(ctx, signal);
        const names = [...new Set((saved?.learning ?? []).map((l) => l.memory_name).filter(Boolean))];
        const where = names.length ? ` in ${names.join(", ")}` : "";
        const doc = saved?.id ? ` (note ${saved.id})` : "";
        const status = String(saved?.status ?? "pending");
        return text(
          `Saved to Membase${where}${doc}; learning ${status}.` +
            (status === "succeeded" ? "" : " A search right now may not find it until learning succeeds."),
          saved,
        );
      },
    };
  };
}

function formatProfile(profile: Page<ProfileFact>): string {
  const facts = (profile.items ?? []).map((f) => String(f.content ?? "").trim()).filter(Boolean);
  return facts.length ? ["About the user:", ...facts.map((f) => `- ${f}`)].join("\n") : "The user's Membase profile is empty.";
}

export function profileToolFactory(deps: ToolDeps): ToolFactory {
  return (ctx) => {
    if (!toolAllowed(ctx, "read", deps.config)) return null;
    return {
      name: "membase_profile",
      label: "Membase profile",
      description:
        "Read who the user is from Membase: standing statements (name, role, lasting preferences), newest " +
        "first. Read it once near " +
        "the start of a conversation, not every turn. Needs a key with Profile access.",
      parameters: ProfileParams,
      resultContentSource: "network",
      async execute(_toolCallId: string, _params: unknown, signal?: AbortSignal) {
        assertCurrent(ctx, signal);
        let profile: Page<ProfileFact>;
        try {
          profile = await deps.clients.main().profileFacts.list();
        } catch (error) {
          throw new Error(describeError(error, "profile"));
        }
        assertCurrent(ctx, signal);
        return text(formatProfile(profile), profile);
      },
    };
  };
}
