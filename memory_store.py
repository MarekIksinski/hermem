#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MemoryStore — hierarchical long-term memory for Hermes Agent.

Ported from HierarchicalRAG/database_manager.py (which stays untouched).
Layers:
  L0: raw text chunks        -> ChromaDB collection 'mem_chunks'
  L1: per-chunk one-liners   -> ChromaDB collection 'mem_chunk_summaries'
  L2: memory-level summary   -> SQL table 'memories'

CPU-only. No cupy, no GPU, no Ollama on the hot path. The agent (Hermes)
supplies the L2 summary itself; L1 one-liners are cheap first-sentence
extracts in v1 (P40 gemma worker may upgrade them later).

Chunk-ID scheme fixes the old rsplit('_',1) bug: ids are '{memory_id}#{index}'.
"""

import hashlib
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import chromadb
from sentence_transformers import SentenceTransformer

from secret_scanner import redact_and_report

DEFAULT_DB_DIR = "/home/marek/.hermes/mem/store"

MEMORY_CLASSES = ("episodic", "semantic", "operational")
# Per-class staleness clocks (days). Episodic never decays.
OPERATIONAL_REVIEW_DAYS = 14
SEMANTIC_STALE_DAYS = 180
# Claude Code lesson: age is prompt text, not score decay. Hits older than
# this get an inline drift caveat rendered in human words ("N days ago").
DRIFT_CAVEAT_DAYS = 1
DRIFT_CAVEAT = ("this memory was written {age} — a point-in-time observation, not live "
                "state; verify against current reality before asserting as fact")


def _first_sentence(text: str, max_len: int = 200) -> str:
    """Cheap L1 stand-in: first sentence of the chunk, truncated."""
    text = " ".join(text.split())
    for sep in (". ", "! ", "? ", "\n"):
        idx = text.find(sep)
        if 0 < idx < max_len:
            return text[: idx + 1]
    return text[:max_len]


def _split_chunks(content: str, chunk_size: int = 1000, overlap: int = 150) -> List[str]:
    """Prose+code aware splitter.

    Fenced code blocks (``` ... ```) are kept atomic — never split mid-block,
    even if oversized. Prose splits on paragraphs; oversized prose hard-splits.
    """
    # 1. Separate fenced code blocks from prose segments
    segments: List[tuple] = []  # (is_code, text)
    parts = content.split("```")
    for i, part in enumerate(parts):
        if i % 2 == 1:  # inside fences
            segments.append((True, "```" + part + "```"))
        else:
            for p in part.split("\n\n"):
                if p.strip():
                    segments.append((False, p.strip()))

    chunks: List[str] = []
    buf = ""
    for is_code, seg in segments:
        if is_code:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(seg)  # atomic, even if > chunk_size
            continue
        if len(buf) + len(seg) + 2 <= chunk_size:
            buf = (buf + "\n\n" + seg) if buf else seg
        else:
            if buf:
                chunks.append(buf)
            while len(seg) > chunk_size:
                chunks.append(seg[:chunk_size])
                seg = seg[overlap:]
            buf = seg
    if buf:
        chunks.append(buf)
    return chunks


class MemoryStore:
    def __init__(self, db_dir: str = DEFAULT_DB_DIR):
        import os

        os.makedirs(db_dir, exist_ok=True)
        self.sql_db_path = os.path.join(db_dir, "memories.sqlite")
        self._init_sql()
        self.embedding_model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
        self.chroma_client = chromadb.PersistentClient(path=os.path.join(db_dir, "chroma_db"))
        self.chunk_collection = self.chroma_client.get_or_create_collection(name="mem_chunks")
        self.summary_collection = self.chroma_client.get_or_create_collection(name="mem_chunk_summaries")

    # ---------- SQL ----------

    def _init_sql(self) -> None:
        with sqlite3.connect(self.sql_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'note',
                    created_at TIMESTAMP NOT NULL,
                    last_accessed_at TIMESTAMP,
                    is_valid BOOLEAN NOT NULL DEFAULT 1,
                    supersedes TEXT
                )
                """
            )
            try:
                conn.execute("ALTER TABLE memories ADD COLUMN supersedes TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            # schema v3: memory class (episodic|semantic|operational).
            # Existing rows default to 'semantic' (they are architecture /
            # constraint facts, which is correct for both seeded memories).
            try:
                conn.execute(
                    "ALTER TABLE memories ADD COLUMN class TEXT NOT NULL DEFAULT 'semantic' "
                    "CHECK(class IN ('episodic','semantic','operational'))"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            # schema v4: secret-hit flag on ground truth (rule ids, space-separated)
            try:
                conn.execute("ALTER TABLE ground_truth ADD COLUMN secret_hit TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            # ---------- schema v3: ground-truth layer (task 8) ----------
            # Verbatim evidence (crash logs, commands, config, snippets).
            # NO embeddings — exact-token search via FTS5 only.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ground_truth (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('bug-fix','error-log','command-output','config','snippet')),
                    class TEXT NOT NULL DEFAULT 'episodic' CHECK(class IN ('episodic','semantic','operational')),
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    memory_id TEXT,
                    secret_hit TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS ground_truth_fts
                USING fts5(title, payload, content='ground_truth', content_rowid='rowid')
                """
            )
            # Keep FTS in sync with the content table.
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS gt_ai AFTER INSERT ON ground_truth BEGIN
                    INSERT INTO ground_truth_fts(rowid, title, payload)
                    VALUES (new.rowid, new.title, new.payload);
                END;
                CREATE TRIGGER IF NOT EXISTS gt_ad AFTER DELETE ON ground_truth BEGIN
                    INSERT INTO ground_truth_fts(ground_truth_fts, rowid, title, payload)
                    VALUES ('delete', old.rowid, old.title, old.payload);
                END;
                CREATE TRIGGER IF NOT EXISTS gt_au AFTER UPDATE ON ground_truth BEGIN
                    INSERT INTO ground_truth_fts(ground_truth_fts, rowid, title, payload)
                    VALUES ('delete', old.rowid, old.title, old.payload);
                    INSERT INTO ground_truth_fts(rowid, title, payload)
                    VALUES (new.rowid, new.title, new.payload);
                END;
                """
            )
            # FTS over memory summaries+tags (L2 layer) so mem_lookup can
            # answer "where does this identifier live?" across BOTH layers
            # (ground-truth payloads AND memory manifests). External-content
            # table; summary/tags only change via INSERT (remember) and hard
            # DELETE (purge) — is_valid taints are filtered at query time.
            # NOTE: never test emptiness via count(*) on memories_fts — an
            # external-content FTS reads the CONTENT table, so it is never 0.
            # Existence is checked in sqlite_master instead.
            fts_existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='memories_fts'").fetchone()
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(summary, tags, content='memories', content_rowid='rowid')
                """
            )
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS mem_fts_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, summary, tags)
                    VALUES (new.rowid, new.summary, new.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_fts_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, summary, tags)
                    VALUES ('delete', old.rowid, old.summary, old.tags);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_fts_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, summary, tags)
                    VALUES ('delete', old.rowid, old.summary, old.tags);
                    INSERT INTO memories_fts(rowid, summary, tags)
                    VALUES (new.rowid, new.summary, new.tags);
                END;
                """
            )
            # one-time backfill for rows written before the FTS table existed
            if not fts_existed:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            # Sidecar queue: the P40 l1_worker.py (separate process) inserts
            # upgraded L1 summaries here; this process (Chroma owner) applies
            # them on the next call. Keeps Chroma single-writer.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS l1_upgrades (
                    chunk_id TEXT PRIMARY KEY,
                    new_text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
            conn.commit()

    def _apply_pending_l1(self) -> int:
        """Apply queued L1 upgrades (written by l1_worker.py) into Chroma L1."""
        with sqlite3.connect(self.sql_db_path) as conn:
            rows = conn.execute("SELECT chunk_id, new_text FROM l1_upgrades").fetchall()
        if not rows:
            return 0
        existing = self.summary_collection.get(ids=[r[0] for r in rows], include=["metadatas"])
        found = set(existing["ids"])
        docs, metas, ids = [], [], []
        for cid, new_text in rows:
            if cid not in found:
                with sqlite3.connect(self.sql_db_path) as conn:
                    conn.execute("DELETE FROM l1_upgrades WHERE chunk_id = ?", (cid,))
                continue
            meta = existing["metadatas"][existing["ids"].index(cid)] or {}
            meta["l1_state"] = "real"
            docs.append(new_text)
            metas.append(meta)
            ids.append(cid)
        if ids:
            emb = self.embedding_model.encode(docs, normalize_embeddings=True).tolist()
            self.summary_collection.update(ids=ids, documents=docs, embeddings=emb, metadatas=metas)
            with sqlite3.connect(self.sql_db_path) as conn:
                conn.executemany("DELETE FROM l1_upgrades WHERE chunk_id = ?", [(i,) for i in ids])
                conn.commit()
        return len(ids)

    # ---------- Write ----------

    def remember(
        self,
        content: str,
        summary: str,
        tags: str = "",
        kind: str = "note",
        supersedes: str = "",
        class_: str = "semantic",
    ) -> Dict[str, Any]:
        if class_ not in MEMORY_CLASSES:
            return {"status": "error", "error": f"class must be one of {MEMORY_CLASSES}"}
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.sql_db_path) as conn:
            dup = conn.execute(
                "SELECT id FROM memories WHERE content_hash = ? AND is_valid = 1",
                (content_hash,),
            ).fetchone()
            if dup:
                return {"status": "duplicate", "id": dup[0]}

            if supersedes:
                # episodic memories are immutable history — supersession of an
                # event record must ERROR, never silently taint.
                target = conn.execute(
                    "SELECT id, class FROM memories WHERE id = ?", (supersedes,)
                ).fetchone()
                if not target:
                    return {"status": "error", "error": f"supersedes id not found: {supersedes}"}
                if target[1] == "episodic":
                    return {
                        "status": "error",
                        "error": f"cannot supersede episodic memory {supersedes} (history is immutable); "
                                 "store the outcome as a new episodic record instead",
                    }
                conn.execute("UPDATE memories SET is_valid = 0 WHERE id = ?", (supersedes,))

            mem_id = uuid.uuid4().hex[:12]
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO memories (id, summary, content_hash, tags, kind, created_at, last_accessed_at, supersedes, class) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mem_id, summary, content_hash, tags, kind, now, now, supersedes or None, class_),
            )
            conn.commit()

        chunks = _split_chunks(content)
        l1 = [_first_sentence(c) for c in chunks]

        # Manifest-dedup (Claude Code lesson): before writing, show the writer
        # its nearest existing neighbours so update/supersede beats duplicate.
        # Cheap: one encode + one L1 query; only a hint, never a block.
        near_dupes: List[Dict[str, Any]] = []
        if self.summary_collection.count() > 0:
            probe = self.embedding_model.encode(summary or chunks[0], normalize_embeddings=True).tolist()
            q = self.summary_collection.query(query_embeddings=[probe], n_results=min(5, self.summary_collection.count()))
            seen = set()
            for meta, dist in zip(q["metadatas"][0], q["distances"][0]):
                mid = meta["memory_id"]
                if mid == mem_id or mid in seen:
                    continue
                seen.add(mid)
                if dist < 0.45:  # normalized embeddings: <0.45 ~ strongly related
                    info = self._meta_sql([mid]).get(mid, {})
                    if not info.get("valid", False):
                        continue  # don't nudge toward superseded/tainted rows
                    near_dupes.append({"memory_id": mid, "distance": round(float(dist), 3),
                                       "class": info.get("class", "?"),
                                       "summary": info.get("summary", "")})

        ids = [f"{mem_id}#{i}" for i in range(len(chunks))]
        meta = [{"memory_id": mem_id, "tags": tags, "kind": kind, "class": class_, "level": 0} for _ in chunks]
        emb0 = self.embedding_model.encode(chunks, normalize_embeddings=True).tolist()
        self.chunk_collection.add(embeddings=emb0, documents=chunks, metadatas=meta, ids=ids)

        sids = [f"{mem_id}#s{i}" for i in range(len(l1))]
        smeta = [{"memory_id": mem_id, "tags": tags, "kind": kind, "class": class_, "level": 1, "l0_chunk_id": ids[i],
                  "l1_state": "standin"} for i in range(len(l1))]
        emb1 = self.embedding_model.encode(l1, normalize_embeddings=True).tolist()
        self.summary_collection.add(embeddings=emb1, documents=l1, metadatas=smeta, ids=sids)

        result = {"status": "stored", "id": mem_id, "chunks": len(chunks), "class": class_}
        if near_dupes:
            result["near_dupes"] = near_dupes  # consider supersedes instead of a new write
        return result

    def session(
        self,
        milestone: str,
        did: str,
        decisions: str,
        dead_ends: str,
        state_now: str,
        next_step: str,
        project: str = "",
    ) -> Dict[str, Any]:
        """Close-out ritual (task 10h-B): enforce the milestone template.

        All five sections are required arguments — the schema IS the template,
        so it cannot be half-written. Assembles the fixed-section episodic
        memory, tags it milestone[,<project>], and auto-links any
        ground-truth rows filed since the previous milestone that are still
        unlinked (memory_id IS NULL).
        """
        empty = [n for n, v in (("did", did), ("decisions", decisions),
                                ("dead_ends", dead_ends), ("state_now", state_now),
                                ("next_step", next_step)) if not v.strip()]
        if empty:
            return {"status": "error",
                    "error": f"milestone template sections missing (write them, even if 'none'): {', '.join(empty)}"}
        if not milestone.strip():
            return {"status": "error", "error": "milestone (one-line header incl. absolute date) required"}

        body = (
            f"MILESTONE: {milestone.strip()}\n\n"
            f"Did: {did.strip()}\n\n"
            f"Decisions: {decisions.strip()}\n\n"
            f"Dead ends: {dead_ends.strip()}\n\n"
            f"State now: {state_now.strip()}\n\n"
            f"Next: {next_step.strip()}"
        )
        tags = "milestone" + (f",{project}" if project else "")

        # auto-link cutoff: last milestone's created_at (else epoch)
        with sqlite3.connect(self.sql_db_path) as conn:
            last = conn.execute(
                "SELECT created_at FROM memories WHERE kind='milestone' AND is_valid=1 "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
        cutoff = last[0] if last else "0000-01-01"

        result = self.remember(content=body, summary=milestone.strip(), tags=tags,
                               kind="milestone", class_="episodic")
        if result.get("status") != "stored":
            return result

        with sqlite3.connect(self.sql_db_path) as conn:
            rows = conn.execute(
                "SELECT id, title FROM ground_truth WHERE memory_id IS NULL AND created_at > ?",
                (cutoff,)).fetchall()
            for gid, _ in rows:
                conn.execute("UPDATE ground_truth SET memory_id = ? WHERE id = ?",
                             (result["id"], gid))
            conn.commit()
        if rows:
            result["linked_ground_truth"] = [g for g, _ in rows]
        return result

    def index(self, class_filter: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """Manifest view: [class] id (age): L2 summary for valid memories.

        Newest first, no content bodies — the Claude Code MEMORY.md pattern:
        a cheap index the agent scans to decide update-vs-create and what to
        recall in full.
        """
        if class_filter and class_filter not in MEMORY_CLASSES:
            return []
        with sqlite3.connect(self.sql_db_path) as conn:
            q = ("SELECT id, class, kind, tags, summary, created_at FROM memories "
                 "WHERE is_valid = 1 {} ORDER BY created_at DESC LIMIT ?")
            params: list = []
            if class_filter:
                q = q.format("AND class = ?")
                params.append(class_filter)
            else:
                q = q.format("")
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            entry = {"id": r[0], "class": r[1], "kind": r[2], "tags": r[3],
                     "summary": r[4], "created_at": r[5]}
            entry.update(self._staleness(r[1], r[5]))
            out.append(entry)
        return out

    # ---------- Read ----------

    def _touch(self, mem_ids: List[str]) -> None:
        if not mem_ids:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(self.sql_db_path) as conn:
            conn.executemany(
                "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
                [(now, m) for m in set(mem_ids)],
            )
            conn.commit()

    def _meta_sql(self, mem_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not mem_ids:
            return {}
        with sqlite3.connect(self.sql_db_path) as conn:
            q = ",".join("?" for _ in mem_ids)
            rows = conn.execute(
                f"SELECT id, summary, tags, kind, created_at, is_valid, class FROM memories WHERE id IN ({q})",
                mem_ids,
            ).fetchall()
        return {
            r[0]: {"summary": r[1], "tags": r[2], "kind": r[3], "created_at": r[4],
                   "valid": bool(r[5]), "class": r[6] or "semantic"}
            for r in rows
        }

    @staticmethod
    def _age_phrase(age_days: float) -> str:
        """Human words, not ISO: 'a raw timestamp doesn't trigger staleness
        reasoning the way '47 days ago' does' (Claude Code memoryAge.ts)."""
        d = int(age_days)
        if d <= 0:
            return "today"
        if d == 1:
            return "yesterday"
        return f"{d} days ago"

    @staticmethod
    def _staleness(mem_class: str, created_at: str) -> Dict[str, Any]:
        """Per-class staleness flags + drift caveat for recall output."""
        flags: Dict[str, Any] = {}
        try:
            age_days = (datetime.now() - datetime.fromisoformat(created_at)).total_seconds() / 86400
        except (TypeError, ValueError):
            return flags
        flags["age_days"] = round(age_days, 1)
        if age_days > DRIFT_CAVEAT_DAYS:
            # Aging is prompt text, never score decay.
            flags["drift_caveat"] = DRIFT_CAVEAT.format(age=MemoryStore._age_phrase(age_days))
        if mem_class == "operational" and age_days > OPERATIONAL_REVIEW_DAYS:
            flags["review_due"] = True
        elif mem_class == "semantic" and age_days > SEMANTIC_STALE_DAYS:
            flags["stale"] = True
        # episodic: never decays
        return flags

    def recall(self, query: str, layer: int = 0, top_n: int = 5,
               class_filter: str = "", rerank: bool = False) -> List[Dict[str, Any]]:
        if class_filter and class_filter not in MEMORY_CLASSES:
            return []
        self._apply_pending_l1()
        coll = self.summary_collection if layer == 1 else self.chunk_collection
        if coll.count() == 0:
            return []
        qemb = self.embedding_model.encode(query, normalize_embeddings=True).tolist()
        # over-fetch when filtering or reranking so post-filters still fill top_n
        wide = rerank or bool(class_filter)
        n = min(top_n * (10 if rerank else 3) if wide else top_n, coll.count())
        res = coll.query(query_embeddings=[qemb], n_results=n)
        out = []
        if not res["ids"] or not res["ids"][0]:
            return []
        metas = res["metadatas"][0]
        mem_ids = [m["memory_id"] for m in metas]
        sql = self._meta_sql(mem_ids)
        self._touch([m for m in mem_ids if sql.get(m, {}).get("valid")])
        seen_mem = set()
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], metas, res["distances"][0]):
            mid = meta["memory_id"]
            info = sql.get(mid, {})
            if info and not info["valid"]:
                continue  # hide tainted memories
            mem_class = info.get("class") or meta.get("class", "semantic")
            if class_filter and mem_class != class_filter:
                continue
            if rerank and mid in seen_mem:
                continue  # candidate set is per-memory (L2 granularity)
            entry = {
                "memory_id": mid,
                "chunk_id": cid,
                "layer": layer,
                "text": doc,
                "distance": round(float(dist), 4),
                "tags": meta.get("tags", ""),
                "kind": meta.get("kind", ""),
                "class": mem_class,
                "created_at": info.get("created_at", "?"),
                "memory_summary": info.get("summary", ""),
            }
            entry.update(self._staleness(mem_class, info.get("created_at", "")))
            # cross-show linked ground-truth ids (task 8)
            with sqlite3.connect(self.sql_db_path) as conn:
                gt = conn.execute(
                    "SELECT id, kind, title FROM ground_truth WHERE memory_id = ?", (mid,)
                ).fetchall()
            if gt:
                entry["ground_truth"] = [{"id": g[0], "kind": g[1], "title": g[2]} for g in gt]
            out.append(entry)
            seen_mem.add(mid)
            if len(out) >= (top_n * 4 if rerank else top_n):
                break

        if rerank and out:
            from reranker import rerank as _rr
            picked = _rr(query, out, top_n)
            if picked:
                by_mem: Dict[str, List[Dict[str, Any]]] = {}
                for e in out:
                    by_mem.setdefault(e["memory_id"], []).append(e)
                reordered = []
                for rank, mid in enumerate(picked):
                    for e in by_mem.get(mid, [])[:1]:  # best chunk per picked memory
                        e = dict(e)
                        e["reranked"] = True
                        e["rerank_position"] = rank
                        reordered.append(e)
                return reordered[:top_n]
                # picked == [] -> "nothing certain" or failure: keep vector order
        return out[:top_n]

    def context(self, query: str, top_n: int = 3, window: int = 1) -> List[Dict[str, Any]]:
        """Contextual L0 search: expand hits with neighbouring chunks (uuid#idx scheme)."""
        hits = self.recall(query, layer=0, top_n=top_n)
        if not hits:
            return []
        out = []
        for h in hits:
            mid, center = h["memory_id"], int(h["chunk_id"].split("#", 1)[1])
            wanted = [f"{mid}#{i}" for i in range(max(0, center - window), center + window + 1)]
            fetched = self.chunk_collection.get(ids=wanted, include=["documents"])
            docs_by_id = {cid: doc for cid, doc in zip(fetched["ids"], fetched["documents"])}
            # keep original chunk order, ascending index
            parts = [docs_by_id[cid] for cid in sorted(wanted, key=lambda c: int(c.split("#", 1)[1])) if cid in docs_by_id]
            h = dict(h)
            h["text"] = "\n\n...\n\n".join(parts)
            out.append(h)
        return out

    def stats(self) -> Dict[str, Any]:
        with sqlite3.connect(self.sql_db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            valid = conn.execute("SELECT COUNT(*) FROM memories WHERE is_valid = 1").fetchone()[0]
            kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall())
            classes = dict(conn.execute(
                "SELECT class, COUNT(*) FROM memories WHERE is_valid = 1 GROUP BY class").fetchall())
            ground = conn.execute("SELECT COUNT(*) FROM ground_truth").fetchone()[0]
        return {
            "memories_total": total,
            "memories_valid": valid,
            "kinds": kinds,
            "classes": classes,
            "ground_truth_rows": ground,
            "l0_chunks": self.chunk_collection.count(),
            "l1_summaries": self.summary_collection.count(),
        }

    # ---------- Ground truth (task 8) ----------

    def ground(
        self,
        title: str,
        payload: str,
        kind: str = "error-log",
        source: str = "",
        memory_id: str = "",
        class_: str = "",
    ) -> Dict[str, Any]:
        """File verbatim evidence (crash log, command output, config, snippet).

        class_ defaults derived from kind: config -> operational,
        snippet -> semantic, everything else -> episodic.
        """
        if kind not in ("bug-fix", "error-log", "command-output", "config", "snippet"):
            return {"status": "error", "error": "kind must be bug-fix|error-log|command-output|config|snippet"}
        if not class_:
            class_ = {"config": "operational", "snippet": "semantic"}.get(kind, "episodic")
        if class_ not in MEMORY_CLASSES:
            return {"status": "error", "error": f"class must be one of {MEMORY_CLASSES}"}
        if memory_id:
            with sqlite3.connect(self.sql_db_path) as conn:
                if not conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone():
                    return {"status": "error", "error": f"memory_id not found: {memory_id}"}
        gt_id = uuid.uuid4().hex[:12]
        # Ground truth is stored VERBATIM — so scan first. Redact-in-place
        # (crash logs keep diagnostic value) and flag the row with rule ids.
        # Only rule ids/labels are ever reported; matched text is not.
        payload, hits = redact_and_report(payload)
        title, title_hits = redact_and_report(title)
        hits = (hits + title_hits) or []
        with sqlite3.connect(self.sql_db_path) as conn:
            conn.execute(
                "INSERT INTO ground_truth (id, kind, class, title, payload, source, memory_id, secret_hit, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (gt_id, kind, class_, title, payload, source, memory_id or None,
                 " ".join(sorted({rid for rid, _ in hits})),
                 datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        result = {"status": "filed", "id": gt_id, "class": class_, "linked_memory": memory_id or None}
        if hits:
            result["redacted"] = [label for _rid, label in hits]
        return result

    def lookup(self, query: str, top_n: int = 5, class_filter: str = "") -> List[Dict[str, Any]]:
        """Exact-token FTS5 search across BOTH layers: ground-truth payloads
        AND memory summaries/tags. Answers "where does this identifier live?"
        in one call. layer='ground_truth' or 'memory' on each hit."""
        if class_filter and class_filter not in MEMORY_CLASSES:
            return []
        with sqlite3.connect(self.sql_db_path) as conn:
            q = """
                SELECT g.id, g.kind, g.class, g.title, g.source, g.memory_id, g.created_at,
                       snippet(ground_truth_fts, 1, '>>', '<<', '...', 24) AS excerpt,
                       g.secret_hit
                FROM ground_truth_fts
                JOIN ground_truth g ON g.rowid = ground_truth_fts.rowid
                WHERE ground_truth_fts MATCH ?
                ORDER BY bm25(ground_truth_fts) LIMIT ?
            """
            rows = conn.execute(q, (query, top_n * (3 if class_filter else 1))).fetchall()
            mq = """
                SELECT m.id, m.kind, m.class, m.summary, m.tags, m.created_at,
                       m.is_valid,
                       snippet(memories_fts, 0, '>>', '<<', '...', 24) AS excerpt
                FROM memories_fts
                JOIN memories m ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH ?
                ORDER BY bm25(memories_fts) LIMIT ?
            """
            mrows = conn.execute(mq, (query, top_n * (3 if class_filter else 1))).fetchall()
        out = []
        mem_ids = [r[5] for r in rows if r[5]]
        sql = self._meta_sql(mem_ids)
        for r in rows:
            if class_filter and r[2] != class_filter:
                continue
            entry = {
                "layer": "ground_truth",
                "id": r[0], "kind": r[1], "class": r[2], "title": r[3],
                "source": r[4], "memory_id": r[5], "created_at": r[6], "excerpt": r[7],
            }
            if r[5] and r[5] in sql:
                entry["memory_summary"] = sql[r[5]]["summary"]
            if r[8]:
                entry["secret_hit"] = r[8]  # rule ids only; content already redacted at write
            out.append(entry)
        for r in mrows:
            if class_filter and r[2] != class_filter:
                continue
            entry = {
                "layer": "memory",
                "id": r[0], "kind": r[1], "class": r[2], "summary": r[3],
                "tags": r[4], "created_at": r[5], "excerpt": r[7],
            }
            if not r[6]:
                entry["tainted"] = True  # superseded/forgotten; kept visible, flagged
            out.append(entry)
        # rank merged results by bm25 proximity: preserve each query's order,
        # interleave ground-truth first (verbatim evidence outranks summaries)
        gt = [e for e in out if e["layer"] == "ground_truth"][:top_n]
        mem = [e for e in out if e["layer"] == "memory"][:top_n]
        return (gt + mem)[: 2 * top_n]

    # ---------- Delete ----------

    def forget(self, memory_id: str, purge: bool = False) -> Dict[str, Any]:
        with sqlite3.connect(self.sql_db_path) as conn:
            row = conn.execute("SELECT id FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not row:
                return {"status": "not_found", "id": memory_id}
            if purge:
                conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            else:
                conn.execute("UPDATE memories SET is_valid = 0 WHERE id = ?", (memory_id,))
            conn.commit()
        if purge:
            self.chunk_collection.delete(where={"memory_id": memory_id})
            self.summary_collection.delete(where={"memory_id": memory_id})
            return {"status": "purged", "id": memory_id}
        return {"status": "tainted", "id": memory_id}
