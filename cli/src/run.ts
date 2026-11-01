/** `membase` without Python: the hosted commands of the Python CLI, same names and flags. */

import { readFileSync } from "node:fs";
import { basename, extname } from "node:path";
import { parseArgs } from "node:util";
import { ConfirmationRequiredError, Membase, MembaseError, type MembaseOptions } from "@unibaseio/membase";

export const VERSION = "0.4.0";

const INSTALL_LOCAL = "pip install 'unibaseio-membase[local]'";
const HOSTED_MCP_URL = "https://api.app.membase.com/mcp-http";

type Client = Pick<Membase, "add" | "search" | "ask" | "capabilities" | "documents" | "memories" | "profileFacts" | "media">;

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
  search Q [--limit N] [--mode full|fast]
                                  search memory
  add CONTENT [--title T]         remember one fact or a short exchange
  memories list [--limit N]       the newest facts
  memories get MEMORY_ID
  documents add [PATH] [--url URL | --text TEXT] [--title T] [--custom-id ID]
  documents list [--kind file|note] [--limit N]
  documents get DOCUMENT_ID
  documents delete DOCUMENT_ID [--confirm]
  forget MEMORY_ID [--confirm]    forget one memory by its mem_ id
  media list [--limit N]          photos and videos
  media get MEDIA_ID
  media search Q [--kind image|video] [--limit N]
  media delete MEDIA_ID [--confirm]
  profile-facts list [--limit N]  who the user is: standing statements, newest first
  profile-facts add CONTENT       keep one statement about the user
  profile-facts forget FACT_ID [--confirm]
  ask MESSAGE                     one answer written by the memory
  capabilities                    what this key may do, the Memories it reaches, and the rules

Writes land in the key's write target. Without --confirm a removal changes nothing and prints
what would go (exit 1).

Local memory (--local, --store), import, agent, media add/ask, serve and mcp run in the Python package:
  ${INSTALL_LOCAL}`;

/** Flags each command takes, beyond the global --api-key / --base-url. */
const FLAGS: Record<string, string[]> = {
  search: ["limit", "mode"],
  add: ["title"],
  "memories list": ["limit"],
  "memories get": [],
  "documents add": ["url", "text", "title", "custom-id"],
  "documents list": ["kind", "limit"],
  "documents get": [],
  "documents delete": ["confirm"],
  forget: ["confirm"],
  "media list": ["limit"],
  "media get": [],
  "media search": ["kind", "limit"],
  "media delete": ["confirm"],
  "profile-facts list": ["limit"],
  "profile-facts add": [],
  "profile-facts forget": ["confirm"],
  ask: [],
  capabilities: [],
};

const GROUPS: Record<string, string> = {
  memories: "list, get",
  documents: "add, list, get, delete",
  media: "list, get, search, delete",
  "profile-facts": "list, add, forget",
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
        limit: { type: "string" },
        mode: { type: "string" },
        kind: { type: "string" },
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
    if (cmd === "media" && (rest[0] === "add" || rest[0] === "ask")) {
      io.err(`media ${rest[0]} runs on local memory: ${INSTALL_LOCAL}, then membase --local media ${rest[0]} ...`);
      return 2;
    }
    const sub = cmd in GROUPS ? rest.shift() : undefined;
    const key = sub === undefined ? cmd : `${cmd} ${sub}`;
    const allowed = FLAGS[key];
    if (!allowed) {
      throw new UsageError(cmd in GROUPS ? `${cmd} needs one of: ${GROUPS[cmd]}` : `unknown command: ${cmd}`);
    }
    const global = new Set(["api-key", "base-url"]);
    const extra = Object.keys(v).filter((f) => !global.has(f) && !allowed.includes(f));
    if (extra.length) throw new UsageError(`${key} does not take --${extra.join(", --")}`);

    let limit: number | undefined;
    if (v.limit !== undefined) {
      limit = Number(v.limit);
      if (!Number.isInteger(limit)) throw new UsageError(`--limit: not an integer: ${v.limit}`);
    }
    const choice = <T extends string>(flag: string, value: string | undefined, choices: readonly T[]): T | undefined => {
      if (value !== undefined && !(choices as readonly string[]).includes(value)) {
        throw new UsageError(`--${flag}: one of ${choices.join(", ")}`);
      }
      return value as T | undefined;
    };

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
      case "search": {
        const [q] = positionals(rest, 1, 1, key);
        out = await m.search({ q, limit, mode: choice("mode", v.mode, ["full", "fast"] as const) });
        break;
      }
      case "add": {
        const [content] = positionals(rest, 1, 1, key);
        out = await m.memories.add({ content, title: v.title ?? "" });
        break;
      }
      case "memories list":
        positionals(rest, 0, 0, key);
        out = await m.memories.list({ limit });
        break;
      case "memories get": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.memories.get(id);
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
        out = await m.add({ content, url: v.url, title, customId: v["custom-id"] ?? "" });
        break;
      }
      case "documents list":
        positionals(rest, 0, 0, key);
        out = await m.documents.list({ kind: choice("kind", v.kind, ["file", "note"] as const), limit });
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
        out = await m.memories.forget(id, { confirm: v.confirm ?? false });
        break;
      }
      case "media list":
        positionals(rest, 0, 0, key);
        out = await m.media.list({ limit });
        break;
      case "media get": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.media.get(id);
        break;
      }
      case "media search": {
        const [q] = positionals(rest, 1, 1, key);
        out = await m.media.search({ q, kind: choice("kind", v.kind, ["image", "video"] as const), limit });
        break;
      }
      case "media delete": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.media.delete(id, { confirm: v.confirm ?? false });
        break;
      }
      case "profile-facts list":
        positionals(rest, 0, 0, key);
        out = await m.profileFacts.list({ limit });
        break;
      case "profile-facts add": {
        const [content] = positionals(rest, 1, 1, key);
        out = await m.profileFacts.add({ content });
        break;
      }
      case "profile-facts forget": {
        const [id] = positionals(rest, 1, 1, key);
        out = await m.profileFacts.forget(id, { confirm: v.confirm ?? false });
        break;
      }
      case "ask": {
        const [message] = positionals(rest, 1, 1, key);
        out = await m.ask({ message });
        break;
      }
      case "capabilities":
        positionals(rest, 0, 0, key);
        out = await m.capabilities();
        break;
    }
    io.out(JSON.stringify(out, null, 2));
    return 0;
  } catch (e) {
    if (e instanceof UsageError) {
      io.err(`error: ${e.message}`);
      return 2;
    }
    if (e instanceof ConfirmationRequiredError) {
      // Nothing was removed: show what would be, from the API's own answer.
      io.out(JSON.stringify(e.body, null, 2));
      io.err(`error: ${e.message}`);
      return 1;
    }
    if (e instanceof MembaseError) {
      io.err(`error: ${e.message}`);
      return 1;
    }
    throw e;
  }
}
