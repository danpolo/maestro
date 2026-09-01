# Lessons

Corrections Dan made to how a session worked, with the reasoning, so they change behaviour rather
than just recording an apology. Newest first.

---

## 2026-08-30 — Don't serialize agent work when the real constraint is the git index, not the task graph

**What Dan said:** "is A1 blocking everything? why cant you run more agents paralely?" — and then,
after I explained the single-working-tree problem: "if git worktrees is the blocker cant you just
create worktrees/branches when needed and delete hem when finish?"

**What I had been doing.** Running the queue's items strictly one at a time, because the plan's
coordination section said "one session at a time" and the subagent-driven-development skill forbids
parallel implementers. I treated both as settled and did not test the premise.

**Why that was wrong.** The plan's serialization existed to prevent *file collision*, and the
skill's rule exists because two agents committing into one working tree corrupt each other's
commits via the shared index. Neither is a statement about the dependency graph. Most items in the
queue touched disjoint files and had no stated dependency on each other — A1 (orchestrator/quota)
and A3 (implementer/switch) shared nothing at all. The binding constraint was one git index, and a
git worktree removes exactly that constraint. Dan saw this immediately; I had accepted an inherited
rule as a law of nature.

**What to do instead.** Before serializing a queue of work, separate three things: (1) genuine
dependencies the plan states, (2) shared files, (3) shared mutable infrastructure. Only (1) forces
sequence. (2) forces "not in the same wave." (3) is usually removable — a worktree per item, on its
own branch, merged back in plan order:

```
git worktree add <persistent-path>/wt-<item> -b sdd/<item> <current head>
```

Verify once that the worktree's cwd shadows any editable install, so each agent tests **its own**
source rather than the main checkout's — this is the failure that would make the whole scheme
silently useless. Wave 1 then ran four implementers plus reviewers concurrently and merged with zero
conflicts, and the merged test count composed exactly (3110 + 12 + 10 + 4 + 2 = 3138), which is how
you prove the fan-out lost nothing.

**Generalisation:** when a constraint blocks obviously-parallel work, ask what the constraint is
actually made of before accepting it. "The tool only supports one" is often "the tool only supports
one *per directory*."

---

## 2026-08-30 — Agent scratch that holds uncommitted work does not belong in `/tmp`

**What happened, unprompted by Dan but worth keeping.** I put the run's workspace and four git
worktrees under the session scratchpad in `/tmp`. `/tmp` was cleared mid-run. All four worktrees
were destroyed, and because no agent had committed yet, every edit from four parallel implementers
was lost — roughly an hour of work across four agents.

**Two rules that follow.**

1. **Anything holding uncommitted work goes on persistent storage.** The workspace now lives at
   `/home/dan/.maestro-sdd/`. It must stay *outside* the repo as well: `tests/test_purity.py` scans
   the **working tree**, not the git index, so a git-ignored scratch directory containing plan
   excerpts still fails the reference-string gates. Moving it out was the right fix; weakening the
   gate to exempt it would have been wrong, since strengthening that same gate is a queue item.
2. **Tell every implementer to commit as soon as it has something green**, rather than accumulating
   a large uncommitted state and committing once at the end. The wipe cost four items precisely
   because nothing had committed. Recoverable work is committed work.

**Related, same root:** three subagents ended their turn parked on a background test run or a
Monitor notification that cannot arrive once the turn ends. Instruct implementers to run the suite
in the **foreground** with a generous tool timeout. A stranded agent looks identical to a working
one until you check.

---

## 2026-09-01 — A green suite cannot see a spelling production never produces

**What happened.** A4 shipped a context-rotation trigger with 27 new tests, all green. The trigger
was a guaranteed no-op. `context_crossed` fell back to `Usage.model`, which in production is the
statusline's display name `"Opus 5"` — normalising to `"opus-5"`, which matches no table key. Every
test injected the slug `"claude-opus-5"` instead. The tests and the code agreed perfectly with each
other and both disagreed with production.

**The rule.** When a lookup is keyed on a value that *arrives from outside the process* — a CLI's
output, another tool's JSON, an operator's config — at least one test must use the spelling that
source actually emits, and the review must check that it does. A fixture is a statement about what
the author believes the world sends. It is not evidence about the world.

The reviewer caught it by reading the live `.orchestrator/usage.json` and running the normaliser
against the real table, rather than reasoning from the diff. That is the move worth repeating:
for any boundary value, go and look at one real instance.

## 2026-09-01 — Persistent worktrees paid for themselves four times in one session

Four separate rate-limit waves killed agents mid-task. In every case the uncommitted work survived,
because the workspace and worktrees live at `/home/dan/.maestro-sdd/` rather than in `/tmp` (the
lesson from 2026-08-30, applied). Combined with "commit as soon as you have something green", the
cost of a 429 dropped from *an item* to *a resume message*.

Two refinements learned this session:

1. **After a rate-limit loss, tell the resumed agent to work in value order** — load-bearing
   adjudications and binding-constraint verdicts first, general polish last — so a second cut-off
   costs minors rather than the finding the round exists to produce.
2. **Re-orient the resumed agent from its own diff, not its recall.** `git diff` in the worktree is
   the truth; a resumed agent's memory of where it was is not.

## 2026-09-01 — Two defect classes that look alike and invert

This queue exists to remove **declared-but-unread** config: a knob an operator sets that no code
reads, so they see no error and get no effect. C3 produced its mirror image — **read-but-undeclared**:
the wiring was correct but the key was absent from the scaffolded template, so an operator could not
discover the knob existed without reading source.

They need opposite fixes, and confusing them makes things worse: the instinct on seeing "knob not in
template" is to add config-reading code, which here would have duplicated an already-correct read
path. Name the direction before fixing.

## 2026-09-01 — Implementers reach for the reference project's name while removing it

Two independent implementers (B1, B2) had their *first draft of a test about removing the reference
project* trip `tests/test_purity.py` by naming that project. Both caught it in self-review.

The failure mode a decoupling gate must survive is not a careless author — it is a careful one
writing a test about the removal. D1's gate must therefore scan `tests/` as well as `maestro/`, and
its review must distinguish "genuinely generic wording" from "obfuscated enough to pass the gate
while still encoding the project."

## 2026-09-01 — Ask the reviewer, don't pre-judge, when you spot something yourself

Twice I noticed a probable defect while packaging a diff (C3's missing template declaration; C7's
budget-checked-before-a-probe arithmetic). Both times I handed it to the reviewer as an open question
with the reasoning, rather than ruling on it or instructing a fix. Both came back confirmed *with
evidence I did not have* — that both sibling threshold keys are declared in the template, and that
the true probe bound is ~24s not ~16s, with a worked example.

Pre-judging would have produced the same fix with a weaker justification, and would have skipped the
independent check that the fix was even the right one.
