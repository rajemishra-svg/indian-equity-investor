# Configurable Multi-Agent Customer Support Platform — Design Document

**Version:** 0.1 (draft) · **Date:** 2026-07-02 · **Status:** pre-implementation
**Scope:** Problem statement, architecture, tenancy model, compatibility contracts, challenges & resolutions, consolidated agent-engineering principles, engineering workflow & CI policy, build plan.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Product Definition](#2-product-definition)
3. [Architecture](#3-architecture)
4. [Tenancy Model](#4-tenancy-model)
5. [Compatibility Contracts](#5-compatibility-contracts)
6. [Challenges & Resolutions](#6-challenges--resolutions)
7. [Agent-Engineering Principles (Consolidated)](#7-agent-engineering-principles-consolidated)
8. [Engineering Workflow & CI](#8-engineering-workflow--ci)
9. [Build Plan](#9-build-plan)
- [Appendix A: Example Industry Config](#appendix-a-example-industry-config-bankingyaml)

---

## 1. Problem Statement

Service-heavy businesses — airlines, retailers, banks, hospitals, telcos — receive a continuous stream of inbound customer queries, the large majority repetitive and answerable from systems the business already owns (order status, refund eligibility, EMI schedule, flight rebooking options, plan upgrades). Today every business picks from three bad options:

1. **Human-only support.** Cost per contact is orders of magnitude higher than the marginal cost of an automated resolution; queues collapse under event-driven spikes (flight disruption, network outage, sale day); skilled agents spend their day on password resets while genuinely hard cases wait.
2. **Rule-based bots and IVR trees.** Exact-phrase matching only; dead-end on compound requests ("my flight was cancelled, I want a refund, and my bag is missing"); cannot take actions in backend systems; transfer with zero context, forcing the customer to repeat everything.
3. **Bespoke AI builds.** Each company spends 6–12 months re-solving identical engineering problems — intent routing, action guardrails, escalation logic, auditability, analytics — and hard-codes its domain objects and policies, so every policy change is a code change and nothing is reusable.

**The gap:** no platform lets a business bring its domain model — business objects, policies, escalation rules — *as configuration*, and get a safe, auditable AI support workforce with guaranteed human escalation, without building one from scratch.

### 1.1 Five sub-problems any credible solution must solve simultaneously

| # | Sub-problem | Failure if unsolved |
|---|---|---|
| a | **Bounded autonomy** — the AI must be structurally unable to act beyond its authority (no account action without identity verification, no refund above policy limits, no clinical/financial judgment) | One unauthorized action destroys trust permanently |
| b | **Lossless escalation** — a human taking over inherits the full case, never re-interrogates the customer | Customers rate "repeat yourself" the #1 support failure |
| c | **Compound requests** — real customers bundle multiple issues in one message | Partial answers force repeat contacts |
| d | **Industry variance** — a bank's objects, regulators, and risks differ from an airline's, yet the orchestration skeleton is identical | Per-industry rebuilds recreate the bespoke-AI problem |
| e | **Per-conversation economics and audit** — known token cost per conversation; replayable record of which model, prompt, and config produced each response | Unmetered COGS and unauditable output are non-starters for regulated buyers |

---

## 2. Product Definition

### 2.1 What it is

**A multi-tenant, industry-configurable AI customer-support platform.** A hub orchestrator receives each customer message, routes it to domain-specialist spoke agents that resolve issues end-to-end against the client's backend systems through governed tools, and escalates to human agents by declarative rules — with the complete case context attached.

### 2.2 Expected behavior — specifically

1. **Resolve, not just answer.** Look up a booking, initiate a refund, block a card, reschedule an appointment — real actions via tools, gated by programmatic preconditions (`initiate_refund` is physically uncallable until `identity_verified` and `booking_loaded` are true in session state).
2. **Handle compound requests.** Decompose a multi-issue message into broad objectives, investigate in parallel against shared case facts, reply once — addressing every issue. Example: *"Flight AI-302 cancelled — refund my ₹18,450 and my bag hasn't arrived"* → three parallel investigations (cancellation status, refund eligibility per fare rules, baggage trace) → one synthesized response with a refund confirmation number, a bag-trace ID, and stated timelines for each.
3. **Escalate on rules, not vibes.** Four levels (L0 auto-resolve → L3 immediate human), triggers declared per industry in YAML (fraud keyword, symptom keyword, dispute > ₹50,000, churn intent), enforced both in the prompt and by a deterministic code backstop. The handoff payload carries case facts, full history, partial progress, and the trigger reason — the customer never repeats themselves.
4. **Onboard by configuration.** New tenant on an existing industry: config file, same day. New industry: YAML + a spoke plugin conforming to the SDK, weeks not quarters. Tenant overrides (thresholds, branding, policies) layer over industry defaults without touching code.
5. **Deploy both ways from one artifact.** Pooled multi-tenant for SMB; dedicated instance in the client's VPC for banks and hospitals — same container image, tenancy is data.
6. **Learn offline.** Nightly Batch API jobs mine completed conversations for sentiment trends, FAQ gaps, recurring escalation causes, and policy-compliance violations, surfaced on a per-tenant dashboard.
7. **Stay auditable.** Every conversation stamped with model ID, prompt version, config version, and token cost; two-year-old conversations remain replayable exactly as they occurred.

### 2.3 Success criteria (v1 targets)

| Metric | Target |
|---|---|
| Autonomous resolution rate (L0) | ≥ 70% of inbound queries |
| Declared L3 triggers escalated | 100% (deterministic backstop, measured) |
| Questions re-asked by human after handoff | 0 |
| New tenant onboarding (existing industry) | < 1 day |
| New industry onboarding | < 2 weeks |
| P95 first-response latency | < 5 s |
| Cost per conversation | Metered per tenant, hard budget caps; blended target ≈ ₹1.5 |

### 2.4 Explicitly out of scope (v1)

Real-time voice; replacing the client's CRM/ticketing system (it integrates with them); medical, legal, or investment advice (always escalates); payments or account mutations above configured authority limits.

---

## 3. Architecture

### 3.1 System layers

```
CHANNEL LAYER          Web chat · Mobile app · IVR/phone · Email · WhatsApp
                                    → Unified Session Gateway
INDUSTRY REGISTRY      Loads all industry plugins at startup; per-request tenant
                       context selects spoke set + object hierarchy + policies
HUB (ORCHESTRATOR)     Intent classification · entity extraction · decomposition
                       · routing · escalation-trigger evaluation · reconciliation
                       · synthesis · session state (ConversationState)
SPOKES (per industry)  Domain-specialist tool-use agents (4–6 per industry)
ESCALATION ENGINE      Declarative L0–L3 rules (YAML) + deterministic detector
                       + priority queues + structured human handoff
DATA STORE             Postgres (tenant_id + row-level security); SQLite for dev
BATCH ANALYTICS        Anthropic Batch API: sentiment · FAQ gen · compliance
                       sweep · escalation-pattern mining
DASHBOARD              FastAPI + Streamlit: live ops metrics + batch results,
                       per tenant, industry-comparable
```

**Star topology is a hard rule.** Spokes never communicate with each other; all results flow through the Hub. This buys central visibility, uniform error handling, and fine-grained control over what each spoke receives — the Hub passes each spoke only its domain-relevant slice of case facts (the baggage spoke never sees card numbers), which doubles as data minimization for regulated tenants.

### 3.2 Launch industries (5, as plugins)

Industry is a **tenant attribute**, not a deployment property — one pooled pod serves an airline, a retailer, and a telco simultaneously. Each industry ships as a plugin: a YAML config + a spoke module conforming to the SDK. The platform core (hub, escalation engine, batch pipeline, dashboard) is industry-agnostic.

| Industry | Spokes | Signature escalation trigger | Object hierarchy (examples) |
|---|---|---|---|
| ✈ Airline | Flight Status, Booking, Baggage/Refund, Loyalty | Delay > 3h; lost baggage | Booking→Flight→Segment→Leg; Passenger→FFP→MilesAccount |
| 🛒 Retail | Order Tracking, Returns & Refunds, Product Info, Payment & Loyalty | Delivery loss; fraud dispute | Order→LineItem→Product→Shipment; Customer→Return→Refund |
| 🏦 Banking | Account & Txn, Loan & EMI, Card Services, KYC & Compliance | Fraud/unauthorized txn → L3 + freeze-account tool; dispute > ₹50k → compliance queue; 3 failed OTP → security lock | Account→Transaction→Dispute; Loan→EMISchedule→Foreclosure; Card→Statement→Reward |
| 🏥 Healthcare | Appointments, Rx & Pharmacy, Billing & Insurance, **Triage (pass-through to human only)** | Symptom keywords (chest pain, breathing difficulty) → immediate L3 | Patient→Appointment→Consultation→Prescription; Bill→Claim→TPA→Settlement |
| 📡 Telecom | Billing & Recharge, Network & Outage, Plan & MNP, Retention | Outage → cluster bulk tickets under one human broadcast; churn intent → retention specialist | Subscriber→SIM→Device→Plan; Bill→UsageRecord→Dispute; ServiceRequest→OutageTicket |

### 3.3 Escalation engine (L0–L3)

| Level | Trigger (YAML-declarative, per industry) | Action |
|---|---|---|
| L0 | Spoke resolves in ≤ 4 turns | Done; log to DB |
| L1 | Spoke fails or low confidence | Hub clarifies / retries alternate spoke |
| L2 | Rule fires (delay > 3h, repeat complaint ≥ 3, dispute > threshold) | Priority queue → human pool |
| L3 | Fraud flag, medical/safety keyword, VIP SLA breach | Immediate live-agent handoff + context dump |

Triggers are enforced **twice**: rendered into the Hub system prompt as numbered criteria with contrastive few-shot pairs (escalate vs resolve), *and* evaluated deterministically in `escalation/detector.py` on the same YAML rules — a prompt regression cannot silently kill escalation. The handoff payload (from `handoff.py`) carries: intent, case facts, full turn history, spoke outputs including partial progress, and the trigger reason.

### 3.4 Project structure

```
customer-support/
├── config/
│   ├── base.yaml                # shared: L0–L3 definitions, handoff template, cost targets
│   ├── airline.yaml … telecom.yaml   # spokes, object hierarchy, triggers, source_priority,
│   │                                 # prompt_examples, exit thresholds — schema_version'd
│   └── tenants/                 # tenant overlays (branding, thresholds, model tier)
├── src/
│   ├── registry.py              # loads industry plugins; resolves config chain per tenant
│   ├── models/
│   │   ├── base.py              # TenantContext, ConversationState, case_facts,
│   │   │                        # SpokeResult (key_findings, conflicts, coverage, confidence),
│   │   │                        # EscalationRecord
│   │   └── {industry}.py        # industry business objects
│   ├── agents/
│   │   ├── hub.py               # classify → decompose (partition manifest) → route →
│   │   │                        # reconcile conflicts → synthesize; escalation checks
│   │   ├── base_spoke.py        # spoke SDK: tool loop, error contract, result schema
│   │   ├── dispatcher.py        # precondition gating; disambiguation forcing;
│   │   │                        # parallel tool execution (all results in one message)
│   │   ├── tool_middleware.py   # PostToolUse normalization: ISO-8601 IST, currency,
│   │   │                        # failure_class mapping, PII masking, injection sanitization
│   │   ├── critic.py            # evaluator-optimizer (Haiku, rubric, max 1 revision)
│   │   ├── memory.py            # case-facts extraction; dialogue summarizer
│   │   ├── synthesizer.py       # multi-issue merge; read-only verify_case_fact tool
│   │   └── {industry}/          # spoke implementations
│   ├── escalation/              # detector.py (deterministic), queue.py (L1–L3 priority),
│   │                            # handoff.py (structured summary)
│   ├── batch/                   # processor.py (Batch API), jobs.py (4 single-shot jobs)
│   ├── metering/                # token metering middleware; per-tenant budgets; billing feed
│   ├── dashboard/               # FastAPI + Streamlit
│   └── api/                     # /chat /escalate /batch/* — per-tenant version pinning
└── tests/                       # per-industry suites + eval sets (routing accuracy per
                                 # category, decomposition coverage, escalation recall)
```

### 3.5 Model routing & cost policy

**Goal: blended cost ≈ ₹1.5 per conversation.** Price sheet (per MTok): Haiku 4.5 `claude-haiku-4-5` $1/$5 · Sonnet 5 `claude-sonnet-5` $3/$15 ($2/$10 intro to 2026-08-31) · Opus 4.8 `claude-opus-4-8` $5/$25 · Fable 5 $10/$50. Cache reads ≈ 0.1× input; Batch API = 50% off.

| Call | Model | max_tokens | Rationale |
|---|---|---|---|
| Escalation detection, preconditions, tool normalization, entity regex | **None — pure Python** | — | Biggest lever: never pay an LLM for arithmetic or rule-matching |
| Hub intent classification (every turn) | Haiku | 256 | Structured output vs fixed taxonomy |
| Compound decomposition | Sonnet 5, **confidence-gated** | 600 | Fires only on low-confidence/multi-intent (~25% of turns) |
| Spoke tool loops | Haiku | 1024/turn, ≤4 turns | Narrow domain + governed tools |
| Multi-issue synthesis | Sonnet 5 | 1024 | Customer-visible composition quality |
| Critic | Haiku | 256 | Rubric check = classification |
| Handoff summary | Haiku | 600 | Structured JSON from state |
| Nightly analytics | Haiku via Batch | short | Effective $0.50/$2.50 per MTok |
| FAQ generation | Sonnet via Batch | 2048 | Prose quality, offline, 50% off |
| Eval judging, golden datasets | Opus 4.8, offline | — | Low-volume, quality-critical |

**Rules:** (1) Escalate-on-uncertainty — Haiku call failing schema validation or self-reporting low confidence retries once on Sonnet; the fallback *rate* is a dashboard metric. (2) **Fable 5 is excluded from the runtime path** — 10× Haiku pricing, and its 30-day retention requirement conflicts with regulated tenants. (3) Prompt caching discipline — frozen per-(tenant, spoke) system prompts, breakpoint after them, case facts in the user turn; note **Haiku 4.5's 4096-token minimum cacheable prefix** (spoke prompts clear it via tool definitions; the lean classifier prompt must be consolidated past 4K or accepts full price); verify with `usage.cache_read_input_tokens`. (4) Per-call max_tokens budgets (table above). (5) Per-tenant metering doubles as billing and budget enforcement; **cost per resolved conversation** is a first-class dashboard metric. (6) Per-tenant model tier in YAML (premium tenants may pin Sonnet spokes).

**Worked cost estimate** (list prices, ~80% cache-read rate): simple conversation (~70% of traffic) ≈ $0.006 (~₹0.5); compound (~25%) ≈ $0.045 (~₹4); **blended ≈ $0.017 ≈ ₹1.5** — versus ₹80–300 per human contact. Sonnet-everywhere would run 3–5×; Opus-everywhere ~10×.

---

## 4. Tenancy Model

### 4.1 Decision

**Multi-tenant core, tiered deployment.** Build the codebase tenant-aware from day one; sell pooled (shared) deployment as the default and dedicated single-tenant instances as the regulated/enterprise tier. Never per-client individual implementations. The question conflates two separable decisions — the *codebase* model (one, tenant-aware) and the *deployment* model (pooled or dedicated per client, at zero engineering cost, because a dedicated instance is the same container image with one tenant row).

### 4.2 Why tiered beats both pure options

| | Pure multi-tenant | Per-client implementation | **Tiered (chosen)** |
|---|---|---|---|
| Banking/healthcare deals | Lost — isolation, data residency, VPC demanded | Won | Won via dedicated tier |
| Operational cost | One fleet | N versions, N upgrade windows, backport hell | One artifact; bounded pinned-version window |
| Long-tail SMB economics | Profitable | Unprofitable | Profitable via pooled tier |
| Custom client code | Dangerous in shared runtime | Easy but breeds forks | Config-only on pooled; custom spokes on dedicated only |

Two product-specific nuances: **economies of scale are operational, not infrastructural** (COGS is tokens, linear per conversation — the savings are one fleet to upgrade and cross-tenant learning), and **the noisy-neighbor resource is the Anthropic rate limit, not CPU** (per-tenant token budgets + concurrency caps are mandatory; the same metering feeds billing).

### 4.3 Mechanics

- **Pooled tier:** one runtime, all industry plugins loaded; Postgres with `tenant_id` on every row + row-level security; tenant-scoped repository layer (no raw table access); optional per-tenant encryption keys; escalation queues, batch jobs, metrics all tenant-keyed.
- **Dedicated tier:** same image, own DB, own Anthropic key, own KMS; deployable in client VPC; PHI/PII never crosses the boundary; custom spokes permitted; client-pinned release version with upgrades on the client's window.
- **Shared control plane (thin):** tenant registry, config store & distribution, billing/token metering rollup, fleet upgrade orchestration, anonymized telemetry.
- **Config resolution chain:** `base.yaml ▸ {industry}.yaml ▸ {tenant}.yaml` — each layer overrides the last.

### 4.4 Hard rules

1. **Dedicated ≠ fork.** One codebase, one container image. Client customization lives in the config chain and plugin spokes, never in a patched core. Generic feature requests get upstreamed.
2. **Tenancy is data, not code.** `TenantContext` threads through Hub, repository, queues, cache keys, metrics, and prompts from the first commit — running single-tenant is trivial; retrofitting `tenant_id` later is brutal.
3. **Tier migration is a runbook, not a project.** Pooled → dedicated = export tenant rows, redeploy image, repoint.

---

## 5. Compatibility Contracts

Six independently versioned contracts, each with its own consumers and lifecycle:

| # | Contract | Mechanism |
|---|---|---|
| 1 | **Config schema** | `schema_version` in every YAML; additive-only within a major; loader accepts N and N−1; `config migrate` CLI; unknown keys → warn + ignore (tolerant reader) |
| 2 | **REST API** | Per-tenant version pin (Stripe-style date pin); additive within a version; sunset headers; 6–12 month deprecation window; contract tests in CI |
| 3 | **Database** | Expand → dual-write → backfill → contract; never destructive in the release that adds the new shape; every release rollback-safe to N−1 |
| 4 | **Conversation state** | `state_version` on persisted ConversationState; upcaster chain (v1→v2→v3) applied lazily on read; historical records never bulk-migrated — old conversations stay readable forever (audit requirement) |
| 5 | **Spoke plugin SDK** | `BaseSpoke` is a public SDK: SemVer; interface frozen within a major; deprecation warnings ≥ 1 minor early; compatibility test-kit partners run in their CI |
| 6 | **Models & prompts** | `(model_id, prompt_version)` stamped on every conversation; per-tenant model pinning; canary rollout per tenant/industry; replayable eval set gates every model upgrade |

**Three universal rules:** backward compatibility = additive-only within a major version (new fields optional with defaults; types/meanings never change; removals only at major boundaries after deprecation). Forward compatibility = tolerant readers (older engines ignore-and-log unknown fields — what lets a v1.6 dedicated instance receive config from a v1.8 control plane). Migrations: eager for schema (expand/contract), lazy for data (upcasters on read).

**Fleet policy:** control plane supports instances at N, N−1, N−2; feature flags per tenant decouple deploy from release. The unifying idea across API, config, and models is **per-tenant version pinning** — tenants upgrade on their own schedule within a sliding three-release window.

---

## 6. Challenges & Resolutions

### 6.1 Data isolation & security

| # | Challenge | Resolution |
|---|---|---|
| 1 | Tenant data leakage in the shared DB | `tenant_id` + Postgres RLS; tenant-scoped repository layer; automated cross-tenant isolation tests in CI |
| 2 | Cross-tenant leakage via LLM context or prompt caching | Cache keys scoped per tenant; prompts never assembled from more than one tenant's data; state, queues, batch inputs all tenant-keyed |
| 3 | Custom client code in a shared runtime | Config-only customization on pooled; custom spokes only on dedicated; longer-term: sandboxed plugin workers |
| 4 | Right-to-erasure (DPDP/GDPR) in shared tables | Per-tenant encryption keys + crypto-shredding; per-tenant retention-policy engine |

### 6.2 Noisy neighbor & rate limits

| # | Challenge | Resolution |
|---|---|---|
| 5 | One tenant's spike starves everyone (shared API rate limits are the contended resource) | Per-tenant token budgets + concurrency caps in metering middleware; priority queues per tier; metering doubles as billing |
| 6 | Batch jobs competing with live traffic | Separate API keys for batch vs real-time; off-peak scheduling; Batch API's 24h window absorbs deferral |

### 6.3 Fleet operations & versioning

| # | Challenge | Resolution |
|---|---|---|
| 7 | Version skew across dedicated instances | N to N−2 sliding window; control plane tolerates skew; per-tenant feature flags |
| 8 | Enterprise pressure for core patches → forks | Hard rule: dedicated ≠ fork; customization via config chain + plugin SDK; upstream generic requests |
| 9 | Risky upgrades on client-controlled schedules | Pinned releases; automated pre-upgrade checks (`config migrate` dry-run, DB migration rehearsal); rollback-safe releases |
| 10 | Observability fragmentation across VPC instances | Thin control plane aggregates anonymized telemetry; minimal outbound contract for data-residency clients; full local dashboards |

### 6.4 Configuration & compatibility

| # | Challenge | Resolution |
|---|---|---|
| 11 | Config drift/breakage across the three-layer chain | Contract #1 (schema_version, additive-only, tolerant reader, validation at distribution time) |
| 12 | Old conversations unreadable by new code (audit killer) | Contract #4 (`state_version` + lazy upcasters; `(model_id, prompt_version)` stamped per conversation) |
| 13 | Partner-written spokes breaking on upgrades | Contract #5 (SemVer SDK, frozen within major, compat test-kit) |

### 6.5 Cost & unit economics

| # | Challenge | Resolution |
|---|---|---|
| 14 | Attributing LLM spend per tenant (COGS is tokens) | Metering middleware stamps every call with `tenant_id` + `conversation_id`; drives billing, enforcement, margin dashboards |
| 15 | Small pooled tenants eroding margin | Haiku-first routing ladder (§3.5); aggressive prompt caching; Batch API analytics; per-tier model policy |

### 6.6 Compliance & tenant lifecycle

| # | Challenge | Resolution |
|---|---|---|
| 16 | Data residency mandates (banking, healthcare) | Dedicated tier in client VPC: own DB/key/KMS; per-tenant audit logs |
| 17 | Onboarding/offboarding at scale | Tenant registry + provisioning automation; offboarding = export + crypto-shred |
| 18 | Migrating a growing client pooled → dedicated | Same artifact + tenant-keyed data → export rows, redeploy, repoint; rehearsed runbook |

---

## 7. Agent-Engineering Principles (Consolidated)

All batches deduplicated and organized by theme. The unifying design law: **move behavior from prose into structure** — schemas, evals, lints, and config — wherever a failure would otherwise be invisible.

### 7.1 Prompt & context engineering

| Principle | Design decision |
|---|---|
| Concrete I/O examples over textual requirements | Every prompt template ships canonical few-shot examples showing exact JSON nesting and ISO-8601 IST timestamps (`2026-07-02T14:30:00+05:30`); examples live in the industry YAML (`prompt_examples`) so they're versioned with the config |
| Worked decomposition examples | Hub decomposer carries 2–3 per-industry worked examples of correct reasoning + tool sequencing for multi-issue messages |
| Case facts survive summarization | `ConversationState.case_facts` — typed block (identifiers, verified-identity status, amounts, dates, commitments, disambiguation answers) extracted eagerly each turn, injected verbatim into every prompt; the summarizer compresses dialogue only. Also the backbone of the human-handoff payload |
| Context layout vs lost-in-the-middle | Case facts and key findings lead the **volatile (user-turn) section** with explicit headings (`## CASE FACTS`, `## SPOKE RESULTS`, `## CURRENT MESSAGE`); the frozen system prompt stays first and cached — primacy inside the volatile region, cache intact |
| No keyword-routing rules in prompts | Routing is intent classification against spoke/tool descriptions — never "if the user says X use Y" prose (systematic per-category accuracy gaps indicate prompt-level steering). Per-category routing-accuracy eval in CI detects regressions |
| Escalation criteria explicit + few-shot, with code backstop | YAML triggers rendered into the Hub prompt as numbered criteria with contrastive pairs (escalate vs resolve); `detector.py` evaluates the same rules deterministically |

### 7.2 The tool boundary

| Principle | Design decision |
|---|---|
| Differentiated, expanded tool descriptions | SDK makes `purpose`, `input_format`, `example_calls`, `edge_cases`, `not_for` required fields; CI lint fails on missing sections |
| Contrastive disambiguation examples | `disambiguation_examples` field (ambiguous scenario → winning tool → why it beats the alternative) required on any pair the semantic-overlap lint flags as adjacent |
| No semantic overlap between tools | Registry-level lint: no two tools visible to one agent may have overlapping trigger semantics; `verb_object_qualifier` naming; descriptions state their contrast |
| Programmatic preconditions | Tools declare `requires: [identity_verified, booking_loaded, …]`; the dispatcher **blocks the call in code** and returns a structured precondition error. Per-industry gates in YAML (banking: OTP before mutations; healthcare: patient verification + consent before Rx). Never trusted to prompting |
| Interface-level least privilege | Input domains constrained structurally: refund tool takes a `booking_id` validated against loaded case facts, not an arbitrary string; URL tools validate format + domain allowlist. Undesired behavior impossible, not discouraged |
| PostToolUse normalization | One middleware choke point wraps every tool result (internal + third-party MCP): ISO-8601 IST timestamps, canonical currency, normalized error envelope, PII masking, size truncation, prompt-injection sanitization — before the model sees it |
| Parallel tool bundling | Spoke prompts instruct bundling independent lookups in one turn; dispatcher executes concurrently and returns **all `tool_result` blocks in a single user message** |

### 7.3 Coordination (hub–spoke)

| Principle | Design decision |
|---|---|
| Star topology, hard rule | No spoke-to-spoke channels; Hub controls per-spoke context slices (least privilege on data) |
| Broad decomposition, never step lists | Decomposition schema is `{objective, success_criteria, relevant_facts}` per issue; spokes own their tool loops end-to-end |
| Explicit MECE partitioning | Decomposer emits a partition manifest with per-issue scope boundaries ("refund = monetary only; rebooking is issue 2"); a coverage validator checks every clause of the customer message maps to an issue or an explicit no-action |
| Parallel investigation, shared context | Compound path: decompose → `asyncio.gather` relevant spokes, each with the shared case-facts block → synthesizer merges into one reply addressing every sub-issue |
| Conflicts reconcile at the coordinator | Spokes never pick a winner between disagreeing sources: `SpokeResult.conflicts` carries both values + source attribution + timestamps; the Hub reconciles via per-industry `source_priority` YAML (banking: core banking > CRM; airline: DCS > OMS); no rule → annotated uncertainty or escalation. (Same pattern as an analyst's source-priority hierarchy — deterministic config first, LLM judgment only for the residual) |
| Centralized synthesis | All multi-spoke results flow Hub → synthesizer for integrated merging |
| Scoped short-circuit tools | Synthesizer gets one read-only `verify_case_fact` tool (checks against state + already-fetched data; no external calls, no mutations) — handles ~85% of verifications without a coordinator round-trip; fresh-data needs route back through the Hub |
| Structured typed payloads between agents | Spokes return `{key_findings, entities, actions_taken, conflicts, coverage, confidence}` — never prose transcripts, reasoning traces, or raw API dumps. Token volume cut at the source |
| Ask the user for a definitive identifier | Lookup tools return structured `multiple_matches`; dispatcher forces the ask-user path (PNR, order number, last-4, registered phone) — never auto-picks; the answer lands in case facts and is never re-asked |

### 7.4 Errors & graceful degradation

| Principle | Design decision |
|---|---|
| Errors handled at the lowest capable level | Spokes own local recovery (retry, fallback source, narrower query) first; only genuine failures escalate |
| Structured error escalation | `{failure_class, executed_query, partial_results, recovery_attempts, suggested_alternatives}` — the Hub reroutes or escalates; partial progress flows into the handoff, never lost |
| Failure taxonomy: access failure ≠ empty result | `failure_class` enum: `access_failure` (timeout/5xx/auth — retryable), `empty_result` (**a valid, informative finding**), `invalid_input`, `precondition_blocked`, `partial_data`. Middleware maps raw errors to the enum; Hub retry policy keys off it — a timeout never reaches the customer as "no records", an empty result never triggers a retry storm (also a cost control) |
| Coverage annotations | `SpokeResult.coverage = {completed, unavailable: [{item, reason}]}`; the synthesizer must address every manifest item — resolved, partial (with reason + next step), or unavailable. No silent omissions; handoffs inherit annotations |

### 7.5 Quality control & evals

| Principle | Design decision |
|---|---|
| Evaluator-optimizer self-critique | `critic.py`: Haiku pass scores the draft against a fixed rubric (policy context cited? timeline stated? next steps explicit? every sub-issue addressed?) with max 1 revision loop; applied selectively (final syntheses + all escalation handoffs) to cap cost |
| Eval suite from week 1 | Routing accuracy per intent category; decomposition coverage; escalation-trigger recall (target 100%); per-industry golden sets. Gates prompt changes and model upgrades (contract #6) |
| Explicit criteria over vague instructions | Compliance sweep flags a response only when it **contradicts** stated policy — not "seems off"; per-category precision tracked; per-category kill-switches in tenant YAML (a 30%-noise report gets ignored entirely, which is worse than a narrower trusted one) |
| Findings carry rationale + confidence | Every batch-job finding and review finding includes both, enabling triage without investigation |

---

## 8. Engineering Workflow & CI

### 8.1 Repo standards (how the team builds this with Claude Code)

```
customer-support/
├── CLAUDE.md                    # always-loaded: architecture map, domain rules
│                                #   ("never weaken escalation gates"), test conventions
├── .claude/
│   ├── rules/                   # glob-scoped conventions:
│   │   ├── spokes.md            #   src/agents/**/*.py — SDK conformance
│   │   ├── config-schema.md     #   config/*.yaml — schema_version, additive-only
│   │   └── testing.md           #   tests/** — per-industry layout
│   ├── commands/                # version-controlled slash commands:
│   │   ├── new-spoke.md         #   /new-spoke — scaffolds spoke + tools + tests
│   │   └── new-industry.md      #   /new-industry — YAML + spoke dir + eval set
│   └── skills/
│       └── spoke-patterns/      # on-demand heavy scaffolding context
│                                #   (context: fork, allowed-tools: file writes,
│                                #    argument-hint for required params)
└── .mcp.json                    # version-controlled; ${GITHUB_TOKEN}-style env
                                 #   substitution; variables documented in README
```

Principles: team guidance lives at **project level, never personal configs** (new teammates must inherit it; personal skills may shadow a project skill by name for individual taste without affecting others). **Always-on vs on-demand**: CLAUDE.md carries what every session needs; bulky examples load via skills only when invoked. **Isolate exploration**: Explore subagents / `context: fork` keep verbose discovery and rejected brainstorming out of implementation context. **Plan mode** for genuinely ambiguous integrations (the channel gateway — WhatsApp/webhook/bot-token — is the canonical case: align on approach before code).

### 8.2 CI/CD policy

| Concern | Decision |
|---|---|
| API selection by latency | Blocking pre-merge checks → sync API; nightly/weekly jobs (test generation, security audit, tech-debt reports, platform analytics) → Batch API at 50% off |
| Non-interactive invocation | `claude -p` with `--output-format json --json-schema` → guaranteed-parseable findings (file, line, severity, suggested fix) posted as inline PR comments |
| Review quality | Few-shot examples of exact finding structure; rationale + confidence per finding; per-file focused passes then one integration pass (attention dilution); context includes existing tests + prior findings (no duplicate suggestions, no re-flagging fixed code); per-category kill-switches for high-false-positive categories |

### 8.3 Batch API invariant (platform architecture constraint)

> **The Batch API cannot execute tools mid-request.** Every batch job must be **single-shot**: all transcripts and reference data pre-fetched and inlined into the prompt. The four nightly jobs (sentiment, FAQ gen, compliance sweep, escalation patterns) are designed as pure classification/generation over inlined data. Any future job requiring tool use runs on the sync API in an off-peak queue — never on Batch. Stated invariant in `batch/jobs.py`.

---

## 9. Build Plan

| Week | Deliverable |
|---|---|
| 1 | `TenantContext` threaded end-to-end; config resolution chain (`base ▸ industry ▸ tenant`) with `schema_version`; base models incl. `case_facts` + `SpokeResult`; Hub skeleton (Haiku classifier + confidence-gated Sonnet path); **eval harness scaffolding** (routing accuracy, escalation recall); token-metering middleware |
| 2 | Spoke SDK (`base_spoke.py`: tool-description schema, error contract, result schema) + dispatcher (preconditions, disambiguation forcing, parallel execution) + tool middleware (normalization, failure_class); all 5 industry spoke sets |
| 3 | Escalation engine (detector + queues + handoff); Hub decomposer with partition manifest + coverage validator; reconciler + `source_priority`; synthesizer + critic |
| 4 | Batch pipeline (4 single-shot jobs) + Postgres schema with RLS + repository layer; per-tenant budgets live |
| 5 | FastAPI server (per-tenant API version pinning) + dashboard (live ops + batch results + cost per resolved conversation) + end-to-end tests per industry; CI lints (tool descriptions, semantic overlap, SpokeResult conformance) |

Control plane (tenant registry, config distribution, fleet orchestration) is Phase 2 — the prototype uses a `tenants.yaml` file and a single pooled pod, but every query and queue is tenant-scoped from the first commit.

---

## Appendix A: Example Industry Config (`banking.yaml`)

```yaml
schema_version: 1
industry: banking
display_name: "Banking & Financial Services"

spokes:
  - id: account_txn
    class: banking.AccountSpoke
    description: "Balance enquiry, transaction history, dispute initiation"
  - id: loan_emi
    class: banking.LoanSpoke
    description: "EMI schedule, prepayment, foreclosure, NOC"
  - id: card_services
    class: banking.CardSpoke
    description: "Block/unblock, rewards, statement, limit change"
  - id: kyc_compliance
    class: banking.KYCSpoke
    description: "Document upload, verification status, AML queries"

object_hierarchy:
  Account:  { children: [Transaction, Dispute, Resolution] }
  Loan:     { children: [EMISchedule, Prepayment, Foreclosure] }
  Card:     { children: [Statement, Reward, Redemption] }
  KYC:      { children: [Document, VerificationStatus] }

source_priority:            # Hub conflict reconciliation order
  balance:    [core_banking, crm]
  txn_status: [core_banking, payment_gateway]

tool_gates:                 # programmatic preconditions (dispatcher-enforced)
  - tools: [lookup_account, get_transactions]
    requires: [identity_verified]
  - tools: [initiate_dispute, block_card, process_refund]
    requires: [identity_verified, otp_verified]

escalation_triggers:
  - level: L3
    rule: "fraud_keyword OR unauthorized_txn_amount > 0"
    action: freeze_account_tool + immediate_human
  - level: L2
    rule: "dispute_amount > 50000"
    action: compliance_queue
  - level: L2
    rule: "failed_otp_count >= 3"
    action: security_queue + block_session
  - level: L1
    rule: "spoke_confidence < 0.6 OR turns >= 4"
    action: hub_retry

prompt_examples:            # concrete I/O — versioned with the config
  decomposition:
    - input: "someone withdrew 40k from my account and my card is also not working"
      output:
        issues:
          - objective: "Investigate the ₹40,000 withdrawal as a potential unauthorized transaction"
            scope: "transaction dispute only; card issue is issue 2"
            escalation_check: "unauthorized_txn → L3 candidate"
          - objective: "Diagnose and resolve the non-working card end-to-end"
            scope: "card status, block/reissue if compromised"
  timestamps: "2026-07-02T14:30:00+05:30"   # ISO-8601 IST, always
```
