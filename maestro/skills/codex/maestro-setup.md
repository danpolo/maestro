# maestro-setup

Set up maestro (autonomous orchestration) for a project. `~/.codex/prompts/*.md` on this machine
had no existing files to check for local convention, so this file follows the plain-markdown
Codex prompt shape (no frontmatter): a filename-derived command name, a short instruction body
you follow when the operator invokes it. `maestro install-skills` places this file at
`~/.codex/prompts/maestro-setup.md`.

Maestro's `init`/`doctor` scripts (DESIGN.md §10) derive everything mechanical from the target
repo — git root, remote, main branch, test command. They cannot derive judgment: what "good"
means for *this* project, what's too dangerous for an autonomous task to touch, which tasks
should run first, or the vocabulary maestro's reports should use when talking to the operator
about upcoming work. That's this prompt's job (DESIGN.md §2 D6, §10 "The setup skill"). It
interviews for those answers, writes them into the project's docs, then calls `maestro init` to
lay down the mechanical scaffolding.

**The normal upstream of this skill is a `grill-me` session.** Dan's standing practice is
to grill a project's design *before* putting it on maestro, in a separate session, which leaves
behind `CONTEXT.md`, `docs/adr/` and (usually) `docs/PLAN.md`. Those documents already answer
most of the interview below — in their own formats, which are not maestro's. Step 2 harvests
them; the interview then becomes a confirmation pass rather than a discovery pass. Do not
re-interrogate an operator about decisions they already settled and wrote down.

Read `docs/DESIGN.md` in the maestro repo (`~/projects/maestro`) first if it's available — §5
(`project.yaml` shape), §10 (setup/doctor), and `maestro/docs/roadmap.py`'s docstring for the
exact roadmap task schema — rather than guessing the shape of anything below.

## Ask when you don't know

This is a skill running in an interactive session, not a script, and that is the whole point of it:
`init` and `doctor` already do everything that can be derived mechanically. Anything you are missing,
unsure about, or reading two ways is a question for the operator — use the **AskUserQuestion** tool
rather than inferring it, picking a plausible default, or leaving a field at its scaffold value and
mentioning it in the final report. A wrong `deny_list_extra`, a wrong dependency edge or a task
mis-marked `autonomous` costs far more later than one question costs now.

That applies to more than the eight numbered questions below. Ask when a harvested document is
ambiguous or two of them disagree, when a phase could reasonably be `autonomous` or `needs-dan`,
when the target repo itself is ambiguous, or when a pre-flight check finds something the operator
has to choose about (see Step 1). Do **not** ask for permission to proceed with work already agreed,
and do not re-ask something the documents answer unambiguously — say what you found instead.

Batch related questions into a single `AskUserQuestion` call rather than a chain of one-at-a-time
prompts, give each option a real consequence in its description, and recommend one. The exception is
Step 3's interview itself, which is deliberately conversational and works better one question at a
time.

## When to run this

- A repo has no `.orchestrator/` directory and no `project.yaml` yet — first-time setup.
- The operator asks to "put this project on maestro", "onboard X onto maestro", or similar.
- An existing project already has planning docs (a roadmap, a backlog, a PROJECT-shaped doc)
  written in prose, and the operator wants them turned into maestro's machine-readable format
  without losing the reasoning behind them.
- For a genuinely fresh project, run this *after* a `grill-me` (or brainstorming) session
  has produced a real design — `CONTEXT.md`, the ADRs, and a plan with concrete build phases —
  and *before* any implementation starts. Implementation from here on is what maestro runs.
  Running it earlier leaves Step 3's roadmap interview with nothing concrete to turn into task
  blocks.

## Step 1 — Confirm the target, check for prior setup, and pre-flight `init`

1. Identify the target repo (ask if ambiguous — never assume the current working directory is
   the target without confirming).
2. Check whether `project.yaml`, `docs/ROADMAP.md`, or `.orchestrator/` already exist there. If
   so, this is a re-run or a migration, not a fresh install — say which, and skip straight to
   Step 4's migration path if there's existing prose to convert.
3. Confirm the repo is actually a git repo with a checked-out branch `init` can read — if it is
   not a git repo at all, stop and say so before interviewing further; there's no point
   collecting judgment calls for a repo `init` can't scaffold yet. A **missing remote is not a
   stop condition**: `init` scaffolds fine without one and a local-only project is a legitimate
   setup. It is a pre-flight finding to report (see the remote bullet below), not a blocker.

Then run these three pre-flight checks, because all three fail *quietly* and all three are
common in a freshly-grilled project that has design docs but no build yet. Report them before
running `init`, not after the first task blocks:

- **Is there a test signal `init` can actually see?** `_derive_repo_facts` (`maestro/cli.py`)
  detects a test command from exactly three things: `package.json` with a non-empty
  `scripts.test`; `pyproject.toml` or `requirements.txt` *plus* a pytest signal (a `tests/`
  directory, a `[tool.pytest]` table, or `pytest` in the project's dependencies); or `go.mod`.
  A project with `tests/` and a `.venv` but **no root manifest** yields an empty `test_command`,
  and the rendered `adapters/test` then fails closed with a `TODO` on every run — the gate
  certifies nothing and every task parks at the merge gate. A project with **neither a manifest
  nor any tests** — which is exactly what `grill-me` leaves behind — is the same trap one
  step deeper: adding only the manifest makes the derivation check pass while pytest exits 5 on
  the empty suite, so every task still parks, now with a summary that looks like the task's own
  failure. **This skill fixes both itself, in Step 1a, before `init` runs.** It is not a question for the operator and not a chore to leave
  behind: setting up the things maestro needs — the manifest included — is what this skill is
  for. Report it as a pre-flight finding, then resolve it.
- **Is there a git remote?** `git remote get-url origin` returning empty is not fatal — `init`
  scaffolds fine — but a failed `main` push now surfaces (a `push_failed` status, a journal
  entry, and — on the Opus-approved path — a Telegram warning) rather than silently claiming
  success; if there's no remote at all, nothing pushes regardless. Say so plainly rather than
  letting the operator assume merged work is pushed.
- **Is the `meta` branch reachable?** `reconcile_meta` (`maestro/metabranch.py`) needs the same
  remote push access as `main` to keep process docs (handoffs, plans, progress, lessons)
  versioned — if the remote check above already failed, `meta` will fail identically and process
  docs will only ever exist in the local sibling worktree. Not fatal — `init`, `doctor`, and
  orchestrator startup all retry automatically — but say so up front.

## Step 1a — Make the test gate real: root manifest, and a suite for it to run

**Trigger:** run this step whenever `init` would derive an empty test command — i.e. the repo has
no root `pyproject.toml` / `requirements.txt` / `package.json` carrying a test signal. Do not gate
it on the repo already having tests. The span between a grilled project and a working maestro
install is this prompt's job, and both halves of the gate — the manifest *and* something for it to
run — are inside that span.

`grill-me` leaves `CONTEXT.md`, `docs/adr/` and `docs/PLAN.md` and **nothing else**. It
writes no manifest and no tests. Assume that is exactly what you are handed; a project that
happens to already have a `tests/` directory is the exception, not the normal case.

There are two shapes. Identify which one you are in before writing anything.

### Case A — a test suite exists, no manifest

Verify the suite runs *before* writing anything:

```bash
cd <repo> && ./.venv/bin/python -m pytest -q
```

A suite that does not pass is a finding for the operator, not something to paper over with a
manifest. Report it and ask before continuing. Then write the manifest below.

### Case B — no test suite at all (the ordinary greenfield case)

**The manifest alone is not enough here, and stopping at the manifest is a silent trap.** Measured
on a scratch repo, 2026-09-11:

| What the repo has | `_derive_repo_facts` test_command | Rendered `adapters/test` result |
|---|---|---|
| no manifest, no `tests/` | `''` | `TODO` branch, exit 1 -> `nonzero_exit` on every task |
| manifest with `[tool.pytest.ini_options]`, no `tests/` | non-empty | pytest exits **5** ("no tests ran") -> `{"pass": false}` on every task |
| manifest + `tests/` present but empty | non-empty | pytest exits **5** -> `{"pass": false}` on every task |

A Case B project that gets only the manifest therefore looks fixed at the derivation check and
still parks every single task at the merge gate — with a summary reading `no tests ran`, which an
implementing agent will read as *its own* failure and try to fix inside its own diff. That is
worse than the `TODO`, because it does not look like a setup problem.

So in Case B write the manifest **and** seed the suite, in the same step:

1. Create `tests/` and one seed test that asserts something **actually true and load-bearing**
   about the repo as it stands — never `assert True`. On a greenfield repo the honest candidates,
   in order of preference: the project's declared package imports; an invariant from
   `docs/PLAN.md` that is already checkable; or, failing both, the structural fact that the design
   docs every task brief routes to are present and parse:

   ```python
   # tests/test_project_skeleton.py
   """Seed test written by the maestro-setup skill.

   Exists so the merge gate runs something real from task one. It asserts the design
   docs every task brief routes to are present. REPLACE IT with the first real
   behavioural test — the first roadmap task's brief says to.
   """
   from pathlib import Path

   ROOT = Path(__file__).resolve().parent.parent


   def test_design_docs_the_build_reads_from_are_present():
       for rel in ("CONTEXT.md", "docs/PLAN.md"):
           assert (ROOT / rel).is_file(), f"{rel} is missing"
   ```

2. **Say plainly, in the Step 7 report, that the gate currently certifies only this.** A gate that
   passes because there is nothing to check is the failure mode this whole step exists to avoid;
   the seed makes tasks *runnable*, it does not make them *certified*.
3. **Put the replacement into the first roadmap task's `notes`** when you write the roadmap in
   Step 4 — one sentence, e.g. "replace `tests/test_project_skeleton.py` with real tests for this
   phase's acceptance criteria." Otherwise the placeholder outlives the project.

### The manifest (both cases)

Write the smallest manifest that makes the project's own test runner discoverable — a
declaration of what is already true, not a packaging decision:

```toml
[project]
name = "<repo-basename>"
version = "0.1.0"
description = "<one line, from CONTEXT.md or the README>"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Rules for it:

- **Declare, don't decide.** `requires-python` comes from the interpreter in `.venv`, the
  dependency list from what the code already imports. Do not add a build backend, a packaging
  layout, or a dependency the project is not already using — that is an architecture decision and
  it belongs to the operator, not to a setup step.
- **Tell the operator what you wrote and why**, in the same breath as the pre-flight finding. It
  is a new tracked file in their repo.
- **Then re-run the pre-flight check** and confirm the derivation actually moved:

  ```bash
  cd <maestro-repo> && .venv/bin/python -c \
    "from pathlib import Path; from maestro.cli import _derive_repo_facts; \
     print(_derive_repo_facts(Path('<repo>'))['test_command'])"
  ```

  A non-empty argv here is the whole point of the step. If it is still empty, stop — `init` will
  render a `TODO` adapter and you now know it before it happens, not after.
- **Non-Python projects:** the same rule applies to the equivalent gap — a Node repo with tests
  and no `scripts.test` gets the one-line `scripts.test` entry that runs its existing runner. Go
  needs no manifest beyond the `go.mod` it already has. Case B applies identically: a Node repo
  with a `scripts.test` and no test files fails its runner the same way, so seed one there too.
- **Then run the suite through the manifest** — not just the derivation check — and confirm it
  exits 0:

  ```bash
  cd <repo> && ./.venv/bin/python -m pytest -q; echo "exit=$?"
  ```

  Exit **5** means "no tests ran": you are in Case B and have not seeded the suite. Exit 0 with at
  least one test collected is the bar.

Step 6 verifies the end of this: the *rendered* `adapters/test` must run the real suite and exit
0, with no `TODO` left in it, and its `summary` must name at least one passing test — an adapter
whose summary reads `no tests ran` has not passed this step.

## Step 2 — Harvest the design docs (the grill-me handoff)

Look for these before asking the operator anything:

| File | What it is | What it feeds |
|---|---|---|
| `docs/GOALS.md` | The product contract: purpose, users, goals with targets, final-product scenarios, acceptance criteria, operator checks, non-goals, hard invariants, trade-offs, priority, authority, revision. grill-me writes it first; ChatGPT's Drive grill writes the same format. | Step 2c, every task's `serves`/`criteria` (Step 5a), Interview Q1 |
| `CONTEXT.md` | Canonical vocabulary — one entry per term, with an `_Avoid_:` list of banned synonyms. A glossary and nothing else; deliberately free of implementation detail. | Interview Q7 |
| `CONTEXT-MAP.md` | Present only in a multi-context repo; points at a per-context `CONTEXT.md`. Merge them for the project glossary, noting which context each term belongs to. | Interview Q7 |
| `docs/adr/NNNN-*.md` | Decisions and the reasoning behind them. Each one is hard to reverse, surprising without context, and the result of a real trade-off. | Q1, Q2, Q3 |
| `docs/PLAN.md` | The agent-facing build plan: invariants, module map, data model, algorithms, build phases with acceptance criteria, out-of-scope list. | Q1, Q2, Q4, Q5, Q8 |
| `README.md` | Original intent, often with a "superseded in places" header pointing at the ADRs. | Q1 background only — never the source of truth where an ADR disagrees. |

`docs/PLAN.md` is **not** guaranteed: `grill-me` contracts only `CONTEXT.md` and
`docs/adr/`. If there is no plan document, the ADRs and the operator are your only source for
build phases, and Q4 stays a real interview question. Say which situation you're in.

Read all of them completely before the interview. Then answer each interview question yourself,
from the docs, and put the proposed answer to the operator for correction. Cite the file and
section you drew each one from, so a wrong inference is visible and cheap to fix.

Harvesting is inference, and inference is exactly where this goes wrong quietly. Where a document
is ambiguous, where two of them disagree, or where you are picking one reading of a phase over
another, ask instead of choosing — a confidently wrong harvested answer is worse than no harvest,
because it arrives already dressed as a citation.

| Interview Q | maestro destination | Where the answer usually already is |
|---|---|---|
| 1 What good looks like | `docs/PROJECT.md`, `gate.chain` | `PLAN.md`'s invariants table — each row is a property that must hold, with the module that enforces it. That *is* the answer. |
| 2 What's risky | `risky_set`, `docs/PROJECT.md` | `PLAN.md`'s "Out of scope" (things needing a new ADR before they're added) plus any external constraint an ADR records. |
| 3 Deny-list | `deny_list_extra`, `secrets`, `confinement.deny` | Invariants about secrets and git-ignored config; ADR-recorded constraints on what must never change. |
| 4 First roadmap tasks | `docs/ROADMAP.md` | `PLAN.md`'s build-phase table, including its stated ordering and per-phase "Done when". See Step 5. |
| 5 Deliverables | `confinement.redo_allow` | `PLAN.md`'s module map. If the deliverable is *external* (a playlist, a deployed service, a Telegram bot's output) there may be no directory to name — say so, and leave `/redo` docs-only rather than inventing a surface. |
| 6 Headline number | `gate.primary_metric` | Usually genuinely absent on day one. A `CONTEXT.md` term that names a measure is a *candidate*, not a gate metric — it only counts once an adapter computes it. Leave it `null` otherwise. |
| 7 Glossary | `docs/PROJECT.md` "## Glossary" | `CONTEXT.md`, via the transform below. |
| 8 bot_files | `bot_files` | `PLAN.md`'s process-supervision / entry-point sections. A CLI driven by timers has no resident process — `[]` is then the correct answer, and worth stating as a decision rather than a default. |

### 2a. Transforming `CONTEXT.md` into the PROJECT.md glossary

The two formats are not compatible, and the difference is load-bearing.
`maestro.docs.upcoming._GLOSSARY_BULLET` matches `^\s*-\s*\*\*(.+?)\*\*\s*[—–-]+\s*(\S.*)$` —
one term, one line, an em/en dash or hyphens as the separator. A `CONTEXT.md` entry is a bolded
term followed by a colon, a definition on the *following* lines, and an `_Avoid_:` line. None of
that parses.

- Compress each definition to a single line. Keep the term string byte-identical to
  `CONTEXT.md`'s so the two files can never disagree about what a thing is called.
- The `_Avoid_:` synonyms are dropped by this transform. That is a real loss: those banned words
  are exactly what maestro would most usefully catch in a task description.
  `_term_aliases` builds aliases only from a parenthetical, so the only way to carry one across
  today is to fold it in — `- **Duet (shared playlist)** — ...` makes `detect_terms` fire when a
  task says "shared playlist". Treat that as a deliberate abuse of the parenthetical convention:
  do it only for the one or two terms whose confusion would actually cause a bad change, and only
  if the operator wants the catch. Default to leaving them out.
- Note in the Glossary section that `CONTEXT.md` is authoritative and holds the full definitions
  and the banned synonyms. Two glossaries drift; say which one wins.

### 2b. PROJECT.md must route to the design docs

This is the highest-value item on this page. `maestro/implementer.py` builds every task brief
telling the agent to "read `docs/ROADMAP.md` and `docs/PROJECT.md` for full context". It never
mentions `CONTEXT.md` or `docs/adr/`. So in a grilled project, unless `docs/PROJECT.md` points at
them, **every autonomous task starts blind to the canonical vocabulary and to every decision
behind the design** — and will cheerfully reintroduce a synonym the glossary bans or an approach
an ADR rejected.

Add a "Source documents" section at the top of `docs/PROJECT.md`, in reading order, stating what
wins on conflict. For example:

```markdown
## Source documents — read these before starting any task

1. `docs/GOALS.md` — the product contract: why the project exists and what done means. Only
   Dan changes it, by a new contract revision; no task weakens a goal, a target or a criterion.
2. `CONTEXT.md` — canonical vocabulary. Use these exact terms in code, commits and comments;
   the `_Avoid_:` lists are banned synonyms, not stylistic preferences.
3. `docs/adr/` — the decisions and why they were made. An ADR outranks every other document
   here except `docs/GOALS.md`, including this one.
4. `docs/PLAN.md` — what to build. Stale wherever an ADR disagrees with it.
5. `README.md` — original intent. Superseded in places; background only.
```

### 2c. The goals contract

`docs/GOALS.md` says what done means; every task serves it and the final gate verifies it. A
grilled project has one already (grill-me and ChatGPT's Drive grill write the same format), so
this step validates it and asks Dan only what is missing. It never invents a target, an oracle
or an approval.

1. Run `maestro goals check docs/GOALS.md --json`. Each issue has a `severity` (`note`,
   `draft`, `gate`) and `"ask"`: `true` when only Dan can answer it (a target, an oracle, a
   behaviour, a link's meaning, an approval), `false` when you can fix it from what the
   contract already says.
2. **No `docs/GOALS.md`** (one issue, `ask: true`): you are the fallback grill. Walk the
   contract steps in grill-me's order — Purpose, Users, Goals, Final product, Scenarios,
   Acceptance criteria, Operator checks, Non-goals and invariants, Trade-offs, Priority,
   Authority, Approval — drafting every answer from `docs/PLAN.md`, the ADRs, `README.md` and
   the repository, and confirm the drafts with Dan in batches. Write format 2 with every field
   the table below requires, then get his approval: `Contract revision: 1` and a
   change-history row with Dan in `Approved by`.
3. **Format 1** (no `Format: 2` line): show Dan what `maestro goals migrate docs/GOALS.md`
   prints (a format-2 skeleton that keeps every id and text), and ask only its
   `Open question:` lines. Write it once he approves.
4. **Format 2:** ask only the issues with `"ask": true`, batched in one AskUserQuestion round,
   each with your recommended answer. Fix the `"ask": false` ones yourself (heading or id
   syntax, the missing side of a link, a `kind`/`layer`/`evidence` spelling, a typo in an id)
   and tell him what you changed; the one exception is below, after the first baseline. A
   criterion with no `Test:` is not a question: binding its test is this setup's job
   (`maestro goals baseline` reports which are unbound).
   Before the first `maestro goals baseline`, make these fixes at the current revision. After
   the first `maestro goals baseline` Maestro has recorded the contract, so do not edit an
   entry alone: collect every fix that changes an entry into one new revision (bump
   `Contract revision`, add one change-history row) and ask Dan to approve it; a change at
   the same revision fails `maestro doctor`. Of an entry's
   fields, only these may change at the same revision: its name, `Rationale`, `Kind`,
   `Layer`, `Evidence`, `Fixtures`, `Environment`, `Data`, `Test`, `Sign-off` and
   `Decision`. Any other change to a goal, scenario, criterion, operator check, invariant or
   trade-off is a new revision, a link between ids included.
5. Never invent. A value nobody has given stays an `Open question:` and holds only the ids it
   names; the rest of the project proceeds.

The fields, and the worst each one's absence does (generated from Maestro's
`goals.CONTRACT_FIELDS`; the grills carry the same table). A section counts with its entries,
so a section whose ids other entries name gates through those links:

| Part | Field | Required | Default |
|---|---|---|---|
| Contract | Format | draft | none: read as format 1, so nothing verifies it at the end |
| Contract | Contract revision | gate | none: Dan approves the contract |
| Section | Purpose | draft | none |
| Section | Users | draft | none |
| Section | Goals | gate | none: every link to a goal gates |
| Section | Final product | gate | none: a contract with no scenario gates |
| Section | Acceptance criteria | gate | none: every link to a criterion gates |
| Section | Operator checks | gate | None. is a complete answer, so write it when there are none; absent, it gates only when an entry names an OP- |
| Section | Non-goals | draft | none |
| Section | Hard invariants | gate | None. is a complete answer, so write it when there are none; absent, it is draft, or gate when an entry names an INV- |
| Section | Trade-offs | no | None. |
| Section | Priority | draft | none |
| Section | Authority | draft | none |
| Section | Change history | gate | none: the revision has no approval row |
| Section | Retired ids | no | None. |
| Goal | Outcome | no | the goal's name |
| Goal | Rationale | no | none |
| Goal | Baseline | draft | none (not_applicable when nothing is measured) |
| Goal | Target | gate | none: an open question for Dan |
| Goal | Verified by | draft | the criteria and checks naming the goal |
| Goal | Constraints | no | none |
| Goal | Depends on | no | none |
| Scenario | Goals | gate | none |
| Scenario | Kind | draft | none (normal, error or boundary) |
| Scenario | Given | draft | none |
| Scenario | When | draft | none |
| Scenario | Then | draft | none |
| Scenario | Criteria | draft | the criteria naming the scenario |
| Scenario | Operator checks | draft | the checks naming the scenario |
| Acceptance criterion | Goals | draft | the goals listing it |
| Acceptance criterion | Scenarios | draft | the scenarios listing it |
| Acceptance criterion | Given | draft | none |
| Acceptance criterion | When | draft | none |
| Acceptance criterion | Then | draft | none |
| Acceptance criterion | Oracle | gate | none: an open question for Dan |
| Acceptance criterion | Threshold | no | none |
| Acceptance criterion | Layer | draft | none (e2e, integration or unit) |
| Acceptance criterion | Evidence | draft | none (test, metric or trace) |
| Acceptance criterion | Fixtures | no | none |
| Acceptance criterion | Environment | no | none |
| Acceptance criterion | Data | no | none |
| Acceptance criterion | Test | no | none: setup binds it |
| Acceptance criterion | Mandatory | no | yes |
| Operator check | Goals | draft | the goals listing it |
| Operator check | Scenarios | draft | the scenarios listing it |
| Operator check | Instructions | gate | none |
| Operator check | Expected | gate | none |
| Operator check | Sign-off | draft | none |
| Operator check | Mandatory | no | yes |
| Hard invariant | Checked by | draft | none |
| Trade-off | Decision | draft | none |
| Trade-off | Bound | gate | none |
| Trade-off | Expires | no | none |
| Authority | Autonomous | draft | none |
| Authority | Ask Dan | draft | none |
| Authority | Retries | draft | none |
| Authority | Stop a failed approach when | draft | none |
| Authority | Blocker | draft | none |
| Authority | Channel | draft | none |
| Change history | Approved by | gate | none: Dan approves the revision |

A threshold conflict or an invariant conflict that `goals check` reports is Dan's decision
(`ask: true`): put it to him with the conflicting ids and revision, the evidence, the
consequences of each option, your recommendation and approve / decline / approve-with-notes /
choose. Priority never decides it.

## Step 3 — Interview

Ask these in order, one at a time, in plain language — don't dump the whole list on the operator
at once. Where Step 2 already produced an answer, **present it for confirmation instead of
asking cold**, naming the document it came from. Skip a question outright only if the answer is
unambiguous from an existing doc, and say so instead of asking.

1. **What good looks like here.** What does a passing gate actually certify for this project
   today? If there's no eval harness yet (the common case — D8), say plainly that the gate is
   test-only for now rather than implying more rigor than exists. This becomes
   `docs/PROJECT.md`'s "What good looks like here" section.

   In a grilled project the invariants table in `docs/PLAN.md` is the real answer to this
   question, and it is sharper than anything an interview would produce. Lift it in, and then be
   precise about the gap: an invariant only becomes something the gate certifies once a test
   asserts it. List which invariants have tests today (often none, on day one) and say that the
   rest are design intent the gate does not yet enforce.

   `gate.chain` is the machine-readable half of that answer, and it is load-bearing: the smoke
   step runs **only** when the chain names it. A fresh scaffold ships `chain: [test]` and an
   `adapters/smoke` stub that returns `pass: false` on purpose ("not implemented" is not
   "succeeded"), so leaving the chain alone is what stops a new project reverting every task it
   completes. Add `smoke` to the chain in the same change that replaces the stub with a real
   check, never before.
2. **What's risky.** What should an autonomous task hesitate on even if it isn't a hard deny —
   production data paths, anything customer-facing, anything expensive to get wrong. This
   becomes `docs/PROJECT.md`'s "What's risky" section and seeds `project.yaml`'s `risky_set`.
3. **The deny-list.** What must an autonomous task *never* touch, no exceptions — specific
   paths, files, or subsystems. This becomes `project.yaml`'s `deny_list_extra` and is rendered
   into `operating_preamble.md`'s `{{ deny_list }}` placeholder alongside `risky_set`. Remind the
   operator that `secrets:` (`.env`, `*.pem`, `*credentials*.json`) is already covered by the
   template and doesn't need repeating here.
4. **First roadmap tasks.** What are the first few concrete, runnable tasks? Get enough detail
   for each to fill a task block (see Step 5's schema): a one-line title, whether it's safe to
   run autonomously (`mode: autonomous`) or needs the operator (`mode: needs-dan`), and any
   dependency ordering between them. A fresh `init` scaffold ships with **zero** tasks on
   purpose — this interview is what actually populates the roadmap.

   With a `docs/PLAN.md` in hand this collapses to two questions worth the operator's time:
   *which phases need you personally*, and *which phase do we start from* — a plan often names a
   deliberate starting phase that is not the first one, for a reason worth preserving in `notes`.
5. **What the deliverables are, and where they live.** Which directories hold the things this
   project actually ships — a notebook folder, an export folder, a docs tree? This becomes
   `project.yaml`'s `confinement.redo_allow`: the surface a `/redo` rewrite may touch when Dan
   reports a problem with something the project shipped. The default is `[docs/]` on purpose —
   maestro cannot guess where a project keeps deliverables, and a default that guessed wide would
   let `/redo` rewrite files nobody nominated. **A project that skips this question has a `/redo`
   that can only edit docs**; say so rather than leaving the operator to discover it.
6. **The headline number, if there is one.** Does this project measure something a passing change
   should be quoted against — an accuracy score, a latency, an error rate? This becomes
   `gate.primary_metric`, the metric named first whenever maestro reports a merge. If there is no
   eval harness yet (the common case on day one) the honest answer is "none yet": leave it
   `null`, and maestro reports "no metrics" rather than inventing one.
7. **The upcoming-work glossary.** Domain vocabulary, abbreviations, or project-specific terms
   maestro's phase reports and upcoming-work summaries should use consistently, so a human
   reading a report doesn't have to guess. This becomes `docs/PROJECT.md`'s "Glossary"
   section — `maestro.docs.upcoming` parses it (unlike the rest of that file, which is free
   prose), so write each entry as its own bullet, exactly `- **Term** — one-line definition`.
   Spell an abbreviation out in parentheses on first mention (`RRF (Reciprocal Rank
   Fusion)`) so task descriptions match on either form.

   If `CONTEXT.md` exists this is not an interview question at all — it is the transform in
   Step 2a. Show the operator the compressed one-line definitions and ask only whether any of
   them lost something.
8. **Does this project run a live process, and if so, which file(s) are it?** This becomes
   `project.yaml`'s `bot_files` — deliberately not something `init` can guess. A merge that
   touches one of these hard-stops the no-eval resumable auto-merge path (escalates to Dan,
   never Opus) and, after landing, runs this project's own `scripts/canary_deploy.py` if it
   has one. The default is `[]`, meaning **none of these protections fire for anything** —
   if this project has a bot, service, or daemon whose source lives in the repo, ask which
   file(s) and say so plainly; don't leave this at the default without asking.

Record answers as you go; don't rely on remembering them across the interview. If an answer raises
a follow-up the operator has to settle — a deny-list entry that would block a roadmap task, a
deliverable directory that doesn't exist yet — ask it then, with `AskUserQuestion`, rather than
carrying the ambiguity into `init`.

### Project priority (P13 W1, B15)

Ask once, alongside the project interview: "What priority weight should this project have?
1 is normal; 2 gives it twice the weight when shared budgets are enabled."
If a project priority is already recorded, use it without asking again. If skipped, inherit
the effective global `defaults.priority` (1 when absent); leave the project override absent.
Accept only a positive finite number, never a bool, zero, negative or nonfinite value.
Write an explicit answer as top-level `priority:` in `project.yaml` after `init`.
Dan can later change and commit it with `/priority [project] <weight>` on the shared bot.
This records the weight; shared budget admission is a later workstream.

## Step 4 — Migrating existing prose docs (skip if this is a genuinely fresh project)

Most real repos are not brand new — they already have a README, a backlog, a design doc, or some
other prose description of what's planned. Converting that into maestro's format instead of
starting from a blank roadmap is the realistic path:

1. Read whatever planning prose already exists (README "Roadmap"/"TODO" sections, issue trackers
   exported to a file, design docs, backlog files — ask the operator where to look if it isn't
   obvious).
2. For each concrete, plannable unit of work you find, draft one task block using the schema in
   Step 5. Preserve the *reasoning* behind each item (why it matters, what it depends on) in the
   task's `notes` field rather than discarding it — the machine-readable fields are for
   orchestration, `notes` is where the human context survives.
3. Anything too vague to be a runnable task yet (an aspiration, not a task) belongs in prose in
   `docs/PROJECT.md`, not forced into a fake roadmap entry.
4. Show the operator the draft roadmap before writing it and ask them to confirm ordering,
   `mode`, `deps` and `scope` — a wrong dependency edge here silently blocks or misorders autonomous
   work later, so this is worth a human check even though the rest of the interview can move
   faster.
5. List only direct deps. A dep another dep already implies (A needs B and C, and B needs C)
   is transitively redundant: maestro removes it automatically (the transitive reduction, at
   loop start and in the dependency map), so do not ask the operator about it.

## Step 5 — Write the roadmap task blocks

`maestro/docs/roadmap.py` parses every fenced ` ```yaml ` block in `docs/ROADMAP.md` — nothing
else in that file is machine-read. Use this schema (see the template's own comment block for the
authoritative version):

```yaml
id: <stable-string-or-number>
status: open              # omit or "open" while active; "complete" when done
mode: autonomous           # or needs-dan
deps: []                   # list of other task ids that must be status: complete first
scope: [src/x.py, tests/]   # every file/dir/glob the task will write, tests included
self_modifying: false      # true excludes it from auto-launch entirely
hold: false                # true is a human hold, independent of deps
title: "One-line description of the deliverable"
serves: [G1]               # goal ids from docs/GOALS.md; [] for polish/housekeeping
contribution: direct       # direct | enabling | maintenance, or per goal {G1: direct, G3: enabling}
criteria: [AC-1]           # the S-/AC-/OP- ids it works on
tests: ["pytest tests/acceptance/test_x.py"]   # the tests it expects to turn GREEN or affect
evidence: "what proves it is done"
notes: >
  Anything an implementer needs that doesn't fit the fields above — including
  migrated reasoning from Step 4.
```

With a `docs/GOALS.md`, every task says `serves:` (no key at all is "unclassified" and holds
the task at execution), its `contribution:` per served goal, and its `evidence:`; a `direct`
task also names the `criteria:` or `tests:` it should turn GREEN.

Fill `scope` for every task: tasks whose scopes overlap run one after another, never in
parallel, because parallel tasks must merge trivially. A task without `scope` is serialised
against everything; `scope: []` says it writes nothing.

Append one such block per task agreed in Step 3/4 to `docs/ROADMAP.md`, below the existing
commented-out example — leave that example in place as a reference, don't delete it.

### 5a. Converting `docs/PLAN.md` build phases into task blocks

A grilled plan's build-phase table is already a dependency-ordered task list with acceptance
criteria. Convert it mechanically:

- **One block per phase.** Reuse the phase's own number in the `id` (`02-collector`, not `task-1`)
  so `PLAN.md` and `ROADMAP.md` stay traceable to each other in both directions.
- **"Do not start a phase before its predecessors pass" is a `deps` chain**, not a comment.
  Encode it. Where the plan names a deliberate starting phase out of numeric order, that ordering
  is the dependency truth — follow it and record why in `notes`.
- **The "Done when" column goes into `notes` verbatim.** The roadmap schema has no acceptance
  field; paraphrasing acceptance criteria is exactly how they get lost. Carry the invariant ids
  through too, so the implementer knows which property its test has to assert.
- **A phase needing a human is `mode: needs-dan`** — an interactive OAuth or login flow, anything
  privileged (installing a system unit, `sudo`), anything requiring a credential only the
  operator holds. A grill session won't have labelled these; this is the one part of the
  conversion you must actually ask about.
- **A phase that is "observe for N weeks, then tune" is not a task.** Leave it in `PLAN.md` prose,
  or add it with `hold: true` so it is visible but never auto-launched.
- **A task inherits its phase's `Serves` as a suggestion, not as proof.** Decide each task's
  `serves:` and its `contribution:` per served goal yourself; never assume `direct`. A harness,
  fixtures or a benchmark is `enabling` (it moves no criterion by itself); polish and
  housekeeping are `maintenance` with `serves: []`, which still runs every regression and
  invariant check. Carry the phase's `Criteria` into `criteria:`, the tests it should turn GREEN
  into `tests:`, and what proves it is done into `evidence:`.

Worked example (the ids come from the project's `docs/GOALS.md`):

```yaml
id: 02-collector
status: open
mode: autonomous
deps: [02a-sample-posts]
self_modifying: false
hold: false
title: "Collector + schema: hourly poll into `plays`, idempotent"
serves: [G1]
contribution: direct
criteria: [AC-1, AC-2]
tests: ["python bench/import_ratio.py"]
evidence: "AC-1 and AC-2 GREEN on the acceptance run; a second collection adds zero rows"
notes: >
  docs/PLAN.md §7 phase 02. Done when: the hourly timer is running, `plays` is
  growing, and running a collection twice adds zero rows (invariant I8 — that
  needs a test that fails loudly if it stops holding). Schema in PLAN.md §3;
  endpoint, 50-item ceiling and the `after` cursor rule in §5. Started first,
  ahead of phase 01, because every day the collector is not running is listening
  history that cannot be recovered later.
```

The same phase also needs sample data before AC-1 can run credibly, so it gets an enabling
child: it serves G1 but moves no criterion by itself.

```yaml
id: 02a-sample-posts
status: open
mode: autonomous
deps: []
self_modifying: false
hold: false
title: "Recorded sample posts and the import-ratio harness"
serves: [G1]
contribution: enabling
criteria: [AC-1]
evidence: "python bench/import_ratio.py runs on the samples and reports a ratio (RED is fine)"
notes: >
  Enabling for phase 02: AC-1 needs 40 recorded posts and a harness that reports
  the ratio before the collector exists. A RED result is the expected outcome.
```

## Step 6 — Call `init` with the answers

Run `maestro init <path>` (or `maestro init` from inside the target repo) to lay down the
mechanical scaffolding — `project.yaml`, adapter stubs, `operating_preamble.md`,
`docs/ROADMAP.md`/`PROJECT.md` skeletons, the systemd unit and launcher. `init` is idempotent:
anything that already exists is written as `<name>.new` beside the original with a diff shown,
never clobbered, so it's always safe to re-run.

After `init` completes:

1. Fill in `docs/PROJECT.md`'s "What this project is", "What good looks like here", "What's
   risky", and "Glossary" sections with the Step 3 answers (or edit the `.new` file and diff it
   in by hand if `init` didn't overwrite an existing one). In a grilled project, add the
   "Source documents" section from Step 2b as well — it is what connects every task brief to
   `CONTEXT.md` and the ADRs.
2. Add the Step 3 deny-list answer to `project.yaml`'s `deny_list_extra`. Note what that key
   *is*: a list of **regular expressions matched against diff text**, not paths. Path-shaped
   answers belong in `confinement.deny`, `secrets` or `prod_stores`, all of which are matched
   against filenames. Write each regex so it matches a credential *assigned to a string
   literal*, not the bare identifier — a schema column named `refresh_token` and an
   `os.environ["CLIENT_SECRET"]` lookup are both legitimate code, and a pattern that blocks
   them blocks the task that was supposed to do the work. Test the patterns against both a
   diff that must be blocked and one that must pass, before moving on.

   **Then edit the rendered `operating_preamble.md` by hand, in the same pass.** Its
   `## The deny list` block is rendered *once*, by `init`, which runs before this interview
   and therefore renders the literal placeholder `(none yet — see project.yaml's risky_set /
   deny_list_extra)` (`maestro/cli.py`, the `mapping` dict). Nothing re-renders it later —
   not `doctor`, not a re-run of `init`, which will not clobber the existing file. So a
   project set up by this skill ships, by default, with a `project.yaml` that has a real deny
   list and a preamble that tells every implementing agent there isn't one. The preamble is
   the document the agent actually reads. Paste the merged `risky_set` + `deny_list_extra` in,
   with one line of reasoning per entry.
3. Fill in `confinement.redo_allow` from the deliverables answer, `gate.primary_metric` from
   the headline-number answer, and `bot_files` from the live-process answer. Leave
   `confinement.self_fix_allow` at its default (`[adapters/, profiles/, docs/]`) unless the
   operator asks otherwise — it is the surface an unattended self-fix may repair, and
   widening it widens what maestro may change about this project's setup without being asked.
4. Append the Step 4/5 task blocks to `docs/ROADMAP.md`. Then, with a format-2
   `docs/GOALS.md`: bind each acceptance criterion to a real test (its `Test:` line) where the
   project can run one, run `maestro goals baseline` to record which criteria are RED, GREEN,
   blocked or operator-only, and run `maestro goals tasks`: it prints a ROADMAP block for each
   unmet mandatory criterion no open task names yet (an enabling harness first where the
   criterion cannot run credibly), with the check the graph runs for it. Show Dan those blocks,
   append the ones he keeps, and give each its `scope:` (the files it writes, per Step 5) in
   place of the block's `# scope:` comment: a proposal cannot know its files, and a task
   without `scope:` runs alone, so goal work would never run in parallel and would wait
   whenever a gate question waits on Dan. Then let `maestro doctor`
   (item 7) check that every mandatory goal has a credible path to GREEN.
5. **If the project has ADRs, append an ADR clause to the rendered `operating_preamble.md`.**
   The stock preamble has no concept of an architecture decision record, so nothing otherwise
   stops an autonomous task from quietly reversing one. This is per-project text, not a maestro
   change:

   ```markdown
   ## Decisions are recorded, and they outrank the task

   `docs/adr/` holds this project's architecture decisions. They are decisions, not
   suggestions, and they outrank a task brief. If a task can only be completed by
   contradicting an ADR, stop and escalate — do not route around it. A decision is
   reversed by writing a new ADR that supersedes the old one, never by a silent code
   change. `CONTEXT.md` is the canonical vocabulary on the same terms: its `_Avoid_:`
   entries are banned in code, commits and comments.
   ```
6. **Run the rendered `adapters/test` and read what it did.** This is the check that closes
   Step 1a, and it is not the same as running pytest yourself:

   ```bash
   cd <repo> && ./adapters/test; echo "exit=$?"
   grep -n TODO adapters/test   # must find nothing
   ```

   It must exit 0 *and* visibly run the project's real suite. An adapter that exits 0 because it
   has nothing to do is worse than one that fails — it makes the merge gate certify nothing while
   reporting success. If a `TODO` is still there, `init` derived an empty `test_command` and
   Step 1a did not take; fix it now, before any task is queued.
7. Run `maestro doctor` and resolve anything it flags before considering setup done.

## Step 7 — Report back

Summarize what was written (which files, whether any were `.new` siblings needing a manual merge
because something already existed), how many roadmap tasks were added and their `mode`, and
whether `maestro doctor` came back clean. State plainly which of these the project is
starting *without*, rather than letting the operator find out at the first failure: no eval
harness (`gate.chain: [test]`, so the smoke is skipped and merges are reported with "no
metrics"), a `/redo` limited to whatever `confinement.redo_allow` names, and a self-fix
limited to the scaffolding. Report whether the shared Maestro bot is configured in
`~/.maestro/.env` (mode 600) and the project is registered for its local mailbox delivery.
DESIGN.md D7/§17.6 uses one bot with `[project]` tags and project-addressed commands;
`init` uses that shared configuration and does not ask for a per-project token. If the
shared configuration is absent, say that setup is needed. Never copy its token into the
project. Report the chosen priority or inherited default and `/priority [project] <weight>`.

Also report, explicitly, the Step 1 pre-flight results and what was done about them. Each is a
silent failure otherwise:

- **The test command.** Whether `init` found one, whether Step 1a had to write a manifest to get
  it (naming the file written and what is in it), and the observed exit code of the rendered
  `adapters/test` against the real suite. If it still fails closed, say that the gate certifies
  nothing until it is fixed — do not report setup as done.
- **The remote.** Whether the repo has one, and if not, that merges stay local because the push
  is fire-and-forget. Local-only is a legitimate setup; an operator who assumes their merged work
  is pushed is not.
- **The `meta` branch**, on the same terms.
