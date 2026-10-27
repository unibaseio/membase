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

The hosted API's agent-protocol routes, answered by the local engine: point any client's base URL
at it. Set `MEMBASE_LOCAL_TOKEN` to require it as a bearer token.

## Local settings

Stores: `~/.membase/memory.db` (default container), `~/.membase/containers/<id>/` (others),
`~/.membase/profile/` (static facts). Settings come from the environment, or from a `.env` file in
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

`MEMBASE_LOCAL=1` makes local the default when `MEMBASE_API_KEY` is not set.

## Media memory

`pip install 'unibaseio-membase[media]'` adds photos, videos and audio recordings, kept apart from
documents and memories: they have their own routes, search and ask, and `/v1/search` and `/v1/ask`
do not read them. Local only for now (in process, or `membase serve`). The extra brings torch for
CLIP; install the CPU build first (`pip install torch --index-url https://download.pytorch.org/whl/cpu`)
unless you want CUDA. CLIP's weights (~1.2GB) download on first use.

An image becomes a caption and a CLIP vector. A video is cut into shots (caption, time span, thumbnail
each) and its sound track into speaker turns matched to the person on screen; an audio file into
speaker turns. Search runs in any language over captions, speech and the pictures themselves.

| Route | Python | MCP tool | Command line |
|---|---|---|---|
| `POST /v1/media` `{path \| url, container?, title?, custom_id?}` | `m.media.add(path, url=, container=)` | `add_media` | `media add FILE \| --url` |
| `GET /v1/media?container=` | `m.media.list()` | `list_media` | `media list` |
| `GET /v1/media/{id}` | `m.media.get(id)` | `get_media` | `media get ID` |
| `DELETE /v1/media/{id}?confirm=` | `m.media.delete(id, confirm=)` | `delete_media` | `media delete ID [--confirm]` |
| `POST /v1/media/search` `{q, container?, kind?, limit?}` | `m.media.search(q, kind=)` | `search_media` | `media search Q [--kind]` |
| `POST /v1/media/ask` `{message, container?}` | `m.media.ask(message)` | `ask_media` | `media ask MESSAGE` |

`path` is a file on the machine running the engine. Items and moments report `path` and `thumb` as
files under the container's `media/` directory. `add` returns once the item is processed, which takes
minutes for a long video. The MCP tools appear only for local memory with the extra installed.

| Setting | Default | |
|---|---|---|
| `MEMBASE_MEDIA_CAPTION_MODEL` | `gpt-4o-mini` | image and shot captions |
| `MEMBASE_MEDIA_SUMMARY_MODEL` | `gpt-4o-mini` | video and recording summaries |
| `MEMBASE_MEDIA_RERANK_MODEL` | `gpt-4o-mini` | query translation, grading search results |
| `MEMBASE_MEDIA_ASK_MODEL` | `gpt-4.1` | `media ask` answers |
| `MEMBASE_MEDIA_SPEAKER_MODEL` | `gpt-4.1` | matching voices to people on screen |
| `MEMBASE_MEDIA_TRANSCRIBE_MODEL` | `gpt-4o-transcribe-diarize` | speech with speakers |
| `MEMBASE_MEDIA_VISUAL_MODEL` | `hf-hub:UCSC-VLAA/openvision-vit-large-patch14-224` | CLIP |

Captions and transcription need an OpenAI-compatible API (`OPENAI_API_KEY`, or `openai-compat` with
`MEMBASE_LLM_ENDPOINT`); the rest uses the chat and embedding providers above.

## Repository

`membase/` is the Python package (`client.py`, `cli.py`, `local/` for local memory and `membase serve`,
`mcp/` for `membase mcp`, `media/` for media memory, which the rest only mounts); `typescript/` is the npm package `@unibaseio/membase` and `cli/` the npm package
`@unibaseio/membase-cli`. `membase-ai` and `membase-sdk` (PyPI and npm) are earlier names.
