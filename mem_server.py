#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hermem — Hermes long-term memory MCP server (stdio).

Exposes the MemoryStore (L0 chunks / L1 chunk one-liners / L2 memory summary)
to Hermes Agent as native tools:

  mem_remember  store a memory (agent supplies the L2 summary)
  mem_recall    semantic search over L0 (raw) or L1 (summaries)
  mem_context   contextual L0 search (neighbour-chunk expansion)
  mem_stats     layer/tag counts
  mem_forget    taint (soft-delete) or purge a memory

Run via .venv/bin/python mem_server.py; registered in ~/.hermes/config.yaml.
"""

import json

from mcp.server.mcpserver import MCPServer

from memory_store import MemoryStore

mcp = MCPServer("hermem")
store = MemoryStore()


@mcp.tool()
def mem_remember(content: str, summary: str, tags: str = "", kind: str = "note", supersedes: str = "",
                 class_: str = "semantic") -> str:
    """Store a durable memory in the long-term hierarchical store.

    content: the full text worth remembering (facts, decisions, procedures).
    Fenced ``` code blocks are kept whole (never split mid-block).
    summary: ONE-2 sentence L2-style summary you (the agent) write — it is
    what future recall shows as context. tags: comma-separated keywords.
    kind: short category, e.g. 'project', 'environment', 'procedure',
    'code-pattern', 'system-config', 'preference'.
    What NOT to save: anything derivable from current state (code patterns,
    architecture, file paths, git history, fix recipes) and ephemeral task
    state — even if the user explicitly asks to save a log/summary, capture
    only what was surprising or non-obvious about it. Convert relative dates
    to absolute ('Thursday' -> '2026-11-12'). For operational/decision
    memories, body structure: the fact, then **Why:** and **How to apply:**
    lines — future recall judges staleness by the Why.
    supersedes: id of an older memory this one replaces (it gets tainted);
    use for facts that go stale — IPs, hosts, tooling state. Cannot target
    an episodic memory (history is immutable — that returns an error).
    class_: episodic = what happened (decisions, closures; immutable);
    semantic = what is known (architecture, conventions; ~180d staleness);
    operational = what is active (projects, branches, hosts; ~14d review
    flag). Disambiguation: will this still be true when no project touches
    it? yes=semantic, no=operational. Default semantic.
    """
    return json.dumps(store.remember(content=content, summary=summary, tags=tags, kind=kind,
                                     supersedes=supersedes, class_=class_))


@mcp.tool()
def mem_recall(query: str, layer: int = 0, top_n: int = 5, class_: str = "") -> str:
    """Semantic search of long-term memory.

    layer=1 searches chunk one-liners (broad: find WHICH memory matters);
    layer=0 searches raw chunks (specific details). Returns text, memory_id,
    tags, class, dates, staleness flags (review_due/stale) and the parent
    memory summary for each hit. class_ filters to one memory class:
    recall(class_='operational') is the weekly-review open-commitments list.
    """
    return json.dumps(store.recall(query=query, layer=layer, top_n=top_n, class_filter=class_),
                      ensure_ascii=False)


@mcp.tool()
def mem_ground(title: str, payload: str, kind: str = "error-log", source: str = "",
               memory_id: str = "", class_: str = "") -> str:
    """File VERBATIM ground-truth evidence (no embeddings, exact-token search).

    payload is stored byte-for-byte: crash logs, error strings, command
    outputs, config snapshots, reference code snippets. kind:
    bug-fix|error-log|command-output|config|snippet. memory_id links the
    evidence to the memory just written (recommended). class_ defaults from
    kind (config->operational, snippet->semantic, else episodic).
    Recall hits show linked ground-truth ids.
    """
    return json.dumps(store.ground(title=title, payload=payload, kind=kind, source=source,
                                   memory_id=memory_id, class_=class_))


@mcp.tool()
def mem_lookup(query: str, top_n: int = 5, class_: str = "") -> str:
    """Exact-token FTS search over ground-truth payloads.

    For error strings, commands, ports, config keys — things semantic
    search is bad at. query is FTS5 syntax (tokens, "phrases", AND/OR).
    Hits show the linked memory's summary. class_ filters by memory class.
    Payloads are scanned+redacted at write time; a secret_hit field names
    which secret rules matched (labels only, never the secret itself).
    """
    return json.dumps(store.lookup(query=query, top_n=top_n, class_filter=class_),
                      ensure_ascii=False)


@mcp.tool()
def mem_context(query: str, top_n: int = 3, window: int = 1) -> str:
    """Contextual L0 search: best raw chunks PLUS their neighbouring chunks,
    for when a hit needs surrounding context to make sense."""
    return json.dumps(store.context(query=query, top_n=top_n, window=window), ensure_ascii=False)


@mcp.tool()
def mem_stats() -> str:
    """Counts of memories, chunks (L0) and one-liners (L1), by kind."""
    return json.dumps(store.stats())


@mcp.tool()
def mem_forget(memory_id: str, purge: bool = False) -> str:
    """Invalidate a memory. Default taints (hidden from recall, kept for audit);
    purge=true deletes its embeddings and row permanently."""
    return json.dumps(store.forget(memory_id=memory_id, purge=purge))


if __name__ == "__main__":
    mcp.run(transport="stdio")
