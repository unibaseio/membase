/** Test doubles: a recording OpenClaw plugin API and a scripted `fetch` for the Membase SDK. */
import { vi } from "vitest";
import type {
  AnyAgentTool,
  OpenClawPluginApi,
  OpenClawPluginDefinition,
  OpenClawPluginToolContext,
} from "openclaw/plugin-sdk/plugin-entry";
import type { MemoryPluginCapability } from "openclaw/plugin-sdk/memory-host-core";

type ToolFactory = (ctx: OpenClawPluginToolContext) => AnyAgentTool | AnyAgentTool[] | null | undefined;

export interface Recorded {
  api: OpenClawPluginApi;
  tools: Map<string, ToolFactory>;
  capabilities: MemoryPluginCapability[];
  hooks: Array<{ name: string; handler: (...args: any[]) => any; opts?: Record<string, unknown> }>;
  logs: { info: string[]; warn: string[]; error: string[] };
}

/**
 * Registers a plugin definition the way OpenClaw's loader does: it calls the entry's
 * `register(api)` with an api carrying `registrationMode` and the plugin's config, and records
 * every registration made through the typed `OpenClawPluginApi` surface the plugin uses.
 */
export function registerPlugin(
  entry: OpenClawPluginDefinition,
  pluginConfig: Record<string, unknown> = {},
  registrationMode: OpenClawPluginApi["registrationMode"] = "full",
): Recorded {
  const tools = new Map<string, ToolFactory>();
  const capabilities: MemoryPluginCapability[] = [];
  const hooks: Recorded["hooks"] = [];
  const logs = { info: [] as string[], warn: [] as string[], error: [] as string[] };
  const api = {
    id: entry.id ?? "membase",
    name: entry.name ?? "Membase",
    source: "test",
    registrationMode,
    config: {},
    pluginConfig,
    logger: {
      info: (m: string) => logs.info.push(m),
      warn: (m: string) => logs.warn.push(m),
      error: (m: string) => logs.error.push(m),
    },
    registerTool(tool: unknown, opts?: { name?: string }) {
      const factory = (typeof tool === "function" ? tool : () => tool) as ToolFactory;
      const name = opts?.name ?? (tool as AnyAgentTool).name;
      tools.set(name, factory);
    },
    registerMemoryCapability(capability: MemoryPluginCapability) {
      capabilities.push(capability);
    },
    on(name: string, handler: (...args: any[]) => any, opts?: Record<string, unknown>) {
      hooks.push({ name, handler, opts });
    },
  } as unknown as OpenClawPluginApi;
  if (!entry.register) throw new Error("entry has no register()");
  entry.register(api);
  return { api, tools, capabilities, hooks, logs };
}

export const ownerCtx = (extra: Partial<OpenClawPluginToolContext> = {}): OpenClawPluginToolContext => ({
  agentId: "main",
  sessionKey: "agent:main:main",
  memoryAudience: { kind: "owner-private", agentId: "main" },
  assertInvocationCurrent: () => undefined,
  assertMemoryAudienceCurrent: () => undefined,
  ...extra,
});

export const groupCtx = (extra: Partial<OpenClawPluginToolContext> = {}): OpenClawPluginToolContext => ({
  agentId: "main",
  sessionKey: "agent:main:telegram:group:42",
  senderIsOwner: true,
  memoryAudience: { kind: "conversation", agentId: "main", sessionKey: "agent:main:telegram:group:42", sessionId: "s-1" },
  ...extra,
});

export function tool(rec: Recorded, name: string, ctx: OpenClawPluginToolContext): AnyAgentTool | null {
  const factory = rec.tools.get(name);
  if (!factory) throw new Error(`tool ${name} not registered`);
  const made = factory(ctx);
  return (Array.isArray(made) ? made[0] : made) ?? null;
}

export interface Call {
  method: string;
  path: string;
  url: URL;
  headers: Record<string, string>;
  body: any;
}

type Reply = { status?: number; json?: unknown; hang?: boolean };
type Route = (call: Call) => Reply | undefined;

/** A `fetch` that answers Membase routes from a script and records every call. */
export function scriptedFetch(route: Route) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const headers = Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>));
    const call: Call = {
      method: init.method ?? "GET",
      path: url.pathname,
      url,
      headers,
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(call);
    const reply = route(call) ?? { status: 404, json: { error: { message: "no route" } } };
    if (reply.hang) {
      return new Promise<Response>((_, reject) => {
        init.signal?.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      });
    }
    return new Response(JSON.stringify(reply.json ?? {}), {
      status: reply.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  });
  return { fetch: fn as unknown as typeof fetch, calls };
}

export const SEARCH_OK = {
  query: "q",
  results: [
    { content: "We settled on Postgres for the ledger.", container: "mv-dec", container_name: "Project decisions", score: 0.82 },
    { content: "Acme wants SSO before the pilot.", container: "mv-crm", container_name: "Customers", score: 0.4 },
  ],
  containers: [
    { id: "mv-dec", name: "Project decisions" },
    { id: "mv-crm", name: "Customers" },
  ],
};

export const FORBIDDEN = {
  status: 403,
  json: { error: { code: "unauthorized", message: "unauthorized" } },
};
