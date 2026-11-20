#!/usr/bin/env python3
"""hermem web UI — tiny read-only browser for the memory store.

stdlib only (http.server), binds 127.0.0.1. Reads SQLite + Chroma in
ro mode, safe to run while the MCP server owns the store.

Run:  .venv/bin/python webui.py [port]   (default 8765)
"""
import html
import re
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MEM = "/home/marek/.hermes/mem"
DB = f"{MEM}/store/memories.sqlite"
CHROMA = f"{MEM}/store/chroma_db/chroma.sqlite3"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def esc(s):
    return html.escape(str(s or ""))


def age(created):
    try:
        return (datetime.now() - datetime.fromisoformat(created)).days
    except Exception:
        return -1


def flags(valid, cls, created):
    f, d = [], age(created)
    if not valid:
        f.append("TAINTED")
    if cls == "operational" and d > 14:
        f.append("REVIEW_DUE")
    if cls == "semantic" and d > 180:
        f.append("STALE")
    return f


def fts_query(query):
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


CSS = """
body{background:#111418;color:#d8dee6;font:14px/1.5 'Segoe UI',system-ui,sans-serif;margin:0}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px} h1 a{color:#7aa2f7;text-decoration:none}
nav{margin:12px 0 20px} nav a{color:#8ab4f8;margin-right:14px;text-decoration:none}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{padding:6px 10px;border-bottom:1px solid #232830;text-align:left;vertical-align:top}
tr:hover{background:#171c22}
.id{font-family:monospace;color:#9ece6a;white-space:nowrap}
.cls{font-family:monospace;color:#bb9af7} .age{color:#7f8896;white-space:nowrap}
.tag{display:inline-block;background:#1e2530;border-radius:4px;padding:1px 7px;font-size:11px;margin-right:4px}
.flag{display:inline-block;border-radius:4px;padding:1px 7px;font-size:11px;margin-left:6px}
.TAINTED{background:#3a1d1d;color:#f7768e}
.REVIEW_DUE{background:#3a2e12;color:#e0af68}
.STALE{background:#2e1a3a;color:#c792ea}
pre{background:#171c22;border:1px solid #232830;border-radius:6px;padding:12px;white-space:pre-wrap;font-size:13px}
input[type=text]{background:#171c22;border:1px solid #2a3140;color:#d8dee6;padding:8px 12px;width:420px;border-radius:6px}
button{background:#7aa2f7;color:#111418;border:0;padding:8px 16px;border-radius:6px;cursor:pointer}
.chain{margin:8px 0;padding:10px;background:#171c22;border-radius:6px}
.arrow{color:#e0af68} .muted{color:#7f8896} a{color:#8ab4f8}
.kv{color:#7f8896;font-size:12px;margin:2px 0}
"""


def page(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>hermem · {esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap"><h1><a href="/">hermem</a> <span class="muted">memory browser (read-only)</span></h1>
<nav><a href="/">manifest</a><a href="/chains">chains</a><a href="/gt">ground truth</a><a href="/stats">stats</a></nav>
{body}</div></body></html>"""


def flag_spans(f):
    return "".join(f'<span class="flag {x}">{x}</span>' for x in f)


def mem_row(c, r, show_valid=True):
    mid, cls, kind, tags, summ, created, valid = r
    tags_html = "".join(f'<span class="tag">{esc(t)}</span>' for t in (tags or "").split(",") if t)
    return (f'<tr><td><a class="id" href="/show/{esc(mid)}">{esc(mid)}</a></td>'
            f'<td class="cls">{esc(cls)}</td><td>{esc(kind)}</td>'
            f'<td class="age">{age(created)}d</td>'
            f'<td>{esc(summ)} {flag_spans(flags(valid, cls, created))}<br>{tags_html}</td></tr>')


def view_index():
    c = ro(DB)
    rows = c.execute("SELECT id,class,kind,tags,summary,created_at,is_valid FROM memories "
                     "WHERE is_valid=1 ORDER BY created_at").fetchall()
    tainted = c.execute("SELECT count(*) FROM memories WHERE is_valid=0").fetchone()[0]
    head = ('<table><tr><th>id</th><th>class</th><th>kind</th><th>age</th><th>summary</th></tr>')
    return page("manifest", f'<form action="/search"><input type="text" name="q" placeholder="search (FTS: tokens, &quot;phrases&quot;, AND/OR)…"> <button>search</button></form>'
                + head + "".join(mem_row(c, r) for r in rows) +
                f"</table><p class='muted'>{len(rows)} valid · {tainted} tainted (hidden from recall, kept for audit)</p>")


def view_show(mid):
    c = ro(DB)
    m = c.execute("SELECT id,summary,kind,class,tags,created_at,is_valid,supersedes "
                  "FROM memories WHERE id=?", (mid,)).fetchone()
    if not m:
        return page("not found", f"<p>no memory <code>{esc(mid)}</code></p>")
    out = [f"<h2>{esc(m[1])}</h2>",
           f'<div class="kv"><span class="id">{esc(m[0])}</span> · class <span class="cls">{esc(m[3])}</span> · kind {esc(m[2])} · {age(m[5])}d ago {flag_spans(flags(m[6], m[3], m[5]))}</div>',
           f'<div class="kv">{"".join(f"<span class=\'tag\'>{esc(t)}</span>" for t in (m[4] or "").split(",") if t)}</div>']
    if m[7]:
        p = c.execute("SELECT id,is_valid,summary FROM memories WHERE id=?", (m[7],)).fetchone()
        if p:
            out.append(f'<div class="kv">supersedes → <a class="id" href="/show/{esc(p[0])}">{esc(p[0])}</a> {esc(p[2])[:70]}</div>')
    kids = c.execute("SELECT id,is_valid,summary FROM memories WHERE supersedes=?", (mid,)).fetchall()
    for k in kids:
        out.append(f'<div class="kv">superseded by → <a class="id" href="/show/{esc(k[0])}">{esc(k[0])}</a> {esc(k[2])[:70]}</div>')
    out.append("<h3>L0 chunks (verbatim)</h3>")
    try:
        ch = ro(CHROMA)
        for cid, text in ch.execute("SELECT e.embedding_id, d.string_value FROM embeddings e "
                                    "JOIN embedding_metadata d ON d.id=e.id AND d.key='chroma:document' "
                                    "WHERE e.embedding_id LIKE ? ORDER BY e.embedding_id", (f"{mid}#%",)):
            out.append(f'<div class="kv">{esc(cid)}</div><pre>{esc(text)}</pre>')
    except Exception as e:
        out.append(f"<p class='muted'>chroma unreadable: {esc(e)}</p>")
    gts = c.execute("SELECT id,kind,title,secret_hit FROM ground_truth WHERE memory_id=?", (mid,)).fetchall()
    if gts:
        out.append("<h3>linked ground truth</h3>")
        for g in gts:
            sec = ' <span class="flag TAINTED">secret_hit</span>' if g[3] else ""
            out.append(f'<div class="kv"><span class="id">{esc(g[0])}</span> [{esc(g[1])}] <a href="/gt?q={esc(g[2])}">{esc(g[2])}</a>{sec}</div>')
    return page("memory", "".join(out))


def view_search(q):
    if not q:
        return view_index()
    c = ro(DB)
    try:
        mem = c.execute("SELECT m.id,m.class,m.kind,m.tags,m.summary,m.created_at,m.is_valid "
                        "FROM memories_fts JOIN memories m ON m.rowid=memories_fts.rowid "
                        "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT 30", (fts_query(q),)).fetchall()
        gt = c.execute("SELECT g.id,g.title,g.kind,snippet(ground_truth_fts,1,'<b>','</b>','…',20) "
                       "FROM ground_truth_fts JOIN ground_truth g ON g.rowid=ground_truth_fts.rowid "
                       "WHERE ground_truth_fts MATCH ? ORDER BY bm25(ground_truth_fts) LIMIT 10", (fts_query(q),)).fetchall()
    except sqlite3.OperationalError as e:
        return page("search", f"<p class='muted'>FTS error: {esc(e)}</p>")
    out = [f'<form action="/search"><input type="text" name="q" value="{esc(q)}"> <button>search</button></form>']
    out.append(f"<p class='muted'>{len(mem)} memories · {len(gt)} ground-truth hits</p>")
    out.append('<table><tr><th>id</th><th>class</th><th>kind</th><th>age</th><th>summary</th></tr>'
               + "".join(mem_row(c, r) for r in mem) + "</table>")
    for g in gt:
        out.append(f'<div class="chain"><span class="id">{esc(g[0])}</span> [{esc(g[2])}] <b>{esc(g[1])}</b><br><pre>{g[3]}</pre></div>')
    return page("search", "".join(out))


def view_chains():
    c = ro(DB)
    roots = c.execute("SELECT id,summary FROM memories WHERE supersedes IS NULL "
                      "AND id IN (SELECT supersedes FROM memories WHERE supersedes IS NOT NULL)").fetchall()
    out = []
    for r in roots:
        line = f'<a class="id" href="/show/{esc(r[0])}">{esc(r[0])}</a> {esc(r[1])[:60]}'
        cur = r[0]
        while True:
            nxt = c.execute("SELECT id,is_valid,summary FROM memories WHERE supersedes=?", (cur,)).fetchone()
            if not nxt:
                break
            mark = "" if nxt[1] else ' <span class="flag TAINTED">tainted</span>'
            line += f'<br><span class="arrow">→</span> <a class="id" href="/show/{esc(nxt[0])}">{esc(nxt[0])}</a> {esc(nxt[2])[:60]}{mark}'
            cur = nxt[0]
        out.append(f'<div class="chain">{line}</div>')
    return page("chains", "".join(out) or "<p class='muted'>no chains</p>")


def view_gt(q=""):
    c = ro(DB)
    out = [f'<form action="/gt"><input type="text" name="q" value="{esc(q)}" placeholder="FTS over payloads…"> <button>search</button></form>']
    if q:
        try:
            rows = c.execute("SELECT g.id,g.kind,g.class,g.title,g.memory_id,g.secret_hit,g.created_at,"
                             "snippet(ground_truth_fts,1,'<b>','</b>','…',30) FROM ground_truth_fts "
                             "JOIN ground_truth g ON g.rowid=ground_truth_fts.rowid "
                             "WHERE ground_truth_fts MATCH ? ORDER BY bm25(ground_truth_fts) LIMIT 20", (fts_query(q),)).fetchall()
        except sqlite3.OperationalError as e:
            return page("gt", f"<p class='muted'>{esc(e)}</p>")
    else:
        rows = c.execute("SELECT id,kind,class,title,memory_id,secret_hit,created_at,'' FROM ground_truth "
                         "ORDER BY created_at").fetchall()
    for r in rows:
        sec = ' <span class="flag TAINTED">secret_hit</span>' if r[5] else ""
        link = f' → <a class="id" href="/show/{esc(r[4])}">{esc(r[4])}</a>' if r[4] else ""
        out.append(f'<div class="chain"><span class="id">{esc(r[0])}</span> [{esc(r[1])}/{esc(r[2])}] <b>{esc(r[3])}</b>{link} <span class="age">{age(r[6])}d</span>{sec}'
                   + (f"<pre>{r[7]}</pre>" if r[7] else "") + "</div>")
    return page("ground truth", "".join(out) or "<p class='muted'>empty</p>")


def view_stats():
    c = ro(DB)
    n = c.execute("SELECT count(*) FROM memories WHERE is_valid=1").fetchone()[0]
    t = c.execute("SELECT count(*) FROM memories").fetchone()[0]
    gt = c.execute("SELECT count(*) FROM ground_truth").fetchone()[0]
    l0 = 0
    try:
        l0 = ro(CHROMA).execute("SELECT count(*) FROM embeddings").fetchone()[0]
    except Exception:
        pass
    classes = c.execute("SELECT class,count(*) FROM memories WHERE is_valid=1 GROUP BY class").fetchall()
    due = [r for r in c.execute("SELECT id,summary,created_at FROM memories WHERE is_valid=1 AND class='operational'")
           if age(r[2]) > 14]
    kinds = c.execute("SELECT kind,count(*) FROM memories WHERE is_valid=1 GROUP BY kind ORDER BY 2 DESC").fetchall()
    body = [f"<p><b>{n}</b> valid / {t} total memories · {gt} ground-truth rows · {l0} L0 chunks</p>",
            "<p>" + " · ".join(f"{esc(k)}: {v}" for k, v in classes) + "</p>",
            "<table><tr><th>kind</th><th>count</th></tr>" + "".join(f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in kinds) + "</table>",
            f"<h3>review_due (operational &gt;14d): {len(due)}</h3>"]
    for r in due:
        body.append(f'<div class="chain"><a class="id" href="/show/{esc(r[0])}">{esc(r[0])}</a> {esc(r[1])[:70]} <span class="age">{age(r[2])}d</span></div>')
    return page("stats", "".join(body))


class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass

    def send(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                self.send(view_index())
            elif u.path == "/search":
                self.send(view_search(q.get("q", [""])[0]))
            elif u.path.startswith("/show/"):
                self.send(view_show(u.path.split("/show/")[1].strip("/")))
            elif u.path == "/chains":
                self.send(view_chains())
            elif u.path == "/gt":
                self.send(view_gt(q.get("q", [""])[0]))
            elif u.path == "/stats":
                self.send(view_stats())
            else:
                self.send(page("404", "<p>not found</p>"), 404)
        except Exception as e:
            self.send(page("error", f"<p class='muted'>{esc(e)}</p>"), 500)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"hermem web UI on http://127.0.0.1:{PORT}")
    srv.serve_forever()
