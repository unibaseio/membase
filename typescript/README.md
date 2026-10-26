<p align="center">
  <img src="https://raw.githubusercontent.com/unibaseio/membase/main/assets/membase-logo.png" width="72" alt="Membase">
</p>

<h1 align="center">@unibaseio/membase</h1>

<p align="center">
  <b>One memory. Every AI. Only yours.</b><br>
  The Membase SDK for TypeScript: the memory layer for AI agents.
</p>

<p align="center">
  <a href="https://membase.com">Website</a> ·
  <a href="https://docs.membase.com">Docs</a> ·
  <a href="https://app.membase.com">Web app</a> ·
  <a href="https://github.com/unibaseio/membase">GitHub</a> ·
  <a href="https://discord.gg/nB9EfPGsSf">Discord</a>
</p>

Memory hosted in your Membase account, or local through the Python package.

```bash
npm install @unibaseio/membase
export MEMBASE_API_KEY="mbk_…"     # Connect › Developer keys in the Membase app
```

```ts
import { Membase } from "@unibaseio/membase";

const client = new Membase();

await client.add({ container: "mv-…", content: "Call notes with Acme: they want SSO before the pilot.", customId: "call-2026-09-24" });
const { results } = await client.search({ q: "what does Acme need before the pilot", limit: 5 });
console.log(results[0].content);

await client.profile({ q: "working hours" });        // who the user is (the key needs Profile access)
await client.containers.list();
await client.documents.list({ container: "mv-…" });
await client.memories.add({ content: "The user prefers dark mode.", static: true });
await client.documents.delete("srcitem_…", { confirm: true });   // Full access; confirm means the person agreed
```

Container ids come from `containers.list()`; document ids from `documents.list()` or the
`document_id` that `add()` returns.

Every method is one operation of the Membase agent protocol; access level, reach and confirmation
rules are enforced server-side. Errors are one class per HTTP status (`PermissionDeniedError`,
`RateLimitError`, …) and carry the API's `code` and `traceId`. 408, 409, 429 and 5xx answers,
connection errors and timeouts are retried twice with backoff, honouring `Retry-After`; the
default timeout is 90 s per attempt, because the first search after a quiet spell waits for the
user's memory to wake.

Node 18+ (global `fetch`). Zero dependencies.

## Local memory

Run the engine on your machine with the Python package (Python 3.12 or 3.13, with
`OPENAI_API_KEY` set for the server) and point the client at it — same methods:

```bash
pip install 'unibaseio-membase[local]'
membase --local serve            # http://127.0.0.1:8787/v1, store under ~/.membase
```

```ts
const local = new Membase({ apiKey: "local", baseUrl: "http://127.0.0.1:8787" });
```

If the server runs with `MEMBASE_LOCAL_TOKEN`, pass that token as `apiKey`.

`membase-ai` and `membase-sdk` on npm are earlier names of this package. The `membase` command
without Python is `@unibaseio/membase-cli`.
