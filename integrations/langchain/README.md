# langchain-membase

[Membase](https://membase.com) long-term memory for LangChain and LangGraph (Python).

Membase keeps a person's memory in their own account: named **Memories** (containers) that
learn from their notes, files and conversations, plus a **profile** of standing facts. This
package gives a LangChain app three ways to use it:

| Piece | LangChain type | What it does |
|---|---|---|
| `MembaseRetriever` | `BaseRetriever` | a Membase search as `Document`s, each citing its Memory in `metadata["container_name"]` |
| `MembaseToolkit` (`membase_tools()`): `MembaseSearchTool`, `MembaseSaveTool`, `MembaseProfileTool` | `BaseToolkit` / `BaseTool` | agent tools: search, save (only when the user asks), read the profile |
| `MembaseRecallMiddleware` | LangChain v1 `AgentMiddleware` | time-boxed recall added to the system message before the model call of a `create_agent` agent |

There is deliberately **no delete or forget tool**, and the package never confirms a
destructive action. It also does not implement LangGraph's `BaseStore`: Membase is a memory
that learns, not a key-value store (there is no get-by-key), so it is offered as retrieval and
tools instead.

## Install

```bash
pip install langchain-membase             # retriever + tools (needs langchain-core)
pip install 'langchain-membase[agents]'   # + the create_agent middleware (needs langchain>=1.1)
```

Python 3.10+. Get a key in the Membase app under **Connect › Developer keys** and set it:

```bash
export MEMBASE_API_KEY=mbk_...
```

A **Read** key can search and read the profile (if its owner ticked it); saving needs a
**Read & write** key. One key is one person's account.

## Retriever

```python
from langchain_membase import MembaseRetriever

retriever = MembaseRetriever(k=5)                     # MEMBASE_API_KEY from the environment
docs = retriever.invoke("what did we decide about the ledger")
for d in docs:
    print(d.metadata["container_name"], "·", d.page_content)
```

* `k` (1–50) is the number of passages in all; unset lets Membase cover every Memory in reach.
* `container="mv-…"` limits the search to one Memory (its id from `client.containers.list()`).
  Both can also be passed per call: `retriever.invoke(q, k=3, container="mv-…")`.
* If no passage comes back **and** some Memory could not answer (no model, dormant), the
  retriever raises `MembaseUnavailableError` instead of returning `[]`: that is not evidence of
  "no memory". When some Memories answered, it returns their passages and logs the rest.
* A `403` (`membase.PermissionDeniedError`) means the key's owner narrowed or revoked it; do
  not retry with that key.

It works anywhere a retriever does, e.g. in an LCEL chain:

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough

def cite(docs):
    return "\n".join(f"[{d.metadata['container_name']}] {d.page_content}" for d in docs)

prompt = ChatPromptTemplate.from_template("Memory:\n{memory}\n\nQuestion: {question}")
chain = {"memory": retriever | cite, "question": RunnablePassthrough()} | prompt | llm
```

## Agent tools

```python
from langchain.agents import create_agent
from langchain_membase import MembaseToolkit

agent = create_agent("openai:gpt-5", tools=MembaseToolkit().get_tools())   # or membase_tools()
agent.invoke({"messages": [{"role": "user", "content": "What did we pick for the ledger?"}]})
```

| Tool | Arguments | Notes |
|---|---|---|
| `membase_search` | `query`, optional `container` (a Memory's **name**) | passages as `- [Memory name] text`; the raw response is the `ToolMessage` artifact; Memories that could not answer are reported |
| `membase_save` | `content` (the user's words), `static`, optional `container` | the description tells the model to save **only when the user asks**; `static=true` records a standing fact in the profile; with several Memories in reach it needs a `container` (or a default `container=` id on the tool) and says which names exist |
| `membase_profile` | optional `topic` | standing and recently changed facts; only for a key whose owner ticked the profile |

`MembaseToolkit(save=False, profile=False)` (or `membase_tools(client, save=False, profile=False)`) leaves tools out; `container=` and `k=` set
defaults. Errors come back as the tool's answer (`handle_tool_error=True`), so the model can
tell the user, e.g. "this Membase key was narrowed or revoked".

Explicit tool calls use the SDK's own timeout (90 s): the first search after a quiet spell
takes longer while the user's memory wakes.

## Recall middleware (LangChain v1)

```python
from langchain.agents import create_agent
from langchain_membase import MembaseRecallMiddleware, membase_tools

agent = create_agent(
    "openai:gpt-5",
    tools=membase_tools(),
    system_prompt="You are a helpful assistant.",
    middleware=[MembaseRecallMiddleware(k=5, timeout=8.0)],
)
```

Before the model call, the middleware searches Membase for the latest user message and appends
the passages (each with its Memory) to the system message of that request.

* **Time-boxed and fail-open**: it waits at most `timeout` seconds (8 by default). On a timeout,
  a `403` or any other error it logs a warning on the `langchain_membase` logger and the model
  runs without memory. A search that ran past the budget finishes in the background, which
  wakes the memory for the next turn.
* **Once per user message**: model calls that follow tool calls in the same turn reuse the
  result.
* **Nothing is persisted**: the recall lives in the model request only, never in the agent's
  state or checkpoint.
* **No automatic writes by default**. `capture_turns=True` (opt-in) saves the user's own
  message(s) of each turn, once, as a note in `container=`, in the background. Assistant
  replies are never saved, so an AI answer is not learned as a fact about the user.

## Many users

A Membase key belongs to one person, so a multi-user app holds one key (or one local
directory) **per end user** and passes `client_for`, a function from user id to that user's
client. Every class and `membase_tools()` accept it. The user id is read from the run:

```python
from functools import lru_cache
from membase import Membase
from langchain_membase import MembaseRecallMiddleware, MembaseRetriever, membase_tools

@lru_cache(maxsize=1024)            # reuse each user's client (and its connection pool)
def client_for(user_id: str) -> Membase:
    return Membase(api_key=load_user_key(user_id))   # that user's key, never another's

retriever = MembaseRetriever(client_for=client_for)
retriever.invoke("...", config={"configurable": {"user_id": "alice"}})

agent = create_agent(
    model,
    tools=membase_tools(client_for=client_for),
    middleware=[MembaseRecallMiddleware(client_for=client_for)],
)
agent.invoke({"messages": [...]}, config={"configurable": {"user_id": "alice"}})
```

The user id is taken from `config["configurable"]["user_id"]`, or from a LangGraph run
context with a `user_id` field (`create_agent(..., context_schema=Ctx)` and
`agent.invoke(..., context=Ctx(user_id="alice"))`). With `client_for` set, a call that carries
no user id raises `MissingUserIdError` (the middleware logs it and skips recall); it never
falls back to another client. `client_for` and `client`/`api_key`/`local` are mutually
exclusive.

**Without `client_for`, one client serves every call.** That suits a personal agent (your own
key, your own memory), not a multi-user app.

Local mode keeps each person in their own directory:
`client_for=lambda uid: Membase(local=f"/data/membase/{uid}")`
(`pip install 'unibaseio-membase[local]'`, Python 3.12+, and an LLM key such as
`OPENAI_API_KEY`).

## Configuration

Every class takes `client=Membase(...)`, or `api_key=` / `base_url=` / `local=`; otherwise
`MEMBASE_API_KEY` and `MEMBASE_BASE_URL` come from the environment. The API host defaults to
`https://api.app.membase.com`.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e . \
  'langchain>=1.1' langgraph langchain-tests pytest pytest-asyncio ruff
.venv/bin/python -m pytest              # offline: the real SDK over httpx.MockTransport
MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py   # against the real API
uv build
```

The offline suite includes LangChain's standard tests (`langchain-tests`) for the retriever
and the three tools, and runs the tools and the middleware inside a real `create_agent`
(LangGraph) graph with a scripted chat model.

## Licence

MIT
