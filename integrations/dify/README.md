# Membase for Dify (tool plugin)

A Dify **tool plugin** that gives Dify agents and workflows long-term memory from one Membase
account: `search_memory`, `save_memory` (deliberate, model-called) and `get_profile`. Dify has
no memory plugin type, so memory is offered as tools with provider credentials.

- [`plugin/`](plugin/) is the plugin itself, exactly what goes into the `.difypkg`. Its
  [README](plugin/README.md) is the Marketplace page (setup, usage, the one-key-one-person
  caveat and the per-user MCP alternative) and [PRIVACY.md](plugin/PRIVACY.md) its privacy policy.

## How it is built

- Every Membase call goes through the official SDK (`unibaseio-membase`, `from membase import
  Membase`), installed by Dify from `plugin/requirements.txt`. Its only runtime dependency is
  `httpx`, which `dify_plugin` already needs, so the SDK costs the package nothing.
- `plugin/membase_dify/client.py` builds the client from the workspace credentials (API key,
  optional base URL limited to HTTPS on `membase.com` / `membase.ai` / `membase.io`) and turns SDK errors into
  messages: a `403` says the key was revoked, narrowed or is read-only and must not be retried.
- `plugin/provider/membase.py` validates credentials by listing the key's containers.
- Tools: `plugin/tools/*.yaml` (en_US + zh_Hans labels, descriptions written for a model) and
  `*.py`. Each returns a text message (passages cited as `[container name] content`) and a JSON
  message. A container that could not answer a search is reported, not shown as "no memory".

## Develop

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r plugin/requirements.txt pytest ruff pyyaml
.venv/bin/python -m pytest -q          # offline: runtime + httpx.MockTransport
.venv/bin/ruff check plugin tests examples && .venv/bin/ruff format --check plugin tests examples
```

The tests load the plugin the way Dify does (`dify_plugin`'s `PluginRegistration` reads
`manifest.yaml` and the provider/tool YAML, `PluginExecutor` validates credentials and invokes
tools) with the SDK answered by an `httpx.MockTransport`.

Against the real API (reads only unless `MEMBASE_SMOKE_WRITE=1`):

```bash
MEMBASE_API_KEY=mbk_... .venv/bin/python examples/live_smoke.py
```

## Package and check

```bash
mkdir -p .cache
gh release download -R langgenius/dify-plugin-daemon --pattern dify-plugin-linux-amd64 --dir .cache
chmod +x .cache/dify-plugin-linux-amd64
find plugin -name __pycache__ -prune -exec rm -rf {} +
.cache/dify-plugin-linux-amd64 plugin package ./plugin -o membase-0.0.1.difypkg
scripts/precheck.sh membase-0.0.1.difypkg <pr-body.md>   # the dify-plugins pre-check CI, locally
```

To try it in a Dify instance before publishing, use remote debugging: Dify's Plugins page shows a
debugging host and key (the bug icon), then run the plugin from
`plugin/` with `INSTALL_METHOD=remote REMOTE_INSTALL_URL=<host:port> REMOTE_INSTALL_KEY=<key>
python -m main`.
