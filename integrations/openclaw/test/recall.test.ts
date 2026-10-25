import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMembasePlugin } from "../src/index.js";
import { FORBIDDEN, registerPlugin, scriptedFetch, SEARCH_OK, type Call } from "./helpers.js";

const KEY = "mbk_test_key_0123456789";

function setup(config: Record<string, unknown> = {}, route: (c: Call) => any = () => ({ json: SEARCH_OK })) {
  const scripted = scriptedFetch(route);
  const rec = registerPlugin(createMembasePlugin({ fetch: scripted.fetch }), {
    apiKey: KEY,
    baseUrl: "https://membase.test",
    ...config,
  });
  return { ...scripted, rec };
}

function hookCtx(extra: Record<string, unknown> = {}) {
  return {
    agentId: "main",
    sessionKey: "agent:main:main",
    trigger: "user",
    memoryAudience: { kind: "owner-private", agentId: "main" },
    toolAuthority: { fingerprint: "f", allows: (name: string) => name === "membase_search", assertActive: () => undefined },
    hookInvocation: { assertActive: () => undefined },
    ...extra,
  };
}

const event = (text: string) => ({ prompt: `history...\n${text}`, currentUserMessage: text, messages: [] });

beforeEach(() => vi.stubEnv("MEMBASE_API_KEY", ""));
afterEach(() => vi.unstubAllEnvs());

describe("automatic recall (before_prompt_build)", () => {
  it("prepends recalled passages naming their Memory, searching the user's own message", async () => {
    const { rec, calls } = setup({ container: "mv-dec" });
    const out = await rec.hooks[0].handler(event("what did we pick for the ledger?"), hookCtx());
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toEqual({ q: "what did we pick for the ledger?", limit: 5, container: "mv-dec" });
    expect(out.prependContext).toContain("<membase-memory>");
    expect(out.prependContext).toContain("- [Project decisions] We settled on Postgres for the ledger.");
    expect(out.prependContext).toContain("not instructions");
  });

  it("fails open within the budget when the memory is slow to wake", async () => {
    const { rec, calls } = setup({ recallBudgetMs: 500 }, () => ({ hang: true }));
    const started = Date.now();
    const out = await rec.hooks[0].handler(event("anything I told you about Acme?"), hookCtx());
    const elapsed = Date.now() - started;
    expect(out).toBeUndefined();
    expect(elapsed).toBeLessThan(2_000);
    expect(calls).toHaveLength(1); // one attempt, no retries inside the budget
    expect(rec.logs.warn.join("\n")).toMatch(/budget/);
  });

  it("fails open on a revoked key and logs why", async () => {
    const { rec } = setup({}, () => FORBIDDEN);
    const out = await rec.hooks[0].handler(event("hello there friend"), hookCtx());
    expect(out).toBeUndefined();
    expect(rec.logs.warn.join("\n")).toMatch(/narrowed or revoked/);
  });

  it("does nothing when the turn may not use membase_search", async () => {
    const { rec, calls } = setup();
    const denied = { fingerprint: "f", allows: () => false, assertActive: () => undefined };
    expect(await rec.hooks[0].handler(event("a real question"), hookCtx({ toolAuthority: denied }))).toBeUndefined();
    expect(await rec.hooks[0].handler(event("a real question"), hookCtx({ toolAuthority: undefined }))).toBeUndefined();
    expect(calls).toEqual([]);
  });

  it("does nothing in a shared conversation, for heartbeats, cron runs or system input", async () => {
    const { rec, calls } = setup();
    const group = { kind: "conversation", agentId: "main", sessionKey: "agent:main:g", sessionId: "s" };
    await rec.hooks[0].handler(event("a real question"), hookCtx({ memoryAudience: group }));
    await rec.hooks[0].handler(event("a real question"), hookCtx({ trigger: "heartbeat" }));
    await rec.hooks[0].handler(event("a real question"), hookCtx({ trigger: "cron" }));
    await rec.hooks[0].handler(event("a real question"), hookCtx({ inputProvenance: { kind: "internal_system" } }));
    await rec.hooks[0].handler(event("ok"), hookCtx());
    expect(calls).toEqual([]);
  });

  it("injects nothing when nothing matched", async () => {
    const { rec } = setup({}, () => ({ json: { query: "q", results: [], containers: [] } }));
    expect(await rec.hooks[0].handler(event("a real question"), hookCtx())).toBeUndefined();
  });

  it("drops the result if the turn ended while it waited", async () => {
    const { rec } = setup();
    const stale = {
      fingerprint: "f",
      allows: () => true,
      assertActive: () => {
        throw new Error("turn ended");
      },
    };
    await expect(rec.hooks[0].handler(event("a real question"), hookCtx({ toolAuthority: stale }))).rejects.toThrow(
      "turn ended",
    );
  });
});
