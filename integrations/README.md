# Membase integrations

Membase memory for agent frameworks. Each folder is one self-contained integration with its own
package, tests and README.

| Folder | Framework | Package | Install |
|---|---|---|---|
| [`agent-framework/`](agent-framework) | Microsoft Agent Framework (Python) | `agent-framework-membase` on PyPI | `pip install agent-framework-membase` |
| [`dify/`](dify) | Dify | Dify plugin `unibase/membase` | Dify › Plugins › Marketplace, search Membase (once listed) |
| [`hermes/`](hermes) | Hermes Agent | Hermes plugin `unibaseio-membase` | `hermes plugins install unibaseio/membase/integrations/hermes` |
| [`langchain/`](langchain) | LangChain / LangGraph (Python) | `langchain-membase` on PyPI | `pip install langchain-membase` |
| [`openclaw/`](openclaw) | OpenClaw | `@unibaseio/membase` on [ClawHub](https://clawhub.ai/unibaseio/plugins/membase) | `openclaw plugins install clawhub:@unibaseio/membase` |
| [`strands/`](strands) | Strands Agents (Python) | `membase-strands` on PyPI | `pip install membase-strands` |

## What every integration does the same way

- Uses the official Membase SDK (`unibaseio-membase` / `@unibaseio/membase`); a key comes from
  **Connect › Developer keys** in the Membase app.
- **One key is one person's memory.** Where the framework passes a user id, a `client_for`
  hook gives each person their own key (or their own local directory); otherwise the
  integration suits a personal agent, and its README says so.
- Automatic recall before a turn waits at most 8 seconds by default and then continues without
  memory, because the first search after a quiet spell takes longer.
- Saving is a tool the model calls when the user asks. Automatic capture is off by default and,
  when turned on, saves only the user's own words. There is no delete tool.
- Every passage names the Memory it came from.

MIT licensed.
