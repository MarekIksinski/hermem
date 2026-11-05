# Port list: Claude Code memory system -> hermem

Source: reconstructed Claude Code TS source at ~/Playground/claude-code
(analyzed 2026-11-05; key files: src/memdir/*, src/services/extractMemories/*,
src/services/SessionMemory/*, src/services/teamMemorySync/secretScanner.ts).
Memory saved in hermem as fc418eb5cfdb.

## Ranked mechanisms

### 1. Secret scanner for the ground-truth layer (do first)
Ground truth stores VERBATIM crash logs / configs / command outputs — exactly
where ghp_, AKIA, PEM blocks, DB URLs appear. Port the gitleaks-subset idea:
~35 high-confidence, prefix-anchored regexes (AWS AKIA|ASIA, GCP AIza,
Anthropic sk-ant-api03, OpenAI sk-...T3BlbkFJ, GitHub ghp_/ghu_/ghs_/github_pat_,
GitLab glpat-, Slack xox, Stripe sk_/rk_, npm_, hf_, Sentry, Grafana, PEM
PRIVATE KEY blocks...).
Two-layer gate:
  a) local write: redact-in-place (replace only the capture group, boundary
     chars survive) + a secret_hit flag on the row. Redact > refuse: crash
     logs keep diagnostic value (CC refuses only because team memory is
     SHARED; hermem is local).
  b) any export/sync/recovery boundary: refuse rows with hits.
Disclosure discipline: report ruleId + human label only, NEVER the matched
text (in tool results, logs, warnings).

### 2. Drift caveat on recall (cheap, immediate)
CC: memories >1 day old get an inline caveat: "This memory is N days old.
Memories are point-in-time observations, not live state — claims about code
behavior or file:line citations may be outdated. Verify against current code
before asserting as fact." Fresh memories get nothing ("warning there is
noise"). Age rendered as "47 days ago", NOT ISO timestamps — "models are poor
at date arithmetic; a raw ISO timestamp doesn't trigger staleness reasoning".
Aging NEVER enters relevance scoring — it is prompt text only.
Hermem: we already compute age_days; attach the caveat text to hits >1d old.

### 3. Write-path negative list (prompt-level, zero code)
Do NOT save what is derivable from current state: code patterns/architecture/
file paths, git history, debugging fix-recipes, anything in project docs,
ephemeral task state. Applies EVEN when the user explicitly asks to save —
"ask what was surprising or non-obvious about it — that is the part worth
keeping". Goes into mem_remember's description / the hermem skill.

### 4. Manifest-based dedup on write
Before writing, show the writer a manifest of existing memories:
"[class] id (47 days ago): L1 summary" — update/supersede beats duplicate.
CC proves a text manifest is enough; no vector dedup needed on the write
path. Hermem: mem_remember could return near-dupe candidates (L1 summary
similarity > threshold) in its response, or a mem_index() tool lists the
manifest.

### 5. Why / How-to-apply body structure
For operational + feedback-ish memories: lead with the rule/fact, then
"**Why:**" (the reason — incident, constraint, deadline) and
"**How to apply:**" (when it kicks in). The Why is what future-you uses to
judge whether a review_due operational memory is still load-bearing.

### 6. Absolute-date normalization at write time
"Thursday" -> "2026-11-12". Keeps the 14d/180d staleness arithmetic honest.
Could be enforced mechanically (flag relative dates in episodic/operational
content).

### 7. LLM re-rank on the READ path (later)
vector+FTS top-50 -> manifest "id [class] age: description" -> one small
structured-output call (P40 qwen3.5:9b fits) picks <=5. Guards CC learned:
validate returned ids against candidate set; filter already-surfaced ids
BEFORE the ranker; suppress reference-doc hits for tools currently in use
but KEEP gotcha/warning hits; session surfacing budget (~60KB) computed by
scanning live messages, so compaction auto-resets it.

### 8. Session-memory template (later, bigger)
Fixed-section markdown notes (Title / Current State / Task spec / Files /
Workflow / Errors & Corrections / Key results / Worklog), italic instruction
lines never edited, per-section token budgets with "MUST condense" reminders,
forked extractor with single-writable-path guard, dual-threshold trigger
(context growth >=5k tokens AND >=3 tool calls since last, prefer natural
breaks), cursor advances only on success, concurrent runs coalesce into one
trailing run, compaction fallback = re-count everything rather than disable.

## Anti-ported (deliberately)
- Four-type taxonomy (user/feedback/project/reference) — orthogonal to our
  episodic/semantic/operational classes; ours is about decay behavior, theirs
  about content domain. Keep ours; steal their write-path rubric instead.
- File-per-memory markdown + MEMORY.md index — our L1 layer already IS the
  index; SQLite+Chroma beats directory scanning at scale.
- Team sync/ETag/delta-push machinery — no multi-device sync in scope.

## Process note
P40-pinned subagent (qwen3.5:9b) produced accurate descriptive analysis but
hallucinated invalid SQL/Chroma syntax in its "portable code" section. Rule:
P40 children for read-and-report; parent verifies any code they propose.
