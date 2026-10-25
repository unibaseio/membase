# membase-strands

[Membase](https://membase.com) memory for [Strands Agents](https://strandsagents.com) (Python).

`MembaseMemoryStore` implements Strands' `MemoryStore` protocol, so it plugs into
`strands.memory.MemoryManager` like the other memory stores:

- **Recall before each user turn.** The manager's context injection calls `search`, and the
  passages it returns are folded into that model call. Each one names the Membase memory
  (container) it came from: `[Project decisions] We chose Postgres for the ledger.`
- **Time-boxed, fail-open recall.** A Membase search runs inside the account's own memory
  container, and the first one after a quiet spell takes longer while it wakes. The
  automatic recall gets 8 seconds by default (`recall_timeout`). If it runs out, the manager
  logs it and the turn goes ahead without memory. The request keeps running in the
  background, so the memory is awake for the next turn. While it is still running, later
  recalls skip immediately instead of piling up.
- **Deliberate writes.** `get_tools()` gives the model `membase_search`, `membase_save` and
  `membase_profile`. Saving is a tool the model calls when the user asked it to remember
  something. Saving every turn automatically (`capture=True`) is opt-in.

```bash
pip install membase-strands
```

## Quick start

Create a developer key in the Membase app under **Connect › Developer keys**. Use
*Read & write* if the agent should save, and tick the profile if it should read it.

```python
from strands import Agent
from strands.memory import MemoryManager
from membase_strands import MembaseMemoryStore

store = MembaseMemoryStore(api_key="mbk_...")       # or set MEMBASE_API_KEY

agent = Agent(
    memory_manager=MemoryManager(
        stores=[store],
        search_tool_config=False,   # the model gets membase_search instead (see below)
    ),
)

agent("Which database did we pick for the ledger?")
# Before the model call, the manager searches Membase and injects:
# <memory>
# <entry source="membase">[Project decisions] We chose Postgres for the ledger.</entry>
# </memory>

agent("Remember that we moved the launch to May.")
# The model calls membase_save(content="We moved the launch to May.", container="Project decisions")
```

### Why `search_tool_config=False`

The manager's built-in `search_memory` tool calls the same time-boxed `search` as injection.
On a cold start it would give up after 8 seconds and show the model an empty list. The
store's own `membase_search` tool waits for the SDK's full timeout (90 s), so a cold start
gets an answer. It also lets the model pick one Memory by name, and it says when a Memory
could not answer. You can keep both tools if you prefer.

## Tools

| Tool | What it does |
|---|---|
| `membase_search(query, container?, limit?)` | Searches every Memory in reach, or one Memory by name or id. Each passage is cited as `[Memory name] …`. A Memory that could not answer is listed as such, so it is not mistaken for "no memory". |
| `membase_save(content, container?, standing_fact?)` | Saves a note in the user's words. `standing_fact=true` records a lasting fact in the user's profile. If the key reaches several Memories and none is named, the tool returns their names so the model can ask which one. |
| `membase_profile(topic?)` | Reads the user's standing facts and recent changes. This needs the owner to have ticked the profile for the key. |

Pick the tools with `tools=("search", "profile")`. For example, drop `"save"` for a Read key.
There is no delete or forget tool, and the store never confirms a destructive operation for
the user.

## Options

```python
MembaseMemoryStore(
    client=None,              # a ready membase.Membase client, or:
    api_key=None,             #   MEMBASE_API_KEY
    base_url=None,            #   MEMBASE_BASE_URL, default https://api.app.membase.com
    local=None,               #   a directory for local mode (pip install 'membase-strands[local]')
    container=None,           # one Memory (name or id) to search and write to; default: all in reach
    name="membase",           # store name in the manager
    max_search_results=None,  # passages per search when the caller passes none (default 5)
    recall_timeout=8.0,       # budget for the automatic recall; None = SDK timeout
    capture=False,            # let the manager write (add_memory tool, extraction)
    extraction=None,          # the manager's extraction config; needs capture=True
    capture_roles=("user",),  # which messages captured turns keep
    tools=("search", "save", "profile"),
)
```

### Automatic capture (opt-in)

By default the store is not `writable`, so the manager never writes to Membase. The only way
to save is the model calling `membase_save` when the user asks. Membase learns from what it
is given, and a transcript of every turn would teach it the assistant's own answers as facts.

To capture anyway:

```python
store = MembaseMemoryStore(container="Conversations", capture=True, extraction=True)
agent = Agent(memory_manager=MemoryManager(stores=[store], search_tool_config=False))
```

With `extraction=True` the manager hands batches of turns to `add_messages`, by default every
5 turns and at the end of each synchronous `agent(...)` call. The store saves the user's
messages from the batch as one note in `container`, and Membase learns from it in the
background. `capture_roles=("user", "assistant")` keeps both sides. `capture=True` also lets
you enable the manager's own `add_memory` tool (`MemoryManager(..., add_tool_config=True)`).
A retried batch can be saved twice.

## One store per person

Membase has no `user_id`. One key, or one local directory, is one person's account. The
`MemoryStore` protocol does not pass a user or session to `search` either. So a store built
from a single key suits **a personal agent**. In a multi-user app, build the store and the
agent per user, from that user's own key or directory, and never route one user's turn
through another user's key:

```python
def agent_for(user) -> Agent:
    store = MembaseMemoryStore(api_key=keys.get(user.id))           # that user's key
    # or: MembaseMemoryStore(local=f"/data/membase/{user.id}")      # one directory per user
    return Agent(memory_manager=MemoryManager(stores=[store], search_tool_config=False))
```

## Errors

- **403** means the key's owner narrowed or revoked it. Automatic recall raises
  `MembaseAccessError`; the manager logs it and continues without memory. The tools return
  "this Membase key was narrowed or revoked…" so the model can tell the user. Nothing retries
  with that key.
- **A Memory that could not answer** (no working model, dormant) is reported. Recall returns
  an entry that says so, and `membase_search` lists it under "Not searched".
- **A recall that runs out of time** raises `MembaseRecallTimeout` (a `TimeoutError`); the
  manager logs it and continues. The tools use the SDK timeout and, if that runs out too,
  return a "try again in a minute" sentence.
- Captured writes (`add`, `add_messages`) raise on failure so the manager can report or retry
  them. The tools turn errors into sentences.

The key is never logged.

## Compatibility

Python 3.10+, `strands-agents>=1.45.0` (the first release with `strands.memory`) and
`unibaseio-membase>=0.3.2`. The offline tests pass on strands-agents 1.45.0, 1.50.0, 1.55.0
and 1.61.1.

## Development

```bash
uv venv && uv pip install -e . pytest pytest-asyncio ruff
.venv/bin/python -m pytest -q                       # offline: real Strands Agent, Membase SDK on httpx.MockTransport
MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py
uv build
```

MIT licence.
