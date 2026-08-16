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

Read `docs/DESIGN.md` in the maestro repo (`~/projects/maestro`) first if it's available — §5
(`project.yaml` shape), §10 (setup/doctor), and `maestro/docs/roadmap.py`'s docstring for the
exact roadmap task schema — rather than guessing the shape of anything below.

## When to run this

- A repo has no `.orchestrator/` directory and no `project.yaml` yet — first-time setup.
- The operator asks to "put this project on maestro", "onboard X onto maestro", or similar.
- An existing project already has planning docs (a roadmap, a backlog, a PROJECT-shaped doc)
  written in prose, and the operator wants them turned into maestro's machine-readable format
  without losing the reasoning behind them.

## Step 1 — Confirm the target and check for prior setup

1. Identify the target repo (ask if ambiguous — never assume the current working directory is
   the target without confirming).
2. Check whether `project.yaml`, `docs/ROADMAP.md`, or `.orchestrator/` already exist there. If
   so, this is a re-run or a migration, not a fresh install — say which, and skip straight to
   Step 3's migration path if there's existing prose to convert.
3. Confirm the repo is actually a git repo with a remote and a main/default branch `init` can
   read — if not, stop and tell the operator what's missing before interviewing further; there's
   no point collecting judgment calls for a repo `init` can't scaffold yet.

## Step 2 — Interview

Ask these in order, one at a time, in plain language — don't dump the whole list on the operator
at once. Skip a question only if the answer is already unambiguous from an existing doc found in
Step 1, and say so instead of asking.

1. **What good looks like here.** What does a passing gate actually certify for this project
   today? If there's no eval harness yet (the common case — D8), say plainly that the gate is
   test-only for now rather than implying more rigor than exists. This becomes
   `docs/PROJECT.md`'s "What good looks like here" section.
2. **What's risky.** What should an autonomous task hesitate on even if it isn't a hard deny —
   production data paths, anything customer-facing, anything expensive to get wrong. This
   becomes `docs/PROJECT.md`'s "What's risky" section and seeds `project.yaml`'s `risky_set`.
3. **The deny-list.** What must an autonomous task *never* touch, no exceptions — specific
   paths, files, or subsystems. This becomes `project.yaml`'s `deny_list_extra` and is rendered
   into `operating_preamble.md`'s `{{ deny_list }}` placeholder alongside `risky_set`. Remind the
   operator that `secrets:` (`.env`, `*.pem`, `*credentials*.json`) is already covered by the
   template and doesn't need repeating here.
4. **First roadmap tasks.** What are the first few concrete, runnable tasks? Get enough detail
   for each to fill a task block (see Step 4's schema): a one-line title, whether it's safe to
   run autonomously (`mode: autonomous`) or needs the operator (`mode: needs-dan`), and any
   dependency ordering between them. A fresh `init` scaffold ships with **zero** tasks on
   purpose — this interview is what actually populates the roadmap.
5. **The upcoming-work glossary.** Domain vocabulary, abbreviations, or project-specific terms
   maestro's phase reports and upcoming-work summaries should use consistently, so a human
   reading a report doesn't have to guess. This becomes `docs/PROJECT.md`'s "Glossary" section.

Record answers as you go; don't rely on remembering them across the interview.

## Step 3 — Migrating existing prose docs (skip if this is a genuinely fresh project)

Most real repos are not brand new — they already have a README, a backlog, a design doc, or some
other prose description of what's planned. Converting that into maestro's format instead of
starting from a blank roadmap is the realistic path:

1. Read whatever planning prose already exists (README "Roadmap"/"TODO" sections, issue trackers
   exported to a file, design docs, backlog files — ask the operator where to look if it isn't
   obvious).
2. For each concrete, plannable unit of work you find, draft one task block using the schema in
   Step 4. Preserve the *reasoning* behind each item (why it matters, what it depends on) in the
   task's `notes` field rather than discarding it — the machine-readable fields are for
   orchestration, `notes` is where the human context survives.
3. Anything too vague to be a runnable task yet (an aspiration, not a task) belongs in prose in
   `docs/PROJECT.md`, not forced into a fake roadmap entry.
4. Show the operator the draft roadmap before writing it and ask them to confirm ordering,
   `mode`, and `deps` — a wrong dependency edge here silently blocks or misorders autonomous
   work later, so this is worth a human check even though the rest of the interview can move
   faster.

## Step 4 — Write the roadmap task blocks

`maestro/docs/roadmap.py` parses every fenced ` ```yaml ` block in `docs/ROADMAP.md` — nothing
else in that file is machine-read. Use this schema (see the template's own comment block for the
authoritative version):

```yaml
id: <stable-string-or-number>
status: open              # omit or "open" while active; "complete" when done
mode: autonomous           # or needs-dan
deps: []                   # list of other task ids that must be status: complete first
self_modifying: false      # true excludes it from auto-launch entirely
hold: false                # true is a human hold, independent of deps
title: "One-line description of the deliverable"
notes: >
  Anything an implementer needs that doesn't fit the fields above — including
  migrated reasoning from Step 3.
```

Append one such block per task agreed in Step 2/3 to `docs/ROADMAP.md`, below the existing
commented-out example — leave that example in place as a reference, don't delete it.

## Step 5 — Call `init` with the answers

Run `maestro init <path>` (or `maestro init` from inside the target repo) to lay down the
mechanical scaffolding — `project.yaml`, adapter stubs, `operating_preamble.md`,
`docs/ROADMAP.md`/`PROJECT.md` skeletons, the systemd unit and launcher. `init` is idempotent:
anything that already exists is written as `<name>.new` beside the original with a diff shown,
never clobbered, so it's always safe to re-run.

After `init` completes:

1. Fill in `docs/PROJECT.md`'s "What this project is", "What good looks like here", "What's
   risky", and "Glossary" sections with the Step 2 answers (or edit the `.new` file and diff it
   in by hand if `init` didn't overwrite an existing one).
2. Add the Step 2 deny-list answer to `project.yaml`'s `deny_list_extra`.
3. Append the Step 3/4 task blocks to `docs/ROADMAP.md`.
4. Run `maestro doctor` and resolve anything it flags before considering setup done.

## Step 6 — Report back

Summarize what was written (which files, whether any were `.new` siblings needing a manual merge
because something already existed), how many roadmap tasks were added and their `mode`, and
whether `maestro doctor` came back clean. If Telegram wasn't configured because no token file was
found, say that plainly rather than silently skipping it — DESIGN.md D7's per-project token in
`.env` is expected to be missing on a fresh project, not an error.
