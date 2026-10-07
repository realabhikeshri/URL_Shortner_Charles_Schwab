# Clarification for REQ-AMBIGUOUS (ambiguity score 0.65)

- **enterprise-ready** (high): Which enterprise capabilities are in scope (authentication, rate limiting, caching, observability, SSO, multi-tenancy)?
  - default: Interpret as read-through caching of redirects, per-client rate limiting and Prometheus metrics. Authentication changes the public API contract, so it is NOT assumed; it is raised as open question Q-1.
- **faster** (medium): Faster for which endpoint, by how much, and under what load?
  - default: Target the redirect hot path - p95 latency under 50 ms with 20 concurrent clients on a developer laptop (SQLite), measured by scripts/loadcheck.py and enforced as a gate.

## Open questions
- Q-1: Is API-key authentication required on write endpoints?
- Q-2: Is there a production SLO (p95/p99, RPS) that should replace the laptop target?
