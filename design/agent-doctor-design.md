# Agent Doctor: Conversational Observability for Copilot Studio Agents

**Status:** Design — v0.1 (design session output)
**Working name:** agent doctor
**Companion doc:** design brief (in PR/branch description)

This document responds to the design brief: it challenges the proposed
architecture where it deserves challenging, answers the five open questions,
and lays out a phased build plan. Verified platform facts are cited inline;
anything still uncertain is flagged.

---

## 1. Summary of the design position

The thesis in the brief is right and worth protecting: **the product is the
judgment layer, not the data layer.** Everything below is organized to keep
diagnosis cost near zero while avoiding two traps: (a) rebuilding a dashboard
with a chat skin, and (b) baking analysis into server-side tools so the LLM
becomes a formatter instead of a diagnostician.

The main architectural changes I'm proposing versus the brief:

1. **Split the tool surface into primitives + digests, not analyses.**
   `get_failure_clusters` as an MCP tool is a category error — clustering
   failures *is* the judgment the LLM should be doing. The server's job is
   compression without judgment: deterministic per-session digests the LLM can
   triage in bulk, plus full-transcript drill-down for the sessions it selects.
2. **v1 runs Claude-side, not as a Copilot Studio agent.** The doctor's
   quality lives in a long diagnostic prompt over large context. Iterate that
   where iteration is cheapest. The Copilot Studio "agent doctor" front end is
   a v2 packaging decision, not a v1 architecture decision.
3. **Skip Fabric/Synapse for v1.** 30-day retention is sufficient for
   diagnosis (which is about the current state of the agent); longitudinal
   signal comes from eval runs, which persist independently. If trend history
   is needed later, snapshot the *digests* (tiny) into your existing Postgres
   rather than syncing raw transcripts into a lakehouse.
4. **Make the doctor gradeable from day one.** The fix-type taxonomy
   (instruction gap / knowledge gap / routing gap / tool failure) is a
   classification scheme — which means diagnosis quality can be measured with
   seeded-failure test cases, and recommendations can be validated by the
   apply-fix → rerun-eval → compare loop.

---

## 2. Verified platform facts (and what they change)

> Facts below were verified against Microsoft Learn and the referenced repos
> as of July 2026. Where a brief assumption was wrong or has moved, it's
> called out with **⚠**.

### 2.1 Integration surface

- Copilot Studio agents can consume MCP servers as tools. *(details: §research)*
- Custom connectors remain the alternative integration path; Troy Taylor's
  Copilot Studio Analytics connector proves the four Dataverse tables are
  sufficient raw material for conversational reporting.

### 2.2 Data

- `conversationtranscript` holds the full activity log as JSON in `Content`;
  records over 1MB split into multiple rows sharing `Name` +
  `ConversationStartTime` with distinct `BatchId` — reassembly is the
  server's job, never the LLM's.
- Transcripts land in Dataverse **after** session end, sometimes with delay.
  The doctor is a retrospective instrument; the design should say so out loud
  rather than pretending to be live monitoring.
- Default retention ~30 days via bulk-delete job (adjustable); Analytics page
  aggregates retained longer than session-level detail.

### 2.3 Auth

- Server-to-server: Entra ID app registration + Dataverse **application
  user** with a security role granting read on the four tables. This is the
  recommended path for the MCP server (see §6.1).

### 2.4 Evaluations

- Evaluations (GA) provide test sets, per-case Pass/Fail with explanations,
  and run-over-run comparison; runs are triggerable programmatically.
  This is the doctor's longitudinal memory and its validation loop (§6.4).

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Front ends                                                 │
│   v1: Claude Desktop/Code (maker-facing)                    │
│   v2: Copilot Studio "agent doctor" agent (owner-facing)    │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP (streamable HTTP)
┌──────────────────────────▼──────────────────────────────────┐
│  agent-doctor MCP server (Python / FastMCP)                 │
│                                                             │
│  Tier 1 — primitives (thin, deterministic)                  │
│    list_agents, list_sessions, get_transcript,              │
│    get_feedback, list_eval_runs, run_evaluation             │
│                                                             │
│  Tier 2 — digests (compression WITHOUT judgment)            │
│    get_session_digests(window, filters)                     │
│    get_agent_snapshot(window)                               │
│                                                             │
│  Internals: transcript reassembly (BatchId), activity       │
│  parsing, digest extraction, OData paging, caching          │
└──────────────────────────┬──────────────────────────────────┘
                           │ Dataverse Web API (S2S, app user)
┌──────────────────────────▼──────────────────────────────────┐
│  Dataverse: conversationtranscript, bot, botcomponent,      │
│             msdyn_botsession, eval runs                     │
└─────────────────────────────────────────────────────────────┘
```

### 3.1 The two-tier tool surface (the core design decision)

The brief proposes tools like `get_failure_clusters`. The problem: whoever
writes that server code has to decide what a "failure cluster" is — and that
decision is exactly the triage-with-judgment the brief says is the LLM's job.
Server-side clustering gives you a dashboard widget returned as JSON.

Instead:

**Tier 1 — primitives.** Thin, deterministic wrappers over Dataverse:
sessions in a window, one reassembled transcript, feedback records, eval
runs. No opinions.

**Tier 2 — digests.** The token-economics layer. A month of traffic might be
hundreds of sessions × tens of KB of transcript — unfeedable. The server
deterministically extracts a compact per-session digest:

```json
{
  "session_id": "…",
  "started": "…", "duration_s": 312, "turn_count": 9,
  "outcome": "Escalated",
  "triggered_topics": ["Fallback", "OrderStatus"],
  "tools_called": [{"name": "LookupOrder", "ok": false, "error": "timeout"}],
  "knowledge_sources_hit": ["sharepoint:PolicyDocs"],
  "first_user_utterance": "why was my order cancelled and when will…",
  "reactions": {"up": 0, "down": 1},
  "signals": ["fallback_triggered", "tool_error", "multi_intent_opening"]
}
```

Everything in the digest is *extractable without an LLM* — outcomes, topic
triggers, tool errors, fallback hits, reaction counts, plus a small set of
cheap heuristic flags (`signals`) that are hints, not verdicts. 200 sessions
of digests fit in one context window. The LLM does the clustering — "18 of 23
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

The fix-type taxonomy is load-bearing twice over: it forces recommendations
to be actionable (each bucket maps to a thing the maker can edit), and it
makes the doctor's output *gradeable* (§6.4).

### 3.3 Front end sequencing

**v1: Claude Desktop/Code over the MCP server.** Reasons:
- The diagnostic prompt needs many iterations; Claude-side you edit a file.
  Inside Copilot Studio you're tuning instructions through the same opaque
  orchestrator you're trying to diagnose — debugging two agents at once.
- Diagnosis wants a big context window and strong long-document reasoning;
  triage-over-200-digests-then-read-5-transcripts is exactly the workload
  frontier models handle and orchestrated topic-based agents handle poorly.
- The v1 user is you (the maker). The "business owner" persona in the brief
  is real but she's the v2 audience; don't pay her constraints yet.

**v2: the same MCP server attached to a Copilot Studio doctor agent** for
owner-facing access — after the prompt is proven. The server doesn't change;
that's the point of putting the judgment in the prompt and the mechanics in
the server.

---

## 4. Answers to the open questions

### 4.1 Auth model → **service principal (application user), not delegated**

Delegated auth ties the doctor's access to a human's roles and an interactive
token flow — wrong for a long-running server and for scheduled digests
(§5, phase 3), and it makes the Copilot Studio v2 front end painful. Use an
Entra app registration + Dataverse application user holding a **custom
security role** with read privileges on the four tables (least privilege;
don't grant a broad OOB role). Keep secrets in the server's environment, not
in Power Platform. Rate limits for application users are generous relative
to the doctor's query volume (digest pulls are batched and cacheable).

### 4.2 Where diagnosis runs → **Claude-side for v1** (see §3.3)

The sharper version of this question is "where does the *prompt* live," and
the answer is: in this repo, versioned, next to the eval cases that grade it.

### 4.3 Retention → **30 days is enough for v1; no Fabric yet**

Diagnosis answers "what's wrong with the agent *now*" — a 30-day window
overshoots the need. What genuinely wants history is *trend* ("is it getting
better since the instruction change?"), and eval run comparisons cover that
without any transcript retention.

If trend-over-organic-traffic is later needed: the digest layer already
exists, and digests are ~1KB/session. A nightly job appending digests to
Postgres (the Open Brain instance) gives indefinite trend history for
effectively zero cost and zero new infrastructure. Fabric/Synapse Link is the
right answer for an enterprise with many agents and a BI team; for this,
it's a standing cost and a second system to operate before the core loop has
proven out. **Decision: defer, revisit only if digest-in-Postgres proves
insufficient.**

### 4.4 Validating diagnosis quality → **two loops, both cheap**

1. **Seeded-failure grading (offline).** Because every diagnosis must land in
   a fix-type bucket, diagnosis is gradeable as classification. Build a small
   set of test fixtures: transcripts (real, redacted, or synthesized) with a
   *known* injected root cause — an instruction deliberately missing a rule, a
   knowledge source deliberately gapped, a topic trigger deliberately
   overlapping. Run the doctor; grade whether it names the right bucket and a
   recommendation that touches the actual defect. This is a standard eval set
   for the doctor itself and it gates prompt changes.
2. **Fix-verification loop (online).** The doctor's recommendation predicts
   which eval cases should flip: apply the edit, `run_evaluation`,
   `compare_eval_runs`. A recommendation that doesn't move its predicted
   cases is a wrong diagnosis — and that outcome feeds back into loop 1 as a
   new fixture. This closes the loop the brief calls "can eval test sets
   grade the doctor's own recommendations" — yes, mechanically.

### 4.5 Fork Troy Taylor's connector vs. clean-room → **clean-room server, strip-mine the connector**

The connector is the right existence proof and the wrong substrate: it's a
Power Platform custom connector (OpenAPI artifact) living inside the
platform's auth and hosting model, and it stops at retrieval/reporting — it
has no digest layer, and the digest layer is where the design's leverage is.
Build the FastMCP server clean-room (consistent with Open Brain), but mine
the connector's query definitions for the hard-won details: which columns
matter, transcript Content parsing, BatchId reassembly, outcome mapping.
Credit it in the README.

---

## 5. Phased plan

**Phase 0 — spike (1–2 evenings).** App registration + application user +
custom role. Python script (no MCP yet): pull one day of
`conversationtranscript`, reassemble a split record, parse Content into a
digest. *Exit: a printed digest that matches what the Monitor tab shows for
the same session.* This retires the two biggest unknowns (auth and Content
format) before any architecture exists.

**Phase 1 — MCP server + doctor prompt.** FastMCP server with Tier 1 + Tier 2
tools; diagnostic prompt v1; wire into Claude Desktop/Code. *Exit: "how did
the agent do this week?" returns a triaged answer with ≤3 findings, each
with a fix-type and a concrete edit.*

**Phase 2 — evals integration.** `run_evaluation`, `list_eval_runs`,
`compare_eval_runs` tools; the fix-verification loop (§4.4.2) becomes
runnable end-to-end. Start the seeded-failure fixture set (§4.4.1) with ~10
cases.

**Phase 3 — the andon cord.** Scheduled daily run of the same prompt over
the same tools ("anything worth knowing?"), delivered to email/Teams. The
suppress rule matters most here: most days the correct digest is silence.
Silence-by-default is what makes the one-day-in-ten alert credible.

**Phase 4 (contingent) — trend store.** Nightly digest append to Postgres,
only if organic-traffic trend questions actually arise in use.

---

## 6. Risks and design-arounds

| Risk | Impact | Design-around |
|---|---|---|
| Governance disables transcript saving in the environment | Kills the whole approach | Phase 0 verifies on the real environment first; document the dependency; digests reduce the retention ask |
| Transcript write delay post-session | "Why did it fail an hour ago" may return nothing | Doctor states its data horizon in answers; never claims live status |
| SharePoint knowledge answers redacted in transcripts | Knowledge-gap diagnoses lose evidence | Digest flags `redacted_content`; doctor reports the blind spot instead of guessing |
| Reactions unavailable on M365 Copilot channel | Loses the cheapest failure signal on one channel | Weight outcome + fallback signals higher; per-channel signal availability in the snapshot |
| Test-canvas sessions absent from analytics | Maker's own testing invisible to the doctor | Fine — evals are the instrumented test path; document it |
| API entitlement limits on app user | Throttling on big digest pulls | Batch + cache digests server-side; digests make re-pulls rare |
| Doctor hallucinates causes from thin data | Trust loss — the exact failure the brief is fighting | Confirm-from-transcripts rule (§3.2.3); suppress rule; seeded-failure eval gate |

---

## 7. What success looks like

- Asking "how's the agent doing?" costs one sentence and returns ≤3 findings
  or an honest "nothing worth your attention."
- Every finding names a fix-type and quotes the edit to make.
- Prompt changes to the doctor are gated by the seeded-failure eval set.
- At least one real instruction fix has gone brief → diagnosis → edit →
  eval-verified improvement, end to end.
