# Membase for Haystack

`membase-haystack` gives [Haystack](https://haystack.deepset.ai) agents and pipelines your
[Membase](https://membase.com) account as their long-term memory:

| Piece | Haystack type | What it does |
|---|---|---|
| `MembaseMemoryStore` | memory store | Owns the Membase client: `search_memories`, `add_memories`, `profile`. |
| `MembaseMemoryRetriever` | `@component` | Pipeline read path: memories as system `ChatMessage`s. Time-boxed, fails open. |
| `membase_tools()` | `Tool`s | `membase_search`, `membase_save` and `membase_profile` for an `Agent`. |

Every passage names the Memory it came from, e.g. `[Project decisions] We chose Postgres for
the ledger.`, so the model can cite it. There is no delete tool.

## Install

```bash
pip install membase-haystack
export MEMBASE_API_KEY="mbk_…"     # Connect › Developer keys in the Membase app
```

A **Read** key can search and, with Profile ticked, read the profile. Saving needs a
**Read & write** key whose writes have one place to go: a key that reaches a single Memory
saves there.

## Use with an Agent

```python
from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage

from membase_haystack import MembaseMemoryStore, membase_tools

store = MembaseMemoryStore()  # MEMBASE_API_KEY from the environment

agent = Agent(
    chat_generator=OpenAIChatGenerator(),
    tools=membase_tools(store),
    system_prompt=(
        "You are a helpful assistant with long-term memory. Search Membase before answering anything that "
        "may depend on what the user told you before, and cite the Memory in brackets. Save to Membase only "
        "when the user asks you to remember something."
    ),
)

result = agent.run(messages=[ChatMessage.from_user("Which database did we pick for the ledger?")])
print(result["last_message"].text)
```

| Tool | What it does |
|---|---|
| `membase_search(query, limit?)` | Searches every Memory the key reaches; each passage is cited as `[Memory name] …`, and a Memory that could not answer is said so. |
| `membase_save(content)` | Saves a note in the user's words, only when the user asks. |
| `membase_profile()` | Reads the user's profile facts, newest first. Needs Profile access on the key. |

`membase_tools(store, tools=("search", "profile"))` leaves saving out, e.g. for a Read key.

## Use in a pipeline

`MembaseMemoryRetriever` puts the relevant memories in front of the model on every run:

```python
from haystack import Pipeline
from haystack.components.builders import ChatPromptBuilder
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage

from membase_haystack import MembaseMemoryRetriever, MembaseMemoryStore

template = [
    ChatMessage.from_system(
        "Memory from Membase (cite the Memory in brackets):\n{% for m in memories %}{{ m.text }}\n{% endfor %}"
    ),
    ChatMessage.from_user("{{ question }}"),
]

pipeline = Pipeline()
pipeline.add_component("memory", MembaseMemoryRetriever(memory_store=MembaseMemoryStore(), top_k=5))
pipeline.add_component("prompt", ChatPromptBuilder(template=template, required_variables=["question"]))
pipeline.add_component("llm", OpenAIChatGenerator())
pipeline.connect("memory.memories", "prompt.memories")
pipeline.connect("prompt.prompt", "llm.messages")

question = "Which database did we pick for the ledger?"
result = pipeline.run({"memory": {"query": question}, "prompt": {"question": question}})
print(result["llm"]["replies"][0].text)
```

The retriever's search gets 8 seconds by default (`recall_timeout`), because the first search
after a quiet spell can take up to a minute while the memory wakes. When it runs out, the
retriever returns no memories and the pipeline goes on; the search keeps running, so the memory
is awake for the next run.

`store.add_memories(messages=...)` saves the user messages' text as one note, so you can keep
what the user said after a run. It keeps the user's own words only.

## One key is one person

A Membase key belongs to one person's account, so one `MembaseMemoryStore` serves one person.
In a multi-user app, build the store per user from that user's own key:

```python
from haystack.utils import Secret

store = MembaseMemoryStore(api_key=Secret.from_token(key_for(user_id)))
```

The store and the retriever serialize with the pipeline. The key is written as a `Secret`, so an
environment-variable key stays a reference and a token key refuses to serialize.

Haystack 3 loads only the components of modules on its deserialization allowlist, so allow this
package's module before loading a saved pipeline (Haystack 2 needs no step):

```python
from haystack.core.serialization import allow_deserialization_module

allow_deserialization_module("membase_haystack.memory")
pipeline = Pipeline.loads(saved)
```

Or set `HAYSTACK_DESERIALIZATION_ALLOWLIST=membase_haystack.memory` in the environment.

## Local memory

```bash
pip install 'membase-haystack[local]'
```

```python
store = MembaseMemoryStore(local="~/.membase")
```

## Requirements

Python 3.10+, `haystack-ai` and `unibaseio-membase` 0.4.0 or newer.

MIT licensed.
