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
