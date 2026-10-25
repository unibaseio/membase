# Membase for OpenClaw

`@unibaseio/membase` (on ClawHub) makes your [Membase](https://membase.com) account the memory of an
[OpenClaw](https://openclaw.ai) agent. It owns OpenClaw's memory slot, recalls what you told
other AIs before each turn, and gives the agent three tools:

| Tool | What it does |
|---|---|
| `membase_search` | Searches your Memories; every passage names the Memory it came from. |
| `membase_save` | Saves a note or a standing fact, **only when you ask the agent to remember something**, in your words. |
| `membase_profile` | Reads who you are (standing and recently changed facts), when the key has Profile access. |

There is no delete or forget tool. Removing memory stays in the Membase app.

## One key is one person

A Membase key belongs to one person's account. OpenClaw identifies agents and sessions, not
end users, so **this plugin is for a personal agent: your OpenClaw and your Membase.** Do not
share one gateway and one key among several people. Each person runs their own key.

For the same reason the plugin uses your memory only in **your own direct conversations**. In a
group chat, or a conversation with someone else, the Membase tools are not offered and nothing
is recalled. `allowSharedConversations: true` lets those conversations *read* your memory. Saving
from them is never allowed.

## Install

Requires OpenClaw 2026.10.1 or newer (Node 24.16+ or 26.1+).

```bash
openclaw plugins install clawhub:@unibaseio/membase
```

Keep the `clawhub:` prefix. Without it OpenClaw looks on npm, where `@unibaseio/membase` is the
Membase TypeScript SDK, not this plugin.

OpenClaw asks you to accept the plugin's capabilities on install. In a script, pass
`--accept-capabilities`. Installing it selects it for the memory slot.

Get a developer key in the Membase app under **Connect › Developer keys**. A **Read** key is
enough to search. Choose **Read & write** if the agent should save. Tick **Profile** if it should
read your profile. Then configure the plugin and select it for the memory slot:

```json5
// ~/.openclaw/openclaw.json
{
  plugins: {
    slots: { memory: "membase" },
    entries: {
      membase: {
        enabled: true,
        // Needed for automatic recall: lets the plugin read the user's message before a turn.
        hooks: { allowConversationAccess: true },
        config: {
          apiKey: "${MEMBASE_API_KEY}", // or a SecretRef; plain text works but is not advised
          // container: "mv-...",      // default Memory for saves and automatic recall
        },
      },
    },
  },
}
```

Restart the Gateway (or `openclaw plugins reload membase`) and check it:

```bash
openclaw plugins inspect membase --runtime
openclaw memory status        # provider: membase, health: ready
```

If `apiKey` is not set, the plugin uses the `MEMBASE_API_KEY` environment variable.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `apiKey` | `MEMBASE_API_KEY` | Membase developer key (`mbk_…`). Declared as a secret input, so SecretRefs work. |
| `baseUrl` | `https://api.app.membase.com` | Another server, e.g. a local engine from `membase --local serve`. `MEMBASE_BASE_URL` also works. |
| `container` | none (all Memories in reach) | Container id that `membase_save` writes to when the model names none, and the only one automatic recall searches. |
| `recallBudgetMs` | `8000` | How long automatic recall may wait. After that the turn continues without memory. |
| `autoRecall` | `true` | Search before each turn you send and add the passages as context. Needs `hooks.allowConversationAccess: true`. |
| `autoCapture` | `false` | Before OpenClaw compacts a long session, ask the agent to save durable facts with `membase_save`. |
| `allowSharedConversations` | `false` | Let group chats and other senders read your memory. They can never save. |

## How it behaves

- **Cold start.** A search runs inside your account's memory, and the first one after a quiet
  spell takes longer while it wakes. Automatic recall therefore makes one attempt and
  gives up after `recallBudgetMs` (8 s). The turn then continues with no memory and a warning in
  the log. The request still wakes your memory, so the next turn is fast. Tool calls use the
  SDK's normal timeout (90 s with retries).
- **Automatic recall.** Recall runs only on turns you send, never on heartbeats or cron runs. It
  runs only when the turn's tool policy allows `membase_search`. It adds at most 5 passages, each
  tagged with its Memory, as context that is labelled as background data and not instructions.
- **Saving is deliberate.** The model calls `membase_save` when you ask it to remember something.
  `autoCapture` only adds OpenClaw's pre-compaction flush, which asks the model to do the same,
  and is off by default. A saved note is learned on your memory's next run, so it may not show up
  in a search right away.
- **The memory slot.** The plugin registers OpenClaw's provider-neutral memory runtime. Gateway
  `memory.search` (version 2), `memory.get` and `memory.status`, and `openclaw memory status`
  read from Membase. The plugin names `membase_search` as its recall tool, so Active Memory's deep
  recall can use it. `openclaw memory search` reads
  only Memory Core's local index, so it does not search Membase; use the agent or the Gateway
  method instead. Memory Core can keep running as OpenClaw's local consolidation sidecar.
- **Errors.** A `403` means the key's owner narrowed or revoked it, or (for a save) that the key is
  Read only. The tool says so and does not retry. A Memory that could not answer is reported as
  such, not as "no memory".
- **Gateway `/tools/invoke`.** OpenClaw does not resolve a conversation audience for direct tool
  invocations, so the plugin falls back to OpenClaw's trusted owner flag: the Gateway's
  shared-secret operator can call the tools, and other callers cannot.

## Develop

```bash
npm install
npm test              # builds, then runs the vitest suites (offline, mocked fetch)
OPENCLAW_NODE=/path/to/node-24.16 npm test   # real-CLI loader test when your own Node is older (npm i node@24.16.0 gives one)
MEMBASE_API_KEY=mbk_... node examples/live-smoke.ts   # live check against your account
npx clawhub package validate .
```

The unit tests register the plugin through its OpenClaw entry and the plugin API types, and point
the Membase SDK at a scripted `fetch`. `test/loader.test.ts` runs the real `openclaw` CLI. That
CLI loads the built plugin, selects it for the memory slot, and checks `memory status` against a
local stand-in for the API. The test is skipped when no Node ≥ 24.16 is available.

## Licence

MIT
