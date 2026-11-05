# HierarchicalRAG → Hermes Long-Term Memory Tool

Goal: adapt this project's hierarchical memory engine (L0 chunks / L1 summaries /
L2 doc summaries, SQLite + ChromaDB) into an MCP server that gives Hermes Agent
a long-term memory store far bigger than its built-in memory budget.

## Agreed design decisions (2026-11-04)

- Work happens in the COPY: /home/marek/Playground/HierarchicalRAG-mem/
  (original HierarchicalRAG/ stays untouched — chatbot must keep working).

- Transport: MCP server (stdio), registered under `mcp_servers:` in
  ~/.hermes/config.yaml → tools appear as `mcp_hermem_*`.
- Storage: completely NEW, separate database. Papers in Database/ untouched.
- Location: ~/.hermes/mem/ (agent-owned).
- CPU-only. No cupy, no spacy.require_gpu(), no Ollama calls. The 3060 is
  fully committed to Strata; MiniLM-L6-v2 embeddings on CPU (~10ms/chunk) are
  enough.
- No LLM in the loop: the agent IS the LLM. It supplies its own L1/L2-style
  summary when writing a memory; the tool only chunks + embeds + stores.
- spaCy knowledge graph: OUT of v1 (noisy triples, extra model weight).
  Possible later extension.
- chat.py orchestrator loop is NOT reused — Hermes replaces Plan→Synthesize.

## Tool surface (v1)

1. mem_remember(content, tags, summary)   -> writes one memory:
   - summary = L2-equivalent (written by the agent)
   - content is split into L0 chunks; each chunk gets a one-line agent-provided
     or truncated L1 stand-in (v1: automatic first-sentence extract, no LLM)
   - returns memory id
2. mem_recall(query, layer=0|1, top_n=5)  -> semantic search over L0 or L1;
   returns text + memory id + tags + created date + distance
3. mem_context(query, top_n=3, window=1)  -> contextual L0 expansion
   (fixed version of find_similar_chunks_with_context)
4. mem_stats()                            -> counts per layer, per tag, storage size
5. mem_forget(memory_id | query)          -> taint (soft-delete) then purge

## Known bugs / pitfalls to fix during the port

- find_similar_chunks_with_context parses chunk ids with rsplit('_', 1);
  URLs/paths contain underscores -> context expansion silently breaks.
  Fix: chunk ids become {memory_uuid}#{index}, parsed on '#'.
- chromadb / sentence_transformers NOT installed in system python; hermes'
  own python is 3.14 (too new for chromadb wheels likely). Use a dedicated
  uv-managed venv pinned to python 3.11 or 3.12.
- chat.py/ingest_papers.py `import cupy` at module top — do not carry over.
- ChromaDB PersistentClient is not safe for two processes at once; the MCP
  server owns the store exclusively (documented, no concurrent CLI access).

## Tasks

1. [x] Scaffold: uv init ~/.hermes/mem/ (python 3.12), deps: chromadb,
       sentence-transformers, mcp. Verify MiniLM downloads + CPU encode works.
2. [x] memory_store.py: port DatabaseManager -> MemoryStore class.
       - SQL table `memories(id uuid, summary, tags, created_at, last_accessed_at,
         is_valid, kind)` replacing `documents`
       - collections `mem_chunks` (L0), `mem_chunk_summaries` (L1)
       - uuid#chunkid scheme (fixes rsplit bug)
3. [x] MCP server mem_server.py exposing remember/recall/context/stats/forget
       (FastMCP stdio). Smoke-test with a manual stdio client.
4. [x] Register in ~/.hermes/config.yaml under mcp_servers: (hermem),
       restart Hermes, verify mcp_hermem_* tools are discovered.
5. [x] End-to-end test: write 5 memories, recall across layers, forget one,
       confirm taint/purge behaves like the paper pipeline's invalidation.
6. [x] Write a skill (hermem-longterm-memory) documenting when/how to use the
       tools vs. built-in memory; note the "summaries come from the agent"
       convention.
7. [x] P40 L1 worker: ~/.hermes/mem/l1_worker.py (qwen3.5:9b, warm ~1s/chunk,
       think=false). Reads chroma sqlite read-only, queues into l1_upgrades
       sidecar table; MCP server applies on next recall. Verified end-to-end.
       Also done: scheduled service via cron job 'hermem-l1-enrichment'
       (every 30m, ~/.hermes/scripts/hermem_l1_tick.sh).
   [ ] Later: spaCy KG re-attach (parked).
       DECIDED 2026-11-05: paper-import is OUT permanently — hermem is the
       agent's memory, the paper chatbot stays a separate archive (unchanged,
       unrunnable here, revival path in memory 566b62b521d7).
8. [x] Ground-truth layer (built 2026-11-05):
       - SQLite table ground_truth(id, kind[bug-fix|error-log|command-output|
         config|snippet], class, title, payload VERBATIM, source, memory_id,
         created_at) + FTS5 virtual table (content-synced triggers).
       - NO embeddings — FTS5 exact-token search only. Tools: mem_ground
         (file evidence), mem_lookup (FTS search). class_ defaults from kind
         (config->operational, snippet->semantic, else episodic).
       - Cross-show: mem_recall hits list linked ground-truth ids;
         mem_lookup hits show linked memory summary. Verified.
9. [x] Memory classes (built 2026-11-05):
       - class TEXT CHECK(episodic|semantic|operational) on memories
         (ALTER, default 'semantic' for existing rows) AND ground_truth.
         Chroma metadata carries class too.
       - Episodic supersede -> ERROR (tested). Per-class staleness in recall:
         operational >14d -> review_due:true; semantic >180d -> stale:true;
         episodic never. age_days always shown.
       - class_ filter on mem_recall + mem_lookup (over-fetch x3 to fill
         top_n). recall(class_='operational') = weekly open commitments.
       - Write-time disambiguation rule in mem_remember description.
       - forget(operational) = tainted 'closed', audit-trail row kept (tested).
   [ ] After Hermes restart: verify mcp_hermem_mem_ground / mem_lookup are
       discovered and the updated mem_remember/mem_recall schemas are live.
       (DONE 2026-11-05: all 7 tools live, store purged + re-seeded.)
10. [x] Port-list from Claude Code study (design 2026-11-05, see
        references/claude-code-port-list.md):
        a. [x] secret scanner (26 gitleaks-subset rules, secret_scanner.py):
           scan+redact at mem_ground write, secret_hit column (rule ids),
           labels-only reporting, DB-URL password redaction. Verified: 11 pos
           + 7 neg cases pass, no secret reaches disk, lookup shows flag.
        b. [x] drift caveat on recall hits >1d old: "written N days ago —
           point-in-time observation, verify before asserting". Fresh hits
           clean. Aging = prompt text, never score decay.
        c. [x] write-path negative list + Why:/How-to-apply: convention +
           absolute-date rule in mem_remember description (covers e, f).
        d. [x] manifest dedup: mem_index() tool (id/class/summary/age manifest,
           class filter) + near_dupes hints in remember result (L1 probe,
           dist<0.45, tainted rows excluded). Verified: near-dupe caught a
           paraphrase of the chatbot memory at 0.391.
        g. [x] P40 LLM re-rank on read path (reranker.py): recall(rerank=True)
           over-fetches x10, one qwen3.5:9b /api/chat call with structured
           outputs picks <=top_n certain memories (per-memory granularity),
           ids validated vs candidates, silent fallback to vector order.
           Pitfall found: /api/chat not /api/generate (messages vs prompt);
           bare format:"json" degenerated to empty -> use "structured" schema.
           Verified: vague GPU query -> hardware memory pos=0; nonsense query
           -> ranker declines, fallback honest. Warm cost ~1.5s.
        h. [x-v1] session close-out ritual (designed+shipped 2026-11-05):
           Option A (agent-side, zero store code). Triggers: milestone-level
           state changes — git push, todo item closed, spec agreed,
           deploy/restart verified, decision with Marek. NOT per commit,
           NOT per tool call: one episodic memory per WORK UNIT.
           Template (fixed sections): MILESTONE / Did (commit hashes, files,
           task ids) / Decisions (+Why: +How to apply:) / Dead ends /
           State now / Next. tags milestone,<project>; class_ episodic.
           Anti-bloat: trivial units (typo fixes) earn nothing — git log
           covers them (negative list still applies). Continuity: session-
           start mem_index surfaces newest milestone per project.
           [ ] B (only if v1 survives 2wk bake): mem_session() tool —
               server-side template validation, auto milestone tag, auto-link
               ground_truth rows filed during the unit. ~40 lines.
           [ ] C (only if we keep forgetting A): cron distiller over Hermes
               session transcripts via P40 — highest cost, dropped qualifiers
               risk; do not build without bake evidence that A fails.
   NOTE: all port-list items live in MCP server (restarted 2026-11-05;
   mem_index/ground/lookup verified via MCP; rerank=true verified at store
   level — confirm once via MCP after next session start).
