# Assumptions

Decisions taken without asking, per the build rules; consequential ones also have ADRs.

## Build and environment
1. Host had Python 3.9 and no `uv`: `uv` was installed with `pip --user` and fetched CPython 3.12. Python 3.11+ is the supported floor.
2. The deterministic provider is the default; Claude is opt-in via `ANTHROPIC_API_KEY` (ADR-003).
3. No git remote exists, so CI workflows were written and statically reviewed but **not executed on GitHub**.
4. Docker was available on the build host and the compose stack was run for real (BUILD_LOG).

## Product (from the three requirements)
5. "Enterprise-ready" (ambiguous scenario) means caching, rate limiting and metrics at first; **authentication is not assumed** until the mid-run requirement change answers Q-1. "Faster" means p95 redirect latency < 50 ms at 20 concurrent clients on a laptop with SQLite.
6. Redirect is 302 by default (307 configurable) to keep disable/delete/analytics effective (ADR-006).
7. Aliases are case-insensitive (stored lower-case); generated codes are case-sensitive.
8. "Unique visitors" are counted per day and summed across a window (an upper bound).
9. Analytics retention default is 90 days; at-most-once delivery of click events is acceptable.
10. Existing links created before auth have no owner and remain modifiable by any valid key.
11. A single trust domain: any valid API key may create links; ownership only restricts modification.
12. Private/loopback targets are rejected by default; the operator may allow them for internal deployments.

## Orchestrator
13. Risk tiers: low = read/docs/tests, medium = schema, dependencies, release sign-off, high = auth/security paths, destructive DDL, a run whose risk class is high.
14. Auto-approval is a demo/CI affordance. The committed brownfield and ambiguous evidence used **human CLI approvals** (identity recorded as the build agent via `ORCH_APPROVER`).
15. The mid-run requirement change in the ambiguous scenario is scripted (`requirements/ambiguous.events.yaml`) to be reproducible; detection and re-planning are the engine's generic hash-based mechanism, as if a person had edited the file.
16. The brownfield alias fix's naive first attempt is scripted to demonstrate self-repair through the real failure path (ADR-012).
17. Approval wait times in committed runs reflect an agent running CLI commands, not human latency.
18. One workspace, one writer: runs are not concurrent against the same workspace.
