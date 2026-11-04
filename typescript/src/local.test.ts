/** The client against local memory (`membase --store DIR serve`): the same routes and answers as hosted. */
import { describe, expect, it } from "vitest";
import { ConfirmationRequiredError, Membase, NotFoundError, UnprocessableEntityError } from "./index.js";

const base = process.env.MEMBASE_LOCAL_BASE_URL;

describe.skipIf(!base)("@unibaseio/membase against local memory", () => {
  const c = new Membase({ apiKey: "local", baseUrl: base });

  it("lists in the memory API's shapes", async () => {
    expect(await c.memories.list()).toEqual({ items: [], has_more: false, partial: [] });
    expect(await c.documents.list({ kind: "note" })).toEqual({ items: [], has_more: false, partial: [] });
    expect(await c.search({ q: "anything", mode: "fast" })).toEqual({ items: [], partial: [] });
    const caps = await c.capabilities();
    expect(caps.actions).toContain("memories.add");
    expect(caps.rules).toEqual({ citation_required: false, retention_notice: "", version: "" });
  });

  it("keeps and forgets profile facts", async () => {
    const added = await c.profileFacts.add({ content: "Prefers tea." });
    expect(added.id.startsWith("pf_")).toBe(true);
    expect((await c.profileFacts.list()).items.map((f) => f.content)).toContain("Prefers tea.");
    await expect(c.profileFacts.forget(added.id)).rejects.toBeInstanceOf(ConfirmationRequiredError);
    expect(await c.profileFacts.forget(added.id, { confirm: true })).toEqual({ id: added.id, status: "forgotten" });
  });

  it("answers refusals with the same error classes", async () => {
    await expect(c.documents.get("doc_missing")).rejects.toBeInstanceOf(NotFoundError);
    await expect(c.search({ q: "x", limit: 99 })).rejects.toBeInstanceOf(UnprocessableEntityError);
  });
});
