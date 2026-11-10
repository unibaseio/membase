# Membase for ChatGPT

The [Agent Plugins](https://agent-plugins.org) package that lists Membase in ChatGPT. It connects
ChatGPT to the hosted Membase MCP server (`https://api.app.membase.com/mcp-http`) over OAuth.

The connection is **read-only**: ChatGPT can search the user's memories, list and open facts, and
read their profile if they allow it. It cannot save, change or delete anything. On the consent
screen the user picks which Memories ChatGPT may read and whether it may see their profile.

## Contents

- `plugin/plugin.json`: the listing (name, descriptions, links, sample prompts) and the review
  test cases (5 positive, 3 negative).
- `plugin/mcp.json`: the MCP server.
- `plugin/skills/membase-memory/SKILL.md`: how ChatGPT should use the tools.
- `plugin/assets/`: icon and logo.

## Build the upload

The ZIP has the plugin files at its root:

```bash
cd integrations/chatgpt/plugin && zip -r ../membase-openai-plugin.zip . -x '.*'
```

Upload it at platform.openai.com/plugins.

## Review account

The test cases expect the fictional sample material from
[`scripts/seed_reviewer_account.py`](../../scripts/seed_reviewer_account.py) (persona "Alex
Rivera", Northwind Studio). Run it once against the reviewer's test account before submitting.
