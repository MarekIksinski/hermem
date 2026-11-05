"""
reranker.py — optional P40 LLM re-rank for hermem recall (task 10g).

Ported from Claude Code's findRelevantMemories: vector search gets a wide
candidate net, then ONE small structured-output call over a cheap manifest
picks the actually-relevant subset. Guards copied from CC:
  - returned ids validated against the candidate set (hallucination guard)
  - ranker may return FEWER than top_n or an empty list ("certain only")
  - failure/timeout -> silently fall back to raw vector order
  - runs on the READ path only; write path stays LLM-free
"""

import json
import urllib.request
from typing import Any, Dict, List

OLLAMA_URL = "http://192.168.1.127:11434/api/chat"  # /api/generate takes 'prompt', not messages
MODEL = "qwen3.5:9b"

SYSTEM = (
    "You select which memory candidates are relevant to a query.\n"
    "Rules:\n"
    "- Pick ONLY memories you are certain will help answer or act on the query.\n"
    "- Return fewer rather than wrong: an empty list is a valid answer.\n"
    "- Warnings, gotchas and constraints about a topic count even if the\n"
    "  query looks routine; pure reference docs about things already in use\n"
    "  do not.\n"
    "- Reply with ONLY a JSON object: {\"selected\": [\"<id>\", ...]} using ids\n"
    "  exactly as given. No other text."
)


def rerank(query: str, candidates: List[Dict[str, Any]], top_n: int,
           timeout: int = 30) -> List[str]:
    """Return selected memory_ids (subset of candidates) or [] on no-fit/failure.

    candidates: dicts with memory_id, class, distance, memory_summary
    (L2 summary is the retrieval handle — the ranker sees summaries, not bodies).
    """
    if not candidates:
        return []
    manifest = "\n".join(
        f"{c['memory_id']} [{c.get('class','?')}] {c.get('memory_summary','')[:160]}"
        for c in candidates
    )
    prompt = f"Query: {query}\n\nCandidates:\n{manifest}\n\nPick up to {top_n}."
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        # Ollama structured outputs (not bare format:"json" — that makes 9B
        # models degenerate to empty responses). Schema enforces the shape.
        "structured": {
            "type": "object",
            "properties": {
                "selected": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["selected"],
        },
        "options": {"temperature": 0.0, "num_ctx": 8192},
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.load(resp).get("message", {}).get("content", "")
        picked = json.loads(raw).get("selected", [])
    except Exception:
        return []  # silent fallback to vector order
    valid = {c["memory_id"] for c in candidates}
    seen, out = set(), []
    for pid in picked:
        if pid in valid and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out[:top_n]
