# Membase

Long-term memory for Dify agents and workflows, from a [Membase](https://membase.com) account.
The plugin adds three tools:

| Tool | What it does |
|---|---|
| **Search memory** | Finds passages in the memory that answer a question, most relevant first. Each passage names the container (a Membase *Memory*) it came from. |
| **Save memory** | Saves one fact, in the user's words, to a container, or to the user's profile as a standing fact. Only when the user asks for it. |
| **Get user profile** | Reads the standing facts about the user and the facts that changed most recently. |

There is no delete or forget tool. Deleting memory is done by the account owner in the
Membase app.

Source repository: https://github.com/unibaseio/membase/tree/main/integrations/dify

## Setup

1. **Create an API key.** In the Membase app (https://app.membase.com) open
   **Connect › Developer keys** and create a key. Choose **Read & write** if the app should
   save memories, **Read** if it should only search. Choose which Memories the key may reach,
   and tick the profile if the app should read it.
2. **Install the plugin** from the Dify Marketplace (**Plugins › Marketplace**, search for
   *Membase*).
3. **Authorize it.** Open **Plugins › Membase › Authorize** (or **Tools › Membase**) and enter:

   | Field | Required | Value |
   |---|---|---|
   | Membase API key | yes | the key from step 1 (`mbk_…`) |
   | Base URL | no | leave empty for the Membase cloud (`https://api.app.membase.com`) |

   Saving checks the key against the Membase API by listing the containers it reaches. A key
   that is mistyped, expired or revoked is rejected with a `403` message at this step.

## Usage

**In an agent or chatbot (Agent mode).** Add *Search memory*, *Get user profile* and, if the key
can write, *Save memory* to the agent's tools. The tool descriptions tell the model when to use
each: get the profile once near the start, search before answering anything that depends on what
the user said before, and save only when the user asks to remember something. A useful line in
the instructions: *"Cite the container name when you use a memory."*

**In a workflow or chatflow.** A typical memory loop:

1. A **Search memory** tool node with the user's message as `query`.
2. An LLM node that receives the search node's `text` output as context.
3. Optionally, a **Save memory** node after an explicit "remember this" branch.

Tool inputs:

- *Search memory*: `query` (required), `container` (optional; a container name or id, empty
  searches every container the key reaches), `limit` (optional, 1-50 passages in all).
- *Save memory*: `content` (required), `container` (needed when the key reaches more than one
  container; if it is missing the error lists the names), `static` (true saves a standing fact
  to the user's profile instead).
- *Get user profile*: `query` (optional topic; adds passages relevant to it).

Each tool returns a readable `text` output and a `json` output with the full answer, including
`container_name` on every passage.

A saved note is accepted at once (`status: queued`) and Membase learns it in the background;
search finds it after that. A profile fact (`static`) is recorded immediately.

## One key is one person's memory

Dify stores tool credentials per workspace, so every end user of every app in the workspace
that uses this plugin reads, and can write, the **same** Membase account: the one that owns the
key. That suits a personal assistant or an internal tool for one person. It does not suit an
app with many end users, who would all see each other's memory.

For a multi-user app, give each end user their own Membase key and use Dify's built-in MCP tool
instead of this plugin, passing the key per request:

1. In Dify, open **Tools › MCP** and add an MCP server (HTTP).
2. Server URL: `https://api.app.membase.com/mcp-http`.
3. Under the advanced options, add the header
   `Authorization` = `Bearer {{request.headers.X-Membase-Key}}`.
4. Your backend calls the Dify app API with each user's own key in an `X-Membase-Key` header.
   Dify replaces the placeholder with that header at call time, so each run reaches only that
   user's memory.

The placeholder is filled only for runs started by an HTTP request to the app; the console's
debug preview has no such header. Never put one user's key in front of another user's request.

## Connection requirements

- The Dify plugin runtime needs outbound HTTPS access to `api.app.membase.com` (port 443). For a
  self-hosted Dify behind a proxy or firewall, allow that host. The Base URL field accepts only
  HTTPS hosts on `membase.com`, `membase.ai` or `membase.io`.
- The first search after a quiet spell can take up to a minute while the account's memory
  wakes; later calls are faster. Each request waits up to 90 seconds and is retried once.
- The account needs a working model under **AI Setup** in the Membase app. Without one, searches
  report the container as unavailable, and saved notes wait unread.

## Errors

| Message | Meaning |
|---|---|
| `Membase refused this request (403 ...)` | The key is wrong, revoked or narrowed, is a Read key used to save, or the container is outside its reach. Do not retry with that key; check it under Connect › Developer keys. |
| `Membase refused to read the profile (403 ...)` | The key's owner did not grant the profile to this key. |
| `Could not search container '...'` | That container could not answer this time (for example no working model). This is not the same as "no memory". |
| `Membase did not answer in time` | A cold start took too long; try again. |
| `422 capability_unavailable` / `dormant` | The account has no working model, or its free turns are spent. |

## Privacy

The plugin sends the tool inputs (search text, the fact to save, an optional topic) and the API
key to the Membase API, and stores nothing itself. See [PRIVACY.md](PRIVACY.md) for what is
sent, what Membase stores, and how to delete it.

## Support

support@unibase.com · https://github.com/unibaseio/membase/issues
