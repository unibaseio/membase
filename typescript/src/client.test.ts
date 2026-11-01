/** The client against a scripted `fetch`: request shapes, error classes, retries. */
import { describe, expect, it } from "vitest";
import {
  Membase,
  PermissionDeniedError,
  RateLimitError,
  NotFoundError,
  AuthenticationError,
  APIConnectionError,
  ConfirmationRequiredError,
  WriteTargetRequiredError,
  UnprocessableEntityError,
} from "./index.js";

type Call = { url: URL; init: RequestInit };

function scripted(responses: Array<{ status: number; body?: unknown; headers?: Record<string, string> }>) {
  const calls: Call[] = [];
  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: new URL(String(input)), init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error("no scripted response left");
    return new Response(next.body === undefined ? null : JSON.stringify(next.body), {
      status: next.status,
      headers: { "content-type": "application/json", ...(next.headers ?? {}) },
    });
  }) as typeof fetch;
  return { calls, fetchImpl };
}

describe("Membase", () => {
  it("needs a key", () => {
    const saved = process.env.MEMBASE_API_KEY;
    delete process.env.MEMBASE_API_KEY;
    try {
      expect(() => new Membase()).toThrow(/MEMBASE_API_KEY/);
    } finally {
      if (saved !== undefined) process.env.MEMBASE_API_KEY = saved;
    }
  });

  it("sends the memory API's shapes with the bearer, and never a container", async () => {
    const receipt = { id: "doc_bm90ZQ", kind: "note", status: "processing", learning: [{ memory_name: "Life", status: "processing" }] };
    const { calls, fetchImpl } = scripted([
      { status: 202, body: { id: "doc_ZmlsZQ", kind: "file", status: "pending", learning: [] } },
      { status: 200, body: { items: [{ type: "memory", id: "mem_eA", content: "Postgres", score: 1, memory_name: "Work" }], partial: [] } },
      { status: 200, body: { items: [], partial: [] } },
      { status: 200, body: { items: [], has_more: false, partial: [] } },
      { status: 200, body: { id: "mem_eA", content: "Postgres", memory_name: "Work" } },
      { status: 202, body: receipt },
      { status: 200, body: { id: "mem_eA", status: "forgotten" } },
      { status: 200, body: { items: [], has_more: false, partial: [] } },
      { status: 200, body: { id: "doc_ZmlsZQ", kind: "file", title: "", memory_name: "Work", learning: [] } },
      { status: 200, body: { id: "doc_ZmlsZQ", status: "deleted" } },
      { status: 200, body: { items: [], has_more: false, partial: [] } },
      { status: 200, body: { id: "med_eA", kind: "image", memory_name: "Trip", summary: "" } },
      { status: 200, body: { items: [], partial: [] } },
      { status: 200, body: { id: "med_eA", status: "deleted" } },
      { status: 200, body: { static: [], dynamic: [] } },
      { status: 200, body: { principal: "apikey", surfaces: [], tools: [], actions: ["search"] } },
    ]);
    const c = new Membase({ apiKey: "mbk_test", baseUrl: "https://api.example.test/", fetch: fetchImpl, maxRetries: 0 });

    const added = await c.add({ content: "hello", customId: "d1" });
    expect(added.id).toBe("doc_ZmlsZQ");
    expect(calls[0].url.toString()).toBe("https://api.example.test/v1/documents");
    expect(calls[0].init.method).toBe("POST");
    expect((calls[0].init.headers as Record<string, string>).Authorization).toBe("Bearer mbk_test");

    const hits = await c.search({ q: "q", limit: 5 });
    expect(hits.items[0].memory_name).toBe("Work");
    await c.search({ q: "q", mode: "fast" });
    await c.memories.list({ limit: 3 });
    await c.memories.get("mem_eA");
    expect(await c.memories.add({ content: "dark mode" })).toEqual(receipt);
    expect(await c.memories.forget("mem_eA", { confirm: true })).toEqual({ id: "mem_eA", status: "forgotten" });
    await c.documents.list({ kind: "note", limit: 10 });
    await c.documents.get("doc_ZmlsZQ");
    await c.documents.delete("doc_ZmlsZQ");
    await c.media.list({ limit: 2 });
    await c.media.get("med_eA");
    await c.media.search({ q: "skiing", kind: "video" });
    await c.media.delete("med_eA", { confirm: true });
    await c.profile({ q: "hours" });
    expect((await c.capabilities()).actions).toEqual(["search"]);

    const seen = calls.map((x) => [x.init.method, x.url.pathname + x.url.search, x.init.body ? JSON.parse(String(x.init.body)) : undefined]);
    expect(seen).toEqual([
      ["POST", "/v1/documents", { content: "hello", title: "", custom_id: "d1" }],
      ["POST", "/v1/search", { q: "q", limit: 5, mode: "full" }],
      ["POST", "/v1/search", { q: "q", mode: "fast" }],
      ["GET", "/v1/memories?limit=3", undefined],
      ["GET", "/v1/memories/mem_eA", undefined],
      ["POST", "/v1/memories", { content: "dark mode", title: "" }],
      ["DELETE", "/v1/memories/mem_eA?confirm=true", undefined],
      ["GET", "/v1/documents?kind=note&limit=10", undefined],
      ["GET", "/v1/documents/doc_ZmlsZQ", undefined],
      ["DELETE", "/v1/documents/doc_ZmlsZQ?confirm=false", undefined],
      ["GET", "/v1/media?limit=2", undefined],
      ["GET", "/v1/media/med_eA", undefined],
      ["POST", "/v1/media/search", { q: "skiing", kind: "video" }],
      ["DELETE", "/v1/media/med_eA?confirm=true", undefined],
      ["GET", "/v1/profile?q=hours", undefined],
      ["GET", "/v1/capabilities", undefined],
    ]);
    for (const x of calls) {
      expect(x.url.toString()).not.toContain("container");
      expect(String(x.init.body ?? "")).not.toContain("container");
    }
    expect((c as unknown as Record<string, unknown>).containers).toBeUndefined();
  });

  it("raises a typed 428 for an unconfirmed removal and a typed 422 for a missing write target", async () => {
    const confirm = { error: { code: "confirmation_required", message: "Pass confirm=true to forget this memory.", details: { id: "mem_eA", verb: "forget_memory" }, retryable: false, trace_id: "t-2" } };
    const { calls, fetchImpl } = scripted([
      { status: 428, body: confirm },
      { status: 422, body: { error: { code: "write_target_required", message: "reaches 2 Memories", details: { memories_in_reach: 2 }, retryable: false, trace_id: "t-3" } } },
      { status: 422, body: { error: { code: "validation", message: "Request validation failed.", details: { fields: [] }, retryable: false, trace_id: "t-4" } } },
    ]);
    const c = new Membase({ apiKey: "mbk_test", baseUrl: "https://api.example.test", fetch: fetchImpl, maxRetries: 2 });
    const e = await c.memories.forget("mem_eA").catch((x) => x);
    expect(e).toBeInstanceOf(ConfirmationRequiredError);
    expect(e.status).toBe(428);
    expect(e.details).toEqual({ id: "mem_eA", verb: "forget_memory" });
    expect(calls.length).toBe(1);
    const w = await c.memories.add({ content: "x" }).catch((x) => x);
    expect(w).toBeInstanceOf(WriteTargetRequiredError);
    expect(w).toBeInstanceOf(UnprocessableEntityError);
    expect(w.code).toBe("write_target_required");
    const v = await c.add({ content: "x" }).catch((x) => x);
    expect(v).toBeInstanceOf(UnprocessableEntityError);
    expect(v).not.toBeInstanceOf(WriteTargetRequiredError);
  });

  it("maps statuses to error classes and keeps the envelope", async () => {
    const env = (code: string, message: string) => ({ error: { code, message, details: {}, retryable: false, trace_id: "t-1" } });
    const { fetchImpl } = scripted([
      { status: 403, body: env("unauthorized", "this key was revoked") },
      { status: 404, body: env("not_found", "no such document") },
      { status: 401, body: env("unauthenticated", "missing credential") },
    ]);
    const c = new Membase({ apiKey: "mbk_test", baseUrl: "https://api.example.test", fetch: fetchImpl, maxRetries: 0 });
    const e = await c.search({ q: "x" }).catch((x) => x);
    expect(e).toBeInstanceOf(PermissionDeniedError);
    expect(e.code).toBe("unauthorized");
    expect(e.traceId).toBe("t-1");
    expect(e.status).toBe(403);
    await expect(c.documents.get("nope")).rejects.toBeInstanceOf(NotFoundError);
    await expect(c.capabilities()).rejects.toBeInstanceOf(AuthenticationError);
  });

  it("retries 429 and 5xx, then gives up with the last answer", async () => {
    const { calls, fetchImpl } = scripted([
      { status: 429, body: { error: { code: "rate_limited", message: "budget" } }, headers: { "Retry-After": "0" } },
      { status: 503, body: { error: { code: "provider_unavailable", message: "down" } } },
      { status: 200, body: { items: [{ id: "mem_eA", content: "A", memory_name: "Work" }], has_more: false, partial: [] } },
    ]);
    const c = new Membase({ apiKey: "mbk_test", baseUrl: "https://api.example.test", fetch: fetchImpl, maxRetries: 2 });
    const out = await c.memories.list();
    expect(out.items[0].id).toBe("mem_eA");
    expect(calls.length).toBe(3);

    const exhausted = scripted([
      { status: 429, body: { error: { code: "rate_limited", message: "budget" } }, headers: { "Retry-After": "0" } },
      { status: 429, body: { error: { code: "rate_limited", message: "budget" } }, headers: { "Retry-After": "0" } },
    ]);
    const c2 = new Membase({ apiKey: "mbk_test", baseUrl: "https://api.example.test", fetch: exhausted.fetchImpl, maxRetries: 1 });
    await expect(c2.memories.list()).rejects.toBeInstanceOf(RateLimitError);
    expect(exhausted.calls.length).toBe(2);
  });

  it("turns a dead connection into APIConnectionError", async () => {
    const failing = (async () => {
      throw new TypeError("fetch failed");
    }) as unknown as typeof fetch;
    const c = new Membase({ apiKey: "mbk_test", baseUrl: "https://api.example.test", fetch: failing, maxRetries: 0 });
    await expect(c.capabilities()).rejects.toBeInstanceOf(APIConnectionError);
  });
});
