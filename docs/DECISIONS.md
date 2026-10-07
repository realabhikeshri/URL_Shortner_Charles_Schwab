# Architecture Decision Records

Format: context → decision → alternatives → consequences. ADRs referenced from code comments keep their numbers.

---

## ADR-001 — A purpose-built asyncio engine instead of LangGraph, Temporal or Prefect

**Context.** The engine is the thing being evaluated. It needs per-node state machines, gate/policy/approval
hooks, path-scoped rollback, dynamic DAG expansion and re-planning, and auditability. All of that must run
offline in CI in under a minute.

**Decision.** Implement a small (~1.1k LOC) asyncio scheduler over a validated `networkx` DAG, with SQLite state.

**Alternatives.** *LangGraph*: good agent-graph primitives, but no first-class approvals with identity, no
rollback, and its checkpointer is not an audit trail; the governance would live outside it anyway. *Temporal*:
the right production answer for durable execution, but it needs a server cluster, deterministic workflow-code
constraints and heavier ops; overkill for a single-machine demo. *Prefect/Airflow*: batch-data oriented,
static DAGs.

**Consequences.** Full control and transparency, with every behaviour unit-tested. We own durability semantics,
which are kept simple: persist before acting, resume rolls back in-flight nodes. ARCHITECTURE §8 describes the
migration path to Temporal-style workers without changing the node contract.

## ADR-002 — SQLite state store per run

**Context.** Runs must survive crashes and pauses and be inspectable after the fact.
**Decision.** `runs/<run_id>/state.db`, one writer, `synchronous=FULL`, rollback-journal mode (no WAL side files
in git), every write committed immediately.
**Alternatives.** JSON files (no transactions), Postgres (an operational dependency for a CLI), event-sourcing only
from the audit log (slower to query, more code).
**Consequences.** Resume is exact; reports and metrics are SQL queries. Single-writer only, which the multi-worker
scaling path replaces with Postgres.

## ADR-003 — Deterministic provider by default; the LLM is optional

**Context.** The demo, CI and tests must be reproducible without an API key, and reviewers must be able to run it.
**Decision.** `LLMProvider` with three implementations behind one `LLMClient`. The default `DeterministicProvider`
fulfils the same typed contracts from curated knowledge: analyst rules, a capability catalog, and change-set
templates ("blueprints") for the URL-shortener domain. `AnthropicProvider` is used when `ANTHROPIC_API_KEY` is
set; `RecordReplayProvider` records and replays.
**Alternatives.** Mocked LLM calls in tests only (the demo would not work offline); always requiring a key
(irreproducible, costly).
**Consequences.** Everything around the "LLM" (gates, policies, retries, rollback, approvals, re-planning, audit)
is exercised for real. The provider itself is deterministic, and one behaviour is explicitly scripted: the
brownfield alias fix's naive first attempt (ADR-012). Generality of the deterministic provider is limited to its
catalog; that limitation is stated in LIMITATIONS.

## ADR-004 — Hash-chained append-only audit log

**Context.** Approvals and policy outcomes in a regulated firm must be tamper-evident.
**Decision.** JSONL, one event per line, `hash = sha256(canonical(event without hash))`, each event carries
`prev_hash`. Writes are fsync'd. `orchestrator audit verify` recomputes the chain.
**Alternatives.** Plain logs (not tamper-evident); a signed log per event (key management); a blockchain (no).
**Consequences.** Any edit, deletion, insertion or reordering is detected, including a forger who re-hashes the
edited event (the next `prev_hash` breaks). It does not prevent truncation of the tail or wholesale regeneration
by someone with write access, so production would anchor the head hash externally (WORM storage / signed
checkpoints). See LIMITATIONS.

## ADR-005 — Git checkpoints with path-scoped rollback

**Context.** Failed write attempts must leave no trace; parallel nodes share a workspace.
**Decision.** A checkpoint is the HEAD sha at attempt start (nothing committed). Each node commits only its own
files on success. Rollback restores just the node's paths (`git checkout <sha> -- paths`, deleting files that
did not exist). The DAG validator guarantees concurrently runnable nodes have disjoint write sets.
**Alternatives.** `git stash` / `reset --hard` (would destroy siblings' in-flight work); a worktree per node
(cleaner isolation, more merge machinery; the scaling path); copy-on-write snapshots.
**Consequences.** Correct under parallelism, cheap, and every line is attributable to a node commit. Residual: a
parallel node's *gates* can observe a sibling's in-flight files (LIMITATIONS).

## ADR-006 — 302 redirects by default, 307 configurable

**Context.** Links can be disabled, deleted or expired, and clicks are counted.
**Decision.** 302 by default with `Cache-Control: private, no-store`; `SHORTENER_REDIRECT_STATUS_CODE=307`
preserves the method for non-GET clients.
**Alternatives.** 301/308: browsers cache permanently, so disable/delete would not take effect and clicks would
be lost.
**Consequences.** Every click reaches the service (by design); the redirect hot path is optimised instead (ADR-013).

## ADR-007 — 7-character random base62 codes with bounded retry

**Context.** Codes must be short, non-enumerable and unique without central coordination.
**Decision.** `secrets.choice` over 62 symbols, 7 characters → 62⁷ ≈ 3.52 × 10¹² codes. With n links stored, a
new code collides with probability n/62⁷ (2.8 × 10⁻⁴ at 10⁹ links). Up to 5 attempts, so failure needs
(n/62⁷)⁵ ≈ 1.7 × 10⁻¹⁸. The unique index settles races (IntegrityError → next candidate).
**Alternatives.** Sequential ids in base62 (enumerable, leak volume); hashes of the URL (collide by design across
users); 6 characters (5.7 × 10¹⁰, too tight at scale).
**Consequences.** No coordination; length is configurable. Generated codes are case-sensitive; custom aliases are
canonicalised to lower case (ADR-016 context).

## ADR-008 — Analytics off the hot path; PII-free unique visitors

**Context.** Analytics failures must never fail a redirect, and raw IP/UA is personal data.
**Decision.** The redirect does `put_nowait` on a bounded `asyncio.Queue` (overflow → counted drop). A background
writer batches inserts, and a purger enforces retention. Visitor =
`sha256(HMAC(secret, day) ‖ ip ‖ ua)`: the daily-rotating salt prevents cross-day joins and the raw values are
never stored.
**Alternatives.** Synchronous insert (latency, failure coupling); `BackgroundTasks` per request (no batching or
back-pressure); Kafka/SQS (the right scaling step, not needed on one node).
**Consequences.** Clicks still queued at a hard crash are lost (at-most-once); a graceful shutdown drains the
queue. "Unique visitors" over a window is the sum of daily uniques (an upper bound); this is documented in the
API.

## ADR-009 — Policy-as-code: YAML rules + Python checks

**Context.** Security, compliance and change-control rules must be reviewable, versioned and enforced at gates.
**Decision.** `policies/*.yaml` declare rules (`check`, `severity`, `stages`, `params`); checks are small async
Python functions. A crashing check fails closed. Results are persisted and audited.
**Alternatives.** OPA/Rego (the industry standard at scale, but another runtime and language; noted as the scaling
path); hard-coded rules (not reviewable by risk/compliance owners).
**Consequences.** Adding a rule is YAML for parameters or a ~20-line check. Agents cannot weaken policies, since
`policies/` is on the workspace denylist.

## ADR-010 — Risk-tiered approvals after automated gates; high risk never auto-approved

**Context.** Human attention is scarce (approval fatigue), and high-risk changes need accountable sign-off.
**Decision.** Approval needs are derived from the autonomy policy, policy outcomes and agent requests, and are
requested only **after** a change passes its automated gates. `--auto-approve=low,medium` is for demos; high
risk requires the explicit `--i-understand-high-risk` flag, and its use is audited (`high_risk_override`).
Approvals record approver identity, timestamp, comment and diff hash. If the diff changes after the request, the
approval is invalidated.
**Alternatives.** Approve before running gates (humans review broken code); single approval per run (too coarse
for regulated changes).
**Consequences.** Humans see only change sets that already pass. In the committed runs, approvals were made by the
build agent via the CLI with an explicit identity (`ORCH_APPROVER`), not impersonating the repo owner.

## ADR-011 — Lightweight OpenTelemetry-shaped tracing

**Context.** Per-node spans are required; the full OTel SDK and an exporter add dependencies and setup.
**Decision.** A ~90-line tracer producing OTel-shaped spans (trace id = run id, span/parent ids, status,
attributes), persisted in `state.db` and exported to `traces.jsonl`.
**Alternatives.** opentelemetry-sdk with an OTLP exporter (the production choice; the span shape maps 1:1).
**Consequences.** Zero setup; converting to OTLP is a small exporter.

## ADR-012 — Fault injection for the self-repair demonstration

**Context.** The brownfield scenario must show a retry with self-repair. A deterministic provider does not make
mistakes on its own.
**Decision.** An opset may declare a `first_attempt` variant (a realistic wrong fix: lower-case every lookup) and
`repair_signatures`. The provider emits the correct opset only when the retry feedback contains a recognised
signature, i.e. the name of the failing test. Unrecognised feedback repeats the wrong approach.
**Alternatives.** Random failures (flaky demo); a synthetic `raise` (unrealistic; does not exercise gates,
rollback and feedback).
**Consequences.** The failure travels the real path: gate fails → rollback → feedback → retry → gates pass. It is
scripted, and documented as such here and in the code.

## ADR-013 — Read-through cache keyed by canonical code; graceful degradation

**Context.** Redirects dominate traffic; aliases are case-insensitive while generated codes are case-sensitive.
**Decision.** Cache the small `Resolved` record under `link:{canonical code}`; mixed-case requests also probe the
lower-case alias entry (accepted only if `is_custom`). Only canonical (lower-case) misses are cached negatively
(5 s), so a cached "missing" can never shadow an alias created later under a different case. Create, update and
delete invalidate the entry. Expiry is evaluated at read time from the cached `expires_at`. Cache errors are
logged and fall through to the database.
**Alternatives.** Cache by requested code (stale case variants after invalidation); write-through (more
invalidation surface); no negative cache (enumeration hammers the DB).
**Consequences.** Correct invalidation with one key per link; mixed-case misses always hit the DB, mitigated by
per-IP rate limits.

## ADR-014 — Manual hardening is folded back into the blueprints

**Context.** Phase 5 requires reviewing the generated service and hardening it, while keeping lineage honest.
**Decision.** Review findings are fixed **in the blueprints** (the deterministic provider's equivalent of
improving prompts) and the scenarios are re-run. The committed service therefore remains byte-identical to
orchestrator output (`make verify-lineage`, `test_scenarios.py`). Findings fixed this way: the base62 alphabet
literal tripped the entropy secret scanner (built from `string` constants instead of suppressing the policy);
Alembic imports moved to module level; the streaming body-limit path raises inside the app so the handler
renders 413; the analytics purger's startup race (found when the orchestrator's own retry masked a flaky test);
`SHORTENER_REDIRECT_STATUS_CODE` from the environment was rejected (found while validating `.env.example`); the
load-check tool's client pool bottleneck. The alias case bug was seeded **deliberately** in greenfield
(`implement.links_api`) for the brownfield scenario.
**Alternatives.** Hand-edit `service/` after generation (breaks lineage); keep known defects (no).
**Consequences.** Every line of `service/` traces to a run. The cost is re-running the scenarios after each
blueprint fix; the BUILD_LOG records each one.

## ADR-015 — Shared workspace with disjoint write sets (not a worktree per node)

**Context.** Parallel nodes need isolation for rollback.
**Decision.** One workspace. Parallelism is allowed only between nodes with disjoint `write_paths` (validated),
rollback is path-scoped, and git operations are serialised.
**Alternatives.** A worktree per node merged at joins (stronger isolation, merge conflicts to handle).
**Consequences.** Simple and fast. Residual risk: a node's test gate can import a sibling's freshly written
module. The scaling path is worktrees or containers per node.

## ADR-016 — API keys: opaque, SHA-256 at rest, owner-only modification

**Context.** Write endpoints need authentication; operations must be simple; links must not be modifiable across
tenants.
**Decision.** `usk_<8-hex prefix>_<256-bit secret>`. The prefix is the indexed lookup; only `sha256(key)` is
stored. Verification uses `hmac.compare_digest`, with a dummy hash for unknown prefixes. Keys are revocable and
managed by a CLI. Links record `owner_key_id`; PATCH/DELETE by another key → 403. Writes are rate-limited per key.
Redirects, metadata and stats stay public.
**Alternatives.** JWT/OAuth2 (issuer, rotation and clock skew for no gain here); bcrypt/argon2 (unnecessary for
high-entropy random keys and slow on every request).
**Consequences.** Simple, fast and safe at rest. No scopes or expiry on keys yet (LIMITATIONS).

## ADR-017 — Service-only container image with pinned dependencies

**Context.** The orchestrator (LLM SDK, CLI) must not ship in the runtime image.
**Decision.** `requirements.service.in` → `requirements.service.txt`, compiled with versions constrained to
`uv.lock` (`make requirements-service`). Multi-stage build, non-root user, explicit migrate step, healthcheck.
**Alternatives.** Install the whole project (larger attack surface); unpinned requirements (non-reproducible).
**Consequences.** A small, reproducible image. The pin must be refreshed when the lock changes (CI could check
this; not yet automated).
