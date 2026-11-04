<p align="center">
  <img src="https://raw.githubusercontent.com/unibaseio/membase/main/assets/membase-logo.png" width="72" alt="Membase">
</p>

<h1 align="center">@unibaseio/membase-cli</h1>

<p align="center">
  <b>One memory. Every AI. Only yours.</b><br>
  The <code>membase</code> command for the hosted Membase API, without Python.
</p>

<p align="center">
  <a href="https://membase.com">Website</a> ·
  <a href="https://docs.membase.com">Docs</a> ·
  <a href="https://app.membase.com">Web app</a> ·
  <a href="https://github.com/unibaseio/membase">GitHub</a> ·
  <a href="https://discord.gg/nB9EfPGsSf">Discord</a>
</p>

```bash
npm install -g @unibaseio/membase-cli      # or: npx @unibaseio/membase-cli <command>
export MEMBASE_API_KEY="mbk_…"             # Connect › Developer keys in the Membase app

membase add "We picked Postgres for the ledger"
membase search "what did we pick for the ledger?"
membase documents add notes/design.md
membase documents list --kind file
membase memories list --limit 20
membase forget mem_… --confirm
membase profile-facts list                 # who the user is (the key needs Your profile ticked)
membase capabilities                       # what this key may do, and the rules
```

Every command prints JSON. `--api-key` and `--base-url` override `MEMBASE_API_KEY` and
`MEMBASE_BASE_URL`; `membase --help` lists the commands. Writes land in the key's write target.
Exit codes: 0 done, 1 the API refused (a removal without `--confirm` prints what would go and
exits 1), 2 a usage error.

The commands and flags are those of the `membase` command in the Python package
[unibaseio-membase](https://pypi.org/project/unibaseio-membase/), which also runs memory on your
machine (`--local`), imports chat exports, learns from agent traces, and serves `membase serve` and
`membase mcp`:

```bash
pip install 'unibaseio-membase[local]'
```

Install one of the two: both put a `membase` command on your PATH. For code, use the SDK
[@unibaseio/membase](https://www.npmjs.com/package/@unibaseio/membase).

## License

MIT; see LICENSE.
