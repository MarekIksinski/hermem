#!/usr/bin/env python3
"""hermem inspector — human browsing of the memory store.

Read-only: opens store/memories.sqlite in immutable/ro mode, so it is
safe to run while the MCP server owns Chroma. Never writes anything.

Usage:
  ./mem_inspect.py                    # manifest (like mem_index)
  ./mem_inspect.py show <id>          # full detail for one memory
  ./mem_inspect.py search <query>     # FTS across both layers (FTS5 syntax)
  ./mem_inspect.py gt [query]         # list/search ground-truth rows
  ./mem_inspect.py chains             # supersede chains (old -> new)
  ./mem_inspect.py stats              # counts + review_due/stale flags
"""
import sqlite3
import sys
from datetime import datetime

DB = "/home/marek/.hermes/mem/store/memories.sqlite"


def ro_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def age(created):
    try:
        d = (datetime.now() - datetime.fromisoformat(created)).days
        return f"{d}d"
    except Exception:
        return "?"


def flags(row_valid, cls, created):
    f = []
    if not row_valid:
        f.append("TAINTED")
    try:
        d = (datetime.now() - datetime.fromisoformat(created)).days
        if cls == "operational" and d > 14:
            f.append("REVIEW_DUE")
        if cls == "semantic" and d > 180:
            f.append("STALE")
    except Exception:
        pass
    return " ".join(f"<%s>" % x for x in f) if f else ""


def cmd_index(class_filter=""):
    c = ro_conn()
    q = ("SELECT id, class, kind, tags, summary, created_at, is_valid "
         "FROM memories WHERE is_valid=1")
    args = ()
    if class_filter:
        q += " AND class=?"
        args = (class_filter,)
    q += " ORDER BY created_at"
    for r in c.execute(q, args):
        print(f"{r[0]} [{r[1]:<11}] {age(r[5]):>4} {r[2]:<13} {r[4]}  {flags(r[6], r[1], r[5])}")


def cmd_show(mid):
    c = ro_conn()
    m = c.execute("SELECT id, summary, kind, class, tags, created_at, is_valid, "
                  "supersedes FROM memories WHERE id=?", (mid,)).fetchone()
    if not m:
        print(f"no memory {mid}")
        return
    print(f"id:        {m[0]}  {'TAINTED' if not m[6] else 'valid'}")
    print(f"class:     {m[3]}    kind: {m[2]}    created: {m[5]} ({age(m[5])} ago) {flags(m[6], m[3], m[5])}")
    print(f"tags:      {m[4]}")
    print(f"L2 summary: {m[1]}")
    if m[7]:
        print(f"supersedes: {m[7]}")
        for r in c.execute("SELECT id, is_valid, created_at, summary FROM memories "
                           "WHERE supersedes=?", (mid,)):
            print(f"  superseded by: {r[0]} ({'valid' if r[1] else 'TAINTED'}) {r[3][:70]}")
    print("\n-- L0 chunks (full stored text) --")
    # chunks live in chroma; read its sqlite read-only
    try:
        ch = sqlite3.connect("file:/home/marek/.hermes/mem/store/chroma_db/chroma.sqlite3?mode=ro", uri=True)
        rows = ch.execute(
            "SELECT e.embedding_id, d.string_value FROM embeddings e "
            "JOIN embedding_metadata d ON d.id=e.id AND d.key='chroma:document' "
            "WHERE e.embedding_id LIKE ?", (f"{mid}#%",)).fetchall()
        for cid, text in sorted(rows):
            print(f"\n[{cid}]\n{text}")
    except Exception as e:
        print(f"(chroma unreadable: {e})")
    gts = c.execute("SELECT id, kind, title, secret_hit, created_at FROM ground_truth "
                    "WHERE memory_id=?", (mid,)).fetchall()
    if gts:
        print("\n-- linked ground truth --")
        for g in gts:
            sec = f"  [secret_hit: {g[3]}]" if g[3] else ""
            print(f"  {g[0]} [{g[1]}] {g[2]} ({age(g[4])} ago){sec}")


def fts_query(query):
    """Same sanitizer as memory_store._fts_query: hyphens are NOT in FTS5;
    unicode61 splits stored text on dots/hyphens -> split+AND bare tokens."""
    import re
    if '"' in query:
        return query
    out = []
    for tok in query.split():
        if tok.upper() in ("AND", "OR", "NOT", "NEAR"):
            out.append(tok)
            continue
        if re.fullmatch(r"[A-Za-z0-9_]+\*?", tok):
            out.append(tok)
            continue
        wildcard = tok.endswith("*")
        pieces = [p for p in re.split(r"[^A-Za-z0-9_]+", tok) if p]
        if not pieces:
            continue
        if wildcard:
            pieces[-1] += "*"
        out.extend(f"{p} AND" for p in pieces[:-1])
        out.append(pieces[-1])
    return " ".join(out)


def cmd_search(query):
    c = ro_conn()
    query = fts_query(query)
    try:
        rows = c.execute(
            "SELECT m.id, m.class, m.summary, m.created_at, m.is_valid "
            "FROM memories_fts JOIN memories m ON m.rowid=memories_fts.rowid "
            "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT 15", (query,)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"FTS syntax error: {e}\n(hyphens need quotes: \"ims-open\"; bare '-' means NOT)")
        return
    rows2 = []
    try:
        rows2 = c.execute(
            "SELECT g.id, g.class, g.title, g.created_at, 'GT' FROM ground_truth_fts "
            "JOIN ground_truth g ON g.rowid=ground_truth_fts.rowid "
            "WHERE ground_truth_fts MATCH ? ORDER BY bm25(ground_truth_fts) LIMIT 15", (query,)).fetchall()
    except sqlite3.OperationalError:
        pass
    if not rows and not rows2:
        print("no hits")
    for r in rows:
        print(f"MEM {r[0]} [{r[1]:<11}] {age(r[3]):>4} {r[2][:90]}  {flags(r[4], r[1], r[3])}")
    for r in rows2:
        print(f"GT  {r[0]} [{r[1]:<11}] {age(r[3]):>4} {r[2]}")


def cmd_gt(query=""):
    c = ro_conn()
    if query:
        query = fts_query(query)
        rows = c.execute(
            "SELECT g.id, g.kind, g.class, g.title, g.memory_id, g.secret_hit, g.created_at, "
            "snippet(ground_truth_fts, 1, '>>', '<<', '...', 20) "
            "FROM ground_truth_fts JOIN ground_truth g ON g.rowid=ground_truth_fts.rowid "
            "WHERE ground_truth_fts MATCH ? ORDER BY bm25(ground_truth_fts) LIMIT 15", (query,)).fetchall()
        for r in rows:
            print(f"{r[0]} [{r[1]}/{r[2]}] {r[3]} -> mem {r[4] or '-'} ({age(r[6])} ago)")
            print(f"   {r[7]}")
    else:
        for r in c.execute("SELECT id, kind, class, title, memory_id, secret_hit, created_at "
                           "FROM ground_truth ORDER BY created_at"):
            sec = "  [secret_hit]" if r[5] else ""
            print(f"{r[0]} [{r[1]}/{r[2]}] {r[3]} -> mem {r[4] or '-'} ({age(r[6])} ago){sec}")


def cmd_chains():
    c = ro_conn()
    roots = c.execute("SELECT id, summary FROM memories WHERE supersedes IS NULL "
                      "AND id IN (SELECT supersedes FROM memories WHERE supersedes IS NOT NULL)").fetchall()
    if not roots:
        print("no supersede chains")
    for r in roots:
        print(f"{r[0]} {r[1][:70]}")
        cur, depth = r[0], 0
        while True:
            nxt = c.execute("SELECT id, is_valid, summary FROM memories WHERE supersedes=?",
                            (cur,)).fetchone()
            if not nxt:
                break
            depth += 1
            mark = "" if nxt[1] else " (tainted)"
            print("    " * depth + f"-> {nxt[0]} {nxt[2][:60]}{mark}")
            cur = nxt[0]


def cmd_stats():
    c = ro_conn()
    n = c.execute("SELECT count(*) FROM memories WHERE is_valid=1").fetchone()[0]
    t = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    gt = c.execute("SELECT count(*) FROM ground_truth").fetchone()[0]
    print(f"memories: {n} valid / {t} total   ground_truth: {gt}")
    for cls, cnt in c.execute("SELECT class, count(*) FROM memories WHERE is_valid=1 GROUP BY class"):
        print(f"  {cls:<12} {cnt}")
    due = c.execute("SELECT id, summary, created_at FROM memories WHERE is_valid=1 AND class='operational'").fetchall()
    now = datetime.now()
    line = [f"{r[0]} ({(now - datetime.fromisoformat(r[2])).days}d) {r[1][:50]}"
            for r in due if (now - datetime.fromisoformat(r[2])).days > 14]
    print(f"review_due (operational >14d): {len(line)}")
    for l in line:
        print(f"  {l}")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("index", "list"):
        cmd_index(args[1] if len(args) > 1 else "")
    elif args[0] == "show" and len(args) > 1:
        cmd_show(args[1])
    elif args[0] == "search" and len(args) > 1:
        cmd_search(" ".join(args[1:]))
    elif args[0] == "gt":
        cmd_gt(" ".join(args[1:]))
    elif args[0] == "chains":
        cmd_chains()
    elif args[0] == "stats":
        cmd_stats()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
