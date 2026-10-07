# Committed run evidence

Each folder is one orchestrator run (`<scenario>-<UTC timestamp>`), produced by the commands in
`docs/SCENARIOS.md`. Open `report.html` in a browser (self-contained) or read `report.md` on GitHub.

| Run | Scenario | Result | Highlights |
|---|---|---|---|
| [`greenfield-20261007T000413`](greenfield-20261007T000413/report.md) | Build the core service from an empty workspace | 14/14 nodes | parallel `implement.codes` / `implement.urls`; tests + docs branch and join at review; 2 gates |
| [`brownfield-20261007T000441`](brownfield-20261007T000441/report.md) | Analytics, expiry, alias case bug | 14/14 nodes | impact analysis; red-first regression test; naive fix fails -> rollback -> self-repair; **human-approved migration** |
| [`ambiguous-20261007T000933`](ambiguous-20261007T000933/report.md) | "enterprise-ready and faster" | 17/17 nodes | clarification gate; mid-run requirement change -> **dynamic re-plan** (risk medium -> high); 3 human-approved high-risk gates; p95 gate |

Cross-run reliability metrics: [`metrics.json`](metrics.json), [`metrics.prom`](metrics.prom) (Prometheus text).

Per-run files:

| File | What it is |
|---|---|
| `report.md`, `report.html` | DAG (Mermaid / SVG), timeline, gates, policy results, approvals, decisions, re-plans, metrics, artifacts |
| `audit.jsonl` | append-only, hash-chained event log (`orchestrator audit verify <run>`) |
| `state.db` | SQLite run state (nodes, transitions, artifacts, decisions, approvals, spans) - enables `resume` |
| `decisions.json` | decision records with parent links (lineage back to the requirement) |
| `metrics.json`, `traces.jsonl` | run metrics; OpenTelemetry-shaped spans |
| `plan.mmd`, `plan.v2.mmd` | plan DAG (and the re-planned DAG) as Mermaid |
| `diffs/` | the exact change set of every attempt, including failed/rolled-back ones |
| `changes.patch` | the run's net change to the workspace |
| `requirement.*`, `clarification.md`, `impact.json` | requirement of record, clarification questions/assumptions, impact analysis |
