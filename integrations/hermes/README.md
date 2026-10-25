# Membase memory for Hermes Agent

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider backed by
[Membase](https://membase.com), the hosted memory API. With it selected as `memory.provider`,
Hermes:

- **recalls before each turn**: a search of the user's Membase Memories with a short time budget
  (8 s by default). The passages are injected as context, each tagged with the Memory it came
  from (`- [Project decisions] We settled on Postgres for the ledger.`). If the search is slow or
  fails, the turn goes on without memory;
- **gets three tools**: `membase_search`, `membase_save` and `membase_profile`;
- **saves only when asked**: the model calls `membase_save` when the user asks it to remember
  something. Capturing the user's words from every turn is a setting you turn on yourself
  (`capture_turns`, off by default).

Only one external memory provider is active at a time in Hermes. Hermes' own MEMORY.md / USER.md
keep working next to it, and what its built-in memory tool records about the user (USER.md) is
also saved to their Membase profile as a standing fact. The agent's own notes (MEMORY.md) and
removals are not.

## Install

```bash
hermes plugins install unibaseio-membase                     # once listed in the Hermes plugin catalog
# before that, straight from the repository:
hermes plugins install unibaseio/membase/integrations/hermes

hermes memory setup unibaseio-membase                        # paste the key, pick the options below
```

`hermes plugins install` installs the Python dependency (`unibaseio-membase`) into Hermes'
environment and can set `memory.provider: unibaseio-membase` for you. Start a new session
afterwards.
`hermes memory status` shows the settings in use (no network call, never the key).

Get a key in the Membase app at **Connect › Developer keys**. Choose **Read & write** if the
agent should be able to save. A **Read** key can search and read the profile but cannot save. Tick
**profile** on the key if you want `membase_profile` to work.

## Configuration

The key lives in the profile's `$HERMES_HOME/.env`. `hermes memory setup` writes it there, and
the plugin reads it through Hermes' per-profile secret scope, never from a file itself.

| Setting | Where | Default | Meaning |
|---|---|---|---|
| `MEMBASE_API_KEY` | `.env` (secret) | required | The developer key: one person's account |
| `base_url` / `MEMBASE_BASE_URL` | `unibaseio-membase.json` / `.env` | `https://api.app.membase.com` | API endpoint |
| `container` / `MEMBASE_CONTAINER` | `unibaseio-membase.json` / `.env` | every Memory in reach | Default Memory (name or id) for recall, saves and captures |
| `recall_budget_seconds` | `unibaseio-membase.json` | `8` | How long the per-turn recall may take (0.5–8) |
| `capture_turns` | `unibaseio-membase.json` | `false` | Save the user's own words from every completed turn as a note |
| `auto_recall` | `unibaseio-membase.json` | `true` | Search before each turn |
| `recall_limit` | `unibaseio-membase.json` | `5` | Passages injected per turn |
| `recall_max_chars` | `unibaseio-membase.json` | `4000` | Upper bound on the injected block |

The settings file is `$HERMES_HOME/unibaseio-membase.json` (mode 0600); it never holds the key.
When a setting appears in both places, the value in the file wins.

## How it behaves

**Recall (prefetch).** Hermes calls the provider before each non-trivial turn. Greetings,
"thanks" and slash commands are skipped. The provider sends the user's message (up to 2,000
characters, after Hermes' secret redaction) to `POST /v1/search` and gets up to `recall_limit`
passages back. The call uses its own client with the recall budget as its timeout and no
retries. Hermes also stops waiting for any external provider after 8 s, which is why a larger
budget has no effect. If a Memory reports an error (no working model, dormant), the block says
it was not searched. A missing passage from that Memory is not proof that nothing is remembered.
A `403` (key narrowed or revoked) stops automatic recall for the rest of the session instead of
retrying every turn.

**Tools.** Explicit calls use the SDK's default timeout (90 s), so they can wait out a cold start.

| Tool | Does |
|---|---|
| `membase_search(query, container?, limit?)` | Passages, each naming its Memory. Memories that could not answer are listed under `unavailable`. |
| `membase_save(content, container?, static?)` | Saves a note to a Memory. It is **queued**: the Memory learns it on its next run and it is searchable after that. `static: true` records a standing fact about the user in their profile instead. |
| `membase_profile(query?)` | The user's standing (`static`) and recent (`dynamic`) facts. Works only when the key's owner ticked the profile. |

`container` takes a Memory's name (case-insensitive) or id. When the key reaches several
Memories and no default is configured, `membase_save` answers with the list to choose from.
There is no delete or forget tool. Deleting stays with the owner, in the Membase app.

**Capture (opt-in).** With `capture_turns` on, the user's own words from each completed turn are
saved as one note, as they wrote them, to the default Memory. The assistant's reply is never sent
and no role labels are added, so an AI answer is never learned as a fact about the user. Captures never run from `cron`, `subagent` or
`flush` agents, or for turns written by bots. With several Memories in reach, set `container`.
Otherwise the capture is refused, and the refusal is logged once.

**Errors** come back to the model as one plain sentence:

| Error | Message |
|---|---|
| `403` | "this Membase key was narrowed or revoked, or the action is above its access level; do not retry with it" |
| timeout | "the memory may be waking up, try again shortly" |
| `422` | "no working model or no free turns" |

The key never appears in an error or a log line.

## Limits

- **Cold start.** A search is a turn inside the user's own Membase account. The first one after a
  quiet spell takes longer while that memory wakes. The automatic recall gives up
  after the budget, so the first turn or two of a session can run with no recall. Later turns
  find the memory awake. An explicit `membase_search` waits up to 90 s.
- **One key = one person.** A Membase key belongs to one account, and Hermes keeps one key per
  profile. This suits a **personal agent**: the profile's owner talking to their own Hermes. If
  the same profile answers several people on a messaging gateway (a group chat, a shared bot),
  every one of them reaches the owner's memory, as they do the built-in MEMORY.md. Give each
  person their own profile and key instead. For code that registers the provider itself,
  `MembaseMemoryProvider(client_for=lambda user_id: Membase(api_key=key_of(user_id)))` picks
  each turn's client from the gateway user id. A turn without a user id then gets no memory at
  all, never someone else's.
- **Saves are not instant.** A note is learned on the Memory's next run. A `static` fact is
  recorded at once.
- **Plan usage.** Searches and learning runs execute in the user's Membase account and count
  against its plan.
- **Hosted only.** The catalog build talks to the hosted API. Membase's local mode
  (`Membase(local=...)`) is not offered here: it would run a model with credentials read from
  the process environment, which Hermes' credential rules do not allow for a plugin.

## Network and data disclosure

The only network peer is the Membase API (`https://api.app.membase.com`, or `MEMBASE_BASE_URL`).
The plugin sends:

- each non-trivial user message, as a recall query;
- `membase_search`, `membase_save` and `membase_profile` calls;
- the user's own words from each completed turn, only when `capture_turns` is on (never the
  assistant's reply).

The only local state is `$HERMES_HOME/unibaseio-membase.json`. The plugin starts no background process,
runs no shell command, sends no telemetry and has no self-update code.

## Development

```bash
cd hermes
git clone --depth 1 https://github.com/NousResearch/hermes-agent .cache/hermes-agent
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e .cache/hermes-agent "unibaseio-membase>=0.3.0,<0.4" pytest
.venv/bin/python -m pytest -q                                   # offline
.venv/bin/hermes plugins validate . --install-deps              # the catalog CI gate (run on a clean checkout)
MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py   # real API, read-only (--write saves one note)
```

The tests install this folder as a user plugin in a temporary `HERMES_HOME`. Hermes' own loader
loads it, and the tests drive it through Hermes' `MemoryManager`: prefetch, tool routing,
`sync_all`, the secret scope and plugin-host isolation. Every HTTP request is answered by an
`httpx.MockTransport`, so the real SDK runs without a network.

## Licence

MIT
