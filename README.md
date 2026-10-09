# agent_guard — free guard rails for unattended agents

**One file. Zero dependencies. Stdlib only.** Drop `agent_guard.py` next to your agent and
import it. No `pip install`, no account, no network calls.

This is the free subset of the *Agent Ops* toolkit: the five guards that stop an unattended
agent from quietly burning money while it looks busy.

## The five failure modes it guards against

| # | Failure mode | What it costs you | Guard |
|---|---|---|---|
| 1 | **Silent budget bleed** — tiny "no-op" calls until the account is drained | the whole budget, invisibly | `BudgetGuard` (per-category caps) + `RunLogger.no_op_rate()` |
| 2 | **Runaway loop** — stuck retrying the same action forever | tokens × ∞ | `LoopBreaker` (repeat-signature + hard iteration cap) |
| 3 | **Unbounded single call** — one expensive call blows the month | the month | `@guard` decorator (pre-check + post-charge) |
| 4 | **Escalation sprawl** — agent pings the owner for everything, or acts on money itself | trust + real money | `require_owner_action()` (only `payout\|purchase\|setup\|other`) |
| 5 | **Invisible history** — no record of what ran, what it cost, how it ended | you can't fix what you can't see | `RunLogger` (append-only JSONL) |

## Install

```bash
# just copy the file
cp agent_guard.py /your/project/
```

Requires Python 3.8+. Nothing else.

## 30-second example

```python
from agent_guard import BudgetGuard, RunLogger, LoopBreaker, guard

budget = BudgetGuard("state/budget.json", caps={"llm": 5.0, "api": 2.0})
runs   = RunLogger("state/run-log.jsonl")

@guard(budget, "llm", est_usd=0.05, logger=runs, agent="researcher")
def call_model(prompt: str) -> str:
    # ... your real LLM call here ...
    return "ok"

call_model("summarise this")          # charged, logged as "shipped"
print(budget.remaining("llm"))        # 4.95
```

## The three-line guard for each failure mode

**1. Budget bleed** — cap by *category*, not by total. A total cap hides which line is eating
the budget.

```python
budget = BudgetGuard("state/budget.json", caps={"llm": 5.0, "api": 2.0})
budget.charge("llm", 0.03, note="summarise")   # raises BudgetExceeded past the cap
```

**2. Runaway loop** — one `tick()` per iteration; it raises `StuckLoop` on the 3rd identical
signature.

```python
lb = LoopBreaker(max_repeats=3, max_iterations=50)
while True:
    lb.tick(action_signature)   # raises StuckLoop when stuck
    do_the_thing()
```

**3. Unbounded call** — wrap the expensive function; it is checked before and charged after.

```python
@guard(budget, "llm", est_usd=0.05, logger=runs, agent="writer")
def expensive(prompt): ...
```

**4. Escalation sprawl** — the only things an agent may escalate are payouts, purchases, and
setup steps it cannot do. Anything else raises `ValueError`.

```python
require_owner_action("attach the ZIP in Gumroad", "setup")   # ok
require_owner_action("rewrite the landing page", "marketing") # ValueError — that's your job
```

**5. Invisible history** — append-only JSONL, plus the metric that actually matters:

```python
runs.log_run("researcher", "no-op", cost_usd=0.01)
print(runs.no_op_rate(last_n=20))   # fraction of recent runs that shipped nothing
```

## Why `no_op_rate` is the number to watch

A run that costs money and ships nothing is invisible in a spend total — it looks like a cheap
run, not a wasted one. `no_op_rate()` makes it visible. If it climbs above ~0.3, your agent is
spinning, and the fix is a rule change, not a bigger budget.

## Run the demo

```bash
python agent_guard.py
```

Prints a full end-to-end transcript: budget-guarded calls, a blocked call, the no-op rate, a
loop that gets stopped, and an owner-action that gets rejected.

## License

MIT — see `LICENSE.txt`. Use it commercially, fork it, ship it inside your product.

---

*This is the free subset. The full **AI Agent Ops Starter Kit** (budget guard, multi-provider
LLM router, decision log, distribution-first checklist) is at
https://renevibe76.gumroad.com/l/iaofux*
