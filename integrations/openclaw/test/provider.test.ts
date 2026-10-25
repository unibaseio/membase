import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryPluginCapability } from "openclaw/plugin-sdk/memory-host-core";
import { createMembasePlugin } from "../src/index.js";
import { FORBIDDEN, registerPlugin, scriptedFetch, SEARCH_OK, type Call } from "./helpers.js";

type Runtime = NonNullable<MemoryPluginCapability["providerRuntime"]>;
type OpenParams = Parameters<Runtime["open"]>[0];
type Authority = OpenParams["context"]["authority"];

const KEY = "mbk_test_key_0123456789";

function runtimeFor(config: Record<string, unknown> = {}, route: (c: Call) => any = () => ({ json: SEARCH_OK })) {
  const scripted = scriptedFetch(route);
  const rec = registerPlugin(createMembasePlugin({ fetch: scripted.fetch }), {
    apiKey: KEY,
    baseUrl: "https://membase.test",
    ...config,
  });
  return { runtime: rec.capabilities[0].providerRuntime!, ...scripted };
}

const owner: Authority = {
  kind: "session",
  sessionKey: "agent:main:main",
  sandboxed: false,
  audience: { kind: "owner-private", agentId: "main" },
};
const group: Authority = {
  kind: "session",
  sessionKey: "agent:main:telegram:group:1",
  sandboxed: false,
  audience: { kind: "conversation", agentId: "main", sessionKey: "agent:main:telegram:group:1", sessionId: "s" },
};

async function open(runtime: Runtime, authority: Authority, assertCurrent: () => void = () => undefined) {
  const { provider } = await runtime.open({ cfg: {}, agentId: "main", context: { authority, assertCurrent } } as OpenParams);
  if (!provider) throw new Error("no provider");
  return provider;
}

beforeEach(() => vi.stubEnv("MEMBASE_API_KEY", ""));
afterEach(() => vi.unstubAllEnvs());

describe("memory slot provider runtime", () => {
  it("declares memory search without pagination, candidates or project filters", async () => {
    const provider = await open(runtimeFor().runtime, owner);
    expect(provider.capabilities).toEqual({ sources: ["memory"], pagination: false, candidates: [], projectFilter: false });
    expect(provider.candidates).toBeUndefined();
  });

  it("search returns cited hits whose references resolve with get", async () => {
    const { runtime, calls } = runtimeFor();
    const provider = await open(runtime, owner);
    const page = await provider.search({ query: "ledger", maxResults: 2 });
    expect(calls[0].body).toEqual({ q: "ledger", limit: 2 });
    expect(page.coverage).toBe("complete");
    expect(page.hits.map((h) => h.excerpt)).toEqual([
      "We settled on Postgres for the ledger.",
      "Acme wants SSO before the pilot.",
    ]);
    expect(page.hits[0].reference.providerId).toBe("membase");
    expect(page.hits[0].citations?.[0].label).toBe("Membase: Project decisions");

    const got = await provider.get({ reference: page.hits[0].reference });
    expect(got).toMatchObject({ status: "ok", text: "We settled on Postgres for the ledger." });
    expect(await provider.get({ reference: { providerId: "membase", id: "mv-dec:unknown" } })).toEqual({ status: "not_found" });
    expect(await provider.get({ reference: { providerId: "other", id: page.hits[0].reference.id } })).toEqual({
      status: "not_found",
    });
  });

  it("honours minScore for scored passages", async () => {
    const provider = await open(runtimeFor().runtime, owner);
    const page = await provider.search({ query: "ledger", minScore: 0.5 });
    expect(page.hits.map((h) => h.excerpt)).toEqual(["We settled on Postgres for the ledger."]);
  });

  it("a Memory that could not answer marks the page partial", async () => {
    const reply = { ...SEARCH_OK, containers: [{ id: "mv-crm", name: "Customers", error: "dormant" }] };
    const provider = await open(runtimeFor({}, () => ({ json: reply })).runtime, owner);
    const page = await provider.search({ query: "x" });
    expect(page.coverage).toBe("partial");
    expect(page.warning).toContain('Memory "Customers" could not answer: dormant');
  });

  it("a slow (waking) memory answers partial with a warning within the budget", async () => {
    const provider = await open(runtimeFor({ recallBudgetMs: 500 }, () => ({ hang: true })).runtime, owner);
    const started = Date.now();
    const page = await provider.search({ query: "x" });
    expect(Date.now() - started).toBeLessThan(2_000);
    expect(page).toMatchObject({ hits: [], coverage: "partial" });
    expect(page.warning).toMatch(/did not answer within 500 ms/);
  });

  it("denial is an error, never an empty success", async () => {
    const { runtime, calls } = runtimeFor();
    for (const authority of [group, { kind: "host", operation: "status" } as Authority, { ...owner, audience: undefined }]) {
      const provider = await open(runtime, authority);
      await expect(provider.search({ query: "x" })).rejects.toThrow(/not available to this caller/);
      await expect(provider.get({ reference: { providerId: "membase", id: "x" } })).rejects.toThrow(/not available/);
    }
    expect(calls).toEqual([]);
  });

  it("shared conversations can read when allowed; an operator always can", async () => {
    expect((await (await open(runtimeFor({ allowSharedConversations: true }).runtime, group)).search({ query: "x" })).hits).toHaveLength(2);
    const operator: Authority = { kind: "operator", scopes: ["operator.read"] };
    expect((await (await open(runtimeFor().runtime, operator)).search({ query: "x" })).hits).toHaveLength(2);
  });

  it("a revoked key fails the search with the 403 explanation", async () => {
    const provider = await open(runtimeFor({}, () => FORBIDDEN).runtime, owner);
    await expect(provider.search({ query: "x" })).rejects.toThrow(/narrowed or revoked/);
  });

  it("rechecks the caller before any I/O", async () => {
    const { runtime, calls } = runtimeFor();
    const provider = await open(runtime, owner, () => {
      throw new Error("caller revoked");
    });
    await expect(provider.search({ query: "x" })).rejects.toThrow("caller revoked");
    expect(calls).toEqual([]);
  });

  it("health works for every caller without revealing content", async () => {
    const ok = runtimeFor({}, (c) => (c.path === "/v1/containers" ? { json: { containers: [{ id: "mv-dec", name: "Secret plans" }] } } : undefined));
    const status = await (await open(ok.runtime, { kind: "host", operation: "status" })).health();
    expect(status).toEqual({ status: "ready" });
    expect(JSON.stringify(status)).not.toContain("Secret plans");

    const revoked = await (await open(runtimeFor({}, () => FORBIDDEN).runtime, owner)).health();
    expect(revoked.status).toBe("unavailable");
    expect(revoked.message).toMatch(/narrowed or revoked/);

    const scripted = scriptedFetch(() => ({ json: {} }));
    const unconfigured = registerPlugin(createMembasePlugin({ fetch: scripted.fetch }), {}).capabilities[0].providerRuntime!;
    const none = await (await open(unconfigured, owner)).health();
    expect(none.status).toBe("unavailable");
    expect(none.message).toMatch(/not configured/);
  });
});
