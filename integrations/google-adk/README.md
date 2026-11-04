# Membase for Google ADK

`membase-google-adk` gives a [Google ADK](https://google.github.io/adk-docs/) agent your
[Membase](https://membase.com) account as its long-term memory, in two ways that can be used
together:

| Piece | ADK type | What it does |
|---|---|---|
| `MembaseMemoryService` | `BaseMemoryService` | What ADK's `preload_memory` and `load_memory` tools search. Recall is time-boxed and fails open. |
| `membase_tools()` | `FunctionTool`s | `membase_search`, `membase_save` and `membase_profile` for deliberate calls. |

Every passage names the Memory it came from, e.g. `[Project decisions] We chose Postgres for
the ledger.`, so the agent can cite it. There is no delete tool.

## Install

```bash
pip install membase-google-adk
export MEMBASE_API_KEY="mbk_…"     # Connect › Developer keys in the Membase app
```

A **Read** key can search and, with Profile ticked, read the profile. Saving needs a
**Read & write** key whose writes have one place to go: a key that reaches a single Memory
saves there.

## Tools

```python
from google.adk.agents import Agent
from membase_google_adk import membase_tools

root_agent = Agent(
    name="assistant",
    model="gemini-3.5-flash",
    instruction="You are a helpful assistant with long-term memory.",
    tools=membase_tools(),  # MEMBASE_API_KEY from the environment
)
```

```bash
adk run path/to/your_agent
```

| Tool | What it does |
|---|---|
| `membase_search(query, limit?)` | Searches every Memory the key reaches; each passage is cited as `[Memory name] …`, and a Memory that could not answer is said so. |
| `membase_save(content)` | Saves a note in the user's words, only when the user asks. |
| `membase_profile()` | Reads the user's profile facts, newest first. Needs Profile access on the key. |

`membase_tools(tools=("search", "profile"))` leaves saving out, e.g. for a Read key.

## Memory service

```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import preload_memory
from membase_google_adk import MembaseMemoryService

agent = Agent(name="assistant", model="gemini-3.5-flash", tools=[preload_memory])
runner = Runner(
    app_name="my_app",
    agent=agent,
    session_service=InMemorySessionService(),
    memory_service=MembaseMemoryService(),
)
```

`preload_memory` searches Membase with the user's message before each turn. That search gets
8 seconds by default (`recall_timeout`), because the first search after a quiet spell can take up
to a minute while the memory wakes. When it runs out, the turn goes on without memory and the
search keeps running, so the memory is awake for the next turn.

ADK can also write sessions to memory (`add_session_to_memory`, `add_events_to_memory`).
That is off by default: Membase saves what the user asked to keep, through `membase_save`.
`MembaseMemoryService(capture=True)` turns it on and keeps the user's own words only
(`capture_roles=("user",)`). `add_session_to_memory` saves the whole session each time, so call
it once when the session ends.

## One key is one person

A Membase key belongs to one person's account. In a multi-user app, pass `client_for`: it
maps the user id ADK passes to that user's own client.

```python
from membase import Membase


def client_for(user_id: str) -> Membase:
    return Membase(api_key=keys.get(user_id))  # that user's key, stored by your app


memory_service = MembaseMemoryService(client_for=client_for)
tools = membase_tools(client_for=client_for)
```

Both also take a ready `client`, or `api_key`, `base_url` and `local`.

## Local memory

```bash
pip install 'membase-google-adk[local]'
```

```python
membase_tools(local="~/.membase")
MembaseMemoryService(local="~/.membase")
```

## Requirements

Python 3.10+, `google-adk` and `unibaseio-membase` 0.4.0 or newer.

MIT licensed.
