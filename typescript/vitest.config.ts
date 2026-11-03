import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
    exclude: process.env.MEMBASE_TEST_BASE_URL ? [] : ["src/e2e.test.ts", "node_modules/**"],
    // A live server's first call can wait while the account's memory wakes (the client's own
    // timeout is 90 s); the unit tests never reach the network.
    testTimeout: process.env.MEMBASE_TEST_BASE_URL ? 120_000 : 5_000,
  },
});
