# Security

## Scope and assets

Service: link integrity, API keys, click data, availability of redirects. Orchestrator: integrity of the code it
produces and of its audit trail, plus not leaking credentials to generated code.

## Threat model (STRIDE-lite)

| | Service | Orchestrator |
|---|---|---|
| **S**poofing | Writes need `X-API-Key` (SHA-256 at rest, constant-time compare, dummy hash for unknown prefixes). Redirects are public by design | Approver identity from `ORCH_APPROVER`/git config is *asserted*, not authenticated (limitation) |
| **T**ampering | Owner-only PATCH/DELETE (403); parameterised SQL via SQLAlchemy; strict request models (`extra=forbid`); body size limit | Workspace denylist (`policies/`, `.github/`, `orchestrator/`), per-node `write_paths`, atomic change sets, hash-chained audit, diff-hash on approvals |
| **R**epudiation | Request ids on every response and log line; structured logs | Every transition, decision, gate, policy result, approval (who/when/comment/diff hash) and override is in the chain |
| **I**nformation disclosure | No IP/UA stored (daily-salted visitor hash); access logs carry no PII; error envelope hides internals (500 is generic); keys shown once and never logged | Subprocess env is scrubbed (only PATH, HOME, LANG…), so API keys do not reach generated code; secret-scan policy on every diff |
| **D**enial of service | Token-bucket limits per key/IP; bounded queue with drop counter; body limit; cache; negative cache | Budgets, per-node timeouts, kill switch |
| **E**levation of privilege | Authorisation is by key ownership; no admin API | Per-agent permissions (`read`/`write`/`run`); no `shell=True`; argv-only; high-risk paths (`auth*`, migrations with destructive DDL) require approval; agents cannot edit policies |

## Controls in place

- **HTTP**: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive
  `Content-Security-Policy` on API responses (not on `/docs`), optional HSTS and CORS allowlist (`SHORTENER_*`).
- **Input**: URL validation (scheme, length 2048, credentials, private/loopback, control characters, IDNA),
  alias regex + reserved words, tag regex, JSON body limit.
- **Supply chain**: dependency allowlist and `pip-audit` policy for agent-added dependencies; `make security`
  runs bandit (medium+) and pip-audit on the repo (clean at the time of writing); Docker image is multi-stage,
  non-root, with a pinned service-only requirements file.
- **Secrets**: none in code (`SEC-001` regex + entropy policy; `.env.example` has placeholders only).
  `ANTHROPIC_API_KEY` is read from the environment and never persisted or logged.
- **Static analysis of generated code**: bandit, forbidden-calls (`eval`, `exec`, `os.system`, `pickle.load(s)`,
  `shell=True`), PII-in-logs check.

## Findings from this build (resolved)

- Entropy scanner flagged the base62 alphabet literal; the generated code was changed instead of weakening the policy.
- JUnit XML parsing of untrusted test output (bandit B314) replaced by parsing pytest's summary text.

## Known gaps (see LIMITATIONS)

No SSO/MFA for approvers; no key scopes/expiry; `/metrics` and `/docs` unauthenticated; no SSRF defence beyond IP
literals; no WAF/abuse-reporting pipeline; the audit head hash is not externally anchored; no formal pen-test.
