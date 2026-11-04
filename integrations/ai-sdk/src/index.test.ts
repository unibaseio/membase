import { generateText, isStepCount } from "ai";
import { MockLanguageModelV4 } from "ai/test";
import { Membase } from "@unibaseio/membase";
import { describe, expect, it } from "vitest";
import { membaseTools } from "./index.js";

/** A Membase client over a fake API: answers by path and records each request. */
function fakeMembase(answers: Record<string, unknown>) {
  const calls: Array<{ method: string; path: string; body: unknown }> = [];
  const fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    calls.push({ method: init?.method ?? "GET", path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const answer = answers[url.pathname];
    if (answer === undefined) return new Response(JSON.stringify({ error: { code: "not_found" } }), { status: 404 });
    return new Response(JSON.stringify(answer), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof globalThis.fetch;
  return { client: new Membase({ apiKey: "mbk_test", baseUrl: "https://membase.test", fetch, maxRetries: 0 }), calls };
}

const run = { toolCallId: "call-1", messages: [], context: {} } as never;

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 1, text: 1, reasoning: 0 },
};

describe("membaseTools", () => {
  it("search answers with each passage's Memory and the Memories that could not answer", async () => {
    const { client, calls } = fakeMembase({
      "/v1/search": {
        items: [
          { type: "memory", content: "Acme wants SSO before the pilot.", memory_name: "Work" },
          { type: "memory", content: "  ", memory_name: "Work" },
        ],
        partial: [{ memory_name: "Personal", reason: "error" }],
      },
    });
    const text = await membaseTools({ client }).membase_search.execute!({ query: "what does Acme need", limit: 5 }, run);
    expect(text).toBe(
      '- [Work] Acme wants SSO before the pilot.\n- Memory "Personal" could not answer. An empty answer from it is not evidence of no memory.',
    );
    expect(calls).toEqual([{ method: "POST", path: "/v1/search", body: { q: "what does Acme need", limit: 5, mode: "full" } }]);
  });

  it("save adds the user's words as a memory and says where it landed", async () => {
    const { client, calls } = fakeMembase({
      "/v1/memories/add": { id: "doc_1", kind: "note", status: "pending", learning: [{ memory_name: "Work", status: "pending" }] },
    });
    const text = await membaseTools({ client }).membase_save.execute!({ content: "I prefer dark mode." }, run);
    expect(text).toBe("Saved to Work. It is learned in the background, so a search may not find it until that finishes.");
    expect(calls[0]).toMatchObject({ method: "POST", path: "/v1/memories/add", body: { content: "I prefer dark mode." } });
  });

  it("profile lists the standing statements", async () => {
    const { client } = fakeMembase({
      "/v1/profile-facts": { items: [{ id: "pf_1", content: "Name: Ann", source_kind: "user_stated" }], has_more: false, partial: [] },
    });
    expect(await membaseTools({ client }).membase_profile.execute!({}, run)).toBe("About the user:\n- Name: Ann");
  });

  it("runs inside an AI SDK tool loop", async () => {
    const { client } = fakeMembase({
      "/v1/search": { items: [{ type: "memory", content: "Favorite editor: Neovim.", memory_name: "Personal" }], partial: [] },
    });
    let step = 0;
    const model = new MockLanguageModelV4({
      doGenerate: async () =>
        step++ === 0
          ? {
              content: [{ type: "tool-call", toolCallId: "c1", toolName: "membase_search", input: '{"query":"favorite editor"}' }],
              finishReason: { unified: "tool-calls", raw: undefined },
              usage,
              warnings: [],
            }
          : { content: [{ type: "text", text: "Neovim." }], finishReason: { unified: "stop", raw: undefined }, usage, warnings: [] },
    });
    const result = await generateText({ model, tools: membaseTools({ client }), prompt: "Which editor do I like?", stopWhen: isStepCount(2) });
    expect(result.text).toBe("Neovim.");
    expect(result.steps[0].toolResults[0]).toMatchObject({ toolName: "membase_search", output: "- [Personal] Favorite editor: Neovim." });
  });
});
