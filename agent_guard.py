"""agent_guard.py — zero-dependency guard rails for unattended autonomous LLM agents.

Five failure modes this module guards against:

1. Silent budget bleed — an agent that keeps making tiny "no-op" calls until the
   account is drained. Guarded by BudgetGuard (hard per-category caps) and
   RunLogger.no_op_rate() (visibility into the silent killer).
2. Runaway loops — an agent stuck retrying the same action forever. Guarded by
   LoopBreaker (repeated-signature detection + hard iteration cap).
3. Unbounded spend per call — a single expensive call blowing the budget.
   Guarded by the @guard decorator (pre-check + post-charge).
4. Escalation sprawl — an agent pinging the owner for everything, or worse,
   taking irreversible financial actions itself. Guarded by
   require_owner_action(), which restricts escalation to payout|purchase|setup|other.
5. Invisible history — no record of what ran, what it cost, or how it ended.
   Guarded by RunLogger's append-only JSONL log.

All state is plain JSON/JSONL on disk. No network, no pip installs, stdlib only.

Quick start
-----------
    from agent_guard import BudgetGuard, RunLogger, guard

    budget = BudgetGuard("state/budget.json", caps={"llm": 5.0, "api": 2.0})
    runs   = RunLogger("state/run-log.jsonl")

    @guard(budget, "llm", est_usd=0.05, logger=runs, agent="researcher")
    def call_model(prompt: str) -> str:
        ...  # your real LLM call; return the text
        return "ok"

Run `python agent_guard.py` to see a full end-to-end demo.
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

__all__ = [
    "BudgetExceeded",
    "StuckLoop",
    "BudgetGuard",
    "RunLogger",
    "LoopBreaker",
    "require_owner_action",
    "guard",
    "OWNER_ACTION_CATEGORIES",
    "VALID_OUTCOMES",
]

T = TypeVar("T")

VALID_OUTCOMES = ("shipped", "partial", "blocked", "failed", "no-op")
OWNER_ACTION_CATEGORIES = ("payout", "purchase", "setup", "other")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class BudgetExceeded(Exception):
    """Raised when a charge would push a category over its cap."""

    def __init__(self, category: str, attempted: float, cap: float, spent: float) -> None:
        self.category = category
        self.attempted = attempted
        self.cap = cap
        self.spent = spent
        super().__init__(
            f"budget exceeded for {category!r}: spent={spent:.4f} "
            f"attempted={attempted:.4f} cap={cap:.4f}"
        )


class StuckLoop(Exception):
    """Raised when an agent repeats the same signature too many times."""

    def __init__(self, signature: str, repeats: int, max_iterations: int) -> None:
        self.signature = signature
        self.repeats = repeats
        self.max_iterations = max_iterations
        super().__init__(
            f"stuck loop detected: signature {signature!r} repeated {repeats}x "
            f"(max_iterations={max_iterations})"
        )


# --------------------------------------------------------------------------- #
# Atomic JSON helpers
# --------------------------------------------------------------------------- #

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to `path` atomically via temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append one JSON object as a line to a JSONL file (append-only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# --------------------------------------------------------------------------- #
# 1. BudgetGuard
# --------------------------------------------------------------------------- #

class BudgetGuard:
    """Per-category spend caps that persist to a JSON file.

    A single *total* cap hides which line item is eating the budget. Caps are
    keyed by category ("llm", "api", "infra", ...) so the leak is visible.
    """

    def __init__(self, path: "str | os.PathLike", caps: Optional[Dict[str, float]] = None) -> None:
        self.path = Path(path)
        state = _load_json(self.path, {"caps": {}, "spent": {}})
        self.caps: Dict[str, float] = dict(state.get("caps", {}))
        self.spent: Dict[str, float] = dict(state.get("spent", {}))
        if caps:
            self.caps.update({k: float(v) for k, v in caps.items()})
        self._save()

    def _save(self) -> None:
        _atomic_write_json(self.path, {"caps": self.caps, "spent": self.spent})

    def set_cap(self, category: str, usd: float) -> None:
        self.caps[category] = float(usd)
        self._save()

    def spent_in(self, category: str) -> float:
        return float(self.spent.get(category, 0.0))

    def remaining(self, category: str) -> float:
        """USD left in a category. Returns inf if the category has no cap."""
        if category not in self.caps:
            return float("inf")
        return self.caps[category] - self.spent_in(category)

    def can_afford(self, category: str, usd: float) -> bool:
        return usd <= self.remaining(category) + 1e-12

    def charge(self, category: str, usd: float, note: str = "") -> float:
        """Charge `usd` to `category`. Raises BudgetExceeded if it would cross the cap.

        Returns the new remaining balance for the category.
        """
        usd = float(usd)
        if usd < 0:
            raise ValueError("charge amount must be >= 0")
        if not self.can_afford(category, usd):
            raise BudgetExceeded(
                category, usd, self.caps.get(category, float("inf")), self.spent_in(category)
            )
        self.spent[category] = self.spent_in(category) + usd
        self._save()
        if note:
            _append_jsonl(
                self.path.with_suffix(".charges.jsonl"),
                {"ts": time.time(), "category": category, "usd": usd, "note": note},
            )
        return self.remaining(category)

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        return {
            "caps": dict(self.caps),
            "spent": dict(self.spent),
            "remaining": {c: self.remaining(c) for c in self.caps},
        }


# --------------------------------------------------------------------------- #
# 2. RunLogger
# --------------------------------------------------------------------------- #

class RunLogger:
    """Append-only JSONL run log with a no-op rate metric.

    `outcome` must be one of shipped|partial|blocked|failed|no-op. The `no-op`
    outcome is the silent budget killer: a run that costs money and ships
    nothing. You cannot see it without a log.
    """

    def __init__(self, path: "str | os.PathLike") -> None:
        self.path = Path(path)

    def log_run(
        self,
        agent: str,
        outcome: str,
        cost_usd: float = 0.0,
        tokens: int = 0,
        duration_s: float = 0.0,
        note: str = "",
    ) -> Dict[str, Any]:
        if outcome not in VALID_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {VALID_OUTCOMES}, got {outcome!r}"
            )
        record = {
            "ts": time.time(),
            "agent": agent,
            "outcome": outcome,
            "cost_usd": float(cost_usd),
            "tokens": int(tokens),
            "duration_s": float(duration_s),
            "note": note,
        }
        _append_jsonl(self.path, record)
        return record

    def runs(self) -> list:
        return _read_jsonl(self.path)

    def no_op_rate(self, last_n: int = 20) -> float:
        """Fraction of the last `last_n` runs that were no-ops (0.0 if no runs)."""
        recent = self.runs()[-last_n:]
        if not recent:
            return 0.0
        no_ops = sum(1 for r in recent if r.get("outcome") == "no-op")
        return no_ops / len(recent)

    def total_cost(self, last_n: Optional[int] = None) -> float:
        runs = self.runs()
        if last_n is not None:
            runs = runs[-last_n:]
        return sum(float(r.get("cost_usd", 0.0)) for r in runs)


# --------------------------------------------------------------------------- #
# 3. LoopBreaker
# --------------------------------------------------------------------------- #

class LoopBreaker:
    """Detects a stuck agent by repeated action signatures + a hard iteration cap.

    Call `tick(signature)` once per loop iteration. If the same signature repeats
    `max_repeats` times in a row, or the total iteration count exceeds
    `max_iterations`, it raises StuckLoop.
    """

    def __init__(self, max_repeats: int = 3, max_iterations: int = 50) -> None:
        self.max_repeats = int(max_repeats)
        self.max_iterations = int(max_iterations)
        self._last_sig: Optional[str] = None
        self._repeat_count = 0
        self.iterations = 0

    def tick(self, signature: str) -> int:
        """Register one iteration. Returns the current iteration count."""
        self.iterations += 1
        if signature == self._last_sig:
            self._repeat_count += 1
        else:
            self._last_sig = signature
            self._repeat_count = 1

        if self._repeat_count >= self.max_repeats:
            raise StuckLoop(signature, self._repeat_count, self.max_iterations)
        if self.iterations >= self.max_iterations:
            raise StuckLoop(signature, self._repeat_count, self.max_iterations)
        return self.iterations

    def reset(self) -> None:
        self._last_sig = None
        self._repeat_count = 0
        self.iterations = 0


# --------------------------------------------------------------------------- #
# 4. require_owner_action
# --------------------------------------------------------------------------- #

def require_owner_action(
    reason: str,
    category: str,
    path: "str | os.PathLike" = "state/owner-actions.jsonl",
    agent: str = "agent",
) -> Dict[str, Any]:
    """Record a human-blocked item. Category MUST be payout|purchase|setup|other.

    Those are the only three (plus a catch-all) things an autonomous agent is
    allowed to escalate. Everything else is the agent's job. Raises ValueError
    for any other category — this is the enforcement, not a suggestion.
    """
    if category not in OWNER_ACTION_CATEGORIES:
        raise ValueError(
            f"category must be one of {OWNER_ACTION_CATEGORIES}, got {category!r}. "
            "If it is not a payout, a purchase, or a setup step, it is the agent's job."
        )
    record = {
        "ts": time.time(),
        "agent": agent,
        "category": category,
        "reason": reason,
        "status": "open",
    }
    _append_jsonl(Path(path), record)
    return record


# --------------------------------------------------------------------------- #
# 5. @guard decorator
# --------------------------------------------------------------------------- #

def guard(
    budget: BudgetGuard,
    category: str,
    est_usd: float,
    logger: Optional[RunLogger] = None,
    agent: str = "agent",
    cost_fn: Optional[Callable[[Any], float]] = None,
):
    """Wrap a function so it is budget-checked, charged, and run-logged.

    - Before the call: refuse to run if `est_usd` exceeds the category's remaining cap.
    - After the call: charge the actual cost. By default the actual cost is
      `est_usd`; pass `cost_fn(result) -> float` to derive it from the return value.
    - Always logs a run: `shipped` on success, `failed` on exception.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            if not budget.can_afford(category, est_usd):
                if logger is not None:
                    logger.log_run(agent, "blocked", 0.0, note=f"budget: {category}")
                raise BudgetExceeded(
                    category, est_usd, budget.caps.get(category, float("inf")),
                    budget.spent_in(category),
                )
            start = time.time()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - we re-raise after logging
                actual = est_usd
                try:
                    budget.charge(category, actual, note=f"failed call in {fn.__name__}")
                except BudgetExceeded:
                    pass
                if logger is not None:
                    logger.log_run(
                        agent, "failed", actual,
                        duration_s=time.time() - start, note=f"{fn.__name__}: {exc}",
                    )
                raise
            actual = float(cost_fn(result)) if cost_fn is not None else float(est_usd)
            budget.charge(category, actual, note=f"call {fn.__name__}")
            if logger is not None:
                logger.log_run(
                    agent, "shipped", actual,
                    duration_s=time.time() - start, note=fn.__name__,
                )
            return result

        return wrapper

    return decorator


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #

def _demo() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="agent_guard_demo_"))
    print(f"demo dir: {tmp}\n")
    try:
        budget = BudgetGuard(tmp / "budget.json", caps={"llm": 0.20, "api": 0.10})
        runs = RunLogger(tmp / "run-log.jsonl")

        @guard(budget, "llm", est_usd=0.05, logger=runs, agent="researcher")
        def call_model(prompt: str) -> str:
            return f"answer to: {prompt}"

        print("1) budget-guarded calls")
        for i in range(3):
            call_model(f"q{i}")
            print(f"   call {i + 1}: llm remaining = {budget.remaining('llm'):.4f}")
        try:
            call_model("one too many")
        except BudgetExceeded as exc:
            print(f"   blocked as expected -> {exc}")

        print("\n2) no-op rate (the silent budget killer)")
        runs.log_run("researcher", "shipped", 0.05)
        runs.log_run("researcher", "no-op", 0.01)
        runs.log_run("researcher", "no-op", 0.01)
        print(f"   no_op_rate(last 4) = {runs.no_op_rate(4):.2f}")
        print(f"   total cost logged  = {runs.total_cost():.4f}")

        print("\n3) loop breaker")
        lb = LoopBreaker(max_repeats=3, max_iterations=10)
        for i in range(5):
            try:
                lb.tick("same-action")
                print(f"   iteration {i + 1}: ok")
            except StuckLoop as exc:
                print(f"   iteration {i + 1}: STOPPED -> {exc}")
                break

        print("\n4) owner-action gate (only payout|purchase|setup|other allowed)")
        require_owner_action("attach the download ZIP", "setup", path=tmp / "owner.jsonl")
        print("   logged a 'setup' action: ok")
        try:
            require_owner_action("rewrite the landing page", "marketing", path=tmp / "owner.jsonl")
        except ValueError as exc:
            print(f"   rejected as expected -> {exc}")

        print("\n5) budget snapshot")
        for cat, rem in budget.snapshot()["remaining"].items():
            print(f"   {cat}: remaining {rem:.4f}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _demo()
