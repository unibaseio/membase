/**
 * Loader-backed smoke test: the real `openclaw` CLI loads the built plugin from this folder,
 * selects it for the memory slot, and asks it for memory status, which goes through the
 * provider runtime to a local stand-in for the Membase API.
 *
 * OpenClaw 2026.10 needs Node >= 24.16; the test runs when the current Node qualifies or
 * OPENCLAW_NODE names a binary that does, and is skipped otherwise. Run `npm run build` first
 * (`npm test` does).
 */
import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { delimiter, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const root = fileURLToPath(new URL("..", import.meta.url));
const cli = `${root}node_modules/openclaw/openclaw.mjs`;

function nodeOk(version: string): boolean {
  const [major, minor] = version.replace(/^v/, "").split(".").map(Number);
  return (major === 24 && minor >= 16) || (major === 26 && minor >= 1) || major > 26;
}

const nodeBin = process.env.OPENCLAW_NODE || (nodeOk(process.version) ? process.execPath : "");
const enabled = Boolean(nodeBin) && existsSync(cli) && existsSync(`${root}dist/index.js`);

describe.skipIf(!enabled)("OpenClaw loads the plugin (real CLI)", () => {
  const home = `${root}node_modules/.cache/membase-loader-test`;
  const requests: string[] = [];
  let server: http.Server;
  let baseUrl = "";

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      requests.push(`${req.method} ${req.url} ${req.headers.authorization ?? ""}`);
      res.setHeader("content-type", "application/json");
      if (req.url?.startsWith("/v1/containers")) {
        res.end(JSON.stringify({ containers: [{ id: "mv-dec", name: "Project decisions" }] }));
        return;
      }
      res.statusCode = 404;
      res.end("{}");
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    rmSync(home, { recursive: true, force: true });
    mkdirSync(`${home}/state`, { recursive: true });
    writeFileSync(
      `${home}/openclaw.json`,
      JSON.stringify({
        plugins: {
          load: { paths: [root] },
          slots: { memory: "membase" },
          entries: { membase: { enabled: true, config: { apiKey: "mbk_loader_test", baseUrl } } },
        },
      }),
    );
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    rmSync(home, { recursive: true, force: true });
  });

  // stdout goes to a file: the CLI exits right after writing, which can drop a piped tail.
  async function openclaw(...args: string[]): Promise<string> {
    const outPath = `${home}/stdout.txt`;
    const fd = openSync(outPath, "w");
    const errFd = openSync(`${home}/stderr.txt`, "w");
    try {
      const code = await new Promise<number | null>((resolve, reject) => {
        const child = spawn(nodeBin, [cli, ...args], {
          env: {
            // Under VITEST=true the OpenClaw CLI suppresses its own output.
            ...Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("VITEST"))),
            // OpenClaw starts helper processes with `node` from PATH; they need the same Node.
            PATH: `${dirname(nodeBin)}${delimiter}${process.env.PATH ?? ""}`,
            HOME: home,
            OPENCLAW_STATE_DIR: `${home}/state`,
            OPENCLAW_CONFIG_PATH: `${home}/openclaw.json`,
            MEMBASE_API_KEY: "",
          },
          stdio: ["ignore", fd, errFd],
          timeout: 240_000,
        });
        child.on("error", reject);
        child.on("exit", resolve);
      });
      const stdout = readFileSync(outPath, "utf8");
      if (code !== 0) throw new Error(`openclaw ${args.join(" ")} exited ${code}: ${readFileSync(`${home}/stderr.txt`, "utf8")}`);
      return stdout;
    } finally {
      closeSync(fd);
      closeSync(errFd);
    }
  }

  it("inspects as a loaded memory-slot plugin with three tools", async () => {
    const out = JSON.parse(await openclaw("plugins", "inspect", "membase", "--runtime", "--json"));
    expect(out.plugin).toMatchObject({
      id: "membase",
      status: "loaded",
      kind: "memory",
      memorySlotSelected: true,
      toolNames: ["membase_search", "membase_save", "membase_profile"],
    });
  }, 300_000);

  it("memory status reaches Membase through the provider runtime", async () => {
    const stdout = await openclaw("memory", "status", "--json");
    const status = JSON.parse(stdout.slice(stdout.indexOf("[")));
    expect(status[0]).toMatchObject({ provider: "membase", health: { status: "ready" } });
    expect(requests).toContain("GET /v1/containers Bearer mbk_loader_test");
  }, 300_000);
});
