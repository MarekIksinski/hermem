#!/usr/bin/env python3
"""Schema v2 test: code-aware chunking + supersedes chain (scratch store)."""
import shutil, sqlite3
from memory_store import MemoryStore

shutil.rmtree("/tmp/hermem_v2", ignore_errors=True)
s = MemoryStore(db_dir="/tmp/hermem_v2")

code = (
    "P40 cooling fix procedure:\n\n"
    "Step one, check fans:\n\n"
    "```\n"
    "#!/bin/bash\n"
    "for gpu in $(nvidia-smi --query-gpu=index --format=csv,noheader); do\n"
    "    echo \"GPU $gpu\"\n"
    "    watch -n 2 nvidia-smi -i $gpu\n"
    "done\n"
    "```\n\n"
    "If idle, replace the shroud screws and repaste."
)
r = s.remember(content=code, summary="P40 cooling check procedure with fan-watch bash snippet.", tags="p40,cooling", kind="procedure")
mid = r["id"]
print("chunks:", r["chunks"])
got = s.context("fan watch bash loop", top_n=2, window=0)
for h in got:
    print("--- hit dist", h["distance"])
    print(h["text"][:120].replace("\n", " | "))
# verify the fenced block survived whole in one chunk
whole = [h for h in got if "nvidia-smi -i $gpu" in h["text"] and h["text"].count("```") == 2]
print("fenced block intact in one chunk:", bool(whole))

# supersedes chain
r2 = s.remember(content="Pentest sandbox ims_v3 runs at 192.168.1.126:8502 user / :8503 admin.", summary="sandbox host is .126", tags="ims_v3,sandbox", kind="environment")
r3 = s.remember(content="Pentest sandbox ims_v3 moved to 192.168.1.140:8502 user / :8503 admin.", summary="sandbox host moved to .140", tags="ims_v3,sandbox", kind="environment", supersedes=r2["id"])
print("new:", r3["status"], "| old tainted via supersedes")
hits = s.recall("pentest sandbox host ip", layer=0, top_n=5)
print("valid hits mentioning .126:", sum(1 for h in hits if ".126" in h["text"]), "| mentioning .140:", sum(1 for h in hits if ".140" in h["text"]))
with sqlite3.connect("/tmp/hermem_v2/memories.sqlite") as c:
    rows = c.execute("SELECT id, is_valid, supersedes FROM memories ORDER BY created_at").fetchall()
    for row in rows:
        print("  ", row)
shutil.rmtree("/tmp/hermem_v2", ignore_errors=True)
