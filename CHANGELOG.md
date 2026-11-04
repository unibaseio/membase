# Changelog

## membase-google-adk 0.1.0 and membase-haystack 0.1.0

Two new integrations, both on `unibaseio-membase` 0.4:

- [`integrations/google-adk/`](integrations/google-adk): `MembaseMemoryService`, a
  `BaseMemoryService` that ADK's `preload_memory` and `load_memory` search (time-boxed recall that
  fails open; writing sessions is opt-in with `capture=True`), and `membase_tools()` with
  `membase_search`, `membase_save` and `membase_profile`. `client_for` maps ADK's user id to that
  user's own client.
- [`integrations/haystack/`](integrations/haystack): `MembaseMemoryStore`, a
  `MembaseMemoryRetriever` component (time-boxed, fails open, serializes with the pipeline) and
  `membase_tools()` for a Haystack `Agent`.

## @unibaseio/membase-ai-sdk 0.1.0 — Vercel AI SDK tools

A new integration, [`integrations/ai-sdk/`](integrations/ai-sdk): `membaseTools()` gives an AI SDK 6
or 7 agent the `membase_search`, `membase_save` and `membase_profile` tools over
`@unibaseio/membase` 0.4.

## 0.4.0 — the memory API

`unibaseio-membase` 0.4.0, `@unibaseio/membase` 0.4.0, `@unibaseio/membase-cli` 0.4.0 and every
integration (below) move to the Membase memory API. **This is a breaking change**: the request and
answer shapes changed in place under `/v1`, the writes moved to `POST` + an action (below), and the
earlier shapes and routes are no longer served.
Earlier releases (0.3.x, npm 0.3.2) speak the earlier shapes and stop working against the hosted API
once it serves the memory API.

### No request names a Memory

- `containers` / `containers.list()` and `GET /v1/containers` are gone, and so is every `container=`
  parameter. Reads cover every Memory the key reaches; writes land in the key's **write target** — a
  key that reaches exactly one text Memory writes there, and one that reaches several is refused with
  `422 write_target_required` (`WriteTargetRequiredError`) rather than written somewhere by guess.
  `capabilities()` names the Memories a key reaches, the actions it holds and the user's standing
  rules.
- Sending a field the API does not know (a `container`, `static`, `metadata`) is a `422`.
- `memories.add(..., static=True)` is gone: a standing fact about the user is a profile fact
  (`profile_facts.add`).

### New shapes

| | 0.3 | 0.4 |
|---|---|---|
| search | `search(q, container=, limit=)` → `{query, results, containers}` | `search(q, limit=, mode="full")` → `{items: [{type, id?, content, score, memory_name, title?}], partial}` |
| facts | — | `memories.list(limit=)`, `memories.get(id)` (`mem_…` ids) |
| add a fact | `memories.add(content, container=, static=, title=)` → varied | `memories.add(content, title=)` → `202 {id: "doc_…", kind: "note", status, learning}` |
| add a document | `add(content, container=, url=, metadata=, custom_id=)` | `add(content, url=, title=, custom_id=)` (= `documents.add`) → `202 {id, kind: "file", status, learning}` |
| documents | `documents.list(container=)` → `{documents}` | `documents.list(kind=, limit=)` → `{items, has_more, partial}`; `doc_…` ids |
| media | local only, `container=` | `media.list(limit=)`, `get`, `search(q, kind=, limit=)`, `delete`; `med_…` ids; `file_url`/`thumb_url` routes |
| removal without `confirm` | `200 {status: "confirmation_required", …}` | `428 confirmation_required` → `ConfirmationRequiredError`; nothing changes |
| removal with `confirm=True` | varied | `{id, status: "forgotten"}` (facts, profile facts) or `{id, status: "deleted"}` |
| profile | `profile(q=)` → `{static, dynamic, results}` | `profile_facts.list(limit=)` → `{items: [{id: "pf_…", content, source_kind, recorded_at}], has_more, partial}`; `profile_facts.add(content)` → the fact and `created`; `profile_facts.forget(id, confirm=)` |
| rules | `rules()` → `GET /v1/rules` | `rules` in `capabilities()`: `{citation_required, retention_notice, version}` |
| ask | `ask(message, model=)` → the exposed agent's answer | `ask(message)` → `{answer, citations: [{memory_name}], partial}`, written by the memory itself; held only by a credential granted it (a marketplace buyer's), a `model` is a `422` |
| capabilities | — | `capabilities()` → `{actions, memories: [{name, kind}], rules, features}` |

### Writes are `POST` + an action

Reads stay `GET` (and `POST /v1/search`, `POST /v1/media/search`, `POST /v1/ask`, local
`POST /v1/media/ask`); every write is a `POST` naming its action. `confirm` stays a query parameter
and the bodies are unchanged. The earlier method on the earlier path answers
`405 method_not_allowed`. The SDK methods keep their names.

| | 0.3 | 0.4 |
|---|---|---|
| `memories.add` | `POST /v1/memories` | `POST /v1/memories/add` |
| `memories.forget` | `DELETE /v1/memories/{id}` | `POST /v1/memories/{id}/forget?confirm=` |
| `add` / `documents.add` | `POST /v1/documents` | `POST /v1/documents/add` |
| `documents.delete` | `DELETE /v1/documents/{id}` | `POST /v1/documents/{id}/delete?confirm=` |
| `media.delete` | `DELETE /v1/media/{id}` | `POST /v1/media/{id}/delete?confirm=` |
| `media.add` (local only) | `POST /v1/media` | `POST /v1/media/add` |
| `profile_facts.list` | `GET /v1/profile?q=` (`profile()`) | `GET /v1/profile-facts?limit=` |
| `profile_facts.add` | — | `POST /v1/profile-facts/add` |
| `profile_facts.forget` | — | `POST /v1/profile-facts/{id}/forget?confirm=` |
| `rules` in `capabilities()` | `GET /v1/rules` (`rules()`) | `GET /v1/capabilities` |

Every list is `{items, has_more, partial}`: the newest `limit` (1–1000, default 100), whether more
were left out, and the Memories that could not answer. Learning status is `succeeded`, `processing`,
`pending` or `failed`. Answers carry a Memory's name (`memory_name`), never its id.

New errors: `ConfirmationRequiredError` (428) and `WriteTargetRequiredError` (a 422,
subclass of `UnprocessableEntityError`). New: `capabilities()` and `profile_facts` (TypeScript
`profileFacts`); `profile()` and `rules()` are gone. Each profile fact call needs the key's
profile tick, and what it may do follows the access level: list at Read, add at Read & write,
forget (with `confirm`) at Full access.

### Local memory

`Membase(local=...)` and `membase serve` answer the same routes with the same shapes and refusals.
A local store's write target is the Memory `MEMBASE_WRITE_TARGET` names, else its only one, else a
new `default`; several and none named is the same `422`. A fact added through the API is kept as a
note (a `kind: "note"` document), so deleting the note forgets what it taught, as hosted. Local
learning finishes before a write answers, so receipts say `succeeded`. Media items name their file
and thumbnails as routes (`/v1/media/{id}/file`, `…/thumbs/{name}`, with `Range` support) instead of
paths, read with `media.file(id, thumb=, range=)` (TypeScript `media.file(id, {thumb, range})`): the
bytes, content type and, for a range, status 206 — with the key, hosted and local alike. Storing media (`media.add`) and `media.ask` stay local-only and take no container. Profile
facts live in the store's profile (`~/.membase/profile/`) and need `unibaseio-membase-core` 0.3.5 (the `[local]` extra installs 0.3.6, which also reads a PDF from its own text layer and reranks a long passage by the part that answers);
a statement added locally is `user_stated`, one kept with `add --static` before 0.4 reads as
`imported`. Local `ask` is the engine's answer from the Memory whose facts match best.

### Command line and MCP

- `membase` (Python and npm): `containers`, `profile`, `--container` and `--static` are gone; new
  `memories list|get`, `documents list --kind --limit`, `search --mode`, `capabilities`,
  `profile-facts list|add|forget`, and hosted `media list|get|search|delete` (npm too). A removal
  without `--confirm` prints the API's answer and exits 1. Local `import` and `agent` write to the
  store's write target.
- `membase mcp` offers the hosted MCP server's tools by the same names and arguments:
  `get_capabilities`, `search_memories`, `ask_memory`, `list_memories`, `get_memory`, `add_memory`,
  `forget_memory`, `list_profile_facts`, `add_profile_fact`, `forget_profile_fact`,
  `list_documents`, `get_document`, `add_document`, `delete_document`, and `list_media`,
  `get_media`, `search_media`, `delete_media` (hosted, or local with the media extra; local also
  `add_media` and `ask_media`). `list_containers`, `get_profile`, `memory_rules`, `ask_agent` and
  every `container`/`static` argument are gone. A refused call, a removal without `confirm`
  included, is a tool error carrying the API's code.

### Integrations

All cite `memory_name`, report `partial`, take no container option and explain a missing write
target: `langchain-membase` 0.2.0, `agent-framework-membase` 0.2.0, `membase-strands` 0.2.0,
Hermes plugin `unibaseio-membase` 0.3.0, Dify plugin `unibase/membase` 0.1.0, OpenClaw plugin
`@unibaseio/membase` 0.2.0. Each requires the 0.4 SDK. Their profile tool (`membase_profile`,
Dify's `get_profile`) lists the user's profile facts and takes no topic.
The Hermes plugin also takes `tools: off` (`MEMBASE_TOOLS`), `recall_mode: fast`
(`MEMBASE_RECALL_MODE`), and, on a Hermes build with `memory.user_store: provider`, keeps the
user's facts in their Membase profile (`record_user_fact`) and recalls the profile before each turn.
