---
name: membase-memory
description: How to use the user's Membase memory from ChatGPT — searching what they have saved, reading their profile, and citing where an answer came from. Use when the user refers to their own past notes, decisions, documents, projects or people, or asks what their memory holds.
---

# Using Membase memory

Membase is the user's long-term memory, shared across the AI tools they use. Through this
connection you can read the parts of it the user approved when they connected; you cannot add,
change or delete anything.

## The tools

- `search_memories` — passages from the user's memory on a question, most relevant first, each
  with the name of the Memory it came from (`memory_name`). Phrase the query as a natural-language
  question, as the user would ask it.
- `list_memories` — the facts the memory learned most recently; each has an id.
- `get_memory` — one fact in full, by the id from `search_memories` or `list_memories`.
- `list_profile_facts` — standing statements about the user (name, role, lasting preferences).
  Present only if the user allowed profile access.
- `get_capabilities` — what this connection may read and the user's standing rules for it.

## Good practice

- When the user asks about their own past ("what did I decide…", "where did I leave off…"),
  search before answering, and say which Memory the answer came from.
- If a search returns nothing relevant, say so plainly rather than guessing.
- The first search after a quiet period can take up to a minute while the memory wakes;
  later searches take a few seconds.
- If the user asks to save or delete something, explain that this connection is read-only and
  that they can do it in the Membase app (app.membase.com).
