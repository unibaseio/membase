# Membase for the Vercel AI SDK

`@unibaseio/membase-ai-sdk` gives an [AI SDK](https://ai-sdk.dev) agent your
[Membase](https://membase.com) account as its long-term memory, as three tools:

| Tool | What it does |
|---|---|
| `membase_search` | Searches your Memories; every passage names the Memory it came from. |
| `membase_save` | Saves a note, **only when the user asks the agent to remember something**, in their words. |
| `membase_profile` | Reads who the user is (their profile facts, newest first), when the key has Profile access. |

There is no delete or forget tool. Removing memory stays in the Membase app.

## Install

Works with AI SDK 6 and 7, with any model provider.

```bash
npm install @unibaseio/membase-ai-sdk ai zod
export MEMBASE_API_KEY="mbk_…"     # Connect › Developer keys in the Membase app
```

A **Read** key can search and, with Profile ticked, read the profile. Saving needs a
**Read & write** key whose writes have one place to go: a key that reaches a single Memory saves there.

## Use

```ts
import { ToolLoopAgent } from "ai";
import { openai } from "@ai-sdk/openai";
import { membaseTools } from "@unibaseio/membase-ai-sdk";

const agent = new ToolLoopAgent({
  model: openai("gpt-5"),
  tools: membaseTools(), // MEMBASE_API_KEY from the environment
  instructions: "You are a helpful assistant with long-term memory.",
});

const result = await agent.generate({ prompt: "Remember that my favorite editor is Neovim" });
```

The same tools work with `generateText` and `streamText`: `tools: membaseTools()`.

Leave a tool out by destructuring it away:

```ts
const { membase_save, ...readOnly } = membaseTools();
```

## One key is one person

A Membase key belongs to one person's account. In a multi-user app, call `membaseTools` inside the
request handler with that person's key, so each request reads and writes their memory only:

```ts
tools: membaseTools({ apiKey: keyFor(userId) });
```

`membaseTools` also takes a ready client (`{ client: new Membase(...) }` from `@unibaseio/membase`),
and the client's own options (`baseUrl`, `timeoutMs`, `maxRetries`, `fetch`).

## Errors and latency

A failed call throws the SDK's error (`PermissionDeniedError`, `RateLimitError`, …). The AI SDK
hands it to the model as the tool's error, so the model can tell the user. The first search after a
quiet spell can take up to a minute while the memory wakes; the client waits up to 90 s and retries
twice.

## Local memory

Point the tools at a Membase engine on your machine (`pip install 'unibaseio-membase[local]'`, then
`membase --local serve`):

```ts
membaseTools({ apiKey: "local", baseUrl: "http://127.0.0.1:8787" });
```

MIT licensed.
