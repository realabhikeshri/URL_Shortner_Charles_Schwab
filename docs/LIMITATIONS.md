# Limitations

What is simplified or not production-ready, and what it would take. Listed roughly by importance.

## Orchestrator

1. **The deterministic provider is not general.** It reproduces the three scenarios (and variants the catalog can
   express) from curated rules and templates. It cannot implement an arbitrary requirement. The engine, gates,
   policies, approvals, rollback, resume and re-planning are real and provider-independent. *Next:* run the same
   scenarios with Claude and compare against the recorded fixtures.
2. **The Anthropic provider has never been run against the live API in this build** (no key was available). It is
   tested against a fake SDK client (request shape, parsing, error mapping, cost) and follows the SDK's documented
   streaming and structured-output patterns. Structured output is "JSON in the system prompt + Pydantic validation +
   fallback", not API-enforced schemas. Expect prompt iteration on real use. `record` mode exists to capture fixtures.
3. **Self-repair and the mid-run requirement change are scripted for reproducibility** (ADR-012, ASSUMPTIONS 15-16).
   The failure path they exercise is real; their *trigger* is not organic.
4. **Approvals are not authenticated.** Approver identity comes from `ORCH_APPROVER`/git config and is recorded in
   a tamper-evident log, but anyone with CLI access can claim any identity. Production needs SSO, segregation of
   duties (approver != author) and signed approvals. In the committed runs the approver is the build agent.
5. **Audit log integrity is local.** Edits and re-ordering are detected; truncating the tail or regenerating the
   whole file is not, since the head hash is not anchored anywhere. Production: WORM storage or periodic signed
   anchors.
6. **Single process, single writer, one workspace.** No distributed workers; SQLite state; nodes share a git
   workspace and rely on disjoint write sets (ADR-015) so a gate can observe a sibling's in-flight file. See
   ARCHITECTURE §8 for the scaling path (worktree/container per node, Postgres, a durable workflow engine).
7. **Budgets are checked between scheduling ticks**, so in-flight nodes can overshoot a token/cost/time budget.
   The deterministic provider reports $0; cost accounting is exercised only by tests and the Anthropic price table.
8. **Policy checks are heuristic.** The secret scanner uses regex + entropy, PII-in-logs is an AST name match
   (a variable called `x` holding an IP is invisible), `pip-audit` for agent-added dependencies degrades to a pass
   when offline (`offline_ok`; CI's `make security` audits online), license checks rely on package metadata.
9. **No cross-run learning or evaluation harness.** Quality of a provider/model change is not measured against a
   benchmark.
10. **Static analysis targets Python/FastAPI/SQLAlchemy only**, and route/model introspection imports the target
    app in a sandboxed subprocess (it executes target code, in the sandbox's trust boundary).
11. **CI was written but never executed on GitHub** (no remote). Every CI step maps to a local `make` target that
    was run, but the workflow YAML itself is untested.
12. **Not implemented from the brief:** Redis-backed or distributed execution; interactive clarification beyond
    approve/reject with a comment; a web UI for approvals (CLI + HTML reports only).

## Service

1. **No URL reputation / malware check, no abuse reporting.** Only structural validation. No DNS resolution, so
   hostnames resolving to private ranges are not blocked (RISKS).
2. **Analytics are at-most-once and in-process.** Queue contents are lost on a hard crash; multi-replica
   deployments write independently (fine for Postgres). "Unique visitors" over a window is the sum of per-day
   uniques. No bot filtering beyond a user-agent family label. *Next:* a real queue and a stream processor.
3. **Cache**: in-memory fallback is per process (stale up to the TTL across replicas without Redis). No request
   coalescing on expiry.
4. **Auth is minimal:** opaque keys with no scopes, expiry or rotation workflow; a single trust domain; links
   created before auth have no owner. `/metrics` and `/docs` are unauthenticated and must be restricted at the
   ingress.
5. **Rate limiting keys on `request.client.host`.** Behind a proxy, uvicorn must be run with `--proxy-headers`
   and `FORWARDED_ALLOW_IPS` set (the image passes the flag; the allow-list is the operator's job).
6. **Database:** migrations are tested on SQLite and exercised on Postgres 16 via the compose stack (BUILD_LOG),
   but there is no Postgres in the automated test suite. No soft-delete purge job, no partitioning of `clicks`.
7. **Performance numbers are from a laptop**, one uvicorn worker, SQLite and warm cache. They show the hot path is
   sound, not capacity. No soak, multi-replica or Postgres load test.
8. **Single-region, no HA design**, no backup/restore procedure beyond what the database provides.
9. **Packaging:** `requirements.service.txt` must be regenerated when `uv.lock` changes (`make requirements-service`);
   not enforced in CI. The image is ~350 MB.
10. **Open product questions** (raised by the analyst and unanswered): Q-2, a production SLO to replace the laptop
    p95 target.
