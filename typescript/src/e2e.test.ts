/** The client against a live Membase server, with a key that reaches one text Memory (its write
target) at Read & write. */
import { describe, expect, it } from "vitest";
import { Membase, PermissionDeniedError, NotFoundError } from "./index.js";

const base = process.env.MEMBASE_TEST_BASE_URL!;
const key = process.env.MEMBASE_TEST_KEY!;

describe("@unibaseio/membase against a live server", () => {
  const c = new Membase({ apiKey: key, baseUrl: base });

  it("names what it may do", async () => {
    const caps = await c.capabilities();
    expect(caps.actions).toContain("search");
    expect(caps.memories?.length).toBeGreaterThan(0);
  });

  it("adds a document, lists it, reads it", async () => {
    const added = await c.add({ content: "hello from typescript", customId: "ts-1", title: "TS" });
    expect(added.kind).toBe("file");
    expect(["succeeded", "processing", "pending"]).toContain(added.status);
    const again = await c.add({ content: "hello from typescript", customId: "ts-1" });
    expect(again.id).toBe(added.id);
    const docs = await c.documents.list({ kind: "file" });
    expect(docs.items.length).toBeGreaterThan(0);
    const one = await c.documents.get(docs.items[0].id);
    expect(one.id.startsWith("doc_")).toBe(true);
    expect(one.memory_name).toBeTruthy();
  });

  it("searches, lists facts and lists the profile facts", async () => {
    const hits = await c.search({ q: "pricing", limit: 3 });
    expect(Array.isArray(hits.items)).toBe(true);
    expect(Array.isArray(hits.partial)).toBe(true);
    const facts = await c.memories.list({ limit: 5 });
    expect(facts).toHaveProperty("has_more");
    const p = await c.profileFacts.list({ limit: 5 });
    expect(p).toHaveProperty("has_more");
  });

  it("saves a fact as a note", async () => {
    const out = await c.memories.add({ content: "We settled on Postgres." });
    expect(out.kind).toBe("note");
  });

  it("is refused above its level and on unknown ids", async () => {
    const docs = await c.documents.list();
    await expect(c.documents.delete(docs.items[0].id, { confirm: true })).rejects.toBeInstanceOf(PermissionDeniedError);
    await expect(c.documents.get("doc_does_not_exist")).rejects.toBeInstanceOf(NotFoundError);
    const bad = new Membase({ apiKey: "mbk_nope", baseUrl: base, maxRetries: 0 });
    const e = await bad.capabilities().catch((x) => x);
    expect(e).toBeInstanceOf(PermissionDeniedError);
    expect(e.code).toBe("unauthorized");
    const res = await fetch(base + "/v1/memories");
    expect(res.status).toBe(401);
  });
});
