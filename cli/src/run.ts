/** `membase` without Python: the hosted commands of the Python CLI, same names and flags. */

import { readFileSync } from "node:fs";
import { basename, extname } from "node:path";
import { parseArgs } from "node:util";
import { Membase, MembaseError, type MembaseOptions } from "@unibaseio/membase";

export const VERSION = "0.3.1";

const INSTALL_LOCAL = "pip install 'unibaseio-membase[local]'";
const HOSTED_MCP_URL = "https://api.app.membase.ai/mcp-http";

type Client = Pick<Membase, "add" | "search" | "profile" | "ask" | "containers" | "documents" | "memories">;

export interface Io {
  out: (text: string) => void;
  err: (text: string) => void;
  readFile: (path: string) => string;
  client: (opts: MembaseOptions) => Client;
}

const defaultIo: Io = {
  out: (t) => process.stdout.write(t + "\n"),
  err: (t) => process.stderr.write(t + "\n"),
  readFile: (p) => readFileSync(p, "utf8"),
  client: (opts) => new Membase(opts),
};

const USAGE = `usage: membase [--api-key KEY] [--base-url URL] <command> ...

Membase — long-term memory for AI (hosted API; key from MEMBASE_API_KEY).

commands:
  containers                      list the containers in reach
  search Q [--container ID] [--limit N]
                                  search memory
  add CONTENT [--container ID] [--static] [--title T]
                                  remember one fact or a short exchange
  documents add [PATH] [--url URL | --text TEXT] [--container ID] [--title T] [--custom-id ID]
  documents list [--container ID]
  documents get DOCUMENT_ID
  documents delete DOCUMENT_ID [--confirm]
  forget MEMORY_ID [--container ID] [--confirm]
                                  forget one memory by id
  profile [Q]                     who the user is
  ask MESSAGE                     an answer composed from memory

Local memory (--local, --store), import, agent, serve and mcp run in the Python package:
  ${INSTALL_LOCAL}`;

/** Flags each command takes, beyond the global --api-key / --base-url. */
const FLAGS: Record<string, string[]> = {
  containers: [],
  search: ["container", "limit"],
  add: ["container", "static", "title"],
  "documents add": ["url", "text", "container", "title", "custom-id"],
  "documents list": ["container"],
  "documents get": [],
  "documents delete": ["confirm"],
  forget: ["container", "confirm"],
  profile: [],
  ask: [],
};

const PYTHON_ONLY: Record<string, string> = {
  import: `membase import reads chat exports with the local engine: ${INSTALL_LOCAL}`,
  agent: `agent memory is local: ${INSTALL_LOCAL}, then membase --local agent ...`,
  serve: `membase serve runs the local engine: ${INSTALL_LOCAL}`,
  mcp: `membase mcp is in the Python package: ${INSTALL_LOCAL}. For hosted memory an MCP client can also connect to ${HOSTED_MCP_URL} directly, with nothing installed.`,
};

class UsageError extends Error {}

function positionals(args: string[], min: number, max: number, what: string): string[] {
  if (args.length < min || args.length > max) throw new UsageError(`${what}: expected ${min === max ? min : `${min}-${max}`} argument(s), got ${args.length}`);
  return args;
}

/** Runs one command line; returns the exit code (0 ok, 1 API error, 2 usage). */
export async function main(argv: string[], io: Io = defaultIo): Promise<number> {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      allowPositionals: true,
      strict: true,
      options: {
        help: { type: "boolean", short: "h" },
        version: { type: "boolean" },
        local: { type: "boolean" },
        store: { type: "string" },
        "api-key": { type: "string" },
        "base-url": { type: "string" },
        container: { type: "string" },
        limit: { type: "string" },
        static: { type: "boolean" },
        title: { type: "string" },
        url: { type: "string" },
        text: { type: "string" },
        "custom-id": { type: "string" },
        confirm: { type: "boolean" },
      },
    });
  } catch (e) {
    io.err(`error: ${(e as Error).message}\n\n${USAGE}`);
    return 2;
  }
  const { values: v, positionals: pos } = parsed;
  if (v.version) {
    io.out(VERSION);
    return 0;
  }
  if (v.help || pos.length === 0) {
    (v.help ? io.out : io.err)(USAGE);
    return v.help ? 0 : 2;
  }
  const [cmd, ...rest] = pos;
  if (cmd in PYTHON_ONLY) {
    io.err(PYTHON_ONLY[cmd]);
    return 2;
  }
  if (v.local || v.store !== undefined) {
    io.err(`local memory runs in the Python package: ${INSTALL_LOCAL}, then membase --local ...`);
    return 2;
  }

  try {
    const sub = cmd === "documents" ? rest.shift() : undefined;
    const key = sub === undefined ? cmd : `${cmd} ${sub}`;
    const allowed = FLAGS[key];
    if (!allowed) {
      throw new UsageError(cmd === "documents" ? "documents needs one of: add, list, get, delete" : `unknown command: ${cmd}`);
    }
    const global = new Set(["api-key", "base-url"]);
    const extra = Object.keys(v).filter((f) => !global.has(f) && !allowed.includes(f));
    if (extra.length) throw new UsageError(`${key} does not take --${extra.join(", --")}`);

    let limit: number | undefined;
    if (v.limit !== undefined) {
      limit = Number(v.limit);
      if (!Number.isInteger(limit)) throw new UsageError(`--limit: not an integer: ${v.limit}`);
    }

    let m: Client;
    try {
      m = io.client({ apiKey: v["api-key"], baseUrl: v["base-url"] });
    } catch {
      io.err("no API key: pass --api-key or set MEMBASE_API_KEY (Connect › Developer keys); "
        + `for local memory: ${INSTALL_LOCAL}`);
      return 2;
    }

    let out: unknown;
    switch (key) {
      case "containers":
        positionals(rest, 0, 0, key);
        out = await m.containers.list();
        break;
      case "search": {
        const [q] = positionals(rest, 1, 1, key);
        out = await m.search({ q, container: v.container, limit });
        break;
      }
      case "add": {
        const [content] = positionals(rest, 1, 1, key);
        out = await m.memories.add({ content, container: v.container, static: v.static ?? false, title: v.title ?? "" });
        break;
      }
      case "documents add": {
        const [path] = positionals(rest, 0, 1, key);
        let content = v.text;
        let title = v.title ?? "";
        if (path !== undefined) {
          if (content !== undefined) throw new UsageError("documents add needs exactly one of PATH, --text or --url");
          try {
            content = io.readFile(path);
          } catch {
            throw new UsageError(`cannot read ${path}`);
          }
          title = title || basename(path, extname(path));
        }
        if ((content === undefined) === (v.url === undefined)) {
          throw new UsageError("documents add needs exactly one of PATH, --text or --url");
        }
        out = await m.add({ container: v.container ?? "default", content, url: v.url, title, customId: v["custom-id"] ?? "" });
        break;
      }
      case "documents list":
        positionals(rest, 0, 0, key);
        out = await m.documents.list({ container: v.container });
        break;
      case "documents get": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.documents.get(id);
        break;
      }
      case "documents delete": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.documents.delete(id, { confirm: v.confirm ?? false });
        break;
      }
      case "forget": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.memories.forget(id, { container: v.container, confirm: v.confirm ?? false });
        break;
      }
      case "profile": {
        const [q] = positionals(rest, 0, 1, key);
        out = await m.profile({ q });
        break;
      }
      case "ask": {
        const [message] = positionals(rest, 1, 1, key);
        out = await m.ask({ message });
        break;
      }
    }
    io.out(JSON.stringify(out, null, 2));
    return 0;
  } catch (e) {
    if (e instanceof UsageError) {
      io.err(`error: ${e.message}`);
      return 2;
    }
    if (e instanceof MembaseError) {
      io.err(`error: ${e.message}`);
      return 1;
    }
    throw e;
  }
}
