# Changelog

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
  `capabilities()` names the Memories a key reaches and the actions it holds.
- Sending a field the API does not know (a `container`, `static`, `metadata`) is a `422`.
- `memories.add(..., static=True)` is gone: the API has no way to write a standing profile fact.

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
| removal with `confirm=True` | varied | `{id, status: "forgotten"}` (facts) or `{id, status: "deleted"}` |

### Writes are `POST` + an action

Reads stay `GET` (and `POST /v1/search`, `POST /v1/media/search`, `POST /v1/ask`); every write is a
`POST` naming its action. `confirm` stays a query parameter and the bodies are unchanged. The earlier
method on the earlier path answers `405 method_not_allowed`. The SDK methods keep their names.

| | 0.3 | 0.4 |
|---|---|---|
| `memories.add` | `POST /v1/memories` | `POST /v1/memories/add` |
| `memories.forget` | `DELETE /v1/memories/{id}` | `POST /v1/memories/{id}/forget?confirm=` |
| `add` / `documents.add` | `POST /v1/documents` | `POST /v1/documents/add` |
| `documents.delete` | `DELETE /v1/documents/{id}` | `POST /v1/documents/{id}/delete?confirm=` |
| `media.delete` | `DELETE /v1/media/{id}` | `POST /v1/media/{id}/delete?confirm=` |

Every list is `{items, has_more, partial}`: the newest `limit` (1–1000, default 100), whether more
were left out, and the Memories that could not answer. Learning status is `succeeded`, `processing`,
`pending` or `failed`. Answers carry a Memory's name (`memory_name`), never its id.

New errors: `ConfirmationRequiredError` (428) and `WriteTargetRequiredError` (a 422,
subclass of `UnprocessableEntityError`). New method: `capabilities()`. Profile, rules and ask are
unchanged.

### Local memory

`Membase(local=...)` and `membase serve` answer the same routes with the same shapes and refusals.
A local store's write target is the Memory `MEMBASE_WRITE_TARGET` names, else its only one, else a
new `default`; several and none named is the same `422`. A fact added through the API is kept as a
note (a `kind: "note"` document), so deleting the note forgets what it taught, as hosted. Local
learning finishes before a write answers, so receipts say `succeeded`. Media items name their file
and thumbnails as routes (`/v1/media/{id}/file`, `…/thumbs/{name}`, with `Range` support) instead of
paths. Storing media (`media.add`) and `media.ask` stay local-only and take no container.

### Command line and MCP

- `membase` (Python and npm): `containers`, `--container` and `--static` are gone; new
  `memories list|get`, `documents list --kind --limit`, `search --mode`, `capabilities`, and hosted
  `media list|get|search|delete` (npm too). A removal without `--confirm` prints the API's answer and
  exits 1. Local `import` and `agent` write to the store's write target.
- `membase mcp`: `list_containers` and every `container`/`static` argument are gone; a removal without
  `confirm` returns the API's `confirmation_required` answer. (The hosted MCP server at
  `/mcp-http` is separate and unchanged.)

### Integrations

All cite `memory_name`, report `partial`, take no container option and explain a missing write
target: `langchain-membase` 0.2.0, `agent-framework-membase` 0.2.0, `membase-strands` 0.2.0,
Hermes plugin `unibaseio-membase` 0.2.0, Dify plugin `unibase/membase` 0.1.0, OpenClaw plugin
`@unibaseio/membase` 0.2.0. Each requires the 0.4 SDK.
