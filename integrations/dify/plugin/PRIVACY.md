# Privacy Policy: Membase plugin for Dify

Last updated: 2026-10-20

This plugin connects Dify apps to Membase, a hosted memory service operated by Unibase. It
reads from and writes to one Membase account: the account that owns the API key a Dify
workspace administrator enters in the plugin's credentials.

## What the plugin sends, and where

Every request goes over HTTPS to the Membase API at `https://api.app.membase.com` (or to
another HTTPS host on `membase.com` / `membase.ai` / `membase.io` if the administrator sets a base URL). No
other service is contacted by the plugin.

| Tool | Data sent to Membase |
|---|---|
| Credential check | the API key only (it lists the key's containers) |
| Search memory | the search text, and optionally a container name or id and a result limit |
| Save memory | the fact to save, as passed by the agent or workflow, and optionally a container and whether it is a profile fact |
| Get user profile | optionally a topic text |

Each request carries the Membase API key in the `Authorization` header. The plugin does not
send Dify user ids, conversation ids, app ids, files, or any data other than the tool inputs
listed above. Tool inputs are free text, so they can contain personal data (names, contact
details, preferences, or anything else an end user typed) when the app passes it on.

## What is stored

- **By the plugin:** nothing. The plugin keeps no database, cache, log or analytics of its
  own, and it does not write tool inputs, results or the API key to logs.
- **By Dify:** the API key and optional base URL, in Dify's credential storage for the
  workspace. The key is a secret field and is never returned in tool output or error messages.
- **By Membase:** a saved fact is stored in the Membase account's memory and becomes part of
  what that account's searches and profile can return. Search and profile requests are
  answered inside the account's own memory container. To answer a search and to learn saved notes, Membase runs a model under the account's
  AI Setup, so the text is also processed by the model provider that the account owner
  configured there, under that provider's terms.

## Who can see the data

A Membase API key belongs to one person's account. Every end user of every Dify app in the
workspace that uses this plugin reads from, and can write to, that one account's memory. Do
not use one key for an app with several end users unless they are meant to share memory;
the plugin's README describes a per-user alternative.

## Retention and deletion

What is saved stays in the Membase account until its owner deletes it in the Membase app
(a container, a document, or a single memory). The plugin has no delete or forget tool.
Removing the plugin or its credentials from Dify does not delete data already saved in
Membase. Revoking the key in the Membase app (Connect › Developer keys) stops all access at
the next request.

## Contact

Privacy questions and requests: support@unibase.com.
Source code: https://github.com/unibaseio/membase/tree/main/integrations/dify
