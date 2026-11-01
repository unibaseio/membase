/** The CLI against a recording client: argument parsing, the calls it makes, exit codes. */
import { describe, expect, it } from "vitest";
import { ConfirmationRequiredError, NotFoundError, type MembaseOptions } from "@unibaseio/membase";
import { main, VERSION, type Io } from "./run.js";

function harness(opts: { files?: Record<string, string>; fail?: Error; noKey?: boolean } = {}) {
  const calls: Array<[string, ...unknown[]]> = [];
  const out: string[] = [];
  const err: string[] = [];
  const clientOpts: MembaseOptions[] = [];
  const rec = (name: string) => async (...args: unknown[]) => {
    calls.push([name, ...args]);
    if (opts.fail) throw opts.fail;
    return { ok: name };
  };
  const io: Io = {
    out: (t) => out.push(t),
    err: (t) => err.push(t),
    readFile: (p) => {
      if (!(p in (opts.files ?? {}))) throw new Error("ENOENT");
      return opts.files![p];
    },
    client: (o) => {
      if (opts.noKey) throw new Error("no API key");
      clientOpts.push(o);
      return {
        add: rec("add"),
        search: rec("search"),
        profile: rec("profile"),
        ask: rec("ask"),
        capabilities: rec("capabilities"),
        documents: { list: rec("documents.list"), get: rec("documents.get"), delete: rec("documents.delete") },
        memories: { list: rec("memories.list"), get: rec("memories.get"), add: rec("memories.add"), forget: rec("memories.forget") },
        media: { list: rec("media.list"), get: rec("media.get"), search: rec("media.search"), delete: rec("media.delete") },
      } as unknown as ReturnType<Io["client"]>;
    },
  };
  return { io, calls, out, err, clientOpts };
}

describe("membase (npm)", () => {
  it("maps each hosted command to the client, like the Python CLI", async () => {
    const cases: Array<[string[], [string, ...unknown[]]]> = [
      [["search", "ledger db", "--limit", "5"], ["search", { q: "ledger db", limit: 5, mode: undefined }]],
      [["search", "ledger db", "--mode", "fast"], ["search", { q: "ledger db", limit: undefined, mode: "fast" }]],
      [["add", "We picked Postgres", "--title", "ledger"], ["memories.add", { content: "We picked Postgres", title: "ledger" }]],
      [["memories", "list", "--limit", "3"], ["memories.list", { limit: 3 }]],
      [["memories", "get", "mem_eA"], ["memories.get", "mem_eA"]],
      [["documents", "add", "--text", "notes", "--custom-id", "d1"], ["add", { content: "notes", url: undefined, title: "", customId: "d1" }]],
      [["documents", "add", "--url", "https://example.com"], ["add", { content: undefined, url: "https://example.com", title: "", customId: "" }]],
      [["documents", "list", "--kind", "note", "--limit", "2"], ["documents.list", { kind: "note", limit: 2 }]],
      [["documents", "get", "doc_eA"], ["documents.get", "doc_eA"]],
      [["documents", "delete", "doc_eA", "--confirm"], ["documents.delete", "doc_eA", { confirm: true }]],
      [["forget", "mem_eA"], ["memories.forget", "mem_eA", { confirm: false }]],
      [["media", "list"], ["media.list", { limit: undefined }]],
      [["media", "get", "med_eA"], ["media.get", "med_eA"]],
      [["media", "search", "skiing", "--kind", "video"], ["media.search", { q: "skiing", kind: "video", limit: undefined }]],
      [["media", "delete", "med_eA", "--confirm"], ["media.delete", "med_eA", { confirm: true }]],
      [["profile", "hours"], ["profile", { q: "hours" }]],
      [["profile"], ["profile", { q: undefined }]],
      [["ask", "Which database?"], ["ask", { message: "Which database?" }]],
      [["capabilities"], ["capabilities"]],
    ];
    for (const [argv, expected] of cases) {
      const h = harness();
      expect(await main(argv, h.io), argv.join(" ")).toBe(0);
      expect(h.calls).toEqual([expected]);
      expect(JSON.parse(h.out[0])).toEqual({ ok: expected[0] });
    }
  });

  it("reads a document from a file, titled by its name", async () => {
    const h = harness({ files: { "notes/design.md": "# Design" } });
    expect(await main(["documents", "add", "notes/design.md"], h.io)).toBe(0);
    expect(h.calls).toEqual([["add", { content: "# Design", url: undefined, title: "design", customId: "" }]]);
  });

  it("passes --api-key and --base-url to the client", async () => {
    const h = harness();
    expect(await main(["--api-key", "mbk_x", "--base-url", "https://api.example.test", "capabilities"], h.io)).toBe(0);
    expect(h.clientOpts).toEqual([{ apiKey: "mbk_x", baseUrl: "https://api.example.test" }]);
  });

  it("sends local and Python-only commands to the Python package", async () => {
    for (const argv of [["--local", "search", "x"], ["--store", "/tmp/m", "capabilities"], ["serve"], ["mcp"], ["import", "a.json"], ["agent", "skills"], ["media", "add", "a.png"], ["media", "ask", "what?"]]) {
      const h = harness();
      expect(await main(argv, h.io), argv.join(" ")).toBe(2);
      expect(h.err.join("\n")).toContain("pip install 'unibaseio-membase[local]'");
      expect(h.calls).toEqual([]);
    }
  });

  it("rejects bad usage with exit code 2", async () => {
    for (const argv of [
      [],
      ["frobnicate"],
      ["documents"],
      ["search"],
      ["search", "q", "--static"],
      ["search", "q", "--container", "mv-1"],
      ["search", "q", "--mode", "slow"],
      ["containers"],
      ["memories"],
      ["documents", "list", "--kind", "pdf"],
      ["media", "search", "x", "--kind", "audio"],
      ["search", "q", "--limit", "many"],
      ["documents", "add"],
      ["documents", "add", "--text", "a", "--url", "https://x"],
      ["documents", "add", "missing.md"],
      ["ask", "--nope", "x"],
    ]) {
      const h = harness();
      expect(await main(argv, h.io), argv.join(" ")).toBe(2);
      expect(h.calls).toEqual([]);
    }
  });

  it("explains a missing key", async () => {
    const h = harness({ noKey: true });
    expect(await main(["capabilities"], h.io)).toBe(2);
    expect(h.err[0]).toContain("MEMBASE_API_KEY");
  });

  it("prints an API error and exits 1", async () => {
    const h = harness({ fail: new NotFoundError("document not found", { status: 404 }) });
    expect(await main(["documents", "get", "nope"], h.io)).toBe(1);
    expect(h.err[0]).toMatch(/^error: /);
  });

  it("shows what an unconfirmed removal would remove, and exits 1", async () => {
    const body = { error: { code: "confirmation_required", message: "Pass confirm=true to forget this memory.", details: { id: "mem_eA", verb: "forget_memory" }, retryable: false, trace_id: "" } };
    const h = harness({ fail: new ConfirmationRequiredError(body.error.message, { status: 428, body }) });
    expect(await main(["forget", "mem_eA"], h.io)).toBe(1);
    expect(JSON.parse(h.out[0])).toEqual(body);
    expect(h.err[0]).toMatch(/^error: /);
  });

  it("prints its version and help", async () => {
    const v = harness();
    expect(await main(["--version"], v.io)).toBe(0);
    expect(v.out).toEqual([VERSION]);
    const h = harness();
    expect(await main(["--help"], h.io)).toBe(0);
    expect(h.out[0]).toContain("usage: membase");
  });
});
