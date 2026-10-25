/** Plugin config: `plugins.entries.membase.config` in the OpenClaw config. */

export const DEFAULT_RECALL_BUDGET_MS = 8_000;

/** The JSON Schema OpenClaw validates the config against; kept equal to `openclaw.plugin.json`. */
export const CONFIG_JSON_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    apiKey: {
      type: "string",
      description: "Membase developer key (mbk_...). Falls back to MEMBASE_API_KEY.",
    },
    baseUrl: {
      type: "string",
      description: "API base URL; defaults to MEMBASE_BASE_URL, then https://api.app.membase.ai.",
    },
    container: {
      type: "string",
      description: "Default container id for saves and automatic recall.",
    },
    recallBudgetMs: {
      type: "integer",
      minimum: 500,
      maximum: 60000,
      default: DEFAULT_RECALL_BUDGET_MS,
      description: "Time budget for automatic recall; on expiry the turn continues without memory.",
    },
    autoRecall: {
      type: "boolean",
      default: true,
      description: "Search Membase before each user turn and inject the passages as context.",
    },
    autoCapture: {
      type: "boolean",
      default: false,
      description: "Ask the agent to save durable facts with membase_save before compaction.",
    },
    allowSharedConversations: {
      type: "boolean",
      default: false,
      description: "Allow reads (never writes) in group chats and conversations with other senders.",
    },
  },
} as const;

export interface MembasePluginConfig {
  /** Undefined means the SDK reads `MEMBASE_API_KEY`. */
  apiKey?: string;
  baseUrl?: string;
  container?: string;
  recallBudgetMs: number;
  autoRecall: boolean;
  autoCapture: boolean;
  allowSharedConversations: boolean;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/** Reads the (already schema-validated, SecretRef-resolved) plugin config. */
export function resolveConfig(raw: Record<string, unknown> | undefined): MembasePluginConfig {
  const cfg = raw ?? {};
  const budget = cfg.recallBudgetMs;
  return {
    apiKey: str(cfg.apiKey),
    baseUrl: str(cfg.baseUrl),
    container: str(cfg.container),
    recallBudgetMs:
      typeof budget === "number" && Number.isFinite(budget) && budget > 0
        ? Math.floor(budget)
        : DEFAULT_RECALL_BUDGET_MS,
    autoRecall: bool(cfg.autoRecall, true),
    autoCapture: bool(cfg.autoCapture, false),
    allowSharedConversations: bool(cfg.allowSharedConversations, false),
  };
}
