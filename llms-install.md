# Installing the Membase MCP server

Membase is a hosted MCP server. There is nothing to clone, build or run: add one remote server
entry to the client's MCP configuration.

- URL: `https://api.app.membase.com/mcp-http`
- Transport: Streamable HTTP (in Cline, `"type": "streamableHttp"`)

## Cline

Add this to `cline_mcp_settings.json` (MCP Servers › Configure › Configure MCP Servers):

```json
{
  "mcpServers": {
    "membase": {
      "type": "streamableHttp",
      "url": "https://api.app.membase.com/mcp-http"
    }
  }
}
```

On first use Cline opens Membase's sign-in and consent page in the browser. The user signs in,
ticks the Memories Cline may use, and approves. A connection made this way only reads: it can
search and list memories, photos and videos, and, if the user allows it, the profile.

To let Cline also save to memory, ask the user for a developer key instead of signing in. They
create one in the Membase app (https://app.membase.com) under Connect › Developer keys › Create
key; it starts with `mbk_`. Add it as a header:

```json
{
  "mcpServers": {
    "membase": {
      "type": "streamableHttp",
      "url": "https://api.app.membase.com/mcp-http",
      "headers": { "Authorization": "Bearer mbk_..." }
    }
  }
}
```

Never write the key into a file that is committed.

## Check it works

Call the `get_capabilities` tool: it lists the Memories this connection may use and what it
may do. An empty list of memories means none is switched on for this connection yet; the user
switches one on in the Membase app under Connect.

Do not install the Python or npm package for this: `unibaseio-membase` and `@unibaseio/membase`
are SDKs for writing code against Membase, not the MCP server.
