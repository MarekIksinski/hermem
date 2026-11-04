#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l1_worker.py — P40 background enrichment for hermem L1 summaries.

Runs as a SEPARATE process from the MCP server (which owns Chroma).
Loop:
  1. read stand-in L1 rows straight from Chroma's sqlite (read-only)
  2. for each, fetch its L0 chunk text, ask Ollama (P40) for a real summary
  3. queue the result in the 'l1_upgrades' sidecar table of memories.sqlite
The MCP server applies the queue into Chroma on its next recall/stats call.

Usage: .venv/bin/python l1_worker.py [--once] [--model qwen3.5:9b]
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

MEM_DIR = "/home/marek/.hermes/mem/store"
OLLAMA_URL = "http://192.168.1.127:11434/api/generate"
PROMPT = (
    "Summarize the following memory note in ONE sentence (max 30 words), "
    "no preamble, keep all names, paths and numbers exact:\n\n{chunk}"
)


def _ro_conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{MEM_DIR}/chroma_db/chroma.sqlite3?mode=ro", uri=True)


def ollama_summarize(model: str, chunk: str) -> str:
    payload = {
        "model": model,
        "prompt": PROMPT.format(chunk=chunk[:3000]),
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1},
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp).get("response", "").strip()


def pending_l1_rows(limit: int = 20) -> list:
    """L1 rows with l1_state standin/missing, read straight from chroma sqlite."""
    con = _ro_conn()
    try:
        return con.execute(
            f"""
            SELECT e.embedding_id, m.string_value
            FROM collections AS c
            JOIN segments AS s ON s.collection = c.id AND s.scope = 'METADATA'
            JOIN embeddings AS e ON e.segment_id = s.id
            JOIN embedding_metadata AS m ON m.id = e.id AND m.key = 'chroma:document'
            LEFT JOIN embedding_metadata AS st ON st.id = e.id AND st.key = 'l1_state'
            WHERE c.name = 'mem_chunk_summaries'
              AND (st.string_value = 'standin' OR st.string_value IS NULL)
            LIMIT {int(limit)}
            """
        ).fetchall()
    finally:
        con.close()


def l0_text(chunk_id: str) -> str:
    """The L0 chunk text for an L1 row (chunk_id 'mem#s3' -> L0 'mem#3')."""
    l0_id = chunk_id.replace("#s", "#")
    con = _ro_conn()
    try:
        row = con.execute(
            """
            SELECT m.string_value
            FROM collections AS c
            JOIN segments AS s ON s.collection = c.id AND s.scope = 'METADATA'
            JOIN embeddings AS e ON e.segment_id = s.id
            JOIN embedding_metadata AS m ON m.id = e.id AND m.key = 'chroma:document'
            WHERE c.name = 'mem_chunks' AND e.embedding_id = ?
            """,
            (l0_id,),
        ).fetchone()
        return row[0] if row else ""
    finally:
        con.close()


QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS l1_upgrades (
    chunk_id TEXT PRIMARY KEY,
    new_text TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
)
"""


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"{MEM_DIR}/memories.sqlite")
    conn.execute(QUEUE_DDL)
    conn.commit()
    return conn


def queue_upgrade(chunk_id: str, new_text: str) -> None:
    with _mem_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO l1_upgrades (chunk_id, new_text, created_at) VALUES (?, ?, ?)",
            (chunk_id, new_text, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="process one batch and exit")
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--interval", type=int, default=300, help="seconds between batches")
    args = ap.parse_args()

    print(f"l1_worker: model={args.model} ollama={OLLAMA_URL}", flush=True)
    while True:
        with _mem_conn() as conn:
            queued = {r[0] for r in conn.execute("SELECT chunk_id FROM l1_upgrades")}
        rows = [r for r in pending_l1_rows() if r[0] not in queued]
        if not rows:
            if args.once:
                print("no pending L1 rows", flush=True)
                return
            time.sleep(args.interval)
            continue
        for chunk_id, _l1_text in rows:
            chunk = l0_text(chunk_id)
            if not chunk:
                continue
            try:
                summary = ollama_summarize(args.model, chunk)
            except Exception as e:
                print(f"  ! {chunk_id}: {e}", file=sys.stderr, flush=True)
                continue
            if summary:
                queue_upgrade(chunk_id, summary)
                print(f"  + {chunk_id}: {summary[:70]}", flush=True)
        if args.once:
            return


if __name__ == "__main__":
    main()
