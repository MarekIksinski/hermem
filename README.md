# hermem — hierarchical long-term memory for AI agents

A local-first long-term memory store exposed to [Hermes Agent](https://hermes-agent.nousresearch.com) over MCP. Architecture ported from my [HierarchicalRAG] research chatbot (paper-ingestion pipeline), retuned for agent memory: code snippets, environment facts, procedures.

## How it works

Three memory layers (SQLite + ChromaDB, all local):

| layer | what | where |
|-------|------|-------|
| L0 | raw text chunks (fenced code blocks stay atomic) | ChromaDB `mem_chunks` |
| L1 | one-line chunk summaries | ChromaDB `mem_chunk_summaries` |
| L2 | memory-level summary | SQLite `memories` |

- **No LLM on the write path.** The agent writes its own L2 summary; the store chunks + embeds with `all-MiniLM-L6-v2` on CPU (~10 ms/chunk).
- **Background enrichment.** `l1_worker.py` (optionally backed by a remote Ollama box) rewrites cheap first-sentence L1 stand-ins into real summaries. It reads Chroma's sqlite read-only and queues results into an `l1_upgrades` sidecar table; the MCP server (single Chroma owner) applies them on its next call.
- **Staleness.** `supersedes=<old_id>` taints the replaced memory in the same transaction; tainted memories vanish from recall but stay for audit.
- **Dedup.** identical content hash -> `status: duplicate`, no re-write.

## Tools (MCP stdio)

`mem_remember`, `mem_recall` (layer 0/1), `mem_context` (neighbour-chunk expansion), `mem_stats`, `mem_forget` (taint or purge).

## Setup

> **Note for your own configuration:** the code contains example values from
> my homelab — the P40 Ollama address in `l1_worker.py`
> (`OLLAMA_URL = "http://192.168.1.127:11434/..."`) and the LAN IPs/ports in
> the test files. Replace them with your own (any Ollama server works, or
> skip the worker entirely — stand-in L1 summaries are fine without it).
> Paths assume `~/.hermes/mem`; adjust `DEFAULT_DB_DIR` / `MEM_DIR` if you
> install elsewhere.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python chromadb sentence-transformers mcp
```

Register in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  hermem:
    command: /path/to/.venv/bin/python
    args: ["/path/to/mem_server.py"]
    timeout: 60
    connect_timeout: 120
```

L1 enrichment tick (cron every 30 min, silent when idle):

```bash
.venv/bin/python l1_worker.py --once   # --model qwen3.5:9b, needs Ollama
```

Tests: `test_smoke.py` (store round-trips), `test_schema_v2.py` (code-aware chunking, supersedes chain). Runtime state lives in `store/` (gitignored).

## Notes

- mcp SDK 2.x renamed `FastMCP` -> `MCPServer` (`mcp.server.mcpserver`).
- Ollama calls must pass `"think": false` — default reasoning burns ~700 tokens for a one-liner.
- ChromaDB `PersistentClient` is single-process; only the MCP server opens it for writes.
