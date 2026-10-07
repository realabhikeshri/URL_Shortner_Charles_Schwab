# Risk register

L = likelihood, I = impact (H/M/L). "Residual" is what remains after the listed mitigations.

## Service (Layer B)

| Risk | L | I | Mitigation (where) | Residual |
|---|---|---|---|---|
| Abuse for phishing/malware links | H | H | http/https only; credentials in URLs rejected; per-key write rate limits; API key required to create; owner-only modification; soft delete + 410 (`domain/urls.py`, `auth.py`, `ratelimit.py`) | No reputation/safe-browsing check, so a malicious target from a valid key is still shortened. Needs a URL-reputation service and abuse reporting |
| SSRF-style targets (private/loopback/metadata IPs) | M | H | IP literals and local host names rejected by default (configurable). The service never fetches targets | Hostnames that *resolve* to private IPs are not blocked (no DNS resolution; DNS rebinding makes a check racy). Irrelevant while nothing fetches, relevant if previews are added |
| Code enumeration / scraping | M | M | 62^7 random space, no sequential ids; per-IP redirect rate limit; short negative cache | Public redirects reveal valid codes to a patient attacker; CDN/WAF rules advisable |
| Hot keys (viral link) | M | M | Read-through cache (memory/Redis); analytics batched off the hot path; 3.3k req/s single-worker measured | Redis hot-key and thundering herd on expiry are untested. Add request coalescing |
| DB growth (clicks) | H | M | `analytics_retention_days` purge (default 90); composite index; batch inserts | Purge is `DELETE`; at volume partition by month or move to a column store |
| Analytics loss | M | L | Bounded queue with counted drops; failures never reach the redirect | At-most-once: a hard crash loses queued clicks (documented, ADR-008) |
| Cache poisoning / staleness | L | M | Invalidation on create/update/delete; TTL 300 s; negative TTL 5 s; canonical keys | Multi-replica with memory cache (no Redis) can serve stale entries up to the TTL |
| API-key leak | M | H | SHA-256 at rest, never logged, revocable, shown once; per-key limits | No key expiry/scopes/rotation workflow yet |
| PII in logs/DB | L | H | CMP-001 policy (AST check on log calls); access log has no IP/UA/query; visitor hashes salted daily | Salt secret must be set in production (`SHORTENER_ANALYTICS_SALT_SECRET`), else it is per-process |
| Unauthenticated `/metrics`, `/docs` | M | L | Intended for an internal network | Restrict at the ingress or add auth; documented in OPERATIONS |
| Idempotency-Key misuse | L | L | Fingerprint check (409), scoped per owner | Keys are never expired |

## Orchestrator (Layer A)

| Risk | L | I | Mitigation (where) | Residual |
|---|---|---|---|---|
| LLM-hallucinated or wrong code | H | H | Nothing is trusted: compile, ruff, targeted tests, coverage, mypy, migration round-trip, bandit, secret scan, forbidden calls; Reviewer **runs** the tests behind each acceptance criterion and cannot approve on LLM say-so | Tests are agent-written too; a model could write weak tests that pass. `tests_fail_first` helps for bug fixes only. Human review of high-risk diffs remains the backstop |
| Prompt injection via repo content | M | H | Agents only propose `ChangeSet`s; the sandbox allowlist/denylist and `write_paths` cap what a hijacked agent can touch (`policies/`, `.github/`, `orchestrator/` are unwritable); no shell, scrubbed env; secrets are not in the subprocess environment | An injection can still produce *plausible bad code* inside allowed paths; relies on gates and approvals. The deterministic default has no injection surface |
| Runaway cost / loops | M | M | Hard budgets (tokens, cost, wall time, files, retries per run); per-node attempt cap; budget exhaustion falls back to the deterministic provider | Budget is checked between scheduling ticks, so in-flight nodes can overshoot |
| Approval fatigue / rubber-stamping | M | H | Approvals requested only after automated gates pass; risk-tiered; high never auto; approver identity, comment and diff hash recorded; diff drift invalidates | Humans can still approve without reading. Evidence in `runs/` shows reviewed claims, but nothing *forces* review depth |
| Partial writes / corrupt workspace | L | H | Change sets validated first, then written; path-scoped rollback; crash recovery rolls back RUNNING nodes on resume; SQLite writes committed per transition | A crash between "files written" and "state row updated" is handled by rollback on resume, but not fault-injected at every point |
| Audit tampering | L | H | Hash chain verified in CI for committed runs | Head hash is not anchored externally; tail truncation or full regeneration by a writer is undetected |
| Parallel nodes interfering | M | M | DAG rejects overlapping write sets; git ops serialised | A gate may observe a sibling's in-flight file (ADR-015) |
| Policy engine bug fails open | L | H | A crashing check *fails closed*; each check has tests | Coverage of policies is by example, not exhaustive |
| Human edits lost or overwritten mid-run | M | M | Manifest hash drift → explicit commit + re-plan, invalidating dependants | Only files the engine committed are watched |
| Provider outage | M | M | Fallback to deterministic provider, recorded and counted | Fallback quality is limited to the catalog (LIMITATIONS) |
