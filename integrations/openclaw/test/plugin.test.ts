import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import defaultEntry, { CONFIG_JSON_SCHEMA, createMembasePlugin, TOOL_NAMES } from "../src/index.js";
import {
  FORBIDDEN,
  groupCtx,
  ownerCtx,
  registerPlugin,
  scriptedFetch,
  SEARCH_OK,
  tool,
  type Call,
} from "./helpers.js";

const KEY = "mbk_test_key_0123456789";
const BASE = "https://membase.test";
const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

function setup(config: Record<string, unknown> = {}, route: (c: Call) => any = () => undefined, mode?: any) {
  const scripted = scriptedFetch(route);
  const entry = createMembasePlugin({ fetch: scripted.fetch });
  const rec = registerPlugin(entry, { apiKey: KEY, baseUrl: BASE, ...config }, mode);
  return { ...scripted, rec };
}

function textOf(result: { content: Array<{ type: string; text?: string }> }): string {
  return result.content.map((c) => c.text ?? "").join("\n");
}

beforeEach(() => {
  vi.stubEnv("MEMBASE_API_KEY", "");
  vi.stubEnv("MEMBASE_BASE_URL", "");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("package metadata", () => {
  it("manifest and entry agree", () => {
    expect(manifest.id).toBe("membase");
    expect(defaultEntry.id).toBe(manifest.id);
    expect(manifest.kind).toBe("memory");
    expect(manifest.contracts.tools).toEqual([...TOOL_NAMES]);
    expect(manifest.configSchema).toEqual(CONFIG_JSON_SCHEMA);
    expect(manifest.configContracts.secretInputs.paths[0].path).toBe("apiKey");
    expect(manifest.uiHints.apiKey.sensitive).toBe(true);
    expect(defaultEntry.configSchema?.jsonSchema).toEqual(CONFIG_JSON_SCHEMA);
  });

  it("package.json carries the ClawHub compatibility metadata", () => {
    expect(pkg.name).toBe("@unibaseio/membase");
    expect(pkg.openclaw.extensions).toEqual(["./dist/index.js"]);
    expect(pkg.openclaw.compat.pluginApi).toMatch(/^>=2026\.10\.1/);
    expect(pkg.openclaw.build.openclawVersion).toBeTruthy();
    expect(pkg.dependencies["membase-sdk"]).toBeTruthy();
    expect(pkg.dependencies.typebox).toBeTruthy();
  });

  it("the entry's config schema applies the documented defaults", () => {
    const parsed = defaultEntry.configSchema?.safeParse?.({ apiKey: KEY });
    expect(parsed?.success).toBe(true);
    expect(parsed?.data).toMatchObject({ recallBudgetMs: 8000, autoRecall: true, autoCapture: false, allowSharedConversations: false });
    expect(defaultEntry.configSchema?.safeParse?.({ recallBudgetMs: 10 })?.success).toBe(false);
    expect(defaultEntry.configSchema?.safeParse?.({ userId: "x" })?.success).toBe(false);
  });
});

describe("registration", () => {
  it("full mode registers the memory slot, three tools and the recall hook", () => {
    const { rec } = setup();
    expect([...rec.tools.keys()]).toEqual(["membase_search", "membase_save", "membase_profile"]);
    expect(rec.capabilities).toHaveLength(1);
    const cap = rec.capabilities[0];
    expect(typeof cap.providerRuntime?.open).toBe("function");
    expect(cap.recallToolNames).toEqual(["membase_search"]);
    expect(cap.providerFlushPlanResolver).toBeUndefined();
    expect(cap.promptBuilder?.({ availableTools: new Set(["membase_search"]) }).join("\n")).toContain("membase_search");
    expect(cap.promptBuilder?.({ availableTools: new Set() })).toEqual([]);
    expect(rec.hooks.map((h) => h.name)).toEqual(["before_prompt_build"]);
    expect(rec.hooks[0].opts).toMatchObject({ requiresToolAuthority: true, timeoutMs: 10_000 });
  });

  it("autoRecall false registers no hook; cli-metadata registers nothing; discovery has no hook", () => {
    expect(setup({ autoRecall: false }).rec.hooks).toEqual([]);
    const meta = setup({}, undefined, "cli-metadata").rec;
    expect(meta.tools.size + meta.capabilities.length + meta.hooks.length).toBe(0);
    const discovery = setup({}, undefined, "discovery").rec;
    expect(discovery.tools.size).toBe(3);
    expect(discovery.capabilities).toHaveLength(1);
    expect(discovery.hooks).toEqual([]);
  });

  it("autoCapture adds a pre-compaction flush that persists only through membase_save", () => {
    const { rec } = setup({ autoCapture: true });
    const plan = rec.capabilities[0].providerFlushPlanResolver?.({});
    expect(plan).toMatchObject({ persistenceToolNames: ["membase_save"] });
    expect(plan && "relativePath" in plan).toBe(false);
  });

  it("registering makes no network call (clients are lazy)", () => {
    const { calls } = setup();
    expect(calls).toEqual([]);
  });
});

describe("who reaches the memory", () => {
  it("offers every tool to the owner's own conversation", () => {
    const { rec } = setup();
    for (const name of TOOL_NAMES) expect(tool(rec, name, ownerCtx())?.name).toBe(name);
  });

  it("hides every tool from a group or another sender by default", () => {
    const { rec } = setup();
    for (const name of TOOL_NAMES) expect(tool(rec, name, groupCtx())).toBeNull();
  });

  it("allowSharedConversations opens reads there, never the save tool", () => {
    const { rec } = setup({ allowSharedConversations: true });
    expect(tool(rec, "membase_search", groupCtx())?.name).toBe("membase_search");
    expect(tool(rec, "membase_profile", groupCtx())?.name).toBe("membase_profile");
    expect(tool(rec, "membase_save", groupCtx())).toBeNull();
  });

  it("without a host audience only the trusted owner bit opens the tools", () => {
    const { rec } = setup();
    const noAudience = { agentId: "main", sessionKey: "agent:main:main" };
    expect(tool(rec, "membase_search", { ...noAudience, senderIsOwner: true })?.name).toBe("membase_search");
    expect(tool(rec, "membase_search", { ...noAudience, senderIsOwner: false })).toBeNull();
    expect(tool(rec, "membase_save", noAudience)).toBeNull();
  });

  it("an audience minted for another agent grants nothing", () => {
    const { rec } = setup();
    const ctx = ownerCtx({ memoryAudience: { kind: "owner-private", agentId: "other" } });
    expect(tool(rec, "membase_search", ctx)).toBeNull();
  });
});

describe("membase_search", () => {
  it("searches with the key and cites each passage's Memory", async () => {
    const { rec, calls } = setup({}, (c) => (c.path === "/v1/search" ? { json: SEARCH_OK } : undefined));
    const t = tool(rec, "membase_search", ownerCtx())!;
    const out = await t.execute("call-1", { query: "ledger database", limit: 3 });
    expect(calls).toHaveLength(1);
    expect(calls[0].url.origin).toBe(BASE);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].headers.Authorization).toBe(`Bearer ${KEY}`);
    expect(calls[0].body).toEqual({ q: "ledger database", limit: 3 });
    expect(textOf(out as any)).toContain("- [Project decisions] We settled on Postgres for the ledger.");
    expect(textOf(out as any)).toContain("[Customers]");
  });

  it("reports a Memory that could not answer instead of 'no memory'", async () => {
    const reply = { query: "q", results: [], containers: [{ id: "mv-dec", name: "Project decisions", error: "no model" }] };
    const { rec } = setup({}, () => ({ json: reply }));
    const out = await tool(rec, "membase_search", ownerCtx())!.execute("c", { query: "x" });
    const text = textOf(out as any);
    expect(text).toContain('Memory "Project decisions" could not answer: no model');
    expect(text).not.toContain("No matching memories");
  });

  it("a 403 says the key was narrowed or revoked and is not retried", async () => {
    const { rec, calls } = setup({}, () => FORBIDDEN);
    await expect(tool(rec, "membase_search", ownerCtx())!.execute("c", { query: "x" })).rejects.toThrow(
      /narrowed or revoked/,
    );
    expect(calls).toHaveLength(1);
  });

  it("stops before the call when the invocation is no longer current", async () => {
    const { rec, calls } = setup({}, () => ({ json: SEARCH_OK }));
    const ctx = ownerCtx({
      assertInvocationCurrent: () => {
        throw new Error("stale invocation");
      },
    });
    await expect(tool(rec, "membase_search", ctx)!.execute("c", { query: "x" })).rejects.toThrow("stale invocation");
    expect(calls).toEqual([]);
  });

  it("without any key the error says how to configure one", async () => {
    const scripted = scriptedFetch(() => ({ json: SEARCH_OK }));
    const rec = registerPlugin(createMembasePlugin({ fetch: scripted.fetch }), {});
    await expect(tool(rec, "membase_search", ownerCtx())!.execute("c", { query: "x" })).rejects.toThrow(
      /not configured.*MEMBASE_API_KEY/,
    );
    expect(scripted.calls).toEqual([]);
  });

  it("falls back to MEMBASE_API_KEY and the global fetch in the published entry", async () => {
    vi.stubEnv("MEMBASE_API_KEY", "mbk_from_env");
    const scripted = scriptedFetch(() => ({ json: SEARCH_OK }));
    vi.stubGlobal("fetch", scripted.fetch);
    const rec = registerPlugin(defaultEntry, { baseUrl: BASE });
    await tool(rec, "membase_search", ownerCtx())!.execute("c", { query: "x" });
    expect(scripted.calls[0].headers.Authorization).toBe("Bearer mbk_from_env");
  });
});

describe("membase_save", () => {
  it("saves the user's words into the configured Memory", async () => {
    const { rec, calls } = setup({ container: "mv-dec" }, (c) =>
      c.path === "/v1/memories"
        ? { status: 202, json: { memory_id: "m1", document_id: "srcitem_1", status: "queued", learning_run: null } }
        : undefined,
    );
    const out = await tool(rec, "membase_save", ownerCtx())!.execute("c", { content: "  We chose Postgres.  " });
    expect(calls[0]).toMatchObject({ method: "POST", path: "/v1/memories" });
    expect(calls[0].body).toEqual({ content: "We chose Postgres.", container: "mv-dec", static: false, title: "" });
    expect(textOf(out as any)).toMatch(/Saved to Membase.*srcitem_1.*queued/);
  });

  it("records a standing fact in the profile with static", async () => {
    const { rec, calls } = setup({}, () => ({ status: 202, json: { static: true, status: "recorded", run_id: "r" } }));
    const out = await tool(rec, "membase_save", ownerCtx())!.execute("c", { content: "I prefer dark mode.", static: true });
    expect(calls[0].body).toMatchObject({ static: true });
    expect(calls[0].body.container).toBeUndefined();
    expect(textOf(out as any)).toContain("profile");
  });

  it("an ambiguous container lists the Memories in reach", async () => {
    const { rec } = setup({}, (c) => {
      if (c.path === "/v1/memories") return { status: 400, json: { error: { code: "validation", message: "container is required" } } };
      if (c.path === "/v1/containers") return { json: { containers: [{ id: "mv-dec", name: "Project decisions" }, { id: "mv-crm", name: "Customers" }] } };
      return undefined;
    });
    await expect(tool(rec, "membase_save", ownerCtx())!.execute("c", { content: "x" })).rejects.toThrow(
      /Memories in reach: Project decisions \(mv-dec\), Customers \(mv-crm\)/,
    );
  });

  it("a Read-only or revoked key gets a clear 403 message", async () => {
    const { rec } = setup({}, () => FORBIDDEN);
    await expect(tool(rec, "membase_save", ownerCtx())!.execute("c", { content: "x" })).rejects.toThrow(/Read & write key/);
  });

  it("a pre-compaction flush does not save the same note twice", async () => {
    const { rec, calls } = setup({ autoCapture: true }, () => ({ status: 202, json: { status: "queued" } }));
    const ctx = ownerCtx({ memoryFlush: { flushId: "flush-1" } });
    await tool(rec, "membase_save", ctx)!.execute("a", { content: "We chose Postgres." });
    const again = await tool(rec, "membase_save", ctx)!.execute("b", { content: "We chose Postgres." });
    expect(calls).toHaveLength(1);
    expect(textOf(again as any)).toContain("Already saved");
  });
});

describe("membase_profile", () => {
  it("formats standing and recent facts", async () => {
    const { rec, calls } = setup({}, () => ({
      json: { static: ["Works in Berlin"], dynamic: [{ id: 1, content: "Now on the ledger project" }] },
    }));
    const out = await tool(rec, "membase_profile", ownerCtx())!.execute("c", { query: "work" });
    expect(calls[0]).toMatchObject({ method: "GET", path: "/v1/profile" });
    expect(calls[0].url.searchParams.get("q")).toBe("work");
    expect(textOf(out as any)).toContain("- Works in Berlin");
    expect(textOf(out as any)).toContain("- Now on the ledger project");
  });

  it("a 403 names Profile access", async () => {
    const { rec } = setup({}, () => FORBIDDEN);
    await expect(tool(rec, "membase_profile", ownerCtx())!.execute("c", {})).rejects.toThrow(/Profile access/);
  });
});
