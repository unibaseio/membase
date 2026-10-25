import { afterEach, describe, expect, it, vi } from "vitest";
import type { MembasePluginConfig } from "../src/config.js";
import { DEFAULT_BASE_URL, MembaseClients } from "../src/membase.js";
import { SEARCH_OK, scriptedFetch } from "./helpers.js";

const config = (extra: Partial<MembasePluginConfig> = {}): MembasePluginConfig => ({
  apiKey: "mbk_test_key_0123456789",
  recallBudgetMs: 8000,
  autoRecall: true,
  autoCapture: false,
  allowSharedConversations: false,
  ...extra,
});

async function origin(cfg: MembasePluginConfig): Promise<string> {
  const scripted = scriptedFetch(() => ({ json: SEARCH_OK }));
  await new MembaseClients(cfg, { fetch: scripted.fetch }).main().search({ q: "where" });
  return scripted.calls[0].url.origin;
}

describe("the Membase API address", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("is api.app.membase.com unless set, whatever the installed SDK release defaults to", async () => {
    vi.stubEnv("MEMBASE_BASE_URL", "");
    expect(DEFAULT_BASE_URL).toBe("https://api.app.membase.com");
    expect(await origin(config())).toBe("https://api.app.membase.com");
  });

  it("MEMBASE_BASE_URL overrides the default, and the plugin config overrides both", async () => {
    vi.stubEnv("MEMBASE_BASE_URL", "https://env.membase.test");
    expect(await origin(config())).toBe("https://env.membase.test");
    expect(await origin(config({ baseUrl: "https://config.membase.test" }))).toBe("https://config.membase.test");
  });
});
