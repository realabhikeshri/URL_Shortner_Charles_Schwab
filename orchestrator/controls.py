"""Run controls: bounded retries with backoff + jitter, hard budgets and the kill switch."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field


class RetryPolicy(BaseModel):
    """Exponential backoff with full jitter: sleep ~ U(0, min(cap, base * 2**(attempt-1)))."""

    base_delay_s: float = Field(0.5, ge=0)
    max_delay_s: float = Field(10.0, ge=0)
    jitter: bool = True

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        ceiling = min(self.max_delay_s, self.base_delay_s * float(2 ** max(attempt - 1, 0)))
        if not self.jitter:
            return ceiling
        return float((rng or random).uniform(0, ceiling))


class Budgets(BaseModel):
    max_wall_time_s: float = Field(900, gt=0)
    max_llm_tokens: int = Field(2_000_000, gt=0)
    max_llm_cost_usd: float = Field(25.0, gt=0)
    max_files_changed: int = Field(200, gt=0)
    max_retries_per_run: int = Field(12, ge=0)


@dataclass
class BudgetUsage:
    tokens: int = 0
    cost_usd: float = 0.0
    files_changed: set[str] = field(default_factory=set)
    retries: int = 0
    wall_time_s: float = 0.0


class BudgetBreach(Exception):
    def __init__(self, budget: str, used: float, limit: float) -> None:
        super().__init__(f"budget '{budget}' exceeded: {used} > {limit}")
        self.budget = budget
        self.used = used
        self.limit = limit


class BudgetTracker:
    def __init__(self, budgets: Budgets, *, elapsed_before_s: float = 0.0) -> None:
        self.budgets = budgets
        self.usage = BudgetUsage()
        self._started = time.monotonic() - elapsed_before_s

    def add_tokens(self, tokens: int, cost_usd: float) -> None:
        self.usage.tokens += tokens
        self.usage.cost_usd += cost_usd

    def add_files(self, paths: list[str]) -> None:
        self.usage.files_changed.update(paths)

    def add_retry(self) -> None:
        self.usage.retries += 1

    def remaining_tokens(self) -> int:
        return max(self.budgets.max_llm_tokens - self.usage.tokens, 0)

    def check(self) -> BudgetBreach | None:
        self.usage.wall_time_s = time.monotonic() - self._started
        b, u = self.budgets, self.usage
        checks: list[tuple[str, float, float]] = [
            ("max_wall_time_s", u.wall_time_s, b.max_wall_time_s),
            ("max_llm_tokens", u.tokens, b.max_llm_tokens),
            ("max_llm_cost_usd", u.cost_usd, b.max_llm_cost_usd),
            ("max_files_changed", len(u.files_changed), b.max_files_changed),
            ("max_retries_per_run", u.retries, b.max_retries_per_run),
        ]
        for name, used, limit in checks:
            if used > limit:
                return BudgetBreach(name, round(used, 3), limit)
        return None


class KillSwitch:
    """A run stops when ``runs/<run_id>/STOP`` exists (written by ``orchestrator stop``)."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "STOP"

    def trigger(self, reason: str) -> None:
        self.path.write_text(reason or "stop requested", encoding="utf-8")

    def triggered(self) -> str | None:
        if self.path.exists():
            return self.path.read_text(encoding="utf-8").strip() or "stop requested"
        return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
