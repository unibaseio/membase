/** Membase memory for the Vercel AI SDK: the membase_search, membase_save and membase_profile tools. */
import { tool, type Tool } from "ai";
import { z } from "zod";
import { Membase, type MembaseOptions, type Page, type ProfileFact, type Receipt, type SearchResult } from "@unibaseio/membase";

export interface MembaseToolsOptions extends MembaseOptions {
  /** A ready client. Otherwise one is built from the other options; `apiKey` defaults to `MEMBASE_API_KEY`. */
  client?: Membase;
}

/** Declared rather than inferred, so the published types hold for AI SDK 6 and 7 alike. A type, not an interface, so it is a `ToolSet`. */
export type MembaseTools = {
  membase_search: Tool<{ query: string; limit?: number }, string>;
  membase_save: Tool<{ content: string }, string>;
  membase_profile: Tool<Record<string, never>, string>;
};

/**
 * The Membase tools, sharing one client. One key is one person's memory, so in a multi-user app
 * call this per request with that person's key. A failed call throws the SDK's error, which the
 * AI SDK hands to the model as the tool's error so it can tell the user.
 *
 * Leave a tool out by destructuring: `const { membase_save, ...readOnly } = membaseTools()`.
 */
export function membaseTools(options: MembaseToolsOptions = {}): MembaseTools {
  const { client: given, ...clientOptions } = options;
  const client = given ?? new Membase(clientOptions);
  return {
    membase_search: tool({
      description:
        "Search the user's long-term memory (Membase) for passages relevant to a question: what they told " +
        "you before, their projects, decisions, preferences and documents. Each passage starts with the " +
        "name of the Memory it came from in [brackets]; cite it in your answer. The first search after a " +
        "quiet spell can take up to a minute.",
      inputSchema: z.object({
        query: z.string().min(1).describe("What to look for, as a natural-language question or topic."),
        limit: z.number().int().min(1).max(50).optional().describe("How many passages in all (1-50)."),
      }),
      execute: async ({ query, limit }) => formatSearch(await client.search({ q: query, limit })),
    }),
    membase_save: tool({
      description:
        "Save one fact or note to the user's long-term memory (Membase). Call this ONLY when the user asks " +
        "you to remember something or plainly means for it to be kept; never to log the conversation. Pass " +
        "the content in the user's own words. Never save secrets, credentials or what other people said.",
      inputSchema: z.object({
        content: z.string().min(1).describe("The fact or note to keep, in the user's own words. One fact per call."),
      }),
      execute: async ({ content }) => formatSaved(await client.memories.add({ content })),
    }),
    membase_profile: tool({
      description:
        "Read who the user is from their Membase profile: standing statements (name, role, lasting " +
        "preferences), newest first. Call it once near the start of a conversation. Needs a key with " +
        "Profile access.",
      inputSchema: z.object({}),
      execute: async () => formatProfile(await client.profileFacts.list()),
    }),
  };
}

/** Search hits as the model reads them: each passage names the Memory it came from. */
export function formatSearch(result: SearchResult): string {
  const lines = (result.items ?? [])
    .map((hit) => ({ where: hit.memory_name || "Membase", content: String(hit.content ?? "").trim() }))
    .filter((hit) => hit.content)
    .map((hit) => `- [${hit.where}] ${hit.content}`);
  const notes = (result.partial ?? []).map((p) =>
    p.reason === "truncated"
      ? `- Memory "${p.memory_name}" had more passages than fit in this answer.`
      : `- Memory "${p.memory_name}" could not answer. An empty answer from it is not evidence of no memory.`,
  );
  if (!lines.length && !notes.length) return "No matching memories in Membase.";
  return [...lines, ...notes].join("\n");
}

function formatSaved(receipt: Receipt): string {
  const where = (receipt.learning ?? []).find((l) => l.memory_name)?.memory_name ?? "the user's memory";
  if (receipt.status === "succeeded") return `Saved to ${where} and learned; a search can find it now.`;
  if (receipt.status === "failed") return `Saved to ${where}, but learning it failed; a search may not find it.`;
  return `Saved to ${where}. It is learned in the background, so a search may not find it until that finishes.`;
}

function formatProfile(page: Page<ProfileFact>): string {
  const facts = (page.items ?? []).map((f) => String(f.content ?? "").trim()).filter(Boolean);
  return facts.length ? ["About the user:", ...facts.map((f) => `- ${f}`)].join("\n") : "The profile is empty.";
}
