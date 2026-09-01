# Maestro — Design Spec

**Status:** design approved 2026-08-09. Not yet built.
**Purpose:** A project-agnostic autonomous orchestration system. An orchestrator agent plans and
decides, implementer agents execute in sandboxed git worktrees, a watchdog keeps the whole thing
alive, and a human is pulled in via Telegram only when it genuinely matters.

Maestro is extracted from the working implementation inside `AbuAliArchive`, which has run this loop
in production since 2026-06-20. That implementation is the reference behaviour; this spec defines
what becomes generic, what each project supplies, and how the extraction is carried out safely.

This document is the single source of truth for the design. Build from it.

---

## 1. Goals & non-goals

**Goals**

- Drive any software project forward autonomously from a machine-readable roadmap.
- Work with **both Claude Code and Codex** as first-class agent backends, including switching
  between them *mid-task*.
- Survive rate limits and context ceilings by handing off, never by compacting.
- Never let autonomy silently degrade a project — every change passes a gate before it counts.
- Be installable into a new project in one guided pass, with no trace of any other project in it.

**Non-goals**

- Multi-user or externally-exposed operation. Single-operator, self-hosted, trusted machine.
- A general agent framework. The seam is the adapter contract in §4, nothing broader.
- Supporting agent CLIs beyond Claude and Codex in the first version. The backend protocol
  permits more; none will be written speculatively.

---

## 2. Decisions

Each of these was an explicit choice made during design. Recorded with rationale so they are not
silently relitigated later.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Pluggable backend drivers, selected per role**, each declaring a capability set | The only shape where Codex is a first-class citizen; mid-work switching requires both backends behind one interface |
| D2 | **Mid-work switching**, triggered by quota exhaustion, a usage threshold, or a manual command | Quota exhaustion currently costs hours of dead time; a threshold trigger switches *before* work is lost |
| D3 | **Checkpoint handoff into the same worktree** on switch | Work lives on disk, not in the conversation. Preserves uncommitted changes; reuses the existing 120K-ceiling handoff machinery |
| D4 | **Per-model context ceilings, read from dedicated table files** beside each agent's global instructions | Ceilings differ per model and shift as models are released. One small table per agent, referenced by the global instructions file, picked up by every project |
| D5 | **Single update channel, adopted at task boundaries, gated by maestro's own test suite** | Maestro will evolve from several projects at once. Auto-update keeps them together; the self-test gate stops a bad commit reaching a running loop |
| D6 | **Setup = idempotent `init`/`doctor` scripts + a thin skill in both agent formats** | Scripts give reproducibility and testability; the skill supplies the judgment a script cannot derive |
| D7 | **One Telegram dev bot per project, token in that project's `.env`** | Clean isolation, no cross-project command misfires, matches how the code already works |
| D8 | **The eval gate is optional, falling back to tests** | Most new projects have no eval harness on day one. Absence is journaled as `verdict=unevaluated` so the gap is visible, never hidden |
| D9 | **AbuAliArchive migrates onto maestro as the first consumer** | Proves the extraction against a real, live project rather than a toy |
| D10 | **Maestro lives at `~/projects/maestro`**, entirely outside any consuming project | No project owns the shared core |

---

## 3. Architecture

Three process layers coordinating through the filesystem — no message passing.

```
[ Watchdog ]      systemd -> tmux, always on.
                  Liveness, resume scheduling, stall detection, HALT, control plane.
     | supervises
[ Orchestrator ]  Plans, decomposes, decides, merges, interprets gate results, talks to the human.
     | spawns N (<= concurrency cap)        ^ result.json (compact), never the transcript
[ Implementers ]  Sandboxed to a git worktree. Edit code, run tests and gates. Never touch main.
```

Implementers signal completion with a `DONE`/`FAILED` sentinel plus `result.json`; the orchestrator
polls and never blocks on any single one.

### Package layout

```
maestro/                        # ~/projects/maestro — installable, `maestro` CLI
  maestro/
    config.py                   # project.yaml schema + validation
    limits.py                   # parse per-model context ceiling tables (D4)
    state.py                    # state.json, journal.ndjson, locks
    backends/
      base.py                   # AgentBackend protocol + Capabilities
      claude.py                 # claude -p driver
      codex.py                  # codex exec driver
      registry.py
    roles.py                    # role -> backend/model resolution, fallback chain
    quota.py                    # usage sampling; decides pause vs switch
    switch.py                   # mid-work switch + checkpoint handoff (D2, D3)
    worktree.py                 # git worktree lifecycle
    implementer.py              # brief / sentinel / result.json protocol
    merge.py                    # deny-list guard, risky reviewer, conflict auto-resolve
    gates.py                    # gate chain: test -> eval -> smoke -> latency (D8)
    adapters.py                 # adapter invocation contract
    hitl/
      telegram.py               # notify, human-requests, answer routing
      commands.py               # control commands incl. /backend
    docs/
      roadmap.py                # machine-readable ROADMAP parser + task model
      project_doc.py
      depmap.py
      upcoming.py               # generic; reads a project-supplied glossary
      graduate.py               # task graduation + consistency guard
    selfheal/
      diagnose.py  selffix.py  redo.py
    orchestrator.py             # the event loop
    watchdog.py
    selfupdate.py               # task-boundary version check + self-test gate (D5)
    cli.py                      # maestro init | doctor | status | run | ctl | watchdog
    templates/                  # project.yaml, adapter stubs, preamble, profiles, systemd unit
    skills/
      claude/SKILL.md
      codex/maestro-setup.md
  tests/
  docs/DESIGN.md                # this file
```

### What each project keeps

`project.yaml`, `adapters/`, `operating_preamble.md`, `profiles/`, the `.orchestrator/`
runtime directory, and its own docs. Nothing else. No orchestration code is copied into a project.

`profiles/` sits at the project root, not under an `orchestrator/` directory — there is no
`orchestrator/` in a scaffolded project at all (`maestro init` creates `adapters/ docs/ profiles/
systemd/`, matching `maestro/templates/`'s own top-level entries one for one; confirmed by a fresh
`maestro init` on a throwaway repo, and by `grep -rn 'REPO / "orchestrator"' maestro/` returning
nothing).

**Nothing project-identifying crosses into maestro** — no tokens, chat IDs, bot names, domain
vocabulary, or evaluation specifics. `.env` is per-project and `init` prompts for its contents.

---

## 4. The seam

**Maestro owns:** watchdog, state and journal schema, usage sampling and thresholds, handoff schema
and routine, implementer protocol, profile launching, concurrency throttle, Telegram layer, decision
and escalation policy, reporting and proposals, the 3-doc engine, backend drivers and switching,
self-update.

**The project supplies** thin adapters behind fixed interfaces, each an executable with a JSON
stdin -> stdout contract:

| Adapter | Returns | Required |
|---|---|---|
| `adapters/test` | `{pass, summary}` | yes |
| `adapters/eval` | `{metrics, baseline, deltas, verdict: claim\|flat\|block, token_free, elapsed_s}` | no (D8) |
| `adapters/smoke` | `{pass, metrics, reason}` | no |
| `adapters/deploy` | `{deployed}` | no |
| `adapters/healthcheck` | `{healthy, detail}` | no |
| `adapters/latency` | `{median_ms, p95_ms}` | no |

Plus `project.yaml` (§5) and `operating_preamble.md` (generic safety rules + the project deny-list).

> In one line: **maestro owns orchestration, survival, human-in-the-loop, state and policy; the
> project owns what good looks like, how to ship, and configuration.**

---

## 5. `project.yaml`

```yaml
version: "2"
# Note: the adopted maestro version is runtime state, recorded in .orchestrator/state.json
# as `maestro_version` (§9). It is deliberately NOT pinned here — see D5.

project:
  name: myproject               # derives tmux session, systemd unit, /tmp paths
  repo_path: /home/dan/projects/myproject
  remote: origin
  main_branch: main

roles:
  implementer:  {backend: claude, models: {claude: claude-sonnet-5, codex: gpt-5.6-terra}}
  judge:        {backend: claude, models: {claude: claude-opus-5,   codex: gpt-5.6-sol}}
  diagnoser:    {backend: claude, models: {claude: claude-opus-5,   codex: gpt-5.6-sol}}

fallback_chain: [claude, codex]

switch:
  on_quota_exhausted: true
  manual: true

model_limits:                   # D4 — defaults shown
  claude: ~/.claude/model_context_limits.md
  codex:  ~/.codex/model_context_limits.md

gate:                           # D8 — eval absent => test-only, journaled unevaluated
  chain: [test]
  primary_metric: null
  baseline_source: null
  bands: {claim: null, flat: null, block: null}

telegram:
  chat_id: <captured by init>
  # token lives in .env as TELEGRAM_BOT_TOKEN — never here, never committed

thresholds:
  five_h_pause_pct: 92
  concurrency_cap: 3
  # context ceilings come from model_limits, not from here

risky_set: []
deny_list_extra: []
secrets: [".env", ".env.local", "**/*.pem", "**/*credentials*.json"]
prod_stores: []
docs: {roadmap: docs/ROADMAP.md, project: docs/PROJECT.md, dependency_map: docs/dependency_map.md}
```

A role declares its model **per backend**, so a mid-work switch resolves the same role under the new
backend without a separate equivalence table. Context ceilings then follow automatically from
whichever model ends up running.

---

## 6. Backend abstraction (D1)

```python
class Capabilities(NamedTuple):
    native_resume: bool         # can resume a session by id
    system_prompt_file: bool    # supports full-replace lean profiles
    usage_telemetry: bool       # can report live context% / quota%
    sandbox: bool

class AgentBackend(Protocol):
    name: str
    def capabilities(self) -> Capabilities: ...
    def launch(self, spec: LaunchSpec) -> Handle: ...
    def resume(self, handle: Handle, prompt: str) -> Handle: ...
    def parse_exit(self, rc: int, log_tail: str) -> ExitVerdict: ...   # ok | quota_exhausted(reset_at) | crashed
    def usage(self) -> Usage | None: ...                               # context_tokens, five_h_pct, weekly_pct
```

Core code branches on **capabilities, never on backend name**. A backend lacking `native_resume`
gets a fresh session seeded from the handoff brief; one lacking `usage_telemetry` loses only
threshold-triggered switching, not correctness.

**claude driver** — wraps the proven invocation: `claude -p --model --system-prompt-file
--strict-mcp-config --mcp-config --add-dir --resume <uuid>`. Usage read from `.orchestrator/usage.json`,
written by the statusline sampler. Quota exhaustion detected from stdout text with reset-time parsing.

**codex driver** — `codex exec` with `--model` and sandbox flags. Codex's statusline already emits
`context-used`, `five-hour-limit` and `weekly-limit`, so usage telemetry is at parity and threshold
switching works in both directions.

> ### Correction (M2 research, 2026-08-12)
>
> The two driver paragraphs above describe tool behaviour that was **verified wrong** during M2's
> research pass. The **decisions** in §2 are unaffected — D1, D2 and D3 all stand. Only the factual
> premises are corrected. Full evidence lives in
> `docs/plans/2026-08-12-m2-backends.md` §"Research findings"; the original text is left in place so
> the claim and its refutation stay visible together.
>
> **C1 — the claude invocation.** Four of the seven flags listed above appear **nowhere** in the
> reference implementation: there is no `--strict-mcp-config`, no `--mcp-config`, no `--add-dir`, and
> no `--dangerously-skip-permissions`. The real argv, brief positional and last, is:
>
> ```
> claude -p --model <model_id> --session-id <uuid4> [--system-prompt-file <abs>] <brief>
> ```
>
> `--system-prompt-file` is emitted only when the file exists, and `--resume <uuid>` **replaces**
> `--session-id` rather than accompanying it. The launch is also indirect — a generated `launch.py`
> run in a tmux window — and stderr is merged into stdout.
>
> **C2 — Codex usage telemetry.** Codex does **not** emit `context-used` / `five-hour-limit` /
> `weekly-limit`. Those are internal TUI status-line item identifiers (a Rust enum) rendered only
> inside the interactive TUI: no external status-line command, no JSON payload, no file, and no
> status-line hook (Codex's 11 hook events do not include one). There is no `codex usage` subcommand,
> and `codex exec --json` carries per-turn token counts only.
>
> **The conclusion nevertheless survives** — telemetry parity is reachable by two other verified
> routes: the per-run **rollout JSONL** (`~/.codex/sessions/…/rollout-*-<thread_id>.jsonl`, whose
> `token_count` records carry a full `rate_limits` snapshot) and **`codex app-server`**'s
> `account/rateLimits/read` JSON-RPC method.
>
> **C2a — do not key usage windows by name.** This account has no five-hour window at all: every
> sampled record shows `window_minutes: 10080` with `secondary: null`. Windows must be mapped by
> `window_minutes`, never by the labels `five_hour`/`weekly` — otherwise a threshold that can never
> fire silently disables switching.
>
> **C2b — one open question resolved early.** §13 asks whether `codex exec` exposes a resume handle
> stable enough for `native_resume = True`. **It does.** The `thread_id` from the `thread.started`
> event is a documented public contract, verified working end to end. The Codex driver declares
> `native_resume = True`.

---

## 7. Mid-work switching (D2, D3)

Three triggers, one path.

1. **Quota exhausted** — the process has already exited; nothing to interrupt.
2. **Usage threshold crossed** — the orchestrator writes a `SWITCH` sentinel into the implementer's
   workspace. The operating preamble instructs implementers to check for it and checkpoint-and-exit
   at the next tool boundary. After a grace period, the tmux window is killed as a fallback.
3. **Manual** — `/backend codex` for future launches, or `/backend codex <task-id>` for one
   in-flight task. Same sentinel path.

The switch itself, in all three cases:

```
1. Resolve target backend from fallback_chain; resolve the role's model under it.
2. Build a handoff brief from the worktree:
     original task brief + commits made + `git diff --stat` + checkpoint notes
     + verification status + remaining steps.
3. Launch the target backend FRESH in the SAME worktree with that brief.
4. Journal `backend_switch {task, from, to, reason}`; notify via Telegram.
```

Uncommitted work survives because it is on disk. Conversation context does not, and is
re-summarised into the brief — the same trade already accepted for context-ceiling handoffs. A
switch also resets the context budget, which is a side benefit on long tasks.

**Never mid-tool-call.** The sentinel is honoured at a tool boundary; the hard kill is a fallback for
an unresponsive implementer, and its lost work is bounded by the last checkpoint.

---

## 8. Model context limits (D4)

The per-model ceiling tables are extracted out of each agent's global instructions file into a
dedicated, machine-parseable file in the same directory. The global instructions file keeps a
one-line pointer to it.

```
~/.claude/model_context_limits.md     <- extracted from ~/.claude/CLAUDE.md
~/.codex/model_context_limits.md      <- extracted from ~/.codex/AGENTS.md
```

Both already share a table shape, which the extraction preserves:

| Model | Prepare handoff | Normally start fresh | Exception ceiling |

`limits.py` parses these into per-model soft/hard ceilings and caches the result to
`.orchestrator/model_limits.json`. Paths are configurable in `project.yaml`, defaulting to the two
locations above. **This replaces the hardcoded 110K/120K constants** with a lookup keyed on the
running model id.

Adding a new model means editing one small table; every project picks it up. A missing file or an
unrecognised model falls back to shipped defaults and raises a `doctor` warning — never a hard
failure on a machine that lacks these files.

> Per the operator's delegate-to-agent rule, the `~/.codex/` side of this extraction is performed by
> invoking the Codex CLI, not by writing into `~/.codex/` directly.

### The rotation trigger — live on both backends (A4, A5, readiness queue)

The ceiling table is not just a lookup library: the orchestrator's poll loop uses it to decide when
to rotate an in-flight implementer onto a fresh session, through the same switch path §7 describes —
`REASON_CONTEXT` (`switch.py`), a fourth trigger beside quota/threshold/manual, in the same worktree
(no `create_worktree` call), always re-briefed rather than resumed, journalled distinctly. A4 built
this trigger; **A5 is what makes it fire for real** — before A5, `Usage.session_id` was `""` on every
real path, so the guard below never passed and the trigger, while fully wired, was correct but inert
by construction.

**Attribution is the precondition, not a formality.** A context reading means nothing without knowing
which conversation it describes, so `AgentBackend.usage()` takes an optional session `Handle`
(`backends/base.py`), and the orchestrator threads it through (`_sampled_usage`). Asked about a
session, each driver must answer *that session's own record or nothing*: Claude reads the session's
own transcript JSONL, located by the uuid **maestro minted at launch** — deliberately not by
re-deriving Claude Code's own encoded-cwd directory name, which is lossy and ambiguous on real paths.
Codex reads the session's own rollout by thread id, and no longer falls back to whatever thread the
driver instance last happened to touch. Either way the returned `Usage` carries
`session_id == handle.session_id`; an account-level sample (`usage()` with no handle — the operator's
own interactive reading, say) leaves `session_id` empty by contract and can never be mistaken for an
implementer's. `orchestrator._attributed_to` checks exactly that equality before any rotation is
allowed to act, and **a reading it cannot attribute to a specific conversation still rotates nothing**
— that refusal is the whole safety property this trigger is built on, not an incidental gap: acting
on an unplaceable reading would mean throwing away a healthy implementer's session on evidence that
could belong to a different agent, or to the operator's own interactive use, entirely.

**The number.** What's compared against a model's `prepare_handoff_high` is one turn's live input
side, not a running total. For Claude it is `input_tokens + cache_creation_input_tokens +
cache_read_input_tokens` of the **last** assistant record in that session's transcript — reproduced
independently against a live statusline reading and found exact to the token. Codex reads the
equivalent figure its own rollout already reports for that thread. Both are read per-record, never
summed across turns: a sum measures cumulative spend, which only ever grows, where a context is a
size that can also come back down.

**Two guards bound the rotation, and both now cover both backends.** *Freshness*
(`_rotation_sample`): the sample must postdate the entry's own launch, checked against the session
record's own mtime — real on Claude via the transcript's mtime, and, after A5's fix round, on Codex
via the rollout's mtime on the handled path. Without this, a task that rotates and then re-reads the
pre-rotation context on the very next poll would rotate again, forever, discarding each fresh agent's
work in turn. *Count* (`MAX_CONTEXT_ROTATIONS = 3`, unchanged since A4): once a task has been rotated
this many times it is left in the session it has — `TASK_TIMEOUT` and the ordinary failure paths
still apply — because a task that needs more fresh sessions than this is one for the operator to look
at, not one for the loop to keep restarting on its own.

---

## 9. Self-update (D5)

Single channel. Versions are materialised as git worktrees; a pointer selects the adopted one.

```
~/.maestro/versions/<sha>/      git worktrees of ~/projects/maestro
~/.maestro/current  ────────>   the adopted version; projects import through this
```

At an idle moment or between tasks — **never mid-task** — the orchestrator compares maestro HEAD
against the `maestro_version` recorded in `state.json`. If newer, it runs maestro's own test suite
against the candidate worktree:

- **Green** — repoint `current`, record and journal the version, notify, and re-exec the
  orchestrator so the new code is loaded. This matches the existing "halt is a restart, every resume
  is a deploy" semantics.
- **Red** — hold `current` where it is and alert with the failing tests. Every project keeps running
  on last-known-good rather than inheriting a broken commit.

Rollback is repointing `current`. Because the version is journaled per task, it is always possible to
tell which maestro version ran a given piece of work.

---

## 10. Setup (D6)

**`maestro init`** — idempotent; anything that already exists is written as `.new` beside the
original with a diff shown, never clobbered.

- *Derives*: git root, remote, main branch, test command, package manager.
- *Scaffolds*: `project.yaml`, executable adapter stubs, `operating_preamble.md`, profiles, the
  3-doc scaffolding, `.orchestrator/`, the pre-commit consistency hook, `.gitignore` entries, and the
  systemd unit plus tmux launcher named from `project.name`.
- *Prompts* for the Telegram bot token, writes it to that project's `.env` only, validates it via
  `getMe`, and captures `chat_id` from a test message.
- *Verifies*: a dry-run `maestro status` and a no-op supervised loop before reporting success.

**`maestro doctor`** — conformance check for an existing project: docs present and parseable,
adapters executable and contract-conforming, model limits resolving, configured backends installed
and their models valid, Telegram reachable, unit installed, gate present (nagging when the project is
running unevaluated), journal and version health.

**The setup skill**, one source rendered into both agents' formats and installed by
`maestro install-skills`:

```
maestro/skills/claude/SKILL.md          -> ~/.claude/skills/maestro-setup/
maestro/skills/codex/maestro-setup.md   -> ~/.codex/prompts/       (installed via the Codex CLI)
```

The skill covers what `init` cannot derive: what good looks like for this project, what is risky,
what belongs on the deny-list, the first roadmap tasks, and the glossary used by the human-facing
upcoming-work document. It interviews, then calls `init` with the answers. It also converts an
existing project's prose docs into the machine-readable roadmap format — the realistic case for any
repository that is not brand new.

**Two entry modes, one skill.** *Fresh project*: the operator runs brainstorming/planning first
(outside maestro) to produce a real plan, however rough; the setup skill runs after that and
*before* any implementation begins, since implementation from here on is what maestro's loop runs —
Step 2's roadmap interview turns that plan into `init`'s first real task blocks (a fresh scaffold
otherwise ships with zero tasks by design). *Existing/running project*: Step 1 detects a prior
`project.yaml`/`ROADMAP.md`/`.orchestrator` and treats the run as a migration, not a fresh install;
Step 3 adapts what is already there — organising existing prose docs (README roadmaps, backlogs,
design docs) into the roadmap format, preserving the reasoning behind each item — rather than
starting blank, then hands off to maestro from that point on.

### Systemd and tmux

Generated units obey the operator's isolation rules: `ExecStart` runs a launcher script that wraps
`tmux new-session -d -s <project>-watchdog`, so the service is auditable from a phone with
`tmux a -t <project>-watchdog`. `Type=simple` (the watchdog process lives in the tmux server's
cgroup, which makes `Type=forking` with a PIDFile unusable), `Restart=on-failure`.

---

## 11. Extraction and migration plan

Ordered so that every stage is independently verifiable and the live loop is untouched until the end.

| Stage | Work | Verified by |
|---|---|---|
| **M0** | Create the repo; add characterisation tests around today's untested paths — control commands, reconcile, merge, gates | Tests green against current AbuAliArchive code |
| **M1** | Extract core modules. Pure refactor, no behaviour change. AbuAliArchive still runs its own scripts | maestro suite green; AbuAliArchive diff empty |
| **M2** | Backend layer: claude driver at parity, then codex driver; switching and handoff | Forced switch on a scratch task, both directions |
| **M3** | Limits extraction (Claude side directly, Codex side via the Codex CLI); self-update and its gate | `doctor` resolves every model; red-test rollback drill |
| **M4** | `init`, `doctor`, skills — proven on a throwaway test project | Fresh project completes a no-op supervised loop |
| **M5** | **AbuAliArchive cutover** — HALT, install maestro, delete the superseded scripts, resume | `/status` matches pre-cutover; one supervised task end to end |
| **M6** | Live switching validated on AbuAliArchive | A threshold trigger produces a real backend switch |

### Scripts superseded at M5

`scripts/orchestrator_run.py`, `orchestrator_ctl.py`, `orchestrator_status.py`,
`launch_orchestrator.py`, `watchdog.py`, `watchdog-launcher.sh`, `maestro_ask.py`,
`maestro_redo.py`, `maestro_selffix.py`, `prepared_actions.py`, `gen_dependency_map.py`,
`gen_upcoming.py`, `mark_task_complete.py`, `check_roadmap_consistency.py`,
`check_verifications.py`, `notify_telegram.sh`. Roughly 8,300 lines.

### Cutover safety

The loop is currently idle (`idle_gated`, all tasks gated), which is a good window. Cutover uses the
HALT sentinel — no elevated privileges required — on a branch, leaving the superseded scripts in
place until M5 verifies. Rollback is removing the install and reverting one commit. In-progress
uncommitted work in the consuming repository is never touched.

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| **M1 is a refactor of a 4,567-line file whose `main()` has cyclomatic complexity 249** — the single largest source of risk in this project, and unrelated to anything Codex | Characterisation tests written *first* (M0); extract module by module against them; AbuAliArchive cutover is last, not first |
| A mid-work switch loses more context than the brief conveys | Brief is reconstructed from the worktree (commits, diff, checkpoint), which is the durable record; switching is opt-in per trigger and journaled |
| Self-update adopts a change that passes tests but breaks a project | Adoption only at task boundaries; version journaled per task; rollback is a pointer repoint |
| A project runs indefinitely with no eval gate | `verdict=unevaluated` journaled on every merge; `doctor` nags persistently |
| Codex driver diverges from Claude behaviour in subtle ways | Core branches on declared capabilities only; both drivers exercised by the same protocol test suite |

---

## 13. Open items to resolve during build

- Exact stall-detection window and the definition of journal progress for the generic watchdog.
- Whether `codex exec` exposes a resume handle stable enough to implement `native_resume`, or whether
  the Codex driver declares it `False` and always re-briefs.
- Whether the Codex statusline can be sampled on the same cadence as Claude's, or needs polling.
- Calibration of the default usage-threshold percentages once switching has run in anger.
- Whether `upcoming.py`'s explanation generation should be backend-agnostic or pinned to one role.

---

## 14. Future research directions

Not scheduled work and not part of the M0–M6 extraction plan — flagged here so a dedicated future
session picks it up deliberately rather than it getting lost.

- **Graph engineering for agent orchestration.** The current frontier beyond "prompt engineering"
  and "loop engineering" in agentic coding is structuring agent work as an explicit graph (nodes =
  agent calls/tools/checks, edges = control/data flow and conditional branching) rather than a
  single supervised loop. Maestro's own orchestrator is loop-shaped today (§7 mid-work switching,
  §9 self-update, the per-stage loop in `docs/EXECUTION.md`). Run a deep-research session to survey
  state-of-the-art graph-based agent orchestration — frameworks, papers, and working projects, not
  just the concept — for ideas that could improve maestro's own model, and to identify any existing
  project worth using as a tool or collaborating with rather than reimplementing. No owner yet; not
  started.
