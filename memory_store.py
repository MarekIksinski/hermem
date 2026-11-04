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

DEFAULT_DB_DIR = "/home/marek/.hermes/mem/store"


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
    ) -> Dict[str, Any]:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.sql_db_path) as conn:
            dup = conn.execute(
                "SELECT id FROM memories WHERE content_hash = ? AND is_valid = 1",
                (content_hash,),
            ).fetchone()
            if dup:
                return {"status": "duplicate", "id": dup[0]}

            mem_id = uuid.uuid4().hex[:12]
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO memories (id, summary, content_hash, tags, kind, created_at, last_accessed_at, supersedes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mem_id, summary, content_hash, tags, kind, now, now, supersedes or None),
            )
            if supersedes:
                conn.execute("UPDATE memories SET is_valid = 0 WHERE id = ?", (supersedes,))
            conn.commit()

        chunks = _split_chunks(content)
        l1 = [_first_sentence(c) for c in chunks]

        ids = [f"{mem_id}#{i}" for i in range(len(chunks))]
        meta = [{"memory_id": mem_id, "tags": tags, "kind": kind, "level": 0} for _ in chunks]
        emb0 = self.embedding_model.encode(chunks, normalize_embeddings=True).tolist()
        self.chunk_collection.add(embeddings=emb0, documents=chunks, metadatas=meta, ids=ids)

        sids = [f"{mem_id}#s{i}" for i in range(len(l1))]
        smeta = [{"memory_id": mem_id, "tags": tags, "kind": kind, "level": 1, "l0_chunk_id": ids[i],
                  "l1_state": "standin"} for i in range(len(l1))]
        emb1 = self.embedding_model.encode(l1, normalize_embeddings=True).tolist()
        self.summary_collection.add(embeddings=emb1, documents=l1, metadatas=smeta, ids=sids)

        return {"status": "stored", "id": mem_id, "chunks": len(chunks)}

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
                f"SELECT id, summary, tags, kind, created_at, is_valid FROM memories WHERE id IN ({q})",
                mem_ids,
            ).fetchall()
        return {
            r[0]: {"summary": r[1], "tags": r[2], "kind": r[3], "created_at": r[4], "valid": bool(r[5])}
            for r in rows
        }

    def recall(self, query: str, layer: int = 0, top_n: int = 5) -> List[Dict[str, Any]]:
        self._apply_pending_l1()
        coll = self.summary_collection if layer == 1 else self.chunk_collection
        if coll.count() == 0:
            return []
        qemb = self.embedding_model.encode(query, normalize_embeddings=True).tolist()
        res = coll.query(query_embeddings=[qemb], n_results=min(top_n, coll.count()))
        out = []
        if not res["ids"] or not res["ids"][0]:
            return []
        metas = res["metadatas"][0]
        mem_ids = [m["memory_id"] for m in metas]
        sql = self._meta_sql(mem_ids)
        self._touch([m for m in mem_ids if sql.get(m, {}).get("valid")])
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], metas, res["distances"][0]):
            mid = meta["memory_id"]
            info = sql.get(mid, {})
            if info and not info["valid"]:
                continue  # hide tainted memories
            out.append({
                "memory_id": mid,
                "chunk_id": cid,
                "layer": layer,
                "text": doc,
                "distance": round(float(dist), 4),
                "tags": meta.get("tags", ""),
                "kind": meta.get("kind", ""),
                "created_at": info.get("created_at", "?"),
                "memory_summary": info.get("summary", ""),
            })
        return out

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
        return {
            "memories_total": total,
            "memories_valid": valid,
            "kinds": kinds,
            "l0_chunks": self.chunk_collection.count(),
            "l1_summaries": self.summary_collection.count(),
        }

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
