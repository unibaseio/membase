# Membase local engine

`pip install 'unibaseio-membase[local]'` runs the memory on your machine. For the hosted API and the
methods, see the [Membase docs](https://docs.membase.com/build/reference/sdk-quickstart/).

## What the local engine stores

<p align="center">
  <img src="../assets/memory-types.svg" width="840" alt="Conversations, documents and agent traces">
</p>

## Local memory over HTTP

```bash
membase --local serve            # http://127.0.0.1:8787/v1
```

The hosted memory API's routes, answered by the local engine with the same requests, answers and
refusals: point any client's base URL at it. Set `MEMBASE_LOCAL_TOKEN` to require it as a bearer
token.

- Reads cover every Memory (container) in the store. Writes land in its write target: the one
  `MEMBASE_WRITE_TARGET` names (made if missing), else the store's only Memory, else a new
  `default` one. A store with several Memories and no `MEMBASE_WRITE_TARGET` refuses a write with
  `422 write_target_required`, as the hosted API refuses a key that reaches several.
- Ids are typed (`mem_…`, `doc_…`, `med_…`); lists are `{items, has_more, partial}`; unknown
  fields (a `container`, say) are a `422`; a removal without `confirm=true` is a `428`.
- The engine learns before it answers, so a write's receipt and every document say
  `status: "succeeded"`. A fact added with `POST /v1/memories/add` is kept as a note (a document of kind
  `note`) whose deletion forgets what it taught, as hosted.
- `/v1/capabilities` names every action (the media ones with the media extra installed); local
  memory has no standing rules, so `rules` is empty.
- Profile facts (`/v1/profile-facts`) live in the store's profile, apart from every Memory. A
  statement added locally is `user_stated`; one kept with `add --static` before 0.4 reads as
  `imported`.

Where local memory differs: `/v1/ask` is open to every caller (hosted, only a credential granted
it may ask) and reads one Memory, the one whose facts match best.

## Local settings

Stores: `~/.membase/memory.db` (the `default` Memory), `~/.membase/containers/<id>/` (others),
`~/.membase/profile/` (profile facts). Settings come from the environment, or from a `.env` file in
the current directory or a parent.

| Setting | Default | |
|---|---|---|
| `OPENAI_API_KEY` | | chat and embeddings |
| `MEMBASE_LLM_PROVIDER` | from your keys | `openai`, `anthropic`, `ollama` or `openai-compat` for chat; set the models below to match |
| `MEMBASE_LLM_ENDPOINT` | | base URL for `ollama` or `openai-compat` |
| `MEMBASE_EMBED_PROVIDER` | `openai` | `openai-compat` uses `MEMBASE_LLM_ENDPOINT` |
| `MEMBASE_EMBED_MODEL` | `text-embedding-3-small` | embeddings |
| `MEMBASE_EPISODE_MODEL` | `gpt-4.1-mini` | conversations into episodes |
| `MEMBASE_DECIDER_MODEL` | `gpt-4o-mini` | picks the episodes a search returns |
| `MEMBASE_READER_MODEL` | `gpt-4o` | `ask` answers |
| `MEMBASE_KNOWLEDGE_MODEL` | `gpt-4o-mini` | documents into topic trees |
| `MEMBASE_AGENT_MODEL` | `gpt-4o-mini` | agent traces |
| `MEMBASE_LANG` | `en` | `zh` for Chinese extraction prompts |

`MEMBASE_LOCAL=1` makes local the default when `MEMBASE_API_KEY` is not set. `MEMBASE_WRITE_TARGET`
names the Memory writes go to (see above).

## Media memory

`pip install 'unibaseio-membase[media]'` adds photos, videos and audio recordings, kept apart from
documents and memories: they have their own routes, search and ask, and `/v1/search` and `/v1/ask`
do not read them. Listing, reading, searching and deleting are the hosted media routes; storing a
file and asking are local only (in process, or `membase serve`). It needs only a model provider
key: no local model is downloaded.

An image becomes a caption. A video is cut into shots (caption, time span, thumbnail each) and its
sound track into speaker turns matched to the person on screen; an audio file into speaker turns.
Search runs in any language over captions and speech.

| Route | Python | MCP tool | Command line |
|---|---|---|---|
| `POST /v1/media/add` `{path \| url, title?, custom_id?}` (local only) | `m.media.add(path, url=)` | `add_media` | `media add FILE \| --url` |
| `GET /v1/media?limit=` | `m.media.list()` | `list_media` | `media list` |
| `GET /v1/media/{id}` | `m.media.get(id)` | `get_media` | `media get ID` |
| `POST /v1/media/{id}/delete?confirm=` | `m.media.delete(id, confirm=)` | `delete_media` | `media delete ID [--confirm]` |
| `POST /v1/media/search` `{q, kind?, limit?}` | `m.media.search(q, kind=)` | `search_media` | `media search Q [--kind]` |
| `POST /v1/media/ask` `{message}` (local only) | `m.media.ask(message)` | `ask_media` | `media ask MESSAGE` |
| `GET /v1/media/{id}/file`, `/thumbs/{name}` | | | |

`path` is a file on the machine running the engine; the item lands in the write target. Items and
moments name their file and thumbnails as routes (`file_url`, `thumb_url`), which answer a `Range`
with `206`. `add` returns the stored item once it is processed, which takes minutes for a long
video. `kind` is `image` or `video` hosted; local memory also has `audio` and `text`. `ask` reads
the Memory whose media match best. `membase mcp` offers the hosted media tools (list, get, search,
delete) over hosted memory and over local memory with the extra installed; `add_media` and
`ask_media` only locally.

| Setting | Default | |
|---|---|---|
| `MEMBASE_MEDIA_CAPTION_MODEL` | `gpt-4o-mini` | image and shot captions |
| `MEMBASE_MEDIA_SUMMARY_MODEL` | `gpt-4o-mini` | video and recording summaries |
| `MEMBASE_MEDIA_RERANK_MODEL` | `gpt-4o-mini` | query translation, grading search results |
| `MEMBASE_MEDIA_ASK_MODEL` | `gpt-4.1` | `media ask` answers |
| `MEMBASE_MEDIA_SPEAKER_MODEL` | `gpt-4.1` | matching voices to people on screen |
| `MEMBASE_MEDIA_TRANSCRIBE_MODEL` | `gpt-4o-transcribe-diarize` | speech with speakers |

Captions and transcription need an OpenAI-compatible API (`OPENAI_API_KEY`, or `openai-compat` with
`MEMBASE_LLM_ENDPOINT`); the rest uses the chat and embedding providers above.

## Repository

`membase/` is the Python package (`client.py`, `cli.py`, `local/` for local memory and `membase serve`,
`mcp/` for `membase mcp`, `media/` for media memory, which the rest only mounts); `typescript/` is the npm package `@unibaseio/membase` and `cli/` the npm package
`@unibaseio/membase-cli`. `membase-ai` and `membase-sdk` (PyPI and npm) are earlier names.
