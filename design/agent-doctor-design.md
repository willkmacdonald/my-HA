# Agent Doctor: Conversational Observability for Copilot Studio Agents

**Status:** Design — v1.0 (design session output)
**Working name:** agent doctor
**Date:** 2026-07-11

This document responds to the design brief: it challenges the proposed
architecture where it deserves challenging, answers the five open questions,
and lays out a phased build plan. Platform facts were verified against
Microsoft Learn docs and the referenced repos as of July 2026; corrections to
the brief's assumptions are marked **⚠**, and things that must be verified
in-tenant are collected in §8.

---

## 1. Summary of the design position

The thesis in the brief is right and worth protecting: **the product is the
judgment layer, not the data layer.** Everything below is organized to keep
diagnosis cost near zero while avoiding two traps: (a) rebuilding a dashboard
with a chat skin, and (b) baking analysis into server-side tools so the LLM
becomes a formatter instead of a diagnostician.

The main changes proposed versus the brief:

1. **Split the tool surface into primitives + digests, not analyses.**
   `get_failure_clusters` as an MCP tool is a category error — clustering
   failures *is* the judgment the LLM should be doing. The server's job is
   compression without judgment: deterministic per-session digests the LLM
   can triage in bulk, plus full-transcript drill-down for sessions it picks.
2. **v1 runs Claude-side, not as a Copilot Studio agent.** The doctor's
   quality lives in a long diagnostic prompt over large context. Iterate that
   where iteration is cheapest. The Copilot Studio front end is a v2
   packaging decision, not a v1 architecture decision — and native MCP
   onboarding means the same server serves both (§2.1).
3. **Skip Fabric/Synapse for v1 — and probably v2.** The cheapest retention
   lever turns out to be Dataverse itself: the 30-day limit is just a
   recurring bulk-delete job you can replace with a longer one (§4.3).
   Longitudinal signal beyond that comes from eval runs and (if ever needed)
   digest snapshots into your existing Postgres, not a lakehouse.
4. **Make the doctor gradeable from day one.** The fix-type taxonomy
   (instruction gap / knowledge gap / routing gap / tool failure) is a
   classification scheme — so diagnosis quality can be measured with
   seeded-failure test cases, and recommendations validated by the
   apply-fix → rerun-eval → compare loop (§4.4).
5. **One new dependency the brief missed: enhanced transcripts.** The
   tool-invocation and knowledge-search detail the doctor needs for two of
   its four fix-type buckets only lands in transcripts when "include
   node-level details" is enabled in agent settings (§2.2). Turning that on
   is a Phase 0 prerequisite, not an optimization.

---

## 2. Verified platform facts (and what they change)

### 2.1 Integration surface — confirmed, with specifics

- Copilot Studio consumes MCP servers natively: Tools → Add a tool → Model
  Context Protocol; **streamable HTTP only** (SSE support was dropped after
  Aug 2025). Auth options: none, API key, or OAuth 2.0 (including dynamic
  client registration). Generative orchestration must be on; DLP policies
  apply because MCP rides the connector layer.
  ([agent-extend-action-mcp](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agent-extend-action-mcp),
  [mcp-add-existing-server-to-agent](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent))
- Only tools (and tool-output resources) are supported — no MCP prompts, no
  sampling. **Design consequence:** the diagnostic prompt cannot ship as an
  MCP prompt for the v2 Copilot Studio front end; it has to live in that
  agent's instructions. One more reason the prompt is a versioned artifact in
  this repo, not something embedded in the server.
- Tool schema quirks to respect in the FastMCP server: no `$ref`/reference
  inputs (tools get silently filtered out), enums treated as strings, avoid
  `exclusiveMinimum` on integers.
  ([mcp-troubleshooting](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-troubleshooting))

### 2.2 Transcript data — confirmed shape, three corrections

- `conversationtranscript.content` (Memo, max 1,048,576 chars) is a JSON
  array of Bot Framework-style activities. **⚠ It is plain JSON text, not
  base64** — the base64 quirk belongs to the adjacent Dynamics 365 Customer
  Service Copilot tables (`msdyn_copilottranscriptdata` etc.); don't confuse
  the two data models.
  ([conversationtranscript reference](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/conversationtranscript),
  [download-copilot-transcript-data](https://learn.microsoft.com/en-us/dynamics365/customer-service/develop/download-copilot-transcript-data))
- The analytics markers the digest layer needs are `valueType`-tagged
  activities: `SessionInfo` (outcome = Resolved | Escalated | Abandon, turn
  count, start/end), `IntentRecognition` (topic + confidence),
  `ConversationInfo` (including `isDesignMode` — **filter test-pane sessions
  with this**), `CSATSurveyResponse`, `VariableAssignment`, `DialogRedirect`.
  Timestamps are epoch seconds; `from.role` distinguishes user/agent; user
  IDs are hashed; topics are referenced by GUID (join `botcomponent` for
  names).
  ([analytics-transcripts-powerapps](https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-transcripts-powerapps),
  [mcscat "Open the Hood"](https://microsoft.github.io/mcscatblog/posts/open-the-hood-copilot-studio-transcripts/))
- **Tool-invocation and knowledge-search detail (which sources were
  searched, node-level traces) only appears with "enhanced transcripts"
  enabled** (agent Settings → Advanced → include node-level details), which
  adds `nodeTraceData` activities. Without it, the doctor is blind on the
  tool-failure and knowledge-gap buckets. Phase 0 prerequisite.
- Records over 1MB split into rows sharing `Name` + `ConversationStartTime`
  with differing `Metadata.BatchId` — group, sort by BatchId, concatenate.
  Transcripts are written **after ~30 minutes of session inactivity** (not
  merely "sometimes delayed") — the doctor's data horizon is roughly
  "sessions that ended half an hour ago or earlier."
- **⚠ `msdyn_botsession` is not in Microsoft's documented analytics table
  set** (`bot`, `botcomponent`, `conversationtranscript`). Session outcomes
  live inside `Content` as `SessionInfo` activities. Verify whether the table
  even exists in the environment (Phase 0); design as if it doesn't.
  ([custom-analytics-strategy](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/custom-analytics-strategy))
- SharePoint-knowledge redaction confirmed, with useful nuance: the
  question and the retrieved `search_results` survive; the generated
  **answer** is replaced with `REDACTED`. The digest should flag these
  sessions so the doctor reports a blind spot instead of guessing.

### 2.3 Query mechanics

- `content` is an opaque Memo column — Dataverse has no JSON-path filtering,
  so **all transcript parsing is client-side** in the server. OData filters
  work on `conversationstarttime` and the `bot` lookup; page with
  `Prefer: odata.maxpagesize` (keep pages ~50–200 when selecting `content`)
  and follow `@odata.nextLink`.
- Hard-enforced service protection: 6,000 requests / 20 min execution / 52
  concurrent per 5-minute sliding window, HTTP 429 with `Retry-After` — the
  client honors it. Daily entitlement for app users is a pooled tenant
  allocation (25k/day base, soft-enforced) — far above the doctor's needs
  once digests are cached.
  ([api-limits](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/api-limits),
  [api-request-limits-allocations](https://learn.microsoft.com/en-us/power-platform/admin/api-request-limits-allocations))
- Server-side aggregates cap at 50k records — irrelevant here since
  aggregation happens over parsed digests anyway.

### 2.4 Evaluations — confirmed, with three caveats that shape §4.4

- Evaluations are GA; test sets up to **100 cases**, 7 test methods
  including a **Custom LLM-judge with your own instructions and pass/fail
  labels** — which is exactly the hook the fix-verification loop needs.
- The REST API (`api.powerplatform.com/copilotstudio/environments/{env}/bots/{bot}/api/makerevaluation/...`)
  can **list test sets, start runs, and read per-case results** — but it is
  (a) flagged *preview* even though the feature is GA, (b) **cannot create
  test sets** (author them in Studio first), and (c) allows one run at a
  time, with results retained 89 days.
  ([analytics-agent-evaluation-rest-api](https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-agent-evaluation-rest-api))
- **Design consequence:** `run_evaluation` / `compare_eval_runs` MCP tools
  are straightforward; a `create_test_set` tool is not possible today. The
  doctor can *recommend* new test cases but a human adds them in Studio.
  Native "Themes → Evaluate" partially fills this gap in-product (§2.6).

### 2.5 Analytics APIs and retention — confirmed

- **No API exposes the Analytics-page KPIs.** Dataverse transcripts (or App
  Insights telemetry) are the only fully programmatic path — the MCP server
  is not duplicating an existing API, it's building the only one.
- Retention as briefed: analytics aggregates ~360 days; session/transcript
  detail in-product 28 days; Dataverse default 30 days via the recurring
  bulk-delete job "Bulk Delete Conversation Transcript Records Older Than 1
  Month" — **which can simply be canceled and replaced with a longer job**
  (e.g., 12 months, filtered to `SchemaType = powervirtualagents`). That's
  the retention lever, and it changes the answer in §4.3.

### 2.6 Build-vs-wait: what Microsoft shipped natively in 2025–26

Microsoft is moving in this direction: **Themes** (AI-clustered user
questions with one-click test-set generation), AI Summary cards, custom
metrics (preview), an agent status/readiness page, and Monitor-tab error
triage. Two reasons this doesn't obsolete the doctor:

1. Everything above is **in-product UX** — none of it is conversational, none
   of it is API-readable, and none of it crosses from *pattern* to *causal
   diagnosis with a recommended edit*. The gap the brief identifies (judgment,
   fix-type attribution, near-zero diagnosis cost) is still open.
2. But it does shape scope: **don't build clustering-of-user-questions as a
   differentiator** (Themes does that), and treat the doctor's eval
   integration as complementary to Themes→Evaluate rather than competing.

The build-vs-wait risk concentrates on one future possibility: Microsoft
shipping a native conversational "ask your analytics" agent. If that arrives,
the doctor's remaining moat is the diagnostic method (fix-type taxonomy,
confirm-from-transcripts, suppress-by-default) and Claude-side operation —
which is also the part that transfers to any other agent platform.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Front ends                                                 │
│   v1: Claude Desktop/Code (maker-facing)                    │
│   v2: Copilot Studio "agent doctor" agent (owner-facing)    │
│       — native MCP tool onboarding, streamable HTTP         │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP (streamable HTTP)
┌──────────────────────────▼──────────────────────────────────┐
│  agent-doctor MCP server (Python / FastMCP)                 │
│                                                             │
│  Tier 1 — primitives (thin, deterministic)                  │
│    list_agents, list_sessions, get_transcript,              │
│    get_feedback, list_eval_runs, run_evaluation,            │
│    get_eval_run, compare_eval_runs                          │
│                                                             │
│  Tier 2 — digests (compression WITHOUT judgment)            │
│    get_session_digests(window, filters)                     │
│    get_agent_snapshot(window)                               │
│                                                             │
│  Internals: OData paging + 429/Retry-After handling,        │
│  BatchId reassembly, activity parsing (valueType markers,   │
│  nodeTraceData), isDesignMode filtering, topic-GUID →       │
│  botcomponent name join, digest cache                       │
└────────────┬───────────────────────────────┬────────────────┘
             │ Dataverse Web API             │ Power Platform API
             │ (S2S app user, MSAL)          │ (makerevaluation)
┌────────────▼────────────────┐  ┌───────────▼────────────────┐
│ conversationtranscript,     │  │ eval test sets, runs,      │
│ bot, botcomponent           │  │ per-case results           │
└─────────────────────────────┘  └────────────────────────────┘
```

### 3.1 The two-tier tool surface (the core design decision)

The brief proposes tools like `get_failure_clusters`. The problem: whoever
writes that server code has to decide what a "failure cluster" is — and that
decision is exactly the triage-with-judgment the brief says is the LLM's job.
Server-side clustering gives you a dashboard widget returned as JSON. (Troy
Taylor's connector illustrates the ceiling of that approach: sensible
aggregate operations, but the output is counts and rates — reporting, not
diagnosis.)

**Tier 1 — primitives.** Thin, deterministic wrappers: sessions in a window,
one reassembled transcript, feedback records, eval runs. No opinions.

**Tier 2 — digests.** The token-economics layer. A month of traffic might be
hundreds of sessions × tens of KB of transcript — unfeedable. The server
deterministically extracts a compact per-session digest:

```json
{
  "session_id": "…",
  "started": "2026-07-09T14:02:11Z", "duration_s": 312, "turn_count": 9,
  "outcome": "Escalated",
  "topics": ["Fallback", "OrderStatus"],
  "tools_called": [{"name": "LookupOrder", "ok": false, "error": "timeout"}],
  "knowledge_sources_hit": ["sharepoint:PolicyDocs"],
  "first_user_utterance": "why was my order cancelled and when will…",
  "csat": null, "reactions": {"up": 0, "down": 1},
  "flags": ["fallback_triggered", "tool_error", "redacted_answers"]
}
```

Everything in the digest is *extractable without an LLM*: outcome and turns
from `SessionInfo`, topics from `IntentRecognition` (+ `botcomponent` join),
tool and knowledge detail from `nodeTraceData` (enhanced transcripts), CSAT
from survey activities, plus cheap heuristic `flags` that are hints, not
verdicts. Test-pane sessions are dropped via `isDesignMode`. 200 sessions of
digests fit in one context window. The LLM does the clustering — "18 of 23
escalations opened with multi-part questions" — then drills into 3–5 full
transcripts via Tier 1 to confirm the causal story before diagnosing.

This is the same shape as Open Brain's eval flow: retrieval is mechanical,
compression is mechanical, *judgment is the model's*.

**What deliberately doesn't exist:** `diagnose_agent`, `get_root_causes`,
`get_failure_clusters`. If the server can compute it deterministically it's a
digest field; if it requires judgment it belongs to the model.

### 3.2 The diagnostic prompt (the actual product)

The system prompt encodes the method:

1. **Triage** — pull the snapshot + digests; identify what's *out of normal
   range* and suppress everything else. "Normal" is defined against the
   agent's own trailing baseline, not absolute thresholds.
2. **Cluster** — group anomalous sessions by shared characteristics from
   digests alone.
3. **Confirm** — read full transcripts for a small sample per cluster. Never
   diagnose from digests alone; digests locate, transcripts explain.
4. **Categorize** — every finding lands in exactly one fix-type bucket:
   - **Instruction gap** — agent had the data and tools, behaved wrong
   - **Knowledge gap** — right behavior, missing/wrong/redacted content
   - **Routing gap** — wrong topic triggered / fallback storms
   - **Tool failure** — connector/action errors, timeouts, bad inputs
5. **Recommend** — end every diagnosis with an edit the maker can make,
   quoted concretely ("add to instructions: *when a question contains
   multiple parts, answer each part separately*"), plus how to verify it
   (which eval cases should flip).
6. **Suppress** — if nothing is out of range, say "nothing worth your
   attention" and stop. The doctor's credibility is spent every time it
   manufactures a finding.
7. **Declare blind spots** — redacted answers, disabled enhanced
   transcripts, channels without reactions, and the ~30-minute write horizon
   are stated, not papered over.

The fix-type taxonomy is load-bearing twice over: it forces recommendations
to be actionable (each bucket maps to a thing the maker can edit), and it
makes the doctor's output *gradeable* (§4.4).

Because Copilot Studio doesn't support MCP prompts (§2.1), this prompt lives
in the repo as a versioned file, deployed as Claude context in v1 and as the
doctor agent's instructions in v2.

### 3.3 Front end sequencing

**v1: Claude Desktop/Code over the MCP server.** Reasons:
- The diagnostic prompt needs many iterations; Claude-side you edit a file.
  Inside Copilot Studio you'd be tuning instructions through the same opaque
  orchestrator you're trying to diagnose — debugging two agents at once.
- Diagnosis wants a big context window and strong long-document reasoning;
  triage-over-200-digests-then-read-5-transcripts is exactly the workload
  frontier models handle and orchestrated topic-based agents handle poorly.
- The v1 user is you (the maker). The "business owner" persona in the brief
  is real, but she's the v2 audience; don't pay her constraints yet.

**v2: the same MCP server attached to a Copilot Studio doctor agent**
(native MCP onboarding, streamable HTTP, API-key or OAuth auth) for
owner-facing access — after the prompt is proven. The server doesn't change;
that's the point of putting the judgment in the prompt and the mechanics in
the server. Expect v2 to need prompt compression (Copilot Studio instruction
limits and orchestrator behavior differ from Claude) — treat it as a port,
with the seeded-failure evals (§4.4) as the regression gate.

---

## 4. Answers to the open questions

### 4.1 Auth model → **service principal (application user), not delegated**

Entra app registration + Dataverse **application user** with a **custom
security role** granting org-scope Read on `conversationtranscript`, `bot`,
`botcomponent` (least privilege; no OOB admin roles). `conversationtranscript`
is user-owned, so org-scope read is what lets the app user see all agents'
transcripts. MSAL client-credentials in Python, scope
`https://{org}.crm.dynamics.com/.default`.

Why not delegated/OBO: it ties access to a human's roles and an interactive
token flow — wrong for a long-running server and unusable for the scheduled
digest (Phase 3). Delegated is worth revisiting only if v2 needs per-user
audit trails on transcript access.

Two things the docs don't settle (verify in Phase 0): whether the **Bot
Transcript Viewer** role can be attached to an application user (it gates the
in-product UI; the Web API path needs table Read privileges, which the custom
role provides regardless), and the exact privilege name in the role editor.
The same app registration also needs a token for `api.powerplatform.com` for
the evaluations API.

### 4.2 Where diagnosis runs → **Claude-side for v1** (see §3.3)

The sharper version of this question is "where does the *prompt* live," and
the answer is: in this repo, versioned, next to the eval fixtures that grade
it.

### 4.3 Retention → **extend the bulk-delete job; no Fabric**

The brief framed this as "30 days vs. build the Fabric sync." Research
dissolved the dilemma: the 30-day limit is a recurring bulk-delete job you
can cancel and replace (e.g., 12 months, `SchemaType = powervirtualagents`).
At single-maker volume the storage cost is trivial, and the MCP server keeps
querying one system. Note this does **not** extend the 28-day in-product
analytics window — irrelevant, since the doctor reads Dataverse, not the
Analytics page.

Synapse Link / Fabric remain the right answer for an enterprise with many
agents and a BI team (with the append-only caveat: by default the bulk-delete
job's deletes propagate to the lake). For this project they're a standing
cost and a second system to operate before the core loop has proven out.
If trend-over-organic-traffic questions arise later, nightly digest appends
(~1KB/session) into the existing Open Brain Postgres give indefinite history
for free. **Decision: replace the bulk-delete job with a 12-month one in
Phase 0; defer everything else.**

### 4.4 Validating diagnosis quality → **two loops, both cheap**

1. **Seeded-failure grading (offline).** Because every diagnosis must land in
   a fix-type bucket, diagnosis is gradeable as classification. Build test
   fixtures: transcripts (real, redacted, or synthesized in the documented
   activity format) with a *known* injected root cause — an instruction
   deliberately missing a rule, a knowledge source deliberately gapped, a
   topic trigger deliberately overlapping, a tool wired to time out. Run the
   doctor over fixtures; grade whether it names the right bucket and a
   recommendation touching the actual defect. This gates prompt changes —
   including the v2 port to Copilot Studio instructions.
2. **Fix-verification loop (online).** The doctor's recommendation predicts
   which eval cases should flip: apply the edit, `run_evaluation`,
   `compare_eval_runs`. A recommendation that doesn't move its predicted
   cases is a wrong diagnosis — and becomes a new fixture for loop 1.
   Platform caveats to design around: test sets are authored in Studio (the
   API can't create them), 100 cases max, one run at a time, results kept 89
   days (the server snapshots run results it wants to keep). The **Custom**
   LLM-judge method (own instructions + pass/fail labels) lets eval cases
   encode the doctor's predicted behavior change directly.

So yes — eval test sets can grade the doctor's recommendations, mechanically:
loop 2 measures whether following the doctor's advice moved the metric the
doctor said it would move.

### 4.5 Fork Troy Taylor's connector vs. clean-room → **clean-room; strip-mine the connector; don't ignore two newer options**

The connector is the right existence proof and the wrong substrate. Verified
specifics: it's a one-commit drop (Feb 2026), delegated-auth only (it copies
the caller's AAD token), fetches a single `$top=5000` page with no
pagination, parses in-memory inside a Power Platform connector script, and
swallows parse errors. Fine for demo scale; not a foundation. **Mine it for
domain knowledge** — its 7 MCP tool names are a sensible baseline taxonomy,
and its activity heuristics (`from.role`, `channelData.topicName`,
`type == "handoff"` for escalation) shortcut parser development. Credit it in
the README.

Two alternatives that didn't exist when the brief's framing formed:

- **Microsoft's official Dataverse MCP server** (`{org}.crm.dynamics.com/api/mcp`)
  can `read_query` any table today. It's the right tool for ad hoc
  exploration during Phase 0, but not the product: no transcript parsing, no
  digest layer, admin-gated enablement, and per-call Copilot-credit billing
  since Dec 2025. Also a useful existence proof that IT will recognize.
- **Copilot Studio Kit ("Copilot Agent Kit")** parses transcripts into
  pre-aggregated `cat_` KPI tables twice daily. If the Kit is already
  installed in the tenant, the digest layer could read those tables instead
  of re-parsing raw JSON. Not worth *installing* for this (managed solution,
  premium connectors, dozens of flows, AI Builder credits) — but worth a
  Phase 0 check for whether it's present.

**Decision: Python/FastMCP clean-room server** (consistent with Open Brain),
borrowing the connector's taxonomy and heuristics, with proper MSAL
client-credentials auth, `@odata.nextLink` pagination, and 429 handling.

---

## 5. Phased plan

**Phase 0 — spike + environment audit (1–2 evenings).** App registration,
application user, custom role. Python script (no MCP yet): pull one day of
`conversationtranscript`, reassemble a split record, parse Content into a
digest. While in there, run the environment checklist (§8): enhanced
transcripts on, transcript saving enabled, bulk-delete job replaced with
12-month retention, `msdyn_botsession` existence, Kit presence. *Exit: a
printed digest matching what the Monitor tab shows for the same session.*
This retires the two biggest unknowns (auth, Content format) before any
architecture exists.

**Phase 1 — MCP server + doctor prompt.** FastMCP server (streamable HTTP)
with Tier 1 + Tier 2 tools; diagnostic prompt v1; wire into Claude
Desktop/Code. *Exit: "how did the agent do this week?" returns a triaged
answer with ≤3 findings, each with a fix-type and a concrete edit.*

**Phase 2 — evals integration.** `run_evaluation`, `list_eval_runs`,
`compare_eval_runs` against the Power Platform API; author the first test
set in Studio; run the fix-verification loop (§4.4.2) end-to-end once.
Start the seeded-failure fixture set (§4.4.1) with ~10 cases.

**Phase 3 — the andon cord.** Scheduled daily run of the same prompt over
the same tools ("anything worth knowing?"), delivered to email/Teams. The
suppress rule matters most here: most days the correct digest is silence.
Silence-by-default is what makes the one-day-in-ten alert credible.

**Phase 4 (contingent) — v2 front end and/or trend store.** Attach the
server to a Copilot Studio doctor agent for owner-facing access (prompt
port gated by the fixture evals); nightly digest appends to Postgres if
organic-traffic trend questions actually arise in use.

---

## 6. Risks and design-arounds

| Risk | Impact | Design-around |
|---|---|---|
| Governance disables transcript saving in the environment | Kills the whole approach | Phase 0 verifies on the real environment first; document the dependency |
| Enhanced transcripts left off | Doctor blind on tool-failure and knowledge-gap buckets | Phase 0 checklist item; server detects absence of `nodeTraceData` and the doctor declares the blind spot |
| ~30-min post-session write delay | "Why did it fail just now" returns nothing | Doctor states its data horizon; never claims live status |
| SharePoint answers `REDACTED` in transcripts | Knowledge-gap evidence weakened | Digest flags `redacted_answers`; questions + retrieved `search_results` survive, so partial diagnosis is still possible — doctor says which half it's missing |
| Reactions unavailable on M365 Copilot channel (and transcripts absent entirely for M365 Copilot agents) | Loses signal on those channels | Per-channel signal availability in the snapshot; weight outcome/fallback signals higher |
| Eval REST API is preview | Breaking changes to §4.4.2 plumbing | Thin client isolated in one module; the connector actions are a documented fallback path |
| Native features (Themes, status page, AI summaries) expand | Overlap erodes the doctor's value | Differentiate on causal diagnosis + fix-type + suppression, not clustering or KPI display (§2.6) |
| Service-protection 429s on big pulls | Slow digest refresh | Honor `Retry-After`, page at 50–200 rows, cache digests — re-pulls become rare |
| Doctor hallucinates causes from thin data | Trust loss — the exact failure the brief is fighting | Confirm-from-transcripts rule (§3.2.3); suppress rule; seeded-failure eval gate |

---

## 7. What success looks like

- Asking "how's the agent doing?" costs one sentence and returns ≤3 findings
  or an honest "nothing worth your attention."
- Every finding names a fix-type and quotes the edit to make.
- Prompt changes to the doctor are gated by the seeded-failure eval set.
- At least one real instruction fix has gone brief → diagnosis → edit →
  eval-verified improvement, end to end.

---

## 8. Phase 0 in-tenant verification checklist

Things the docs don't settle or that vary by environment:

- [ ] Transcript saving enabled in PPAC for the target environment (and not
      overridden by an environment-group rule)
- [ ] Enhanced transcripts ("include node-level details") enabled on the
      target agent; confirm `nodeTraceData` activities appear
- [ ] Replace the default bulk-delete job with a 12-month retention job
      (filter `SchemaType = powervirtualagents`)
- [ ] Custom security role: confirm the exact read-privilege names for
      `conversationtranscript` in the role editor; confirm the app user can
      read all agents' transcripts (org scope)
- [ ] Whether Bot Transcript Viewer is assignable to an application user
      (nice-to-know; custom role should suffice for Web API)
- [ ] Does `msdyn_botsession` exist in this environment? (Design assumes no)
- [ ] Is the Copilot Studio Kit installed? (If yes: evaluate reading `cat_`
      KPI tables as a digest shortcut)
- [ ] Evaluations REST API reachable with the app registration's token
      against `api.powerplatform.com` (preview API; confirm permissions)
- [ ] Which channels the agent runs on → per-channel signal availability
      (reactions, transcripts)

## Sources

Key references (full citations inline above): Microsoft Learn — MCP
extensibility, evaluations REST API, transcript table reference and
analytics-transcripts guides, custom analytics strategy, admin transcript
controls, API limits; Power CAT `mcscatblog` transcript deep-dive; Troy
Taylor's SharingIsCaring Copilot Studio Analytics connector; Power CAT
Copilot Studio Kit repo and docs; Dataverse MCP server docs and launch/update
blogs.
