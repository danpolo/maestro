# M4a — Resolve the `pending()` placeholders before cutover

**Date:** 2026-08-16
**Stage:** M4a, an unplanned pre-M5 stage. Not in `docs/EXECUTION.md`'s stage list; opened because the
M4 session's finding #1 recorded a blocker phrased as "needs a real fix … *before M5 makes
`/approve`/`/fix` reachable against a live task*", and auditing that finding showed it understates
the problem by an order of magnitude.

**Precondition, verified this session:** `python3 -m pytest -q` → exit 0, zero `F`/`E` markers, on
commit `d60b1ec`. M0–M4 all green.

---

## The problem

`maestro/pending.py` exists because M1 extracts modules in a fixed dependency order, and an early
module sometimes calls a helper that the mapping table assigns to a later one. The extraction rule
forbids changing the call site, so the name is bound to a placeholder that **raises
`NotImplementedError` when called**. Its docstring states the contract plainly:

> Every placeholder is replaced by a real import when its owning module lands.

**That replacement never happened for any of them.** Thirteen placeholders are still live in seven
modules, and eleven of them have owning modules that were extracted for real in M1:

| Module | Name | Declared owner | Owner exists? |
|---|---|---|---|
| `maestro/gates.py:40` | `get_task_by_id` | `maestro.docs.roadmap` | ✅ |
| `maestro/quota.py:30` | `notify_telegram` | `maestro.hitl.telegram` | ✅ |
| `maestro/hitl/telegram.py:42` | `run_dep_map` | `maestro.docs.roadmap` | ✅ |
| `maestro/hitl/telegram.py:43` | `parse_runnable_tasks` | `maestro.docs.roadmap` | ✅ |
| `maestro/hitl/telegram.py:44` | `parse_prep_tasks` | `maestro.docs.roadmap` | ✅ |
| `maestro/hitl/commands.py:70` | `_finalize_manual_action` | `maestro.parking` | ✅ |
| `maestro/hitl/commands.py:71` | `park_regression` | `maestro.parking` | ✅ |
| `maestro/hitl/commands.py:73` | `attempt_self_fix` | `maestro.selfheal.selffix` | ✅ |
| `maestro/hitl/commands.py:75` | `_remove_from_state` | `maestro.orchestrator` | ✅ |
| `maestro/merge.py:28` | `_danreq` | `maestro.hitl.telegram` | ✅ |
| `maestro/merge.py:30` | `_judge_complete` | `maestro.judge` — **wrong**, it is `maestro.selfheal.diagnose` (plan L76) | ✅ |
| `maestro/merge.py:32` | `_smoke_for_task` | `maestro.smoke` — **wrong**, it is `maestro.gates` (plan L70) | ✅ |
| `maestro/merge.py:34` | `run_dep_map` | `maestro.docs.roadmap` | ✅ |
| `maestro/merge.py:36` | `_self_fix_path_ok` | `maestro.selfheal` → `maestro.selfheal.selffix` (plan L77) | ✅ |
| `maestro/merge.py:37` | `_redo_path_ok` | `maestro.selfheal` → `maestro.selfheal.redo` (plan L78) | ✅ |
| `maestro/hitl/commands.py:68` | `prep_actions` | `maestro.prep_actions` | ❌ **does not exist** |
| `maestro/docs/roadmap.py:42` | `prep_actions` | `maestro.prep_actions` | ❌ **does not exist** |
| `maestro/parking.py:74` | `prep_actions` | `maestro.prep_actions` | ❌ **does not exist** |

Two of the owner strings are simply wrong (`maestro.judge`, `maestro.smoke` are modules that were
never planned and do not exist); the M0–M1 plan's authoritative mapping table puts `_judge_complete`
in `selfheal/diagnose.py` and `_smoke_for_task` in `gates.py`.

### Why this blocks M5, not just `/approve` and `/fix`

The reference project's live loop calls these paths routinely. After cutover, every one of them is a
`NotImplementedError` at runtime:

- `merge_and_eval` → `_judge_complete`, `_smoke_for_task`, `run_dep_map`, `_self_fix_path_ok`,
  `_redo_path_ok`, `_danreq` — this is the **main merge path of the loop**, not a rare branch.
- `quota.py` → `notify_telegram` — the quota-exhaustion notification.
- `gates.py` → `get_task_by_id`.
- `telegram.py` → the roadmap parsers behind `/status` and `/progress`.
- `commands.py` → `/approve`, `/fix` (the M4 finding), plus the prepared-action sidecar behind
  `/reject`, `/manual` and the progress report.

The suite does not catch any of this because characterisation tests monkeypatch these names — that is
exactly what `pending()` was designed to allow. A placeholder is invisible to a test that replaces it.

### The third owner: `prep_actions` has no maestro home at all

`prep_actions` is used as a **module**, not a function (`prep_actions.get_action(...)`,
`.set_action`, `.remove_action`, `.has_action`) at nine call sites across `commands.py`,
`roadmap.py` and `parking.py`. Its reference implementation is `scripts/prepared_actions.py`
(107 lines) — which appears in `docs/DESIGN.md` §11's **"Scripts superseded at M5"** list. So M5 as
specified deletes the only implementation and leaves nothing behind it. It must be extracted first.

---

## Design decision: `deferred()`, not a module-level import

Rebinding these as ordinary top-level imports would create import cycles. The M1 dependency order is
`… → hitl/telegram → hitl/commands → selfheal → parking → orchestrator`, and the outstanding
placeholders all point *forwards* along that order — that is the whole reason they exist. A
top-level `from maestro.parking import park_regression` inside `commands.py` inverts an edge that
`parking.py` already depends on.

So `maestro/pending.py` gains a sibling:

```python
def deferred(name: str, owner: str) -> Callable[..., object]:
    """Late-bound reference to `owner.name`, resolved on first call."""
```

It imports the owning module on **first call**, not at import time, then delegates. Properties this
preserves, all of which matter:

- **Call sites stay byte-for-byte identical** — the M1 extraction invariant is not touched.
- **The name is still a plain module attribute**, so every existing characterisation test that does
  `monkeypatch.setattr(commands, "park_regression", stub)` keeps working unchanged.
- **No import cycle is possible**, because nothing is imported until the loop is actually running.
- Failure is still loud: an unresolvable owner raises at the call site with the owner named.

`prep_actions` is the exception — it is a module reference, not a callable, and
`maestro/prep_actions.py` will be a leaf (state + paths only). It gets a plain
`from maestro import prep_actions`.

---

## Tasks

### Task 1 — extract `maestro/prep_actions.py`
Verbatim copy of the reference `scripts/prepared_actions.py` under M1 rules: **function bodies
unchanged; only the import block and the derivation of module-level path globals may differ**
(`Paths.from_env()`, matching `commands.py:55`). Anything that looks wrong goes in
`docs/FOUND_BUGS.md` and is copied wrong anyway.
Plus `tests/characterization/test_prep_actions.py` — dual-subject, against the existing
`subject` fixture, one test per public function.

### Task 2 — `deferred()` in `maestro/pending.py`
Add the resolver above and unit-test it directly in `tests/test_pending.py`: resolves on first call,
delegates args/kwargs and return value, raises with the owner named when the owner is bogus, and is
still monkeypatchable as a module attribute.

### Task 3 — rebind all fifteen callable placeholders
Per the table above, with the two wrong owners corrected to the mapping table's answer. `pending()`
itself stays in the codebase (it is the honest thing to bind a genuinely-unextracted name to) but
must have **zero remaining call sites** in `maestro/` when this task is done.

### Task 4 — the gate that would have caught this
`tests/test_no_unresolved_pending.py`: import every module in the `maestro` package, walk its
module-level attributes, and fail on any that is still an unresolved `pending()` placeholder —
naming the module, the attribute and the declared owner. Then, for every `deferred()` binding,
assert the owning module imports and actually has the attribute, so a typo'd owner (`maestro.judge`)
fails at test time instead of at 3am in the live loop.

---

## Done when

1. `grep -rn 'pending(' maestro/ --include=*.py` returns only the definition in `pending.py`.
2. `tests/test_no_unresolved_pending.py` passes and would fail if any binding were reverted.
3. Full suite green with **zero skips** and a count ≥ 4192 (the M4 baseline) plus the new tests.
4. `AbuAliArchive` per-stage check unchanged.

## Out of scope

- The M4 finding #2 slug↔display-name mismatch in `limits.py`/`doctor`. Real, but it degrades to a
  `WARN` by design and blocks nothing at cutover. Left for M5/M6 as that finding already proposed.
- `maestro/watchdog.py`. Still unspecified (DESIGN.md §13); unchanged from the M4 judgement call.
