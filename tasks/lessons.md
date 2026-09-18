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

## 2026-09-02 — "Mutate a copy, not the checkout" is not enough; name the isolation

Two reviewers mutated state they were told not to. The first edited its worktree directly and
restored it correctly, so I added "mutate a copy, never the checkout" to every later dispatch. The
second *followed that rule* and still detached the worktree's HEAD two merges backwards — its scratch
copies shared the worktree's gitdir, so git commands run from them acted on the real repository. It
then died mid-restore.

Nothing was lost, but only because the commits already existed: the branch still pointed at the fix
head, every tracked file already matched it, and both untracked files were verified byte-identical to
their committed blobs by `sha256` before restoring.

A copy is not isolated because it is a copy — it is isolated when no `.git` resolves above it. Say
that explicitly: *copy to `/tmp/<name>/`, outside the repository, and never run a git command from
there.* The durable fix is smaller still: a reviewer's job is reading and running tests. Any
experiment that needs mutation should be run by the implementer under the controller's direction, so
that mutation only ever happens where a commit already protects it.

## 2026-09-02 — Tell the implementer to re-verify the line, not to apply the token

Three times I folded a one-token correction into a documentation line and added "re-check the whole
line/paragraph against primary evidence rather than applying my token." Every time it found something
I had missed: `launch.sh` alongside the `systemd/` I had named; `.gitignore` and the pre-commit hook
alongside those; and two further places a paragraph silently assumed three switch triggers where the
code has four. A fourth pass found two more over-broad sentences beside the one under repair.

The instruction costs one clause and repeatedly outperformed my own reading of the same line.

## 2026-09-02 — A roll-up built by grepping the ledger inherits the ledger's phrasing drift

I built the final review's deferred-minors list by grepping ~2000 ledger lines for the phrases I
thought I had used. It missed 13 of ~32 findings, because three sessions had written that ledger and
the wording drifted. The final reviewer found and triaged them anyway, and told me.

Build the triage list incrementally as each finding is deferred. A retrospective pattern-match over
your own prose is not a record; it is a guess about how consistent you were.

## 2026-09-02 — The same question, asked about the neighbouring axis

The one defect this queue's process missed came from a ruling that was correct but under-scoped. When
item C4 made the *backend*-resolution paths diverge, I split the finding properly — docstring to the
documentation item, code to a filed follow-up — and judged the hazard latent because nothing yet fed
either path divergent data. Right answer.

I never asked the same question about *model* resolution. There it was already live: item C1 had made
a task's `model:` win at the launch site while the context-ceiling lookup still used the role table,
so a task pinned to a larger model was measured against a smaller model's ceiling and had its
conversation discarded ~20K early. Two of the final review's five Important findings were that one
unasked question.

When a ruling establishes "two paths that only look like one," immediately ask which *other* values
travel those same two paths.

## 2026-09-09 — A gap already owned by a setup skill is not a decision for the calling session

Closing the graph-engineering architecture session, I told Dan to "decide early" whether DuetFlow
needs a `pyproject.toml`, framing the missing manifest as a P0 blocker that would make the baseline
measurements meaningless. He pushed back: preparing a target repo is the maestro setup skill's job.

He was right, and the skill already says so verbatim — `maestro-setup` Step 1's pre-flight checks
name this exact failure (`tests/` plus a `.venv` but no root manifest → empty `test_command` →
`adapters/test` fails closed on every run) and require resolving it with the operator before `init`.
I had read the P0 plan and the blind-spots file, but not the skill that owns the step P0 depends on.

Before escalating a prerequisite into a decision for the next session, check whether an existing
skill or script already owns it. Otherwise you hand back a question that was answered before you
asked it.

## 2026-09-13 — `monkeypatch.delenv(..., raising=False)` contains nothing when the var is absent

P11's operator-view tests call `cli.main(...)` in-process, and every `cmd_*` sets
`os.environ["MAESTRO_REPO"]` without restoring it (deliberate: a real invocation is a fresh
process, one project per process). I contained the leak with
`monkeypatch.delenv("MAESTRO_REPO", raising=False)` and three `tests/test_selfupdate.py`
assertions kept failing — in a different file, a long way from the cause: the leaked root made
`selfupdate.adopt` take the new per-project-pin branch instead of writing `~/.maestro/current`.

`monkeypatch.delitem` records an undo entry *only when the key is present*. Deleting an absent
variable therefore registers nothing, and a later plain `os.environ[...] = ...` survives teardown
and every subsequent test in the session.

Contain an environment variable a test's code-under-test *writes* with an explicit
snapshot/restore fixture, not with `delenv(raising=False)`. And when a failure appears in a file
you did not touch, suspect process-global state before suspecting your change to the other file.

## 2026-09-18 — Measure the context before naming the next task, and close out on the seam

Having finished F6's lane tests I reported the numbers and announced "starting F7 now unless you
want something else", citing context as "roughly 50K — comfortably inside the 160K handoff line".
The measured figure was 156K. I was at the handoff line, not comfortably inside it, and F7 is the
largest of the three Fs: starting it would have produced a half-built stage at the ceiling.

Two distinct errors, and the second is the one that bit:

1. I estimated the context instead of running
   `python3 ~/.claude/skills/close-session/scripts/context_usage.py`, which CLAUDE.md explicitly
   requires ("Measure the context, don't estimate it"). A three-fold underestimate is not a near
   miss — the percentage (16%) reads reassuring while the absolute count is the thing that binds.
2. Finishing a committable unit of work *is* the seam a session closes on. I treated it as a
   checkpoint to narrate and pushed straight on to naming the next job, so the sweep, the STATE
   update and the handoff only happened when Dan asked why they hadn't.

Measure at every commit, not once per session; and when a unit lands, run the close-session sweep
before proposing the next one rather than after being asked for it.

## 2026-09-18 (later) — Doing the skill's job by hand is still skipping the skill

Same day, same screw, one turn further. This time I did measure the context (164K), did stop at
the handoff line instead of starting F7, and did write a handoff. Then Dan asked why I hadn't used
`close-session`. I hadn't used `handoff` either. I had hand-rolled both.

The rationalisation is worth recording because it is not obviously wrong: the context-governor
hook said "write a handoff, and close the session", and the F7 brief said "write a handoff at
160K". I executed both sentences literally, produced a file that looked like the deliverable, and
never checked whether a skill owned the job. Having something that resembles the output is exactly
when the skill check feels most skippable and is most load-bearing — the skill is a checklist, and
a checklist's value is the items you would not have thought of.

What the hand-rolled version missed, concretely: `docs/graph-engineering/STATE.yaml:693` still
pointed the next session at the superseded handoff. `next_graph_prompt.py` prints from that field,
so the next session would have been handed the old brief and re-derived the whole code survey — the
~70K this session spent earning it, spent again. The sweep step catches that; my summary did not,
because I was recalling what I'd done rather than walking a list against disk.

Producing the artifact is not the same as running the process that decides which artifacts are
needed. When a skill exists for the job, invoke it *and then* write the file — not the reverse,
and not instead.

---

## A green lane plus a stalled lane are the same bug class: nobody ran the code

2026-09-18, P12B F7.

Two things went wrong in the same feature, and they have one root.

**A lane that polls to `MAX_POLLS` with nothing failed is an exception swallowed inside
`advance`.** `_failure_context` selected `attempt_index`, a column `attempts` has never had.
The `OperationalError` fired inside `advance`, the event loop's `except Exception` turned it
into "that task didn't work out", and the attempt sat `claimed` with no binding while the loop
polled forever. The previous session spent its remaining budget theorising about which route
the implementer had bound. The answer cost one run:

```python
real = wf_runner.WorkflowRunner.advance
def advance(self, node, **kw):
    try: return real(self, node, **kw)
    except BaseException: traceback.print_exc(); raise
monkeypatch.setattr(wf_runner.WorkflowRunner, "advance", advance)
```

Reach for that *before* theorising about the scenario. A stalled lane with no failure is
almost never a routing mistake — routing mistakes produce a bound route you can read off the
attempt row.

**Why no test had caught it:** the shipped `diagnoser` role declares
`min_reasoning_strength: strong`, so in every lane before F7's the diagnoser was refused a
route *before* the claim, and everything downstream of the claim had never run. Unit tests
covered the compile, the verdict round-trip and the router floor — all the parts — and nothing
ran the path.

**The same root, worse form.** Asking "what else did those unexercised paths hide?" found that
the failure context is assembled, stored, flagged onto the worker argv, parsed by the worker —
and never read. `grep -rn 'failure_context' maestro/` returns only producers. The brief the
model sees is built from the context manifest. So the escalation picked a stronger model and
told it nothing about the failure, with a green suite, because the lane tests write the
diagnoser's answer straight into the attempt inbox and never run its worker.

**The rule this leaves.** When a feature's value depends on something reaching a model, a test
that stops at the last controller-owned row proves the plumbing and not the feature. Assert on
the text the driver was handed. And when a role's own declared floor means no existing test can
reach the code under it, that is not a detail — it is a statement that the path is unrun, and
the first lane that reaches it will find whatever is there.

## 2026-09-18 — P12 s1: a test that supplies its own input hides the wiring that should supply it

The P12B failure-ladder lanes passed `--worktree` to the worker by hand in their `on_poll`, so
every lane was green while the graph loop itself never created a task worktree or passed one to
anything. Under `engineering.runner: graph` no code task could reach `accept`, and no test
noticed, because no lane drove a task to a settled `succeeded` run.

**The rule this leaves.** When a lane has to inject an argument the controller should have
produced, that is a finding, not test setup: stop and check that the controller produces it.
For any runner, keep at least one lane that drives a task from open to accepted with nothing
hand-fed in between.

## 2026-09-18 — Wiring gaps are fixes, not decisions; fix before documenting
- Correction: the P12 s2f handoff put docs before two open threads and offered `disposition: needs-dan` for them.
  Both threads (gate checks an uncommitted tree; candidate winner never merged) are wiring gaps whose intended
  behaviour the spec already fixes, so they are ours to fix, and fixing them first keeps the docs from describing
  soon-to-change behaviour.
- How to apply: in a handoff, order behaviour fixes before docs. Reserve needs-dan for real design/scope choices
  (e.g. a D01-D08 change), never as a default fallback for "not small".
