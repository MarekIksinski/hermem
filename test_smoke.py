#!/usr/bin/env python3
"""Smoke test for MemoryStore: write, dedupe, recall L0/L1, context, stats, forget."""
import time

t0 = time.time()
from memory_store import MemoryStore

s = MemoryStore()
print(f"init: {time.time()-t0:.1f}s")

r1 = s.remember(
    content=(
        "The hermem project adapts the HierarchicalRAG engine into an MCP memory tool for Hermes.\n\n"
        "Decisions: store lives at ~/.hermes/mem, CPU-only embeddings with all-MiniLM-L6-v2, "
        "no Ollama on the write path. The original HierarchicalRAG chatbot folder must stay "
        "untouched; work happens in the copy HierarchicalRAG-mem.\n\n"
        "The P40 at 192.168.1.127 runs Ollama and is reserved for a possible v2 background "
        "worker that rewrites L1 chunk one-liners into real summaries using gemma."
    ),
    summary="hermem project core decisions: store location, CPU-only embeddings, untouched original chatbot, P40 reserved for v2 L1 enrichment.",
    tags="hermem,architecture",
    kind="project",
)
print("remember:", r1)

r2 = s.remember(
    content=(
        "Marek's RTX 3060 12GB VRAM is fully committed to the Strata engine serving Hermes "
        "itself on 127.0.0.1:8085. Any local GPU work (CUDA, embeddings, spaCy GPU) will OOM. "
        "Keep new tools CPU-only."
    ),
    summary="3060 VRAM is 100% committed to Strata serving Hermes; new tools must be CPU-only.",
    tags="hardware,constraint",
    kind="environment",
)
print("remember:", r2)

dup = s.remember(
    content=(
        "Marek's RTX 3060 12GB VRAM is fully committed to the Strata engine serving Hermes "
        "itself on 127.0.0.1:8085. Any local GPU work (CUDA, embeddings, spaCy GPU) will OOM. "
        "Keep new tools CPU-only."
    ),
    summary="dup test",
    tags="x",
    kind="note",
)
print("dup:", dup)

t1 = time.time()
hits = s.recall("where does the memory database live", layer=1, top_n=3)
print(f"recall L1 ({time.time()-t1:.2f}s):")
for h in hits:
    print("  ", h["memory_id"], h["distance"], "|", h["text"][:80])

hits0 = s.recall("GPU out of memory", layer=0, top_n=2)
print("recall L0:")
for h in hits0:
    print("  ", h["memory_id"], h["distance"], "|", h["text"][:80])

ctx = s.context("reserved for v2 background worker", top_n=1, window=1)
print("context expansion:")
for h in ctx:
    print("  blocks joined:", h["text"].count("...") + 1, "| len:", len(h["text"]))

print("stats:", s.stats())

target = dup["id"] if dup["status"] == "duplicate" else r2["id"]
f = s.forget(target)
print("forget:", f)
hidden = s.recall("RTX 3060 VRAM committed Strata", layer=0, top_n=5)
print("after taint, L0 hits mentioning 3060:", sum(1 for h in hidden if "3060" in h["text"]))
