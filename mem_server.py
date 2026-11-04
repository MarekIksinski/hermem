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
def mem_remember(content: str, summary: str, tags: str = "", kind: str = "note", supersedes: str = "") -> str:
    """Store a durable memory in the long-term hierarchical store.

    content: the full text worth remembering (facts, decisions, procedures).
    Fenced ``` code blocks are kept whole (never split mid-block).
    summary: ONE-2 sentence L2-style summary you (the agent) write — it is
    what future recall shows as context. tags: comma-separated keywords.
    kind: short category, e.g. 'project', 'environment', 'procedure',
    'code-pattern', 'system-config', 'preference'.
    supersedes: id of an older memory this one replaces (it gets tainted);
    use for facts that go stale — IPs, hosts, tooling state.
    """
    return json.dumps(store.remember(content=content, summary=summary, tags=tags, kind=kind, supersedes=supersedes))


@mcp.tool()
def mem_recall(query: str, layer: int = 0, top_n: int = 5) -> str:
    """Semantic search of long-term memory.

    layer=1 searches chunk one-liners (broad: find WHICH memory matters);
    layer=0 searches raw chunks (specific details). Returns text, memory_id,
    tags, dates and the parent memory summary for each hit.
    """
    return json.dumps(store.recall(query=query, layer=layer, top_n=top_n), ensure_ascii=False)


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
