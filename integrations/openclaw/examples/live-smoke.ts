/**
 * Live smoke test against the real Membase API, through the plugin's own OpenClaw entry.
 *
 *   npm run build
 *   MEMBASE_API_KEY=mbk_... node examples/live-smoke.ts
 *
 * Optional: MEMBASE_BASE_URL (another server), MEMBASE_CONTAINER (a container id to search and
 * save into), MEMBASE_SMOKE_WRITE=1 (also save one note; off by default because saving is a
 * deliberate act). Without MEMBASE_API_KEY it prints how to run and exits 0.
 *
 * It registers the built plugin the way OpenClaw does (entry.register(api)), then: checks the
 * memory slot's health, searches through the membase_search tool, runs the automatic-recall
 * hook with its budget, reads the profile, and optionally saves.
 */
import entry from "../dist/index.js";

const key = process.env.MEMBASE_API_KEY;
if (!key) {
  console.log("MEMBASE_API_KEY is not set; nothing to do. Usage: MEMBASE_API_KEY=mbk_... node examples/live-smoke.ts");
  process.exit(0);
}

const config: Record<string, unknown> = { apiKey: key };
if (process.env.MEMBASE_BASE_URL) config.baseUrl = process.env.MEMBASE_BASE_URL;
if (process.env.MEMBASE_CONTAINER) config.container = process.env.MEMBASE_CONTAINER;

const tools = new Map<string, (ctx: Record<string, unknown>) => any>();
const capabilities: any[] = [];
const hooks: Array<{ name: string; handler: (event: unknown, ctx: unknown) => Promise<any> }> = [];
const api = {
  id: "membase",
  name: "Membase",
  source: "live-smoke",
  registrationMode: "full",
  config: {},
  pluginConfig: config,
  logger: { info: console.log, warn: console.warn, error: console.error, debug: () => undefined },
  registerTool: (factory: any, opts: { name: string }) => tools.set(opts.name, factory),
  registerMemoryCapability: (capability: unknown) => capabilities.push(capability),
  on: (name: string, handler: any) => hooks.push({ name, handler }),
};
entry.register?.(api as any);

const owner = {
  agentId: "main",
  sessionKey: "agent:main:main",
  memoryAudience: { kind: "owner-private", agentId: "main" },
};
const text = (r: any) => r.content.map((c: any) => c.text).join("\n");
let failures = 0;

async function step(name: string, fn: () => Promise<string>, optional = false) {
  const started = Date.now();
  try {
    const out = await fn();
    console.log(`ok   ${name} (${Date.now() - started} ms)\n${out.replace(/^/gm, "     ")}`);
  } catch (error) {
    if (!optional) failures += 1;
    console.log(`${optional ? "note" : "FAIL"} ${name} (${Date.now() - started} ms): ${(error as Error).message}`);
  }
}

const { provider } = await capabilities[0].providerRuntime.open({
  cfg: {},
  agentId: "main",
  context: { authority: { kind: "session", sessionKey: "agent:main:main", sandboxed: false, audience: owner.memoryAudience }, assertCurrent: () => undefined },
});

await step("memory slot health", async () => JSON.stringify(await provider.health()));
await step("membase_search tool (SDK timeout, wakes the memory)", async () =>
  text(await tools.get("membase_search")!(owner).execute("smoke-1", { query: "what am I working on?", limit: 5 })),
);
await step("automatic recall hook (recall budget)", async () => {
  const recall = hooks.find((h) => h.name === "before_prompt_build");
  if (!recall) return "autoRecall is off";
  const out = await recall.handler(
    { prompt: "What did we decide recently?", currentUserMessage: "What did we decide recently?", messages: [] },
    {
      ...owner,
      trigger: "user",
      toolAuthority: { fingerprint: "smoke", allows: () => true, assertActive: () => undefined },
    },
  );
  return out?.prependContext ?? "(nothing injected)";
});
await step("memory slot search (recall budget)", async () => JSON.stringify(await provider.search({ query: "recent decisions", maxResults: 3 }), null, 1));
await step("membase_profile tool (needs Profile access on the key)", async () =>
  text(await tools.get("membase_profile")!(owner).execute("smoke-2", {})),
  true, // a key without Profile access answers 403; that is a setting, not a failure
);
if (process.env.MEMBASE_SMOKE_WRITE === "1") {
  await step("membase_save tool", async () =>
    text(
      await tools.get("membase_save")!(owner).execute("smoke-3", {
        content: `Membase OpenClaw plugin live smoke test ran on ${new Date().toISOString().slice(0, 10)}.`,
      }),
    ),
  );
} else {
  console.log("skip membase_save (set MEMBASE_SMOKE_WRITE=1 to save one note)");
}
await provider.close();

console.log(failures ? `${failures} step(s) failed` : "all steps passed");
process.exit(failures ? 1 : 0);
