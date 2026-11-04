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

await client.add({ content: "Call notes with Acme: they want SSO before the pilot.", customId: "call-2026-09-24" });
const { items, partial } = await client.search({ q: "what does Acme need before the pilot", limit: 5 });
console.log(items[0].content, items[0].memory_name);

await client.memories.add({ content: "The user prefers dark mode." });   // a fact; the receipt is a doc_ note
const { items: facts } = await client.memories.list({ limit: 20 });   // mem_ ids
await client.documents.list({ kind: "file" });
await client.profileFacts.list();                    // who the user is (the key needs Your profile ticked)
await client.capabilities();                          // what this key may do, the Memories it reaches, the rules
await client.memories.forget(facts[0].id, { confirm: true });   // Full access; confirm means the person agreed
```

No call names a Memory: reads cover every Memory the key reaches, and writes land in its write
target. Ids are typed (`mem_…` a fact, `doc_…` a document, `med_…` a photo or video); lists are
`{ items, has_more, partial }`; `partial` names the Memories that could not answer.

Every method is one operation of the Membase memory API; access level, reach and confirmation
rules are enforced server-side. Errors are one class per HTTP status (`PermissionDeniedError`,
`RateLimitError`, …) and carry the API's `code`, `details` and `traceId`. A removal without
`confirm: true` changes nothing and throws `ConfirmationRequiredError` (428); a write from a key
that reaches several Memories and has no write target throws `WriteTargetRequiredError` (422). 408, 409, 429 and 5xx answers,
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

A store with several Memories writes where `MEMBASE_WRITE_TARGET` (set for the server) names.

If the server runs with `MEMBASE_LOCAL_TOKEN`, pass that token as `apiKey`.

`membase-ai` and `membase-sdk` on npm are earlier names of this package. The `membase` command
without Python is `@unibaseio/membase-cli`.
