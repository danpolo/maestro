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
| D7 | **One shared Maestro bot, credentials in `~/.maestro/.env` (P13 B9)** | Tagged messages and explicit project addressing; one network poller delivers durable local mailboxes |
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

Everything `maestro init` scaffolds is maestro-specific configuration, a generated wrapper
script, or runtime state — **never a copy of `maestro/`'s own orchestration code.** That
coupling claim is the point of this section; the list below illustrates it and is
deliberately not asserted as complete, because it has been wrong three times from exactly
this failure — an enumeration followed by "nothing else" rots the moment `init` scaffolds
one more file, and nothing tests it. Confirmed examples, each unconditionally scaffolded by
`cmd_init` (`maestro/cli.py`): `project.yaml` (the project's own settings), `adapters/` (its
test/eval/smoke/deploy commands), `operating_preamble.md` and `profiles/` (what an
implementer is told), `launch.sh` and `systemd/` (how the supervised loop runs unattended —
`launch.sh` is the exact target of the generated unit's `ExecStart`), `.gitignore` and
`.git/hooks/pre-commit` (both openly maestro-specific rather than generic hygiene:
`_GITIGNORE_BLOCK` opens `# maestro (DESIGN.md §10 scaffolding)` and ignores
`.orchestrator/`/`.env`; `templates/pre-commit` calls itself "the consistency hook
DESIGN.md §10 mentions" and exists to run `maestro doctor --pre-commit`), the
`.orchestrator/` runtime directory, and the project's own docs.

`profiles/` sits at the project root, not under an `orchestrator/` directory — there is no
`orchestrator/` in a scaffolded project at all. `maestro init`'s top-level *directories* are
exactly `adapters/ docs/ profiles/ systemd/`, matching `maestro/templates/`'s own top-level
directory entries one for one (its `launch.sh.tmpl`, `operating_preamble.md` and
`project.yaml.tmpl` render to the loose root files named above; its `pre-commit` renders to
`.git/hooks/pre-commit`, git's own hook location, not a project-root directory entry).
Confirmed by a fresh `maestro init` on a throwaway repo, and by
`grep -rn 'REPO / "orchestrator"' maestro/` returning nothing.

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

roles:                          # §17.2 — optional; a role states only how strong its model
  implementer: {strength: balanced}   # must be. Absent = the role template's default.
  reviewer:    {strength: strong, independent_of: [implementer]}
                                # The model and harness are derived from the adopted policy.

routing:
  near_cost_margin: 0.20             # §17.5 — effort preference inside a quota pool

switch:
  on_quota_exhausted: true
  manual: true

model_limits:                   # D4 — defaults shown
  claude: ~/.claude/model_context_limits.md
  codex:  ~/.codex/model_context_limits.md
  antigravity: ~/.gemini/model_context_limits.md

gate:                           # D8 — eval absent => test-only, journaled unevaluated
  chain: [test]
  primary_metric: null
  baseline_source: null
  bands: {claim: null, flat: null, block: null}

priority: 1                     # optional override; global defaults.priority, then 1
# Shared TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID live in ~/.maestro/.env,
# mode 600, never in project.yaml or committed files.

thresholds:
  five_h_pause_pct: 92
  concurrency_cap: 3
  host_pool_concurrency: 0   # agent slots per backend across ALL projects on this host
                             # (~/.maestro/reservations.json); 0 = record, don't bound
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

## 7. Mid-work switching (D2, D3, D4)

Four triggers, one path.

1. **Quota exhausted** — the process has already exited; nothing to interrupt.
2. **Usage threshold crossed** — the orchestrator writes a `SWITCH` sentinel into the implementer's
   workspace. The operating preamble instructs implementers to check for it and checkpoint-and-exit
   at the next tool boundary. After a grace period, the tmux window is killed as a fallback.
3. **Manual** — `/backend codex` for future launches, or `/backend codex <task-id>` for one
   in-flight task. Same sentinel path.
4. **Context ceiling crossed** — the running session's own live context crosses the model's
   `prepare_handoff_high`. §8 covers attribution and the anti-loop guards; the two ways this
   trigger's mechanics differ from the other three are called out in the steps below.

The switch itself, in all four cases:

```
1. Use the existing switch target selection (eligible role columns or an explicit manual
   backend); resolve model and effort from the task's frozen inputs.
   (Trigger 4 skips target selection: its target is always the backend the task is
   already running on, never a different one — a rotation, not a switch to elsewhere.)
2. Build a handoff brief from the worktree:
     original task brief + commits made + `git diff --stat` + checkpoint notes
     + verification status + remaining steps.
3. Launch the target backend FRESH in the SAME worktree with that brief.
4. Journal `backend_switch {task, from, to, reason}` (trigger 4 instead: `session_rotation`,
   keeping the same five fields — a distinct event name rather than a `from == to` reading
   that could pass for a bug); notify via Telegram.
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

`~/.gemini/model_context_limits.md` (Antigravity, 2026-09-14) uses the same shape. Every row there
carries the `agy/` harness prefix (`limits.AGENT_MODEL_PREFIX`), because agy also serves third-party models
whose limits are agy-specific. `limits.resolve(model, backend="antigravity")` applies the prefix, so
an agy row never answers a claude/codex lookup of the same model name.

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

### How "projects import through this" is enforced (added 2026-09-03)

`maestro/bootstrap.py` is what makes the pointer above load-bearing. Before it, `current` was
written by `adopt()` and **read by nothing**: the adopted version was a record, and what a project
actually ran was whatever its venv resolved `import maestro` to. For the editable install the
reference project was cut over with, that was the maestro *working tree* — uncommitted edits
included, with the self-test gate running after the deployment rather than before it.

`cli.main()` now calls `bootstrap.reexec_into_adopted_version()` before argparse and before any
`cmd_*` binds `$MAESTRO_REPO`. If a version is adopted and this process is not it, the process
replaces itself (`os.execve`) with `python -m maestro.cli`, the adopted checkout leading
`$PYTHONPATH`. One seam covers every entry point — the generated `launch.sh`, the watchdog's tmux
spawns, the quota-reset cron, the pre-commit hook, and an operator's ad-hoc CLI — because they all
funnel through `main()`.

- **`PYTHONPATH` is sufficient**, and no symlink or reinstall is needed: an editable install appends
  its `_EditableFinder` to `sys.meta_path`, *after* the `PathFinder` that reads `sys.path`. The
  editable mapping is the fallback, not the winner. (Verify from a neutral cwd — `python -c` puts the
  cwd on `sys.path` and will mask this if you test from inside the repo.)
- **`$MAESTRO_BOOTSTRAPPED` is the guard and the opt-out**, one variable for one idea: *this process
  is already the checkout it should be.* It makes the redirect single-shot, it is what pins a
  candidate's suite inside the candidate (`selfupdate._self_test_env`, `tests/conftest.py`) so a gate
  cannot grade the deployed code instead, and it is the escape hatch for anyone editing a checkout.
- **Fail-open throughout.** No pointer, a pruned worktree, a path that is not a checkout, an `exec`
  that will not start — each leaves the process running in place. `maestro doctor`'s `adopted_code`
  check is the visibility half: it compares where maestro was *imported from* against what `current`
  names, so a divergence is reported rather than assumed away.

`bootstrap.py` imports nothing from `maestro`. It runs before `$MAESTRO_REPO` is set, and importing
anything that reaches `maestro.state` at that moment would bind `Paths.from_env()` to the wrong root
for the life of the process.

---

## 10. Setup (D6)

**`maestro init`** — idempotent; anything that already exists is written as `.new` beside the
original with a diff shown, never clobbered.

- *Derives*: git root, remote, main branch, test command, package manager.
- *Scaffolds*: `project.yaml`, executable adapter stubs, `operating_preamble.md`, profiles, the
  3-doc scaffolding, `.orchestrator/`, the pre-commit consistency hook, `.gitignore` entries, and the
  systemd unit plus tmux launcher named from `project.name`.
- *Uses* the shared `~/.maestro/.env` credentials and registers the project for shared-bot
  delivery. If absent, reports that shared setup is needed; it does not prompt for a project bot.
- *Verifies*: a dry-run `maestro status` and a no-op supervised loop before reporting success.

**`maestro doctor`** — conformance check for an existing project: docs present and parseable,
adapters executable and contract-conforming, model limits resolving, configured backends installed
and their models valid, Telegram reachable, unit installed, gate present (nagging when the project is
running unevaluated), journal and version health.

**The setup skill** is maintained in the canonical shared skill library and exposed through
its managed runtime links:

```
~/agent-skills/skills/maestro-setup/ -> ~/.claude/skills/maestro-setup/
                                  -> ~/.codex/skills/maestro-setup/
```

The skill covers what `init` cannot derive: what good looks like for this project, what is risky,
what belongs on the deny-list, project priority (once, default if skipped), the first roadmap tasks, and the glossary used by the human-facing
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

Generated units obey the operator's service-ownership rule: `Type=forking` and a direct
`ExecStart=/usr/bin/tmux new-session -d -s <project>-watchdog -n main ...`, so the service is
auditable from a phone with `tmux a -t <project>-watchdog`. `ExecStop` kills that exact named
session. The standalone `launch.sh` remains a blocking manual launcher; the generated unit does
not invoke it.

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

---

## 15. Third-party verification gate (B06, B17)

Two of the graph-engineering blind spots are not research questions and were deliberately left
unanswered by both the local-experiment pass and the targeted research pass:

- **B06 — backend capabilities.** Which models, effort controls, resume/cancel behaviour,
  instruction delivery, sandboxing and usage telemetry an agent CLI actually has.
- **B17 — free-use guarantees.** Which licence and terms an optional dependency actually ships
  under, and whether normal maestro operation stays free.

They were separated out because an answer is a fact about **one exact version** and expires
silently. That is not a theory: within a single research window the installed CLIs had already
left the vendor documentation behind — `codex` 0.153.4 returns both quota windows structurally
(no PTY collector needed, contrary to the research), `agy` 1.1.27 exposes no usage surface at all
(contrary to the research), and `claude` 2.1.263 returns a five-hour percentage with a *null*
reset epoch and an account-level sample that named a different model than the run being measured.

So the rule is not "research these now". The rule is:

> **Integration verification gate.** Any new or materially updated backend, or any optional
> external dependency, must pass implementation-time capability, version, licensing, cost and
> conformance verification **before** it is marked supported. Verification is recorded against the
> exact installed version, with its evidence, in `maestro/thirdparty.json`.

### How it is enforced

| Enforcement | Where | What it catches |
|---|---|---|
| Coverage invariants | `tests/test_thirdparty.py` | A backend registered in `BACKENDS`, or a distribution declared in `pyproject.toml`, that has no verification row at all. Computed from the repository only — no host access, so it is deterministic in CI and on any machine. |
| Schema invariants | `maestro/thirdparty.py::parse_register` | A row that claims a `verified_version` without citing evidence; a dependency row with no licence or no stated source for it; a backend row that does not list the controls actually observed. |
| Drift detection | `maestro doctor` (WARN) | An installed version that has moved away from the one a row was verified against. A CLI that auto-updates overnight must be *loud*, not fatal to the whole checklist. |
| Re-observation | `maestro/thirdparty.py::status_for` / `audit` | Agent CLIs auto-update constantly. When a backend's version moves, the gate re-reads its `--help` (no agent turn, no quota): if every recorded `controls_observed` entry is still present the status is `reobserved` and does not block; a vanished control, or an unreadable `--help`, stays `drifted`. Dependencies are not re-observed. |
| The gate itself | `maestro doctor --thirdparty` (exit ≠ 0) | Drift that re-observation could not clear, as a **required** check. This is the command an implementing agent runs as the last step before calling a backend or dependency integration complete. |

`status` is one of `verified`, `absent` (not installed here — a row is a fact about software, not
a requirement that this machine have it), `candidate` (shortlisted, never verified, not yet in
use), `reobserved` (a backend bumped past its row with every recorded control still present —
reported, not blocking; the row keeps naming the version the deeper verification used),
`drifted` or `unverified`. Only `drifted` and `unverified` block, and only when the subject
is actually **in use** — a driver is registered for it, or `pyproject.toml` declares it. That is
what lets the register carry a shortlisted provider (Tree-sitter, D07/P9) before anyone adopts it:
the row is inert until the moment adoption puts it in use, and then it blocks.

### What the register is not

It is a **verification ledger, not the runtime capability catalog**. Routing does not read it,
`Capabilities` is not derived from it, and nothing in it decides what a driver may do. The
capability catalog is P6 work; when it is built it should be *populated from* verified rows rather
than duplicating them, so that there is still exactly one place where "we checked this, against
this version, and here is the evidence" is written down.

B06 and B17 therefore stay **open** in the blind-spots register. That is correct and deliberate:
they are closed per adoption, not once.

## 16. The graph runner (`engineering.runner: graph`), as qualified in P12

The graph-engineering migration (`docs/graph-engineering/`) replaces the legacy loop's hard-coded
lane with a compiled workflow per task (nodes, edges, attempts in the control store). This
section records the behaviour P12 fixed while driving whole tasks through `orchestrator.main`;
the per-phase record lives in `docs/graph-engineering/STATE.yaml`.

### Task worktree lifecycle

- **Create at claim.** A claimed writer (`permissions_ref: task_worktree_writer`) gets its tree
  from `task_tree.ensure` at `.orchestrator/worktrees/<task>[/<candidate>]` on branch
  `impl/<task>[/<candidate>]`, cut from the revision's `code_base_sha`. Readers (gate, review,
  diagnoser, merge) get the same path from `WorkflowRunner.worktree_for` once it exists. If git
  cannot create it, the claim is failed with the refusal `worktree_unavailable: <reason>` and the
  node's `on_failure` edge runs; no agent is launched into a tree that is not there.
- **Brief header.** Every agent brief opens with the task id and title, the `WORKTREE`, the
  `WORKSPACE` (the attempt inbox), and for a writer an explicit instruction to commit on its
  branch, because uncommitted work is never integrated. An `agent_cli` agent reports by writing
  `DONE` or `FAILED` into the workspace; a model-endpoint harness reports through its runtime and
  is not told about the sentinel.
- **Harness commit.** A writer that cannot commit for itself (the model-endpoint harness has four
  tools and no shell; a `kind: script` lane) is committed on a clean finish by
  `task_tree.commit_all`, authored `maestro harness <maestro-harness@localhost>`. An `agent_cli`
  agent commits its own work.
- **The gate checks commits only.** The verification gate's evidence is bound to HEAD, so the
  gate refuses a task tree with uncommitted changes (`uncommitted_changes: <paths>`) and the run
  parks at the gate with no evidence recorded, rather than one node later at merge's freeze.
- **Release at settle.** When the run settles, each task tree is released
  (`task_tree.release`): a clean tree is removed; a tree with uncommitted changes is kept,
  because that work exists nowhere else; the branch is deleted only when merged (`git branch -d`,
  which refuses otherwise). Each release is a `TaskTreeReleased` event naming the candidate, the
  run outcome and what was done. The first time a run hands a writer its tree, a
  `TaskTreePrepared` event says whether the tree was already there and whether it held
  uncommitted changes.

### The target checkout follows the merge

Merge moves the target branch by compare-and-swap (`git update-ref`), which does not touch a
worktree that has the branch checked out, such as the project checkout itself. So every such
checkout is then fast-forwarded with `git read-tree -m -u <old> <new>`, which keeps unrelated local
edits and refuses, touching nothing, when one would be overwritten. The refusal is recorded as
`stale_checkouts` on the `TargetBranchFastForwarded` event and in merge's detail. Graduation
(`mark_roadmap_complete`) also commits only its own paths (`git commit -- <paths>`). P12 found
why both are needed (finding D8): a checkout left on the old tree has an index that reads as a
staged revert of the merge, and graduation's whole-index commit put that revert on `main`.
Graduation stages only the paths that exist (`git add` with one unmatched path stages nothing),
and strips a task's section without crossing a neighbouring task's yaml block; finding D18 was
a roadmap of bare consecutive blocks losing every later task, left uncommitted on `main`.

Graduation also carries the accepted run's review follow-ups (non-blocking findings, D16) past
the review artifact (`maestro/docs/followups.py`), in the same commit. Collected across every
run of the task's workflow and deduplicated, they wait in the backlog
(`.orchestrator/backlog.json`, `maestro/backlog.py`) for the end-of-project polish stage
(§19.13, P13 W7). Only a follow-up the reviewer marks `before_dependents` (a task that depends
on this one would build on the defect) runs early, as `<id>-followups` (autonomous, `serves:
[]`, `deps: [<id>]`, the task's scope and checks, `followups_of: <id>`), and each open
dependent gains it in `deps` in the graduation commit. A `-followups`, triage or polish task's
own follow-ups never queue another task. `docs/FOLLOW_UPS.md` is no longer written (old files
stay as history). The accept report (`phase_report`) names the count and where they went.

### The watchdog sees a live graph run

- **A running run is in flight.** The graph runner never fills `state.json`'s `in_flight` and
  journals only when a run opens and settles, so the watchdog's journal-silence stall check
  killed every attempt longer than `STALL_WINDOW_MIN` (finding D9). The watchdog now reads the
  control DB read-only: a `task_runs` row with status `running` and a live orchestrator skip the
  stall check for that poll and keep the silence timer fresh. No DB, no table, or a locked or
  corrupt file counts as no live run, so the stall behaviour is unchanged there.

### Candidates (free-quality widening, `[INV-06]`)

Candidates are off by default (E1 is unqualified). When the policy widens the implementer, each
candidate writes its own tree and branch. When its agent is done, its worker runs the task's own
checks on the candidate's committed tree and reports the evidence and head commit for the
controller to ingest; the `candidate_select` join picks exactly one conclusive candidate. Every
unlabelled node after the join (gate, review, merge) runs on the **winner's** tree, and merge
integrates the winner's branch only. A losing branch is kept, unmerged.

### Settling, parking and prep

- A run whose failure path reaches `park` **settles `failed`** while the `park` node stays
  `claimed` (an inline wait node with no outcome): the run no longer counts as live, and the
  branch stays for whoever picks the task up.
- **B14 prep tasks** (`dispatch: manual`) are opened by the graph loop too (it reads
  `parse_runnable_tasks() + parse_prep_tasks()`, as the legacy loop does). Under the manual
  template, `prep` runs, then `human_action` holds its claim with no outcome until the
  operator's receipt; the run stays `running` meanwhile.

### The proof review's verdict

The reviewer (`agent_definition_ref: reviewer`, e.g. `proof_review`) is judged by its verdict,
not by its `DONE`. Its brief asks it to write `review.json` into the workspace —
`{"verdict": "approved" | "changes_requested" | "rejected", "findings": [...]}`, one blocking
defect per finding — and then `DONE`. The agent handler adopts the file as a
`review_disposition` (in the node's outputs as `review`):

- **approved** → the node succeeds and the run goes on to merge.
- **changes_requested / rejected with findings** → a `verification_failed` bad output. As with a
  gate failure or Dan's rejection, the retry cone reaches back to the implementer (implementer,
  gate, review), and the implementer's rework brief carries the findings as
  `NOTE FROM ORCHESTRATOR`. The usual budget of three quality attempts applies, then the run parks.
- **no file, unreadable, an unknown verdict, or a rejection with no findings** → `[INV-04]`, it
  fails closed as `garbage_output`: the reviewer is asked again (its cone is itself), then parks.

P12 found the gap this closes (finding D6): DuetFlow 02's reviewer described four P1 defects in
its `DONE`, and the graph merged the work because `DONE` was all it read. A model-endpoint
harness bound to the reviewer reports through its runtime and is not yet judged this way.

### Risky files and the deny-list

Dan's decision (2026-09-25, P12 finding D10): an autonomous task **may edit `risky_set`
files**; the gate is the proof review, not Dan. The legacy loop's Sonnet risky reviewer and its
escalation are not ported.

- **What was touched is computed, not declared.** When a reviewer node is routed, the controller
  diffs the tree's HEAD against the merge base with the run's `code_base_sha` (the target branch
  when none was recorded) and matches the changed paths against `project.yaml` `risky_set`
  (a substring, or a glob when it has metacharacters) and the policy's
  `quality_gates.risky_paths`. The task's declared `scope` plays no part.
- **The reviewer is told.** The touched risky files go into the reviewer's manifest and its
  brief (`RISKY FILES TOUCHED: …`, with an instruction to scrutinize exactly those hunks and not
  to approve an unjustified or oversized risky change). The manifest also records the commit
  reviewed (`reviewed_commit`).
- **The reviewer is stronger.** For a risky diff the reviewer's strength floor is one rung above
  the strongest model that wrote the tree (every succeeded writer attempt in the run, and one
  that ended otherwise while the tree may hold its work, see "Reliability carry-overs
  (P13 W8)" below; the winner's only, when widened). The catalog's only comparable ordering is
  `MODEL_STRENGTHS` (`light < balanced < strong`), so "stronger" means a higher rung. If the
  writer is already `strong`, or its model is not in the catalog, the review is refused
  (`route_no_stronger_reviewer`) and the run parks — never bound to an equal or weaker model.
  An ordinary (non-risky) review keeps its role's own demand.
- **Merge refuses what nobody approved.** Before staging, merge's guard
  (`merge.integration_guard`, stage `guard`) diffs the frozen candidate commit against its merge
  base with the target head. A risky path in that diff needs an approving proof-review verdict of
  that exact commit; a lane that composes no reviewer (`script`, `resumable`, or a policy that
  skipped review) therefore refuses (`risky_unreviewed`).
- **The deny-list is absolute.** The same guard applies the hard patterns
  (`merge._HARD_DENY_PATTERNS` + `deny_list_extra`) to the lines the candidate *adds*, and refuses
  any changed path under `secrets` or `prod_stores` (globs matched as globs, as `confinement`
  does). A diff that cannot be computed is a refusal.

A guard refusal fails the merge node with `guard: <reason>` as its detail and a `merge_refused`
journal event; the target never moves and the run settles `failed` (parked), branch kept.

### Checks only Dan can confirm (`kind: dan`)

A ROADMAP verification with `kind: dan` (`id`, `check`, optional `instructions`, optional
`steps` — imperative `{do, run}` items or plain strings, rendered as numbered steps under "Dan
action needed"; `{worktree}` becomes the task tree's absolute path) is a required criterion that
nothing runs.
The gate and staging pipeline run the default `pre_merge` automatic checks. Commands
requiring deployed code declare `stage: post_merge` and run automatically on the integrated
project checkout. `kind: dan` observations follow merge in `operator_check`, with Pass / Fail
buttons (`/verify <id> pass` or `/verify <id> fail <reason>`). Only these observations ask Dan.
Their `operator-verify-<task>-<sha12>` records are distinct from actual merge review, which
still uses `dan_confirm`, Approve / Reject and the pre-merge rework cone. A passing
observation records a review disposition bound to the merged subject. Acceptance requires
every declared criterion and its current evidence; an all-deferred task may integrate a
frozen, guarded tree but cannot be accepted early. A post-merge machine failure or operator
Fail publishes one durable repair task per source run through ROADMAP CAS and control-store
registration. Its `repairs_task`, `repairs_run` and `repair_subject` preserve the relation;
it inherits the parent's prerequisites, avoiding a dependency on the failed parent. Restart
recovers the publication intent instead of duplicating the task. The original merge is never
retried and the ordinary Retry / Shelve failure ask is suppressed for published repairs.
The operator wait is
durable (paused attempt plus request/answer files; a restarted loop neither re-asks nor
re-claims), never auto-approves (an unconfigured bot is journaled once as
`dan_verify_unconfigured` and the request is sent once the bot is configured), and is listed in
`state.json` `awaiting_dan_verification`, which `maestro watchdog` treats like `waiting_on_dan`:
the loop's silence while it waits is not a stall. The legacy runner does not support `kind: dan`.

### Failure handling the controller owns

- **Controller errors.** An exception while advancing a node is decided, never swallowed:
  raised before any worker launched, the claim is failed with
  `controller_error: <Type>: <message>` (so `on_failure` runs, which can launch a diagnoser);
  raised after the launch, the node is left to its worker. Either way a `ControllerError` event
  keeps the traceback tail. An error outside `advance` (ingest, reconcile, settle) becomes one
  `GraphTickFailed` run event per distinct error.
- **Interrupted merge.** A controller lost inside `merge` is resolved by its postcondition probe
  (`maestro.reconcile.merge_probe`): if `impl/<task>` is not yet in the target, the node is
  re-readied with the failure kind `interrupted` (blocked class; it spends no retry budget); if
  it is, or git cannot say, the node and run fail and the merge is never replayed.
- **One acceptance transaction.** The acceptance event, the decision row and the task status
  are committed in one `store.transaction()`: a failure part-way leaves none of them.
- Every accepted run writes **two** `acceptance_decisions` rows (merge's on the integrated tree,
  then accept's re-derivation from stored evidence), both bound to the merged commit.

### Modes and policy

- `hybrid.paid_roles` names **role ids** (`reviewer`, `diagnoser`), not node ids.
- Only the paid-subscription mode is headed for real qualification (the s3 pilots). Free,
  hybrid and the script lane pass fixture lanes only and stay labelled **unqualified**: a fixture
  pass never claims production readiness. Every E1–E5 optimisation also stays off or as shipped,
  unqualified, until real measurements exist.

### Retention (B15)

`project.yaml` carries a `retention:` block, defaulting to `{prune: false, warn_bytes:
1073741824}` (`config.RETENTION_DEFAULTS`, basis in `config.RETENTION_BASIS`). Pruning is refused
(`prune: true` is an error) because `[INV-A03]` keeps raw outcomes and no pruning path exists.
`maestro doctor` warns when `.orchestrator` (excluding task worktrees) exceeds `warn_bytes`. The
default rests on fixture runs (about 2 MB per accepted run, almost all control DB and WAL) and
is re-measured in the real pilots.

### Parity with the legacy loop

The graph runner replaces the legacy loop, so every legacy behaviour is carried unless it
does not apply to the graph *and* the graph already has something better. Carried into
`orchestrator._graph_main`:

- **Telegram.** `▶ Starting|Preparing <id>: <title>` once per opened run; the dependency map
  pushed on a ROADMAP change (`maybe_push_roadmap_map_change`, every poll); after an
  accepted run graduates (on its settle, with the per-poll check as the restart repair),
  `phase_report` with the re-rendered map, the task definition read before graduation strips
  it, and the acceptance decision's reason as the report line (the graph has no smoke dict,
  and no metrics are invented); one `✗ <id> failed at <node>` notice per failed settle, with
  the latest failed attempt's classified failure, refusal or result detail. A restarted loop
  resumes only running runs, so it never repeats a notice.
- **Publishing.** `git push origin main` after graduation (failure ignored; no `origin`
  pushes nothing, journaled once), then `metabranch.reconcile_meta`, which also runs once at
  startup. A merge that changed a `bot_files` path is canary-deployed before the report; a
  failed canary skips it. The changed files come from the merge's recorded
  `target_before..target_after`, since a settled run's merged branch is gone.
- **State and status.** `state.json` `graph_runs` lists the live runs (task, run, title,
  start, live nodes) and `phase` names the newest (idle when none is). They stay out of
  `in_flight`, whose rows the watchdog reaper, `reconcile_in_flight`, the host ledger and
  `/approve`/`/backend` read as tmux sessions. `/status`, `/progress`, `maestro status`,
  `print_status`, the dependency map and `UPCOMING.md` read both. The map and status are
  regenerated at startup and whenever a run opens or settles.
- **Human in the loop.** `hitl_mode` compiles a `MERGE-REVIEW` check into the `dan_confirm`
  step of every run opened while it is on (`compiler.merge_review_check`), so Dan reviews
  the change before merge through the `kind: dan` mechanism. `mode: needs-dan` needs nothing
  extra, because `taskgraph` gives it the manual lane. The B12 `questions:` gate runs before a
  run opens, and Dan's answers reach the writer's brief (manifest `dan_answers`).
  `_send_hitl_reminders` (now also for `awaiting_dan_verification`, stamped in
  `awaiting_dan_verification_reminded`) and `poll_proposal_answer` run every poll, and the
  paused loop in `main` also reads the proposal answer.
- **Scheduling and upkeep.** `_order_runnable` ordering. B8 `resource:` mutex labels are held
  from open to settle. A legacy `resume_state` cooldown is respected. `_self_update_maybe`
  runs when idle, and `apply_ready_self_fixes` / `apply_ready_redo` run every poll.
- **Self-modifying tasks** are never opened. `parse_runnable_tasks` and `parse_prep_tasks`
  exclude them, as they do for legacy.
- **Manual prep (G7).** The prep agent gets legacy's B14 contract (`runner.prep_contract`:
  all automatable prep, never the human step, `dan_action`/`post_action_cmd` in
  `result.json`). `human_action` runs in the controller (`operator` is a controller
  permission) and asks Dan once per attempt (`action-<task>-<hash>`) with the prep's
  `dan_action` (else the ROADMAP's), the worktree, and **Done / Redo / Abandon** buttons,
  or `/verify <task> done | redo <why> | abandon`; `/waiting` lists it (`kind: action`).
  Done records a receipt; the gate then runs `post_action_cmd` in the tree first (1800 s,
  `python3` mapped to the venv; non-zero fails the gate). Redo is a `verification_failed`
  whose retry cone re-runs the prep with Dan's reason in its brief, and asks anew; Abandon
  fails the run. **Deliberate difference:** the manual lane now has a `merge` after the gate
  (template 1.1.0) — legacy merged the prep *before* Dan acted
  (`handle_prep_done`); the graph lands nothing until Dan acted and the gate passed on
  that tree, which DuetFlow's `{worktree}` instructions already assume.
- **Async jobs (G7).** `await_job` polls the task's `await: true` auto checks
  (`gates._classify_verifications`, task tree else repo) at most once a minute: all landed
  → the gate runs every check; a hard failure or 8 h (`AWAIT_VERIFY_TIMEOUT_SEC`) pending →
  the run fails. Dan hears 🚀 once when the job is launched. **Difference:** retrying a failed
  await reopens the task (re-launching the job); legacy's `/approve` re-ran only the check.
- **Resumable tasks (G7).** A gate that fails into `cooldown` applies
  `handle_incomplete`'s rules to its printed `PROGRESS=n/m` (unparseable, 12 sittings or 2
  stalls → park; else `resume_state` and "⏳ progress saved, resuming in ~Nh"), spends no
  quality retry (one sitting per cooldown), and the task reopens once `resume_after` has
  passed; graduation clears `resume_state`. Progress rides the kept `impl/<task>` branch:
  legacy's per-sitting data-only merge to main is **replaced** by one gated merge at
  completion.
- **Reopening and the failed notice (G7).** A task with a settled run reopens only on an
  explicit marker (`state.json["graph_reopen"]`): `/unpark <task>` writes it, as does
  **Retry** on the failed-run notice, which is now a Dan request (`failed-<task>-<run>`, once
  per run) with **Retry / Shelve**; Shelve parks it in `parked_tasks`. The fresh run leaves
  the failed one untouched and continues on the kept branch; a marker also retries a
  compile refusal once.
- **Remerge (`maestro ctl remerge <task>`).** When only `merge` failed (a conflict with a
  task that landed first) after an approving review, the operator fixes `impl/<task>` by
  hand and, with the loop stopped, runs the verb: a `partial_failure` plan repair re-runs
  merge and accept only, carrying implementer, gate and proof review forward — no model,
  no new review. The failed run stays `failed`, merge's attempt budget restarts, and the
  diagnoser's repair quota is not spent (`maestro/workflows/remerge.py`). The loop resumes
  the successor like any `running` run. A risky-set edit is still refused unless the
  review approved the exact commit merged, so a hand fix of a risky diff needs a review.

Deliberately replaced, not ported:

| Legacy | Graph replacement |
|---|---|
| `paused_until` usage-cap break, `get_effective_cap` throttle | pool pauses (`routing.refresh_pauses`, `pool_pauses`), P12B F4/F5 |
| `_threshold_switches` | routing fallback / step-down (`_graph_fallback`) |
| `_context_rotations` | the Context Gate, which is not built and is not built in phase work (AGENTS.md) |
| `sonnet_review_proofs` | the `proof_review` node and its `review.json` verdict |
| one implementer retry, `_do_retry` | the node retry policies and failure ladder, then `diagnose` and plan repair (W7) |
| `_timeout_expired`, `reconcile_in_flight` | attempt deadlines, writer leases, `reconcile` |
| `merge_and_eval` safety checks | merge's freeze and staging pipeline (G2, done separately) |
| `poll_awaiting_verifications` | the `await_job` node's own poll (G7, above). Its legacy input (an async job in `waiting_on_dan`) cannot exist under the graph runner: the runner switch refuses while one is waiting, and the graph never writes one |
| `_merge_data_only` per resumable sitting | one gated merge when the task completes; progress rides the kept `impl/<task>` branch (G7) |
| `/approve`, `/reject` on a `waiting_on_dan` id | the same verbs answer the graph request waiting on the task: Approve/Done, Reject-with-reason/Abandon; with nothing waiting they point to `/waiting` and `/verify` (a legacy id that exists keeps its meaning) |
| `/redo <task> <msg>` (rewrite the parked deliverable) | a manual step's Redo: the prep re-runs with `<msg>` in its brief (legacy `/redo` when no graph step waits) |
| `/fix <task>` (implement a saved diagnosis) | the `diagnose` node, plan repair and the failure ladder; no diagnosis file is written, so `/fix` says so and offers `/unpark` |
| `/backend <name>` pin, `/backend <name> <task>` switch | routing binds each attempt (`routing`, catalog, fallback/step-down); graph routing never reads the pin, so under the graph nothing is pinned or moved and the reply says so |

Still open: nothing from the G7 list. `/ask <task> <question>` also supports pending
graph verification and manual-action requests in `awaiting_dan_verification`. It reads the
exact request, checks task/run/snapshot identity, and loads the last successful
implementer or prep context from its immutable manifest. A fresh support call on the
configured implementer backend inspects that worktree and gives advice with expected
output. It requests `writable=False` and advice only, and the handler never submits Dan's
verdict. Filesystem enforcement follows the existing backend capabilities; for drivers
without a filesystem sandbox, the read-only instruction remains advisory.
Missing worktree or saved context
is reported rather than silently inspecting the project root. Verification replies use
`/verify <task> approve | reject <reason>`; manual steps use `done | redo <reason>`.
Legacy requests retain their prep-session resume path. Spec and permission questions
are not handled by this command.

### Reliability carry-overs (P13 W8)

Ten items the P12 pilots and the W1 rollout left open. Each has a lane or test that was red
before its change.

**A node nobody decided is failed, never left `uncertain`.**

- An agent that exits cleanly with no sentinel is a `no_sentinel` bad output: retried from its
  `start_sha` and failed once the budget is spent.
- A handler that raises, inline or in a worker, settles its node `failed` as `controller_error`
  (never retried through the retry cone), with the postcondition probe's answer in the evidence
  and a `ControllerError` event. The run reaches the failed-run notice, which names the node.
- The reconciler also adjudicates a running attempt that holds **no lease** (an async launch, a
  test designer, a controller-run step) once its bound pid is dead and no receipt exists
  (`Reconciler._reconcile_unleased`). Before, only writer leases were scanned.
- A dead node is decided in this order: a join-reaped candidate, the retry cone (with the
  probe's words in the evidence), its probe, and otherwise **failed**. Probes
  (`runner._POSTCONDITION_PROBES`): `merge`, `worktree` (what the dead agent left),
  `human_receipt` (Dan's recorded answer) and `async_job` (the awaited artifact; it never
  answers "not done", so a dead launch is never replayed).
- A controller lost while asking Dan resumes the same attempt, so he is asked once.
- Accepted as built: a dead gate worker goes down the verification retry cone (the implementer
  is redone, bounded by the F6 budget); a dead `accept` or `candidate_select` fails its run
  with the notice rather than re-running; a lease-less worker that dies **before it binds a
  pid** (attempt still `claimed`) is still not noticed.

**Operator verbs for one graph node: `maestro ctl retry|cancel <task_id> [<node_id>]`.**

- The CLI is an external actor: it checks the request (`command_inbox.check_graph_request`,
  read-only), enqueues `graph_retry` / `graph_cancel`, and the graph loop applies it on its
  next poll after checking again. A refused request queues nothing, names the nodes that can
  be stopped now and exits 1.
- A node must be claimed, running or uncertain in a running run. A run is cancelled only while
  it runs. A task is retried only once its run failed or was canceled; a live run must name a
  node.
- `cancel <task> <node>` fails the node (`operator_canceled`) and the run takes its failure
  path (`diagnose`, then the notice). `cancel <task>` stops every live node, finishes the run
  `canceled`, releases its trees and drops the runner: no failure edge runs and Dan gets no
  failed-run notice.
- `retry <task> <node>` queues a fresh attempt (`operator_retry`) that spends no quality budget
  and restarts a writer from the stopped attempt's `start_sha`. `retry <task>` is the Retry
  button's reopen marker.
- Stopping a node kills its tmux window, signals the pid the attempt bound (SIGTERM, a bounded
  grace, SIGKILL, a liveness check) and releases the lease by its epoch. It never revokes the
  lease and never signals the controller's own pid. A writer that survives SIGKILL refuses the
  verb before anything is recorded. An operator's cancel records no routing outcome.

**The loop consumes the command inbox** (`maestro/command_inbox.py`).

- Both loops, and the paused-by-user wait, drain once per poll after `poll_control_commands`.
  Each `received` command is claimed, applied by its handler and settled `committed` or
  `rejected` with the reason; the journal has `command_applied` / `command_rejected`.
- A type with no handler is rejected by name. A handler that raises rejects only its own
  command. `telegram_control` rows are left to `hitl.commands`. A `ctl` verb waits,
  `received`, for the graph loop: the paused-by-user wait has no handler for it.
- A command a dead controller left `processing` is settled by the next drain: a state command
  is applied again (those handlers are safe to repeat), an operator verb is rejected and
  journaled, never replayed.
- `clear_pause` clears only the pause the request names, and only once the controller's own
  clock says it expired. `clear_blocked_on` drops only answered requests. `reconcile_leases`
  runs the recovery scan with the attempt inboxes, so a receipt on disk is still ingested.
- A settled command is never re-opened by its key: the watchdog chains a new key to an applied
  lease-reconciliation request, and enqueues nothing while one waits.

**Children started in a tmux window run the launcher's checkout.** `tmux new-window` gives a
window the tmux server's environment, so the controller's `PYTHONPATH` never reached it and a
plain `python -m maestro.x` imported the venv's editable install (the live repository). `/ask`,
`/redo`, selffix **and every graph worker** now start through `maestro/pinned.py`
(`bootstrap.pinned_module_argv`): run as a file, stdlib only, it puts its own checkout first on
`sys.path` and runs the named module as `__main__`. No environment variable is set, so nothing
leaks to grandchildren or to a project's gate. Direct `subprocess` children of the controller
inherit its environment and are unchanged.

**The release self-test is isolated and retried once.** `self_test` gives its child a fresh,
empty `MAESTRO_HOME` (a temporary directory, removed afterwards), and runs the suite with
`-n auto` when pytest-xdist is installed (a serial run took about eleven minutes per release on
the 4-core host). `release` runs a red head once more, serially, before holding it and prints
the first red to stderr (the hook's `release.log`).
`maybe_self_update`'s own legacy self-test has no retry. Accepted: the suite's slow tier still
shares the `agents` tmux session with live loops; the retry is the mitigation.

**A pilot records its idle adoptions** (G7 item 7). See `pilot.manifest` in §17.1. A pilot is
not pinned.

**The risky-edit gate holds across a repair and on a free route** (D10 edges 2-4).

- The reviewer's strength floor reads the run's repair lineage (`WorkflowRepaired` events
  walked back from this run), each run by its own revision's writers. A writer a plan repair
  carried forward has no attempt in the successor run and used to leave the floor absent.
  An earlier run of the task outside that lineage counts while the tree still holds its
  commits: a failed or canceled run keeps `impl/<task>`, and a fresh run (Retry,
  `ctl retry <task>`) builds on it.
- A writer attempt records where its work ended (`attempts.end_sha`, written where the runner
  ends it: result ingest, a held attempt, a dead writer, an operator stop, a launch that
  failed), unless it leaves the tree with uncommitted changes. A retry that restores the tree
  to its predecessor's start (the one thing that discards a writer's work) sets that
  predecessor's `end_sha` to its `start_sha`: an end equal to the start says the attempt left
  nothing, committed or not. A succeeded attempt of the run's lineage always counts toward
  the floor, and one that ended otherwise (it stopped to ask Dan, was blocked or was killed)
  counts unless its row says it left nothing: either keeps what it wrote, and a later attempt
  may commit its uncommitted edits (`git add -A`) or amend its commits into its own, so the
  commits the tree holds do not decide it.
- An earlier run's attempt (`_kept_earlier_writers`) that did not succeed and left nothing
  never counts. Another counts when `start_sha..end_sha` holds a commit of the reviewed range
  (the run's base to the reviewed HEAD). One with no `end_sha` counts when this lineage's
  first writer was handed a tree with uncommitted changes (`TaskTreePrepared`: a settled run
  keeps such a tree, and the next run's writer commits what is there). Any other is placed by
  where it started: the tree was kept when the reviewed tree's first writer started ahead of
  the run's base, and the attempt then counts unless it started where that writer did.
  Ancestry decides no more than that: an amend removes the commit an attempt started from,
  and a rebase onto a moved target puts it on the base's own history, where a branch reset
  and built on again also leaves it. A reset branch is ahead of nothing, and nothing counts.
  Git failing to answer refuses the review. Accepted: these rules can raise the floor above
  what is needed (a row from before `end_sha`, a writer a plan repair killed, an earlier run
  before a reset once the branch has been built on again, a launch that failed onto a tree
  with uncommitted changes, an attempt whose uncommitted edits a later attempt's restore
  removed), and a retry that reproduces a stopped attempt's exact commit id counts both
  attempts; deleting the task's branch and tree clears them. Not seen: work a writer put
  where `git status` does not show it (a stash another attempt pops), and, for a run already
  going when this was adopted, uncommitted changes it was handed (its `TaskTreePrepared`
  event is written at its next writer claim and describes the tree then).
- A model-endpoint reviewer is told to write `review.json` with its `write_file` tool, and the
  worker judges the result with `handlers.adopt_review`. No verdict fails closed; a rejection
  sends its findings back to the implementer. Before, any answer on a free route counted as a
  passed review. That tool writes into the attempt inbox, so a reader is refused the inbox's
  protocol files (`handlers.INBOX_PROTOCOL_FILES`: envelope, heartbeat, receipt, result),
  their `.tmp` siblings and anything beneath them (`ToolBox.reserved`). The write is a
  confinement breach and the attempt fails; otherwise a reviewer could write its own
  `result.json` and `receipt.json` and be ingested as approved without `adopt_review`.
- The merge guard scans only **added** lines, and a glob secret is matched as a glob. Both
  differ from legacy on purpose.
- A deny-list or `risky_set` glob with a directory part (`secrets/*`, `src/pay/*`) is matched
  against the whole path and every trailing part of it, so it names that directory at any depth
  and nothing else (`confinement._denied_by`). It used to be reduced to its last segment, which
  named every file. `**/` is zero or more directories wherever it stands, and `/secrets/*`,
  `./secrets/*` and `secrets/*/` name what `secrets/*` names. `*` crosses `/` (`fnmatch`), so
  `**/secret*` also denies `secretary/notes.md`: a glob may deny more than a strict reading,
  never less. Plain substrings behave as before.
- Open: a model-endpoint **diagnoser** is not told about `plan_repair.json`, so its run parks
  as "diagnoser returned no proposal". It fails closed; the cost is liveness on a free route.

**A shared model policy newer than this reader does not stop the loop.** Adoption reads the
shared `current` pointer; routing reads only the adopted revision. A `current` this reader
cannot read (an unknown schema, a damaged revision file, a pointer that is not a revision) is
not adopted: `adopt` returns `deferred`, the controller journals `model_policy_unreadable` once
per revision, and the project keeps running on what it adopted until idle self-update brings
the reader. An unreadable **adopted** revision still fails closed ("refusing to use a moving
default").

---

## 17. Config layers, strength-only roles and the route model (P13 W1)

Decided by Dan on 2026-10-10 (P13 spec §1.1, B2-B4, B9). W4 (agy) and W5 (pace routing) build
on this section; change it here before changing the code.

### 17.1 Three config layers

| Layer | File | Holds |
|---|---|---|
| Global | `~/.maestro/config.yaml` (`$MAESTRO_GLOBAL_CONFIG` overrides the path) | every default: `roles`, `budget`, `defaults` (incl. `priority`), `runaway`, `sprint`, `telegram`, `thresholds` |
| Project | `<repo>/project.yaml` | only what differs, plus the keys that are project facts (`project`, `docs`, `risky_set`, ...) |
| Task | the ROADMAP task | per-task overrides, applied last. Today that is the task's `model:` field alone, read by the implementer; it is not part of `effective_config` |

- Merge rule: mappings merge key by key, recursively. A scalar or a list in a higher layer
  replaces the lower value whole (lists are never concatenated). `null` in a higher layer is
  "absent", as it is everywhere else in `maestro.config`.
- `maestro.config.effective_config(repo)` returns the merged document and, per leaf key, the
  layer it came from (`global` or `project`). `maestro config show --effective` prints both.
- `load_project_yaml()` returns the merged document, so existing readers see global defaults
  without a change at each call site. With no global file the result is what it always was.
- Code defaults stay where each reader has them (for example `RETENTION_DEFAULTS`).
- `doctor` fails on a top-level key neither layer knows, in either file. It is also the only
  place that reports a global file that does not parse: at runtime such a file reads as no
  global layer.
- Auto-slim is built and live in W1 slice 5 (`0f0d2f9`, 2026-10-12): at authoritative
  idle adoption, including an up-to-date idle pass, a project key equal to the global value
  is dropped. Project facts and safety lists stay local. Controller ownership and the
  shared config lock protect the change; ambiguous YAML and dirty config defer. Comments
  and effective values are preserved, with type-sensitive equivalence checked before a
  project.yaml-only commit. A tagged notice is retried from the commit marker until its
  API-confirmed local receipt exists. Delivery remains at-least-once across a crash between
  sending and receipt. Both real projects removed eight leaves with zero effective changes;
  evidence: `handoffs/2026-10-12-p13-w1-slice5-complete.md`.
- `pilot: {manifest: <path>}` (P13 W8, G7 item 7) is a project fact: read from the project's
  own file only, relative to the project root, kept by the slim and known to `doctor`. A
  project that declares it gets one amendment appended to that JSON manifest at every
  adoption (`kind: code_under_test`, `maestro_from`, `maestro_to`, `adopted_by`,
  `recorded_by: maestro`; `maestro_from` is `null` on a first run), written atomically and
  journaled `pilot_manifest_amended`. A manifest with no `amendments` yet gets the list.
  The adoption is never blocked: a manifest that is missing, is not a JSON object, or
  whose `amendments` is not a list is left alone and journaled
  `pilot_manifest_not_amended`. The itv pilot (B17) should declare it.
- Secrets are not config: the bot token is in `~/.maestro/.env`, never in either YAML file.

### 17.2 Role = `{strength, independent_of}`

```yaml
roles:
  implementer: {strength: balanced}
  reviewer:    {strength: strong, independent_of: [implementer]}
  diagnoser:   {strength: strong}
  planner:     {strength: strong}
```

- Role names are the graph's role ids: `implementer`, `reviewer`, `planner`, `diagnoser`,
  `test_designer`. The legacy loop's `judge` reads `reviewer`
  (`model_policy.LEGACY_ROLE_NAMES`).
- `strength` raises the policy's numerical floor through `CLASS_FLOORS`:
  `light: 0`, `balanced: 40`, `strong: 48`. It never lowers a policy requirement.
- `independent_of` names roles whose model family this role should not share (soft, B19).
  It is accepted and validated; nothing routes by it until W4 gives a model its family.
- A role has **no** `effort`, `backend`, `model`, `models` map or fallback list; `fallback_chain`
  is gone. `doctor` fails on any of them. Fallback is the router's job.
- The role templates' `min_reasoning_strength` becomes the shipped default of `strength`;
  `effort_policy` is removed. A project's own role template that still states it is refused
  with that sentence.
- The policy's role table is separate from project configuration. Under schema 2 each
  role has `{min_intelligence, effort, effort_rule}`:

  | Role | Minimum intelligence | Effort | Rule |
  |---|---:|---|---|
  | implementer | 40 | medium | preference |
  | test_designer | 40 | medium | preference |
  | reviewer | 48 | medium | preference |
  | planner | 50 | high | minimum |
  | diagnoser | 50 | high | minimum |

- `minimum` excludes efforts below the named level; `preference` leaves every sufficiently
  capable effort eligible and applies the near-cost rule in 17.5. `xhigh` and `max` are
  ordinary levels above `high`, where the harness actually exposes them.
- The graph takes the policy requirements and configured strengths, retaining its role
  template floor when config supplies none. `derived_roles` uses the same router for
  each harness's model/effort column and for the neutral default head. Legacy launchers
  deliver that selected effort through `EffortControls`, including switch/rotation
  launches and Codex's fresh fallback. Switching uses the task's frozen model inputs.
  An adopted policy never falls back to a hardcoded model. Manual task pins keep their
  existing verified launch seam: a model-only override outside automatic eligibility
  is checked against native/lifecycle/retirement facts and requests no effort (the
  harness default), without inventing a scored pair. An eligible pinned pair delivers
  its chosen effort. A project role pin cannot create policy eligibility.
- A task freezes the merged `roles` (global under project) when it starts and keeps them
  until it ends. A record frozen before this section holds the old shape and runs at the
  shipped strengths.

### 17.3 Route = `(harness, model, effort)`

The shipped inventory is `maestro/model_pairs.json`, Dan's 47 initial pair estimates.
CLI delivery was verified on 2026-10-11: Claude 2.1.296 help lists low, medium,
high, xhigh, max; Codex 0.162.1's cache lists them for Luna and Astra. Sol lacks
its own cache entry, so each level was verified with one ephemeral read-only
`codex exec -m gpt-6.1-sol -c model_reasoning_effort=<level>` call. All five exited
0 and reported the requested effort. No level was substituted; ultra is not in
Dan's table. AGY's named-effort pairs remain inert until W4.

A route is one **pair** of a model and an effort on one harness. The pair, not the model,
carries the routing judgement:

| Per pair (policy) | Per model (catalog) | Per harness (register) |
|---|---|---|
| `intelligence`, derived class, `relative_cost`, approval | context limits, quota pool, native verification facts (family integration waits for W4) | `EffortControls`, version, sandbox, telemetry |

- Policy schema 2 (reader version 5): a `routes` row is
  `{backend_id, model_id, effort, intelligence, relative_cost}`, unique on the first three,
  with optional boolean `provisional`. Intelligence is an integer 0–100 (not a boolean);
  class is derived by `class_of`, never stored. Effort names use the five-level ladder
  `low, medium, high, xhigh, max`; `null` represents a harness with no dial.
  Policy `roles` uses the requirement shape in 17.2. `retired` stays `[backend_id, model_id]`:
  a retired model has no pair at any effort. A schema 1 policy stays readable (17.5).
- Under schema 2 the catalog is given the rows as explicit pairs, and a model with no row is
  not served. Project role pins cannot re-admit a model the policy dropped under either
  schema. `replace_route`, `retire_routes` and `rollback_policy` handle every effort of
  a model together; `assign_roles` refuses schema 2 because roles no longer name models.
- Catalog: `ModelProfile` keeps what is true of the model at any effort (limits, pool,
  `native_context_verified`). The class and cost are a `PairProfile` on the entry, keyed
  `(model_id, effort)` (`pair_profiles`). `CapabilityCatalog.routes()` yields
  `(entry, model_id, effort)`; `models()` yields `(entry, model_id)` for per-model facts.
  `ModelProfile` has no strength or cost; schema 1 rows build historical pairs directly.
- Delivery stays `EffortControls` (INV-A07). A pair is a route only when
  `entry.effort_controls.resolve(effort)` returns a native or mapped value. A pair whose effort
  cannot be delivered is not a route, whatever the policy says
  (`BackendCapabilityEntry.delivers`, `undeliverable_pairs`).
- A model with no approved pair has no route: there is no default grade for a model the
  catalog only lists.
- A harness with no effort dial (agy, where the effort is in the model id such as
  `gemini-3.8-flash-high`) routes the pair that names **no** effort, `(served id, None)`, and
  nothing is sent as `--effort`. On a harness that has a dial, a pair with no effort is not a
  route. Parsing an effort out of the id, and the canonical family shared with another
  harness (B8), are W4's work; the per-model column above is where the family goes.
- `relative_cost` estimates subscription quota per attempt, not API token prices. Each
  quota pool has its own baseline; raw costs are never compared across pools. Initial
  estimates remain provisional, especially AGY; no new measurement system is built here.
  The same 47-row inventory builds shipped and published policy. AGY remains inactive
  until W4 verifies its delivery, family and pool mapping.

### 17.4 Approval (B4)

- Built and rolled out in W1 slice 3 Task 9 (`15e0f01`, 2026-10-11). The modelctl approval flow asks for
  intelligence (integer 0–100) and positive relative cost for each useful effort of a
  new class. Dan can accept, edit or decline each pair. Only approved pairs enter the
  policy; a model with no approved pair has no route. Schema 2 skips the old model-to-role
  assignment step and reports role requirements and separate effort rows.
- The daily modelctl scanner imports Maestro from its checkout, upgraded by Task 9.
  The live scanner uses pair approval. Owner model-wide grades remain compatibility
  inputs to the existing lifecycle path, not schema 2 routing judgments.
- `~/.maestro/models/asks/telegram.enabled` is the explicit transport rollout switch;
  without it asks are held as `send_pending`. Task 9's combined approval covered the
  new pair-approval messages that future scans can produce with upgraded checkout
  code and live policy. No real asks are sent during isolated implementation or review.
- Telemetry may later suggest a regrade, with its evidence. It never regrades by itself.

### 17.5 Resolution until W5

W1 slice 3 gathers candidates globally. There is no fixed harness priority.

1. Filter approved, deliverable pairs by billing, context, modalities, tools, permissions,
   task restrictions and intelligence. Demand is the maximum of risk/class demand,
   template/config floor and policy minimum. High risk, architectural/security work or
   failure history of at least 50% raises the floor to 48 only under schema 2; effort
   still follows the role rule. Planner/diagnoser keep their high minimum. The existing
   failure ladder maps strength step-downs through the class floors.
2. Group eligible candidates by quota pool. Inside a pool, a preference role's near-cost
   band is `cost <= cheapest * (1 + routing.near_cost_margin)` (default 0.20, validated
   in 0..1). Band pairs come first. Prefer the role's effort within that band unless
   another band pair's measured success rate is at least 0.10 higher, with both rates
   past `OutcomeStatistics`' evidence floor. Then order by cost, descending intelligence,
   model id and effort. Outside the band order by cost, descending measured success,
   descending intelligence, model id and effort. Minimum roles have no preference band.
   For example, Claude implementer chooses Sonnet medium (0.50 inside Opus low's 0.42
   band); Codex chooses Sol low (0.62, with Sol medium 1.00 outside its band).
3. Rank pools by descending weekly pace delta, then `sha256(node_id + "/" + pool_id)`.
   This hash avoids alphabetic harness priority. Concatenate each pool's ordered pairs;
   existing admission walks the list, falling through saturated/paused pools. If nothing
   is eligible, block explicitly; if only capacity prevents launch, wait explicitly.
4. A resumed session keeps its harness through the existing session decision.

Pace uses QuotaPulse's formula (`~/projects/QuotaPulse/bridge/bridge.py`, `compute_pace`):
`elapsed_percent = round(clamp((now - (resets_at - 7d)) / 7d * 100, 0, 100), 1)`;
`pace_delta = round(elapsed_percent - used_pct, 1)`. Positive means unused headroom
relative to pace. Only a fresh weekly (10080-minute) window counts; missing, stale or
unusable observations are neutral (0.0). The controller pulls Maestro's native usage
readers at most once per driver per 300 seconds. Errors keep the prior observation,
which ages out after its 900-second freshness bound. Resolution itself makes no read.
QuotaPulse bridge ingestion/reset signals wait for W3; the 5h gate, budget, evidence
guard, sprint and `objective: pace` wait for W5. Existing exhaustion/capacity safeguards
remain. `doctor` reports the retired harness-priority config key.

W5 implementation in progress: `objective: pace` now adds a current estimated
five-hour check and a measured class-success guard before weekly ranking. Applicable
Claude Opus scope constraints remain separate; missing scope is unknown, and its
exhaustion cannot disable unscoped models. Pace waits preserve the quality floor
even when legacy `fallback: step_down` is configured. Initial per-pool burn/runtime
estimates come from `routing.pace`, with task overrides last. Five eligible completed
exact-shape runtimes can replace the initial runtime; eligible nonoverlapping pool
observations can supply an explicitly estimated burn using recorded prices. Coverage
and fallback remain visible. Verified quality is filtered before own-pool recorded
token cost; explicit canonical equivalents may share quality, while each harness
retains its own pool costs. Both graph factories apply effective global/project/task
`runaway.mode` to the separate nullable planner-band reader. Off uses class-only
evidence; on/shadow require matching validated integer bands and leave unavailable
forecasts unmeasured. No W13 producer or A07 shape change is included.

Replay records the actual quality source, counts, shape inputs, evidence floor and
known attempt identities. Aggregate statistics fallback has null attempt identities
and cannot supply costs. Real pace resolutions reread both detailed rows and the
latest authoritative aggregate verdicts, including amendments. History remains
lossless with no last-N cap or stale verdict cache; retained data grows linearly,
and the burn overlap scan can be quadratic per candidate. No hard history bound
is claimed. B7 now atomically reserves the selected route's estimated five-hour
percentage charge in the existing host ledger before claim, with effective project
priority shares, waiting aging and starvation protection. Existing runners read
current effective project priority for each new budget reservation, so a committed
`/priority` change applies to their next admission without restart. An invalid
current weight is refused rather than replaced with a cached weight. Already
admitted bindings, held charges and historical decisions remain frozen; other
routing objectives do not read this budget-priority input. Burn is applied once;
weekly/scoped hard gates remain separate and no raw costs cross pools. Frozen
admitted holds are restored from attempt bindings on restart and released by the
existing terminal/refused-claim paths, with same-tick controller-error cleanup.
The original charge and full five-hour/runtime projection stay immutable. A fresh
reset deadline advancing by more than the existing 600-second jitter tolerance
attributes remaining estimated exposure to the new window, including early resets,
under that same admission lock. Same-window jitter never decays a live hold; a
runtime overrun holds its full frozen cost, and legacy holds keep their known
charge. Recovery and release follow the attempt even after its routing objective
changes. Version 2 pace decisions now freeze each actual stage's inputs/results;
the atomic host transaction supplies budget admission/refusal evidence. The runner
mandatorily journals admitted and waiting decisions in the existing event store
before claim, and admitted attempts retain the same decision through lifecycle's
binding writer. A failed journal write waits without creating an attempt and
reconciliation releases its unused hold. A recorded admission proves budget, not
claim or worker launch. The ledger-file/SQLite crash interval is not a cross-store
transaction; no worker can launch before the event commits. Old names-only records
remain historical. Invalid nonfinite snapshot numbers carry explicit unavailable
markers for standard JSON, preserving actual unknown quota semantics. Decision
storage has no size bound. The existing graph quota refresh now persists typed
sprint decisions through the controller's atomic state writer. On-by-default
`sprint` config supplies a 24-hour reset horizon, 5-point behind-pace margin and
70/high forecast bar. Current weekly, announced Codex, forecast Codex and available
banked-credit signals remain distinct; stale/future/malformed/history-only inputs
cannot create a current signal. Forecasts have no reset deadline; banked decisions
ask for operator activation and provide no acquisition or activation API.
`/sprint on|off|stop|status` uses the existing durable Telegram command mailbox.
Known weekly identities retain off/stop over restart and small deadline jitter;
unknown pools bind their individual skip/stop to the first observed week and
re-arm on the next observed reset. Claimed sprint commands recover with frozen
week fields and project/bot/chat receipts; unrelated verbs keep their fail-closed
contract. Start notices explain the actual weekly/announced/forecast/banked reason.
Their Skip-this-week button controls only the exact delivered project's pool,
message, week and episode; `/sprint off` controls the whole project. The listener
only acknowledges the spinner, while the controller applies the inbox command.
Stale/foreign/replayed buttons cannot suppress a later week; lost external delivery
receipts may cause a duplicate notice, but only its bound message has control.
Real pace resolution now reads current controller sprint decisions and ranks an
active pool by its weekly target minus observed weekly use. Inactive or unusable
targets retain ordinary valid weekly pace; malformed/expired weekly evidence is
neutral. Immediate off/stop/Skip-this-week invalidation takes effect before quota
refresh, and an already admitted attempt keeps its frozen route. The weekly journal
freezes actual sprint inputs, observed and effective pool scores, and applied targets;
quota observations, five-hour limits, quality and atomic shared budget stay separate.
Current own-pool sprint decisions also stage eligible review follow-ups before
queue exhaustion and promote solely budget-waiting runs within the existing loop.
Source acceptance, holds, parked/operator waits, prerequisites, goal/replan gates
and other owners' mutexes still apply after staging, including merged duplicate
provenance. Current source requirements override saved metadata; graduated sources
retain their saved requirements. Counted maxima and mutex unions preserve sequential
work's minima. Compiled nodes carry task minima, and the same host admission round
and ledger recheck stricter current polish requirements. An oversized request keeps
the existing failed audit attempt without launching a worker or holding capacity.
Already-open polish peers participate with their effective current mutex labels.
The existing admission round selects one owner, excluding its labels from its own
retry while holding off peers. Selected ownership is saved in the existing
`graph_runs` rows before dispatch and restored after restart, including the idle
interval after an operator retry. A settled or canceled owner releases the mutex
for a waiting peer on the next poll. No additional resource or dispatch authority
is introduced.
Compatible selected idle owners retain retry priority. Incompatible idle inherited
partial allocations are reconciled as whole available bundles in stable run age
and identity order, so converging requirements cannot retain x=A/y=B while both
need {x,y}. Active attempts and raw/frozen owners remain protected; a blocked idle
contender receives no new partial bundle. Synthetic live/restart tests cover these
boundaries and cancellation followed by peer progress. They do not establish
starvation freedom under arbitrary source edits or progress past a live protected
owner or a closed admission gate.
Only a complete ready frontier of actual selected-pool budget refusals, without
active attempts or other waits, receives sprint ordering. Explicit task priority
remains; controls, quality, five-hour and atomic budget gates are rechecked normally.
An unusable ROADMAP graph disables sprint reordering and journals the cause while
ordinary eligibility and admission continue. The persisted
100% target is a weekly sprint target, never a five-hour quota grant. Forecast
episodes retain inputs and a nullable matching observed rollover; absence stays
unknown. This is synthetic bounded evidence, not a live pilot. See
`docs/W5_PACE_ROUTING.md` for the current contract and proof limits.

Migration: a schema 1 row `(backend, model)` carries one class and cost and no effort. An
entry built from such rows (`pairs_migrated=True`) reads each graded model
at `medium`, at `high` and at no effort, all with the old class and cost; the harness then
keeps the ones it can deliver. The router binds a role at the effort it used before
(`catalog.MIGRATION_ROLE_EFFORTS`: `high` for planner and diagnoser, `medium` otherwise), and
under the free objective at the highest. A no-effort pair is never filtered, and a harness
that cannot deliver the role's effort keeps the efforts it does deliver. This keeps
routing for frozen tasks and old adopted revisions even after schema 2 publication; current
schema 2 requirements do not replace historical demand/effort rules. Explicit pairs are never
filtered by the migration effort.

Kept from the old router for a migrated entry (Dan, 2026-10-10): a task whose demand was
raised to `strong` by high risk, by an `architectural` or `security` class, or by a failure
rate of 50% or more binds at `high` whatever its role. A `strong` role floor alone does not,
and explicit schema 2 pairs are never filtered this way: schema 2 raises intelligence only.

Not carried over from the old router: the demand no longer names an effort, so low risk no
longer asks for `low`. Risk and failure rate still move the strength floor.

Dan's §8 A–G are future, not built: actual subscription-cost calibration; cost per
successful task including retries; role-specific intelligence; adaptive effort; better
cross-pool optimization using measured costs and reset horizons; latency/context-aware
routing; benchmark calibration against Maestro outcomes. This slice adds no telemetry,
learning, latency optimizer or Context Gate. Context Gate remains an independent track.

### 17.6 One bot (B9)

W1 slice 4's shared transport is released and live (`d9e0a5e`, 2026-10-12), after
Dan's combined rollout approval; the completion handoff records the live verification.

One Maestro bot uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` from
`~/.maestro/.env` (mode 600). These credentials override stale project or inherited values,
including subprocess sends. Every outgoing message/edit and attachment caption starts with
`[project]`, using `project.name` (repository basename when absent).

- `maestro telegram register --repo <path>` registers delivery independently of model-policy
  adoption; run/watchdog startup and configured `init` also register. Project names must be unique.
- `maestro telegram poll` is the sole `getUpdates` consumer. An exclusive advisory lock
  refuses a second poller. The operator runs it in a named `agents` tmux window.
- `$MAESTRO_HOME/telegram` holds registration records, a persisted update offset and atomic,
  fsynced per-project mailbox deliveries. The network offset advances only after durable
  delivery or a successful refusal notice. Controllers and halted watchdogs read local files;
  controller settlement consumes delivery. Restart/redelivery retains update-id idempotency.
- Address a command with its project first: `/status itv`, `/priority [itv] 2`,
  `/approve itv T1`. Reply context can identify the project. Omission works with one registered
  project; with several, an unaddressed command is refused with the available names.
  Unknown bracketed selectors, duplicate names and conflicting explicit/reply targets fail closed.
  Quoted messages must originate from the shared bot; forged tags and old-bot replies are refused.
- Buttons and free-text replies resolve the actual request message ID, never a request ID alone.
  With several pending requests, reply to the exact message; an unmatched/answered reply cannot
  answer a different pending request. The local listener acknowledges buttons and answers help
  every 0.5 seconds; the controller owns state writes. The halted watchdog answers read-only
  controls and leaves mutating commands in the mailbox for controller restart.
- `maestro.hitl.commands.COMMANDS` declares metadata and callables for actual dispatch,
  help and all aliases, including intake. `maestro telegram commands --botfather` exports
  one matching line per registered command; `maestro telegram commands` prints addressing/help.

### 17.7 Project priority (B15)

`priority:` overrides effective `defaults.priority`, which defaults to 1 (normal). Only a
positive finite numeric value is valid; bool, zero, negative, malformed and nonfinite values
are refused, including by the required `doctor` priority check. The canonical setup skill asks once, uses a recorded answer, and inherits the
default if skipped. `/priority [project] <weight>` atomically rewrites the YAML scalar,
preserving comments, and commits only `project.yaml`, leaving unrelated staged work alone.
It refuses an already dirty or untracked project config, and validates the rewritten
YAML before writing so anchors/aliases cannot leave an invalid committed config.
Use a literal numeric priority when an alias would be invalidated. W5's current
shared budget admission consumes this effective project weight; its coordination
reads the current value for each new reservation by an existing runner, preserving
already admitted holds. Its remaining proof limits are documented in
`W5_PACE_ROUTING.md`. Auto-slim is live
as described in §17.1; Context Gate remains unbuilt.

## 18. Telemetry, complexity and fault reporting (P13 W2)

W2's isolated source `89e0817` captures nullable, append-only telemetry without changing
node verdicts, workflow counters or launch/session decisions. It shares the controller's
event transaction behind diagnostic savepoints; no new writer or schema migration.
The reviewer supplies nullable complexity 1–5 using one fixed files/reach/behavior/risk
rubric. Diff statistics use the run's immutable base and claim's reviewed commit;
invalid score, manifest or Git evidence stays flagged and cannot fail the run.

One revisable cause-to-fault table extends the seven review origins. Agents supply
observations, not blame. Every spending extra attempt has one logical allocation;
append-only amendments retain per-cause role/admission context, operator rulings,
actual reset triggers and judged repair links through restart and callback replay.
Missing, malformed or contrary evidence remains flagged unclassified. Waiting without
a claim spends nothing. Typed failures and normal ROADMAP/staged execution preserve
only observed causes; arbitrary command output/status cannot invent special blame.

`maestro report usage --database <absolute-db-path> [--task <task-id>]` opens an explicit
read-only snapshot without controller ownership, writes, migration or current repricing.
It reports task counts and task/fault/role/route token splits, wall time, complexity/diff
and extra-step evidence with measured coverage. Prices come from immutable claim-time
bindings; costs aggregate only inside quota pools. Quota deltas remain per-window
account observations, never attributed or summed consumption. Legacy missing telemetry
stays unavailable. The original saved P12 replay and its historical limits are preserved.

`W2_USAGE_REPORT.md` describes the contracts; `agent-plans/p13-w2-proof-inventory.md`
maps requirements to real graph/runtime and report/CLI tests. Source frozen/clean
qualification and independent review closures are in `agent-plans/p13-w2-completion.md`;
the final docs-only SHA/gates and full close-out are in root
`handoffs/2026-10-15-p13-w2-complete.md`. W5 admission remains unknown. W6/W11/W13
capture APIs implement no future product behavior or planner forecasts. There is no
live W2 pilot, root integration, release, push or Context Gate in this workstream.

## 19. Goals contract and outer acceptance tests (P13 W11, goal-driven TDD)

Normative requirements: `docs/agent-plans/p13-goal-driven-tdd/01`–`03` (R01–R70, Dan
2026-10-17); status per requirement: `TRACEABILITY.md` there. This section describes what is
built; later items (briefs, review statuses, conflicts, checkpoints, progress state, the
W11b completion gate) add their parts when they land.

### 19.1 The contract (`docs/GOALS.md`, `maestro/docs/goals.py`)

Format 2 (`Format: 2`, `Contract revision: N`) holds goals `G`, scenarios `S-`,
acceptance criteria `AC-` (Given/When/Then, oracle, threshold, layer, evidence, fixtures,
bound `Test:`), operator checks `OP-`, hard invariants `INV-`, bounded trade-offs `TO-`,
priority, authority, change history and retired ids (field list:
`GDT-1-FORMAT.md`). A file with no `Format:` line is the W11a six-section format 1; it still
parses, gets one note pointing at `maestro goals migrate` (prints a v2 skeleton, never
writes, invents nothing), and is never gate-failed for v2 parts it lacks.

Every problem has a severity and the ids it affects: `note` (guidance, e.g. goal count),
`draft` (shown while drafting) and `gate` (blocks the affected work at execution). Ids are
stable: a duplicate, a retired id reused, an unknown reference or an orphan goal, scenario
or criterion is `gate`; a missing target or oracle and every open question are `gate` but
scoped to the ids they name. A changed target, verification, oracle or threshold without a
change-history row for the current revision is `gate`, and `goals.changes(old, new)` names
what turned stale.

### 19.2 Draft view and execution gate (GDT-2)

The same contract is read two ways. `goals.diagnose(doc, tasks, completed)` is the drafting
view: every non-note problem, every open task's linking problems, and coverage (a goal no
task serves; in format 2 a criterion with no bound test, a goal nothing runnable verifies,
and a criterion whose Given/When/Then/Oracle names a source path or a `name()` call, which
should state observable behaviour unless Dan approved that detail).

`goals.task_gate(doc, task)` is the execution gate for one ROADMAP task. A task links to the
goals in its `serves:` and the `AC-`/`S-`/`OP-` ids in its `criteria:` plus those ids' goals.
It is held by its own linking problems (no `serves` key is unclassified and held;
`serves: []` is a deliberate answer and is not; an unknown goal or criterion is held) and by
every contract `gate` problem on an id it links to; a whole-contract one holds any task
that links to anything. Draft problems never hold a task. Both runners
(`roadmap.parse_runnable_tasks`, `parse_prep_tasks`) skip held tasks next to drift-blocked
ones, journal `goal_gate_hold` once per change, and on an internal error journal
`goal_gate_error` and hold nothing. `maestro doctor`'s goals check is a required FAIL naming
each held task and why, WARN when only drafts remain, else OK.

### 19.3 The acceptance baseline (GDT-3, `maestro/acceptance.py`)

Each criterion's `Test:` binds it to one command (`` `pytest tests/x.py::t` ``,
`` `make accept` ``, `` `python bench.py` ``) or to an existing test id
(`tests/x.py::t`). `maestro goals baseline [path] [--json] [--timeout N]` runs every bound
criterion once in the project, records every operator check, stores the result in
`.orchestrator/acceptance_baseline.json` with the contract revision, and exits 0 only when
every criterion is a credible RED or GREEN and no fixture or data path leaves the project.

| State | When |
|---|---|
| `red_expected_missing_behavior` | the harness ran and an assertion failed: the only credible RED |
| `green_already_satisfied` | it passed (or a metric is on the passing side of its threshold); kept as regression protection |
| `blocked_environment` | the harness, not the product: an import error, a missing tool (exit 127), nothing collected, a collection error, a fixture or service failing in setup, a measurement that did not run, an unsafe `Test:` |
| `operator_only` | an `OP-` check: recorded with its sign-off, never an automated pass |
| `unknown` | no test bound, a timeout, an exception that is neither an assertion nor the environment, a skip, a failing non-pytest command, a metric with no number, no threshold number or no direction |

pytest outcomes come from its own JUnit XML (`--junitxml`), never from console text. A
metric criterion compares the first standalone number on the last printed line with the
threshold's first standalone number (the `95` of `p95` is part of a name, never a value);
the direction comes from the threshold or oracle wording (`under`/`below`/`at most`/`no more
than`/`<`/`<=`, `over`/`above`/`at least`/`no less than`/`>`/`>=`), and only the inclusive
wordings (`at most`, `no more than`, `within`, `<=`, `≤` and their `above` twins) pass at the
threshold itself. Wording that names no direction is `unknown`, never a guess (C10).

Safety (R19, defence in depth: a bound test is project code and runs with the project's
rights anyway): the `Test:` is one argv command, never a shell (`;|&<>` and backticks or `$`
are refused, and so is `sh`/`bash`/`zsh`/`dash`/`ksh`/`fish` as the command); an interpreter
may not run inline code (`python -c`, `node -e`, `ruby -e`, `perl -e`; option clusters are
read as the interpreter reads them, so `python -X dev -c`, `python -cprint(1)` and
`ruby -I lib -e`, `node --eval=1` are refused while `python -mpytest -c pytest.ini`,
`ruby -W:no-deprecated t.rb` and `perl -d:Trace t.t` run, and a wrapper's own options and
durations, `timeout -s KILL 10s`, `env -u FOO`, are skipped to find the program, a long
option by any unique prefix as GNU reads it (`timeout --sig KILL`, `env --uns X`); `env -S`
and `--split-string` (`--split`, `--s`) split a command line like a shell and are refused);
every path-like
argument (one containing `/`) is resolved and refused when it leaves the project; the
environment is the caller's minus every secret-named variable (`*_KEY`, `*APIKEY*`,
`*TOKEN*`, `*SECRET*`, `*PASS*`, `*CREDENTIAL*`, `*AUTH*`, `*DSN*`, `*DATABASE_URL*`), with
the project's `.venv` first on `PATH`; every run has a timeout.

A stored result closes nothing at a newer contract revision: `acceptance.current(results,
doc)` keeps only results taken at the current revision, `acceptance.stale` lists the rest.

Known limits: there is no network sandbox, so a test that needs a service it cannot reach is
`blocked_environment` (the honest result); a failing non-pytest command is `unknown` because
a missing behaviour and a broken harness look alike from an exit code; a baseline from an
older revision is stale as a whole (no per-criterion carry-over for unchanged criteria yet).

### 19.4 ROADMAP contribution, proposal, feasibility (GDT-4, `maestro/goal_roadmap.py`)

A ROADMAP task names its goal work with these keys:

| Key | Meaning |
|---|---|
| `serves:` | the goals it serves (GDT-2; `[]` is a deliberate "none", no key is unclassified) |
| `criteria:` | the `S-`/`AC-`/`OP-` ids it should move (GDT-2) |
| `contribution:` | `direct`, `enabling` or `maintenance`: one value for every served goal, or a `{G1: direct, G2: enabling}` mapping |
| `tests:` | the test ids or commands it expects to affect (one string is one test) |
| `evidence:` / `acceptance_criteria:` | what shows the task is done |

What holds a task and what is only a draft (no false blockers, `01` §4):

- **gate** (holds it): a contribution that is none of the three, a mapping naming a goal the
  task does not serve, `serves: []` with `direct` or `enabling`, a `tests:` that is not a
  string or a list of strings.
- **draft** (format 2 only, shown by `goals check`/doctor, never holds): a served goal with no
  contribution (no key, or a mapping that skips it), a `direct` task naming no `criteria:`
  and no `tests:`, a task with no `evidence:` or `acceptance_criteria:`.

`goals.contribution_map(doc, tasks)` lists per goal the open tasks by contribution; a task
serving a goal without saying how is `unclassified`, never assumed `direct`, so a phase that
serves G1 does not make each child direct (C5).

`goal_roadmap.propose_tasks(doc, results, tasks)` turns the current-revision baseline into
ROADMAP task blocks, one set per unmet mandatory criterion that no open task already names in
`criteria:`: a `blocked_environment` or `unknown` result gets `<ac>-harness` (enabling) and
then `<ac>-<slug>` (direct, depending on the harness); a RED gets the direct task only; GREEN,
`OP-` and optional criteria get none. A criterion's goals are its own plus every goal listing
it (scenario goals only when neither names one). No current-revision baseline is a
`ValueError`. `maestro goals tasks [path] [--json]` prints the proposals and never writes
ROADMAP.

`goal_roadmap.feasibility(doc, tasks, results)` (format 2 only) is the plan check before
execution: with no current baseline it is one problem; otherwise every unmet mandatory
criterion needs an open task naming it in `criteria:` (a task that only serves the goal, or a
completed one, is not a path), and one whose test cannot run (`blocked_environment`,
`unknown`) also needs an open `enabling` task naming it. Doctor's goals check is a required
FAIL `blocks at execution: …; not feasible: …; also: <drafts>`. The runners do not read it:
feasibility is a setup/doctor gate, and the W11b completion gate (GDT-10) refuses done on any
unmet criterion at the end.

Known limit: feasibility reads the stored baseline, so after a direct task graduates and its
criterion turns GREEN in the code, doctor still shows a FAIL for it until `maestro goals
baseline` reruns; GDT-9 updates progress state from the gate's observations.

### 19.5 Role briefs (GDT-5)

`goals.excerpt(doc, serves, role=, task=, results=)` is each role's GOALS section, built by
`goal_progress.manifest_fields(repo, task, role=)` from `docs/GOALS.md` and the stored
baseline; the graph runner passes the node's `agent_definition_ref`, the legacy implementer
`implementer`. No role was added.

Critical lines, never cut: the purpose; the contract revision; the served goals in full
(format 2: outcome, rationale, baseline, target, verifiers, constraints, dependencies, open
questions) with their scenarios, criteria (Given/When/Then, oracle, threshold, bound
`test:` or "none bound yet", baseline state at the current revision or "none at revision N",
whether the task names it) and operator checks; the task's `tests:`; every hard invariant;
every trade-off with its decision, bound and expiry; the priority order; and the role's part:

| Role | Its part |
|---|---|
| `implementer`, `test_designer` | SCOPE (D23): goals never widen the task; extra goal work is reported for the planner, not built |
| `reviewer` | the expected contribution; task correctness judged first, goal impact separately from evidence; goal work the task did not ask for is a scope finding (GDT-6 routes it) |
| `planner` | every mandatory criterion not GREEN at this revision with its state and goals, the operator checks for completion, or "no acceptance baseline" |
| `diagnoser` | the authority policy (retries, when to stop an approach, what to ask Dan, blockers); a repair never changes a goal, criterion, bound test or invariant |
| `triage` | polish keeps every invariant and adds no goal work; its non-goals are critical |

Lower-priority lines fill what is left of `MAX_GOALS_TOKENS` (1000 tokens): the other goals
named in the priority order (one line each), the non-goals in their order, then goals no
priority names. A cut names what it dropped with its section (`the summaries of G4, G5
(docs/GOALS.md §Goals); non-goals 3-200 of 200 (docs/GOALS.md §Non-goals)`); when the
critical lines alone exceed the bound they are kept in full and the cut says so. The
criteria of goals a task does not serve reach only the planner. Format 1 keeps its excerpt
and gains the role's rule. Sizes for the `v2_GOALS.md` fixture, task serving G1: 418
characters before (no criteria, tests, invariants or baseline), 2045-2402 after by role.

### 19.6 Goal impact and the goals guard (GDT-6)

**Review** (`maestro/workflows/goal_progress.py`). The reviewer judges task correctness first;
goal impact is a separate `goal_progress` entry per goal, with an `impact` (`progress` is
read as an alias) and evidence items. The six impacts are `advanced`, `enabled`,
`unchanged_expected`, `no_progress_unexpected`, `regressed`, `unknown`; a stored W11a
`no_progress` reads back as `no_progress_legacy`, never as an unexpected miss, and a
reviewer writing `no_progress` today gets `unknown` plus a flag. Evidence kinds are `test`,
`metric`, `trace`, `operator`, `diff`. The rule, not the claim, decides, in this order:

1. a failing test of a criterion GREEN at the baseline → `regressed`, whatever was claimed;
2. `advanced` over any failing test → `unknown`;
3. `no_progress_unexpected` on an `enabling` or `maintenance` contribution →
   `unchanged_expected` (no direct progress was promised, so it needs no proof);
4. `advanced`, `regressed`, `no_progress_unexpected` need a test result, a complete
   measurement (number and baseline), a trace or an operator observation; a diff alone, a
   metric without a number or baseline, or a mocked test for a criterion whose layer is not
   `unit` is not proof → `unknown` with a targeted follow-up naming the evidence to get.
5. Evidence must point the claimed way (`regressed` is a verified degradation, `01` §6.2):
   for `regressed` a passed test shows no degradation, a failed test of a criterion that
   was not GREEN at the baseline is "still failing, not a regression", and a metric must be
   worse than its baseline in its criterion's direction; for `advanced` a metric must be
   better. A metric whose criterion names no direction proves neither (nor does any metric
   under a legacy-format contract, which has no criteria). The reviewer's
   `goals_check.criteria` carries each metric criterion's `direction` (`below`/`above`/
   `None`). A failed test of no criterion, a trace and an operator observation still count.

| Decided impact | Effect |
|---|---|
| `regressed` (any goal, `serves: []` included) | synthesized blocking finding; reworks |
| `no_progress_unexpected` (a served goal) | a shortfall in `disposition["goal_shortfalls"]`; merges (C17) |
| `unknown` | a follow-up in `disposition["follow_ups"]`, joined after the verdict so it never turns one (D16) |
| others | recorded |

Goal work the task did not ask for (`unasked_goal_work`) is W9's `out_of_scope` blocking
finding: the path is to be reverted, never added to the rework's scope (D23). Every approved
review's goal impact is telemetry's `GoalImpactObserved` with `attribution: unattributed`
and its provenance (run, node, attempt, evidence); no rule blames the planner (R58). W2's
`CAUSE_FAULT["task_did_not_serve_goal"]` reads `unattributed` (mapping version 2, GDT-12), and
an old row keeps its `captured_fault: planner`; attribution by evidence is §19.14's.

**Goals guard** (`maestro/workflows/goal_guard.py`), in the graph's verification gate beside
W9, deterministic. The controller freezes `goal_guard.policy` (base, revision, baseline
states, criteria to rerun, the files each `Test:` binds, the task's `test_corrections:`) and
passes it to the gate worker as `--goals-guard`:

- `tamper_findings`: the committed diff may not touch `docs/GOALS.md` (only Dan changes the
  contract, even with a correction), delete a bound file, modify one unless
  `test_corrections:` names every criterion it binds, add a skip/xfail/deselect to one or
  to a `conftest.py` above one, or change test selection in a pytest config at the root or
  above a bound path (`pytest.ini`/`tox.ini` `[pytest]`, `setup.cfg` `[tool:pytest]`,
  `pyproject.toml` `[tool.pytest]` and `[tool.pytest.ini_options]`, pytest 9's
  `pytest.toml`/`.pytest.toml` `[pytest]`, parsed on the base and on the task's tree: a
  changed `testpaths`, `norecursedirs`, `python_files`/`_classes`/`_functions` or
  `collect_ignore`, a changed `addopts` with `--deselect`, `--ignore`, `-k`, `-m` (joined
  `-ksmoke` too), or a skip/xfail in any option; anything outside those sections, such as a tox
  `commands = python -m pytest`, is no finding). Bound paths are each `.py` file a `Test:`
  names, a directory a pytest `Test:` names as a test path with or without a `/` (protecting
  every file below it, also behind a wrapper such as `timeout 300 pytest tests`; never `.`;
  never the value of `-k`/`-m`/`-p`/`-c`/`-o`, and never an argument of another command such
  as `make tests`), the
  modules such a test imports from its own directory or below (`from helpers import x` →
  `tests/acceptance/helpers.py`), and the criterion's `Fixtures:`/`Data:` path. Only a
  modified or deleted file counts; an added one never does. A skip or xfail is never a
  correction. Findings are ordered by path. An unreadable diff is a finding, never a pass.
- `regression_check`, once the task's own checks pass: rerun every criterion GREEN in the
  current-revision baseline plus those the task's `tests:` names (incremental selection,
  R65) on the task's tree. GREEN→RED blocks whatever the task serves. GREEN that cannot run
  (`blocked_environment`/`unknown`: a timeout, fixture data the task's worktree lacks) is
  rerun on a detached checkout of the base in the same environment (the tree's `.venv`
  linked in, removed afterwards): still GREEN there → blocking (the task caused it); not
  GREEN there either → a note in the gate's `goals_guard_notes`, no finding (`regressed` is
  a verified degradation, `01` §6.2). A base that cannot be checked out blocks. A criterion
  still RED never blocks a partial merge (C12). With no current baseline nothing is
  protected and the policy says so. `regression_findings` remains as the
  `(findings, observed)` wrapper.

A guard refusal fails the gate like W9's: `goals_guard_findings` reach the rework brief, and
a regression hands back the observed `acceptance` states but no check evidence rows. The
guard is graph-only; the legacy orchestrator has none (like W9).

`acceptance.run_criterion` sets `PYTHONDONTWRITEBYTECODE=1`: the rerun left `__pycache__/`
in the task tree and the next gate attempt refused on uncommitted changes.

Known limits: a project test that writes other files into its tree would hit the same
uncommitted-changes refusal; the rerun costs one run per protected criterion per gate
attempt, plus one base checkout when a GREEN criterion cannot run; a non-pytest binding
(`make accept`) names no test file, so its files are protected by the regression rerun, not
by the tamper check; a `conftest.py` change other than an added skip/xfail/deselect is not
flagged (to avoid false holds); imports are followed one level, from the bound test file
only. Tasks without `serves:` are held once a project has `docs/GOALS.md` (R29/R31,
intended).

### 19.7 Goal conflicts and authority (GDT-7)

**Conflicts** (`maestro/goal_conflicts.py`, pure but for the store). Only Dan decides when
progress on one approved goal costs another (`01` §8, R48); the Priority section informs the
recommendation and never decides (R08). Kinds are `01` §8.1's `incompatible`, `tradeoff`,
`degradation`, `threshold`, `invariant` (the grill finds `incompatible`, GDT-13); stages are
`grill`, `setup`, `review`, `replan`, `finish` (R47; replan and finish wire in GDT-8/GDT-10
through the same API). `context(doc, task, decisions)` is the JSON the controller freezes for
workers: revision, priority, `invariants` (`{criterion: [INV…]}`), metric `directions`,
`terms` (a hash per goal of target and verifying ids, per criterion of oracle, threshold and
`Test:`), the stored decisions, the task and goal names, and `judged` (the criteria an earlier
stage of the same change already judged on this tree; the review's are the gate's reruns).
`judge(regressions, advanced, ctx, stage=, place=)` sorts one change's `Regression`s
(goal, criterion, value, baseline, `state`, and `evidence` text shown to Dan only):

| Regression | Result |
|---|---|
| on a criterion that checks an `INV-`, something advanced | one blocking `hard_invariant_regression` per criterion (naming every goal of it), even with an accept decision (R51, C23) |
| on goal B, criterion C named by a stored decision on B | that decision's verdict (below), whatever advanced this time, nothing included |
| on goal B, no decision names C, another goal advanced | a `tradeoff` `Conflict` per regressed goal, `id = GC-` + sha1(kind, advanced, regressed, criteria)[:8] |
| otherwise (nothing advanced elsewhere, or the same goal; an invariant with nothing advanced) | `plain`: the caller keeps GDT-6's finding |

Coverage is by (regressed goal, criterion), the latest decision naming them, never by
conflict id: the stored baseline (written only by `maestro goals baseline`) keeps an
accepted criterion GREEN, so every later change regresses it again, whatever it advances.
With unchanged terms for the decision's ids: `preserve` → blocking "Dan decided GC-x (date):
keep B"; `accept` with a bound → `accepted` while every measured worsening (percent of
baseline, in the criterion's direction) is within it, else asked again "beyond the +10% Dan
approved on <date>" (C22); no measurement → asked again; an unmeasured accept → covered only
for the same facts (C24); either accept covers a criterion in `judged` (the review never
re-asks what the gate measured on this tree). `other` (free text) **never** accepts (Dan,
2026-10-18): the same facts still regressing block with "Dan answered GC-x on <date>:
'<text>'; the change still regresses B (C)", so the task reworks and parks as usual when its
budget runs out; new facts ask again. Facts are structured only: per regression the hash of
criterion, state, value and baseline, never the reason text (temp paths and addresses differ
each run). Terms changed for any of the decision's ids → asked again; a revision elsewhere in
the contract is no reason. A re-ask when nothing advanced names the goals the decision traded
for, so it keeps the conflict's id and Dan's new answer replaces the old. At most
`MAX_QUESTIONS_PER_ROUND` questions go out a round; the rest block as "waits for the next
question round" (R66).

`escalation(conflict, ctx, place=)` is a `dan_questions` question plus
`resolution`/`keep`/`bound` per option and the `conflict` (with `terms`, `facts` and
`fingerprint`). The question names the ids, the advancing criteria, revision, where it is
asked (`place`, else the stage: the gate says "at the gate (review stage)"), task and
evidence; `why_undefined` says priority
never decides, what is held (this task only; independent work continues) and why the
recommendation. Options: `Keep B` (rework, A's progress may shrink, no contract change) and
`Accept B worse by up to +N%` (only when measured; the TO- line and test tolerance Dan's
revision needs, and that until then the decision covers the criterion in later tasks too;
anything worse asks again), or `Accept B failing AC-…` when unmeasured.
Recommended: accept when measured and A ranks above B, else keep B. Free text is Telegram's
"Other". `decide(spec, answer, at)` maps a tapped label to its option, anything else to
`other`; `record`/`load` keep one decision per id in `.orchestrator/goal_decisions.json`:
the read-modify-write holds an exclusive `fcntl` lock on its directory, the write is
atomic, and a corrupt file is moved aside to `goal_decisions.json.corrupt-<utc>`, never
overwritten. `merge(stored, answered)` joins answered decisions the store lacks.

`contract_problems(doc)` (setup): two mandatory metric criteria on the same `Test:` whose
thresholds cannot both pass (`threshold conflict: …`), and a `TO-` naming an `INV-` or a
criterion that checks one (`invariant conflict: …`). `goals.py` validation adds them as gate
problems with ids, so `goals check`, doctor and the runner's holds see them and only work on
those ids holds.

**Authority** (`maestro/goal_authority.py`, R61, R09). `LOCAL`: implementation_detail,
retry, test_selection, local_debugging, within_scope_fix, routine_plan_adjustment.
`RESERVED`: goal_change, acceptance_threshold, final_state_definition, goal_conflict,
scope_expansion, hard_constraint_relaxation, security_exception, paid_operation,
destructive_action, product_question. `requires_dan(action, doc=None) -> (bool, reason)`; an
unknown action raises `ValueError`. The contract's `Ask Dan:` reserves a local action by
keyword (`any retry`); `Autonomous:` never grants a reserved one, and naming one there
(`paid services`) is a `draft` problem from `goals.py` validation. Reserved actions are found
by phrase, not bare word (`goal conflict`, `delete production`/`destructive`, `security
exception`, `hard constraint`, `paid service`/`spend money`), so `resolve merge conflicts,
delete temp files` is ordinary work.

**Wiring.** Gate: `goal_guard.policy(repo, task, base, decisions=)` also freezes
`conflicts` (`context` with the runner's `goal_decisions()`, read at each gate dispatch, so a
decision Dan just gave counts), `named` (the task-named criteria) and `values` (baseline
metric values). `regression_check` sends each verified GREEN→RED (one `Regression` per goal
of its criterion, its state, value and baseline for a metric) through `judge(...,
stage="review", place="the gate (review stage)")`, with the goals a named criterion turned
GREEN as `advanced` (none: a stored decision still covers; else GDT-6 is unchanged). GDT-6's
finding stays for a plain regression and for a hard invariant (beside
`hard_invariant_regression`); a conflict or a covering decision replaces it. Questions fail
the gate with `outputs["dan_questions"]` (findings in `goals_guard_findings`, no check
evidence); covered conflicts pass, recorded as `goal_conflicts_accepted`. The runner's
`_question_cone` treats a verification node like a reviewer (cone back to the implementer),
and the rework note carries Dan's answer plus the gate's findings. Review: the reviewer's
`goals_check` carries the same `conflicts`, with the same decisions and `judged` = the
criteria the gate reran for the task (`goal_guard.reruns`: GREEN at the baseline or
task-named; the gate runs before the review); `goal_progress.read` sends decided `regressed`
records (their cited evidence, state RED) against decided `advanced` ones (their criteria,
in any check mode) through `judge`; a record in a conflict carries `conflict` and loses its
GDT-6 finding; `judge`'s blocking texts are `goal_conflict` findings; its questions join the
reviewer's own up to `MAX_QUESTIONS_PER_ROUND` (the rest wait as findings); accepted ones go
to `disposition["goal_conflicts_accepted"]`. So one conflict reaches Dan once: the gate
asks, and neither the review nor a later gate asks the same facts again. Runner:
`_record_dan_answer` also calls `decide`/`record` when the question carries `conflict` and
keeps the decision on the question record (`goal_decision`); `goal_decisions()` is the store
∪ every answered conflict question of the task (its `goal_decision`, else `decide` on the
raw answer), writing back any the store lost, so a failed write never wastes Dan's answer
or turns a byte-identical re-ask into a refused `garbage_output`.

Known limits: an accepted degradation stays GREEN in the stored baseline (only `maestro
goals baseline` writes it) until Dan's contract revision adds the `TO-` line; what covers
the criterion meanwhile is the stored decision, matched by (regressed goal, criterion) in
every later task's gate and review (GDT-10 must still read accept decisions); `other`
answers reach agents only through the rework brief's DAN'S DECISIONS and never accept; the
facts hold exact values, so a remeasured metric under an `other` answer or an unmeasured
accept asks again; authority phrases are a fixed English list.

### 19.8 Milestone checkpoints and modified-B replanning (GDT-8)

**The store** (`maestro/goal_store.py`). `.orchestrator/goal_decisions.json` is the one
goals store (GDT-9 absorbs it; the path stays, nothing migrates): `decisions` (§19.7,
unchanged), `episodes`, `checkpoints`, `replans`. `update(repo, section, mutate)` rewrites
one section and keeps the rest, under §19.7's directory lock, atomic write and
corrupt-file-aside; `goal_conflicts.record`/`load` wrap it (GDT-7's `record` wrote
`{"decisions": …}` alone and would have dropped the other sections).

**Episodes** (`maestro/goal_replan.py`). At graduation the `goal_progress` records of the
accepted run's last approved review that has them (a later lens that judges no goals never
drops them) become one episode per (task, goal), id `<task>:<G>`,
upserted, so a repair re-graduation or a rework never counts twice: run, revision,
`terms` (a hash per goal of its target, verifying ids and each verifying criterion's
oracle, threshold, test and mandatory flag: it changes only with Dan's revision), the
task's contribution (`none` when unclassified), served, impact, evidence (kind, id, name,
result, value, baseline, criterion) and a proven `contradiction`. The reviewer may add
`"infeasible": "<why>"` to an entry: on a decided `no_progress_unexpected` whose proof
names a criterion it is `contradiction: {criterion, why, facts}`, facts the hash of the
criterion and the evidence's kind/result/value/baseline (never the text); otherwise a flag
and a targeted follow-up (R42).

**Triggers** (`triggers(store, doc, checkpoint=None, misses=)`, R44). Per goal, at most one
signal per graduation, none while one of the goal's replans waits on Dan, in this order:

| Kind | Fires when | Facts |
|---|---|---|
| `contradiction` | an episode at the current terms carries a proven contradiction; else a checkpoint's feasibility problem (`goal_roadmap.feasibility`) this graduation opened (absent while the task was still open) | the contradiction's facts; the problem text's hash |
| `milestone_gap` | at a checkpoint the goal's automated mandatory criteria hold a RED, none BLOCKED/UNKNOWN, and either none turned GREEN since the last checkpoint that judged the goal (else the baseline) while an unsettled accepted direct episode exists, or the milestone is the goal's `convergence` | hash of the RED ids and every accepted direct task (settled or not), so a settled gap asks again only when a task joins it or the REDs change |
| `repeated_misses` | `thresholds.goal_replan_misses` (default 2; below 1 → 2) distinct tasks' episodes are served, `direct`, `no_progress_unexpected`, at the current terms | the episode ids |

`enabled`, `unchanged_expected`, `advanced`, `unknown`, `no_progress_legacy` and `regressed`
never count, nor enabling/maintenance/unclassified contributions, `serves: []`, or a goal
whose mandatory verifiers are all operator checks (R70, C25). `advanced` does not reset the
miss count (convergence is the gap's job). An applied or decided replan of the goal at the
same terms settles its signal's facts and every episode of the goal the signal counted or
saw (`trigger.seen`, the goal's episodes at the current terms when it was raised): those
never fire again, so `Keep going` on a contradiction does not let the same misses raise a
`repeated_misses` replan at the next graduation (a new miss counts from zero). A fact
outside them, or changed terms, is new evidence. The signal id is `RP-` + sha1(goal, kind,
sorted facts)[:8].

**The proposal** (`propose`, R45): `root_cause` (a deterministic hypothesis, the evidence,
a confidence, `attribution: unattributed`, R58), `scope` (RED criteria, the goal's open
tasks, the tasks a hold would take, `independent: continues`), up to three alternatives
plus `Keep going` (within `dan_questions.MAX_OPTIONS`), each with `changes`
(`add_tasks` ROADMAP blocks, `deps_rewire` = the tasks that wait for them, `contract_text`),
`cost` (tasks, attempts spent, paid), expected `impact` per criterion and `requires_dan` /
`authority_reason` from `goal_authority.requires_dan`, then `recommended` and `why`:

| Signal | Alternatives | Recommended |
|---|---|---|
| repeated misses, milestone gap | `Add a diagnosis task` (an `enabling` task on the RED criteria, `<g>-diagnose-<rp>`; the goal's open direct tasks wait for it; `routine_plan_adjustment`), `Revise G's contract` (`acceptance_threshold` when a criterion has a threshold, else `goal_change`), `Keep going` | the diagnosis |
| a proven contradiction | the same | the contract revision |
| a feasibility problem | `Add the missing tasks` (`goal_roadmap.propose_tasks` for the named criteria, ids suffixed with the replan id), the contract revision, `Keep going` | the missing tasks |

Every task a proposal adds carries a required gate check the graph runs (GDT-10 review H3;
before it, INV-04 refused these tasks and their run never settled): the diagnosis task
writes its cause to `docs/diagnosis/<task>.md` (`cause_file`), whose first line is
`Cause: <token>` (`goal_replan.CAUSE_TOKENS`: plan, scope, goal_mapping, harness, measurement,
dependency, constraint, agent, unknown), checked by `goal_replan.cause_check` (`head -n 1 |
grep` for that line, the first line only since the GDT-12 review, L1; GDT-12, so W12's
attribution can read it; a diagnosis task queued before keeps
`test -s`);
the others get `goal_roadmap.verifications` (§19.10: the bound command, a credible run
for a harness task, else `kind: dan`). `propose(..., repo=)` runs them in the project.

An optional `analyse(system, user)` seam (the graph loop passes `selfheal.diagnose
._judge_complete`) may replace the hypothesis and confidence and add an approach to the
diagnosis task's notes; its answer is schema-checked, and on any failure the deterministic
proposal stands. No deterministic alternative costs another goal, so none is a
`goal_conflict` yet (that path is §19.7's API, stage `replan`).

**Authority, hold, answer** (R46, C27). A recommendation Dan need not approve is applied:
status `applied`, its tasks go into the graduation commit, `goal_replan_applied` is
journaled and Dan gets an FYI. Otherwise the replan is `asked` through
`poll_question({"id": "dq-replan-<RP>", "task_id": <graduated task>, "run_id":
"replan-<RP>", "wait_key": "replan-<RP>", "spec": {question, why_undefined, options,
recommended, replan}})`; the `awaiting_dan_questions` wait is keyed by `wait_key`
(`verify._wait_key`), so two replans asked at one graduation keep a wait each. `held(repo,
tasks)` is what dispatch skips: while a replan is asked, or after Dan approved a contract
change until his revision lands (the goal's terms or the contract revision change), every
open task serving the goal other than as maintenance, and its dependents; while an added
task is open, the tasks waiting for it and their dependents. Maintenance work, `serves: []`
and other goals keep dispatching; a running task is never cancelled. `describe_holds`
says what each hold waits for (Dan's answer, his revision of `docs/GOALS.md`, an added
task). `poll(repo, channel, at, present=)` reads answered questions: a tapped alternative
is `approve` (its tasks are returned to be committed; a contract part holds as above),
`Keep going` is `keep_going`, anything else is Dan's `notes` (recorded and shown, never
applying a change, like §19.7's `other`). Every answer settles the signal's facts and lifts
the asked hold; a store write that fails leaves the replan asked, so the next poll decides
it again from the answer file. The replans `poll` reads are the store ∪ the `dq-replan-*`
question records (as §19.7's decisions): one the store lost (a corrupt file moved aside)
comes back from its question's `replan` payload, `decided` when the record holds Dan's
recorded `replan_decision`, else `asked`, and is written back (`goal_replan_restored`). A
row `poll` cannot read is journaled `goal_replan_poll_failed`; the others are still
decided. A settled replan's tasks (`applied`) that are neither in the ROADMAP nor completed
(`present`) and were never seen there are returned again each poll, and by a repair
re-graduation (`on_graduation(completed=)`); once seen they are `landed`, so a task Dan
deletes by hand stays deleted. Every brief of a task serving the goal carries DAN'S REPLAN
DECISIONS (`goal_progress.manifest_fields` `replan_decisions`).

**Milestone checkpoints** (`maestro/goal_milestones.py`, R43). Found at graduation only:
`phase_complete` (the task's `group_id`/`group` has no other open task),
`enabling_unblocked` (an enabling task for G that an open task serving G depends on),
`risky_integration` (risk high/critical, serving a goal), `strategic_pivot` (the revision
changed since the last checkpoint, or a replan settled since), `convergence` (no open task
serves G any more); none → no checkpoint. `checkpoint` reruns only the touched goals'
mandatory criteria that have a `Test:` (R65) with `acceptance.run_criterion` on the merged
tree and never writes the baseline; the record `{id, at, task, run_id, milestones,
revision, goals: {G: {AC: state}}, before, skipped: {G: [AC]}, unknown, feasibility,
follow_ups}` goes to
`checkpoints`; `before` is the last checkpoint that judged the goal, else the baseline. A
criterion that turned BLOCKED/UNKNOWN (not already so before) is a targeted follow-up in
the graduation commit, recorded in the review follow-up backlog for the polish stage
(§19.13; before GDT-11 it queued an `<id>-followups` task that a format-2 gate held) (R42). The checkpoint
runs inside the graduation, which journals nothing else, while the watchdog kills a loop
silent for 20 minutes with no run live and nothing waiting on Dan (three kills → HALT):
each criterion is journaled before it runs (`goal_checkpoint_running`, `<task>: <AC>
(i/n)`), and none starts once the budget is spent: `goal_milestones.budget_for()`, the
default cap `CHECKPOINT_BUDGET` (600 s) reduced when the configured `stall_window_min`
leaves less than the cap, one criterion's 300 s timeout and a minute's margin, so a
checkpoint stays inside the stall window. A criterion not started is "not rerun": listed
in the record's `skipped` (never as an UNKNOWN state in `goals` or `before`, so the next
checkpoint's `before` ignores it), kept at its baseline for feasibility, blocking a
`milestone_gap` verdict for its goal, and a targeted follow-up ("make the touched goals'
tests finish within the cap"). A re-graduation of the same accepted run
reuses the checkpoint recorded for it (task and run id) with its follow-ups instead of
running the tests again under a new id.

**Wiring** (graph only, like the goals guard; the legacy loop gets none).
`orchestrator._graph_graduate` reads the open ROADMAP tasks and calls `on_graduation`
before `mark_roadmap_complete`, which takes `replan_tasks=` and appends their blocks (CAS
recorded) in the graduation commit; checkpoint follow-ups join the review follow-ups. The
poll loop calls `poll` after `prune_awaiting`, which keeps `replan-` waits, with `present`
(the ROADMAP's and the completed ids), commits the returned tasks
(`roadmap.add_replan_tasks`, idempotent), then `verify.prune_replan_waits(asked_ids)`
drops a `replan-` wait whose replan the store no longer has asked, so a lost store cannot
keep the queue from being exhausted forever. `add_replan_tasks` leaves an uncommitted
ROADMAP (Dan's edit in progress, as `reduce_roadmap_deps` reads it) uncommitted: the
tasks join the working tree dispatch reads, `goal_replan_tasks_uncommitted` is journaled,
and the next graduation commits the ROADMAP; a failed `git add`/`commit` is journaled
`goal_replan_commit_failed`. `parse_runnable_tasks` and `parse_prep_tasks` skip `held(...)`
(empty without the store; `goal_replan_hold` journaled once per change, never raising);
the per-poll repair graduation also covers accepted tasks a replan holds
(`_graph_repair_candidates`). The queue counts as exhausted only once no replan question
waits and no replan holds work: otherwise `goal_replan_waiting` (`describe_holds`) is
journaled and told to Dan once per change, and the idle heartbeat keeps the loop's journal
alive. A task a replan added that is parked (failed for good, `parked_tasks`) keeps the
work waiting for it held, as the dependents of any parked task stay blocked (releasing
them would run goal work on a cause nobody found), but that work is Dan's to unblock, not
a wait on a replan: the exhausted check uses `waiting_holds`, which drops a replan whose
added task is parked, so the queue exits as it does with a normal parked task, and
`goal_replan_task_parked` is journaled and told to Dan once (`/unpark` retries it).
A replan failure at graduation or in the poll journals `goal_replan_failed` (one
failing signal; the others still act) and never blocks the graduation. A checkpoint
follow-up is a `FollowUp` of round 0, node `goal checkpoint`. Without `docs/GOALS.md`
none of this runs.

Known limits: a checkpoint is synchronous in the graduation, bounded as above; the
deterministic hypothesis is generic until GDT-12's evidence-based attribution; the
diagnosis wait is a dispatch hold, not a rewritten `deps:` line; a replan task added while
the ROADMAP holds Dan's uncommitted edit is committed by the next graduation, which commits
the whole ROADMAP as graduation always has.

### 19.9 Durable goal-progress state (GDT-9, `maestro/goal_state.py`)

R35 / C28: a fresh process resumes the goals from disk, shows the real revision and
baseline, and never reports done (that verdict is GDT-10's, which reads this state with
§19.7's decisions and the hold reasons).

**The store, version 2** (`maestro/goal_store.py`). The same file and path (nothing
migrates) gains two sections. `criteria`: one observation per (criterion, revision),
upserted: `{id, revision, state, source: gate|checkpoint, at (UTC ISO), task, run_id,
commit, command, reason, protected}`. `contract`: one snapshot per legal revision seen,
`{revision, sha256, text, at}` (the input `goals.changes(old, new)` needed). Every
`update` stamps `"version": 2` (a newer maestro's higher version is kept); a file without
one is GDT-8's layout and reads the same; a missing section reads `[]`; unknown keys are
kept. `problem(repo)` says why the file does not read; a reader never moves it, the next
writer does (§19.7's corrupt-file-aside, unchanged).

**Reading** (writes nothing; nothing at all without `docs/GOALS.md`).

| Function | What |
|---|---|
| `results(repo, doc)` | the current-revision baseline overlaid by each observation at this revision that is strictly later than the baseline's `ran_at`; `[]` without a current baseline (§19.4's "no baseline" stays). Rows at other revisions count for nothing (R67); a later `maestro goals baseline` wins again |
| `sources(repo, doc)` | `{AC: "observed by the gate after T1 (abc1234)"}` for each result an observation gives |
| `protected(repo, doc)` | baseline GREEN ∪ every criterion observed GREEN at this revision; never shrinks (each row's sticky `protected`), so a regression Dan accepted keeps GDT-7's bound; a later baseline rerun wins in `results` but never un-protects, only a new revision clears it (slightly stronger than GDT-7's baseline-only set); empty without a current baseline |
| `protected_states(repo, doc)` | the baseline's states with the protected ones GREEN: what the gate reruns and the reviewer judges |
| `contract_problems(repo, doc)` | `changes(last snapshot, doc).problems` plus each id seen at an earlier snapshot, absent now and not retired (GDT-1-FORMAT) |
| `progress(repo, doc, tasks)` | below; `None` without `docs/GOALS.md` |

`progress` (shown by `maestro goals status [path] [--json]`, read by GDT-10) holds the
revision and format; the baseline (`ran_at`, revision, `stale` when it is another
revision); per criterion `{state, source (baseline, gate, checkpoint), at, task, run_id,
commit, command, reason, mandatory, protected, automated, decisions: [GC id,
resolution]}`, operator checks with `automated: false`; `unmet` (mandatory criteria not
GREEN, and every mandatory operator check: never an automated pass, R14); `decisions` and
`replans` (status, kind, resolution, `awaiting_revision`); `holds` (the execution gate's
and `goal_replan.held`'s, with reasons; a hold on a replan whose added task is parked says
it needs `/unpark`, GDT-8 L1's wording); `next_work` (the next safe work: open tasks naming
an unmet criterion that are not held, not parked and have no open or parked dependency,
ROADMAP order; parked tasks are `state.json`'s `parked_tasks`, read without the controller
store, or `progress(parked=)`); `stale` (R11: `changes(previous snapshot, doc).stale`, the
open tasks whose `criteria:`, `evidence:` or `tests:` name one, and observations and
episodes at other revisions); `contract_problems`; `store_problem`; `reproduce`
(`maestro goals baseline` and each criterion's command). Holds and next work are computed
on read, never stored. There is no done, complete or verdict field.

**Writing.** `collect_observed(store, artifacts, run_id)` (as `collect_progress`) takes
`outputs.acceptance` and `outputs.snapshot` from the run's last succeeded gate attempt
(`decision: accepted` with `snapshot` and `evidence`); a rejected gate, one that asked Dan,
or an unreadable store gives `{}`. `orchestrator._graph_goal_replan` passes it to
`on_graduation(observed=)`, which after the episodes calls `record_gate` and
`observe_contract`, after a new checkpoint `record_checkpoint` (the merged HEAD as commit;
a criterion the cap skipped is no observation), and gives the replan `results(...)`. Each
of these writes is best-effort: a failure journals `goal_state_failed` and the graduation
and replan go on. `record_gate` stores only a credible RED or GREEN: the gate's sandbox is
not the controller's environment, so its BLOCKED/UNKNOWN (already a guard note) never
replaces a credible baseline or earlier row (checkpoint rows run in the controller's
environment and keep every state). The same observation recorded again (the repair
re-graduation of the same run: same source, run, commit and state) keeps its first `at`,
so a baseline rerun since still wins. `maestro goals baseline` calls `observe_contract`
after storing the baseline, which stays its own file, the bottom layer, written only
there; a snapshot that cannot be written is a warning, never a traceback.
`observe_contract` records the first snapshot of a format-2 contract with no
whole-contract gate problem (one with no ids: its revision or history; a section-level
open question names no goal and, like a scoped one, does not count), then a new one only on a higher revision whose change from the last
(`changes().problems`) has none either. A scoped gate problem (an open question, a missing
target or oracle) does not stop a snapshot: R05/R66 run beside it. An illegal edit is
never recorded, so it keeps failing. The poll writes nothing; the legacy loop is
unchanged.

**Readers changed.** The doctor (`cli._check_goals`) runs the feasibility gate on
`results(...)`: once a direct task's gate observed its criterion GREEN, "no open task names
it" no longer fails (§19.4's false FAIL), and `contract: …` (`contract_problems`) joins
the required failure. The checkpoint's feasibility (`merged`) uses `results(...)`; its
`before`/`was` stay the baseline's and the checkpoints', so the gap trigger counts GREEN
changes truly. The gate policy (`goal_guard.policy`, `reruns`) and the reviewer's
`goals_check` read `protected_states`: a criterion an earlier task turned GREEN is rerun
and protected (C12); it has no stored measurement, so its RED-baseline value is dropped
from `values` and a metric conflict on it is unmeasured (asked again, GDT-7's safe path).
The gate also reruns every AC id the task's `criteria:` names, not only its `tests:`
(`named`, GDT-7's `advanced`, still reads `tests:` only), so a direct task's criterion is
observed at its gate whatever its `tests:` says. A criterion protected only by an
observation (GREEN since the baseline, not at it: `policy["observed_only"]`) that is RED
on the task's tree is rerun on the base, as BLOCKED/UNKNOWN are: RED there too means the
target is already RED (a flaky pass, or a later merge broke it), so it is a
`goal_protected_red_on_base` note in the gate's `goals_guard_notes`, not a regression;
GREEN on the base, or a base that cannot be checked out, blocks. A baseline-GREEN
criterion keeps GDT-6's rule: RED blocks without a base comparison (lead's decision on
review M4, spec-internal).
Every role's excerpt is built from `results(...)`, a criterion an observation gave labelled
with `sources` instead of `baseline` (`goals.excerpt(sources=)`); without a store the
excerpt is unchanged. `maestro goals tasks` and Telegram `/status` stay baseline-only.

Spec-internal choices (no Dan decision): the path stays and a missing version is layout 1;
the baseline stays separate; strict revision matching, no carry-over (R67); the latest
observation wins, labelled with source and commit; holds and next work are computed on
read; a silent removal is a doctor/status FAIL, not a dispatch hold (a task naming a
removed id is already held as an unknown id); the legacy-path doctor reads the same
overlay, but the legacy loop records no observations, so it sees the baseline only.

Deviations from the accepted design (`~/maestro-evidence/gdt-evidence/gdt9-design.md`), each needed
to meet it:

1. Each `criteria` row has a sticky `protected` flag: with one row per (id, revision) an
   accepted GREEN→RED would otherwise overwrite the GREEN and `protected` would shrink.
2. A gate row's revision is the one in the gate commit's own `docs/GOALS.md` (`git show
   <snapshot>:docs/GOALS.md`), else the current one: gate outputs carry no revision, and
   a contract revision landing between the gate and the graduation must not file an old
   tree's result under the new revision.
3. An observation beats a baseline result only when strictly later than `ran_at`; an
   unparseable or naive time keeps the baseline (conservative: RED where truth may be
   GREEN, never the reverse). `protected` is empty without a current baseline, as
   `results` is.
4. A gate's `reason` is fixed text and its `command` the contract's `Test:` (gate outputs
   carry states only).
5. The graduation writes are wrapped (`goal_state_failed`), so a progress write can never
   cost the graduation its replan tasks (the orchestrator's catch-all returns none).
6. `observe_contract` records nothing for a format-1 contract or one with a
   whole-contract gate problem, so every snapshot is a legal contract (the review narrowed
   it from every gate problem: a scoped open question used to stop every snapshot; a
   section-level one no longer does either, verification L1).
7. `maestro goals status` exits 0 whatever it reports (it is a view; the doctor is the
   gate) and 1 without `docs/GOALS.md`.

Known limits: a lost store falls back to the baseline only (RED where truth may be GREEN),
so the doctor's false FAIL can return until `maestro goals baseline` reruns, and the
contract snapshots restart from the current contract; observations come only from the
graph loop's gate and checkpoints. A regression Dan accepted (GDT-7) now reads RED in
`results` (GDT-7's stored baseline kept it GREEN), so the doctor's required "AC-x is red
and no open task names it" FAIL stands from his acceptance until his `TO-` revision
lands; this is the truth (R30), and `progress` shows the covering `GC-` decision on the
criterion. A checkpoint row's commit is the controller's `HEAD` when it ran: normally the
merged commit (`fast_forward._follow_checkouts` moves the checkout), but a refused
`read-tree` (`stale_checkouts`) leaves the working tree older than `HEAD`, and Dan's
uncommitted edits are not captured either (review L7; recorded rather than fixed, the
case is rare and journaled).

The reviewer honours the gate's red-on-base note (review M4 follow-up, lead's decision,
spec-internal): when this run's last committed gate noted `goal_protected_red_on_base` for
a criterion (`WorkflowRunner.gate_red_on_base`, read from its `goals_guard_notes` by
`goal_guard.red_on_base`), the reviewer's `goals_check` marks that criterion
`red_on_base`. A reviewer's `regressed` resting on a failing test of it is then
`unchanged_expected` with the same note (in the record's `reason`; the unread
`Read.notes` was removed), never a blocking finding or a rework. The gate also journals
`goal_protected_red_on_base` (task, run, criterion; nothing is sent to Dan), and the
reviewer's brief says "AC-x was RED on the base too" so its own judgement can take it
into account. Without the note (the base was GREEN, or no gate ran) an observation-only
criterion stays protected GREEN and a failing test of it is a verified regression; a
baseline-GREEN criterion is unchanged (GDT-6); other evidence in the same entry (a failing
test of another protected criterion, a worse metric) still blocks.

Residual limits (verification lows L2, L4, L5): the base comparison only helps when the
base is deterministically RED, so a flaky RED on the tree with a GREEN base still blocks;
a rework the LLM reviewer asks for in its own words (its own failing-test finding or
`changes_requested`) is unchanged, because the reviewer's authority is not changed (the
rule and the brief only stop the synthesized "Goal G regressed" finding); and `progress`
reads parked tasks from `state.json`, a projection once `control.sqlite3` exists, since
no lock-free read of the authoritative store exists, so after a controller crash or a
deleted file `maestro goals status` may list a parked task as next work until the next
orchestrator read rewrites it (view-only, soft).

### 19.10 The completion gate (GDT-10, `maestro/goal_completion.py`)

R53, R54 / C30: an empty queue is not "done". With a format-2 contract the project is
`complete` only when every mandatory criterion, operator check, scenario, goal and hard
invariant is met at the current contract revision and commit; otherwise `incomplete`
(the loop can act) or `blocked` (only Dan, or an outside change, can unblock it).

**Where.** At queue exhaustion in both loops (`orchestrator._graph_goal_finish`,
`_legacy_goal_finish`, through `_goal_finish`) and on `maestro goals finish [path]
[--json] [--timeout N]` (exit 0 only for `complete`). Without `docs/GOALS.md` nothing is
read and both exhausted paths run exactly as before. Format 1 is not judged: today's
exhaustion plus one sentence in the existing ping ("(goals not verified: docs/GOALS.md is
format 1.)"). A non-complete step never parks the loop (`paused_by_user`): it records
`state.json` `goal_outcome` `{verdict, revision, commit, report, at}` (on change), prints
`[goals] …`, heartbeats and polls on, so `/unpark`, an answer or Dan's revision is picked
up without `/resume`. `complete` keeps today's path (`queue_exhausted`, the proposal-test
ping and park) after its own message. Steps: `none`, `verifying` (the run continues next
poll), `working` (corrective work landing or added), `asking`/`stopped` (blocked),
`complete`.

**The verification run** (`run`, R67, C15). Keyed `FIN-<sha1(revision|HEAD)[:8]>`: every
mandatory criterion and every criterion a hard invariant names in `checked by`, contract
order, rerun with `acceptance.run_criterion` on the controller's checkout. Each result is
upserted into the goals store's new `finish` section (`{id, revision, commit, started_at,
done, results, verdicts, told}`, last 20 rows) as soon as it exists; a slice stops at
`goal_milestones.budget_for()` after at least one criterion and the next poll or a fresh
process continues; nothing reruns at the same key (the CLI's budget is unbounded). A
metric runs `METRIC_RUNS` (3) times: a uniform RED or GREEN counts with the median, a mix
or a non-credible run is UNKNOWN "unstable: …" (the contract threshold is the tolerance;
a `TO-` or Dan's accept never relaxes it before his revision). A done run is also stored
as `finish` observations (`goal_state.record_finish`, source `finish`). Only this run is
final evidence: gate and checkpoint observations, review episodes (mocked or not) and
older revisions or commits close nothing. The baseline now records `commit`
(`acceptance.store`).

**Trust** (R24, R25). A bound test file changed between the baseline's commit and HEAD
(`goal_guard.tamper_findings`) without a *graduated* task (completed ids, or `status:
complete`) naming the criterion in `test_corrections:` makes that criterion `dan`; the
task's own entry is read from the current ROADMAP, else the newest ROADMAP revision since
the baseline's commit that has it (a graduated task leaves the ROADMAP); an open task's
declaration has not run and excuses nothing. When that diff cannot be read
(`goals_guard_diff_unavailable`), every bound criterion is `dan` ("bound tests not
compared against …"); a baseline without a commit skips the sweep and says so under
Limitations. The sweep is cached on the FIN row (`trust`, keyed by the baseline's commit,
HEAD, the ROADMAP's tasks and the graduated ids). Tracked uncommitted changes other than
`docs/ROADMAP.md`, `.orchestrator/` and the views the loop regenerates after every launch
(`merge._MERGE_DISCARDABLE`: `docs/dependency_map.md`/`.png`, `docs/UPCOMING.md`) make the
checkout `dan` ("uncommitted changes: a.py"); a non-git checkout is `dan` too; `git
status` not answering is a gate failure (`blocked`), not "not a git checkout". The run
records whether any part of it saw such changes (`dirty`, checked before each criterion
and at the end): once the tree is clean at the same HEAD, that row's results are dropped
and the run starts again.

**The verdict** (`verdict`, pure). Per root item:

| Item | met | work | dan |
|---|---|---|---|
| criterion | GREEN on the finish run | RED/BLOCKED/UNKNOWN with a bound test, no conflict (an open task naming it that the loop can run, now or once its open dependencies run: `named`, no new round) | untrusted; a gate problem names it; no `Test:` bound; a finish conflict asked or accepted (waits on his revision) or deferred; every open task naming it cannot run, with the real reason: parked (`/unpark T`), held (the hold's reason), drift-held, not dispatchable (no `mode:`, self_modifying, hold, manual) or waiting for a dependency that cannot run ("T5 (names AC-02) waits for T4, which is held: …", followed recursively); its goal at the cap |
| operator check (mandatory) | Dan's Pass at this revision and commit | Dan's Fail (his words quoted) | not signed off (asked only by the graph loop) |
| contract | no contract or whole-contract gate problem | — | otherwise |
| checkout | clean | — | dirty or not git |

Derived: an invariant is met when its checks are (none: `dan`, "no negative check"); a
scenario when its mandatory criteria and checks are, but one proved only by unit-level
criteria is `dan` (and a doctor draft, `goals.unit_only_scenarios`); a goal when its
mandatory verifiers are (one with only optional verifiers is met and listed under
Limitations: "G is proved by nothing mandatory"). "Can run" is the loop's own predicate:
`roadmap.unrunnable` (the one `parse_runnable_tasks` applies) plus parked tasks, followed
into each pending dependency (`_why_not`). `complete` needs the run done and every item
met; `incomplete` when any item is work; else `blocked`. Optional criteria, optional checks, and open,
parked or held tasks no unmet item names are listed as deferred and never block.
`goal_state.progress` still has no verdict.

**Operator checks** (R14, C9). Asked through the existing question path only when every
other mandatory item is met, at most `MAX_QUESTIONS_PER_ROUND`, no recommendation: id
`dq-finish-op-<OP>-<fin>`, `run_id` `finish-<FIN>`, wait key `finish-<FIN>-<OP>`. Each poll
(`_graph_goal_finish_poll` → `goal_completion.poll`) records Dan's answer into the store's
new `operator` section `{id, revision, commit, finish, result, observation, answer, at,
question_id}`: Pass passes, Fail or his own words fail (a fix task follows); a sign-off
counts only at the same revision and commit. A row the store lost is restored from the
question record (`op_signoff`); a row stored whose question was never marked recorded (a
crash in between) is marked on the next poll; an answer to an older FIN's question not
yet recorded (HEAD moved after it was asked) is still recorded (a sign-off for its own
commit, a conflict decision kept). A question already recorded is never polled again at
its FIN, which would re-add a wait nothing clears. `finish-` waits belong to no run (`verify.RUNLESS_PREFIXES`,
kept by `prune_awaiting`); those of an older FIN are pruned (`prune_runless_waits`).

**Corrective rounds** (R55, C31). Per goal with work (Priority, then contract order), one
`finish_gap` replan (`goal_replan`, stage `finish`, facts `[FIN, AC=state…]`) through
GDT-8's store, apply/ask, landing (`unlanded`, `add_replan_tasks`) and question path. Its
tasks: `goal_roadmap.propose_tasks`'s direct task per criterion, renamed `<id>-<rp>`, with
`verifications` V1 = the criterion's bound command and notes naming the round, the state,
earlier rounds and any GDT-8 diagnosis to read; a failed operator check adds
`<op>-fix-<rp>` quoting Dan, verified by the goal's mandatory criteria. Routine
(`routine_plan_adjustment`) → applied, journaled, no 🧭 message; a contract reserving it →
asked. Rounds count per goal at the goal's current terms (applied, or decided approve), so
Dan's revision resets them. The cap is the first number in the contract's `Stop a failed
approach when:` (digits or one–nine), else `thresholds.goal_finish_rounds`, else 2. At the
cap the goal's items are `dan` and Dan is asked once per facts ("One more round",
recommended the contract revision, "Keep going": the project stays blocked, never done).
While a finish round's task is open and the loop can run it (`_why_not`), or a settled
replan's task has not landed, the gate waits (`working`) instead of rerunning; one the
loop cannot run is named by the verdict instead.

**Every proposed task carries a check the graph runs** (review H3, a fix to GDT-8):
`goal_replan.propose(..., repo=)` gives every task it adds without `verifications`
`goal_roadmap.verifications`: a direct task's are its criteria's (`goal_roadmap.checks`:
`env PYTHONDONTWRITEBYTECODE=1` + the bound command, pytest also behind a wrapper with `-p
no:cacheprovider`; a metric's `python -m maestro.acceptance AC`, its state against the
threshold); an enabling (harness) task's that its criterion runs credibly (`python -m
maestro.acceptance AC --credible`: RED or GREEN, exit 0); a task with no runnable command
`kind: dan` (Dan confirms its evidence). A diagnosis task writes its cause to
`docs/diagnosis/<task>.md` (`goal_replan.cause_file`), checked by `test -s`; the next
corrective round's notes name that file. `python -m maestro.acceptance AC [--credible]
[--timeout N]` (`acceptance.main`) runs one criterion in the current project and stores
nothing (exit 2 for an unknown id). A `kind: manual` check is never used: it passes
`pre_validate` but the graph's `decide()` gets only Dan's dispositions, so it never
discharges and the task would fail its gate for good.

**Finish conflicts** (R47). A protected criterion (`goal_state.protected`) RED on the run
while another goal advanced (GREEN now, not at the baseline) goes through §19.7's `judge`
(stage `finish`, place "the completion gate") with the stored and answered decisions:
asked as `dq-finish-<GC>-<fin>` ("Goal conflict at the completion gate"), Dan's answer
`decide`d and `record`ed by the poll; an accept still blocks until his revision; an
invariant regression is work. No other regression is a conflict: it is plain work.

**The outcome report** (R56): `.orchestrator/goal_outcome.md` and `.json` (uncommitted, a
tracked file would move HEAD), rewritten each evaluation: verdict line; goals → scenarios
→ evidence (`AC-01 GREEN — \`cmd\` (finish run, 0.4 s; baseline RED)`, metrics "measured
8.2, 8.4, 8.3 (median 8.3) …; baseline 42 → −80%", operator sign-offs); hard invariants;
accepted trade-offs (`TO-` and Dan's accepts); unmet and next (every round at the current
terms, its tasks and questions); waiting on Dan; limitations; deferred; reproduce; the
evidence ids.

**Messages** (each through the existing `notify_telegram` or question path; told once per
verdict fingerprint, stored on the FIN row before sending; `maestro goals finish` passes
no notify, so it tells nothing and records nothing as told):
- `✅ Project complete at contract revision {N} ({commit12}): {g} goals, {a} criteria, {o}
  operator checks verified. Report: .orchestrator/goal_outcome.md (\`maestro goals finish\`
  prints it).`
- `🔁 Not done yet — incomplete (round {r} of {cap}) at revision {N} ({commit12}): {up to 3
  unmet}. Added: {task ids} ({RP ids}). Report: .orchestrator/goal_outcome.md`, or without
  the round and `Added:` when no round was added this time.
- `⏸ Not done — blocked at revision {N} ({commit12}). Waiting on you: {up to 5 "id: why"}.
  Nothing else can run. Report: .orchestrator/goal_outcome.md`
- `⏸ Not done — blocked at revision {N}. Waiting on you: the completion gate failed:
  {Type}: {message}. Nothing else can run.` (once per process, exception type and the line
  that raised it: the text can name a temp file that changes every poll)
- format 1: the existing "All queued tasks are done" ping plus ` (goals not verified:
  docs/GOALS.md is format 1.)`.
- Question "Operator check {OP}": `{OP} {name} ({goals}). Do this: {instructions} Expected:
  {expected} Contract revision {N}, commit {commit12}; every automated check passed.`; why:
  `Only you can observe this; an operator check is never an automated pass. Pass closes it
  for this revision and commit. Fail, or a reply saying what you saw, adds a fix task.`;
  options Pass ("{OP} is verified at revision {N}, commit {commit12}") and Fail ("a fix
  task is added for {goals}").
- Question "Goal conflict at the completion gate": §19.7's escalation text.
- A `finish_gap` replan question (§19.8's path): `{RP}: {G} is not done (the completion
  gate found it unmet[ {n} rounds running (cap {cap})]). {hypothesis} …`, Keep going's
  consequence adds "; the project stays blocked, never done".

Journal: `goal_finish_running` (heartbeat per criterion and per metric repeat, and the
rerun on a clean tree), `goal_finish_verdict` (and the report rewrite: only when the
verdict's fingerprint or its rounds change, kept as the FIN row's `verdicts`),
`goal_finish_round`, `goal_finish_asked`, `goal_finish_signoff`, `goal_finish_decided`,
`goal_finish_failed`; `goal_state_failed` when the observation copy fails.

Spec-internal choices (accepted design `~/maestro-evidence/gdt-evidence/gdt10-design.md` §10, no Dan
decision): the per-(revision, HEAD) run with budgeted resume; trust only this run; metrics
3 runs, a mix UNKNOWN, the threshold is the tolerance; Dan's accept is `dan`, never met;
operator checks asked last, valid per revision and commit, free text is a Fail, no CLI
sign-off; `incomplete` beats `blocked`, unrelated work deferred; unit-only scenarios,
unchecked invariants and uncorrected bound-test changes are Dan's; corrective work is
GDT-8's replan path (diagnosis between rounds is GDT-8's milestone gap); the cap
precedence and per-goal counting at current terms; one question at the cap, never
downgraded; the report uncommitted under `.orchestrator/`; non-complete never parks;
legacy is verdict, report and message only; format 1 not judged; store version 2 with two
new sections; the baseline records its commit; a dirty tree blocks; an internal error is
`blocked`.

Deviations from the accepted design, each needed to meet it:

1. A mandatory criterion with no `Test:` is `dan`, not work: a task cannot bind its test,
   only Dan's contract revision can.
2. A goal verified only by optional entries is `met` (nothing mandatory to prove; the
   design was silent).
3. Finish conflicts build §19.7's context with task `""`, so the question does not say
   "in task finish".
4. Corrective tasks are only `propose_tasks`'s direct task per criterion (its enabling
   harness task is dropped; a BLOCKED/UNKNOWN criterion's notes ask to make the test
   runnable without touching its oracle), and every corrective task carries
   `verifications`: the graph refuses an agent task without a required verification
   (INV-04), which the design had not accounted for. V1 is `goal_roadmap.checks` (see
   "Every proposed task carries a check"): `env PYTHONDONTWRITEBYTECODE=1` plus the bound
   command (pytest, also wrapped, with `-p no:cacheprovider`), a metric's
   `maestro.acceptance` state, so the gate's tree stays clean; a failed operator check's
   fix task with no bound mandatory criterion to guard it is `kind: dan`.
5. The `test_corrections:` sweep reads the graduated tasks' own entries, from the ROADMAP
   revisions since the baseline's commit when they left the current ROADMAP.
6. A checkout that is not a git repository is `dan` (the run is tied to no commit).
7. The report's "Unmet and next" lists every finish round at the goal's current terms from
   the store, not only this evaluation's.
8. `poll(journal=)` and `evaluate(completed=)` are parameters (the orchestrator passes its
   journal and completed ids instead of reading `.orchestrator/completed_tasks.json`).
9. Tests: `test_review_evidence_and_gate_observations_never_close_a_criterion` runs AC-03
   and AC-04 RED, so nothing advances and the gate-protected AC-02's RED is plain work, as
   designed, not a finish conflict.
10. The cap lane's AC-02 is GREEN in task worktrees and RED on the controller checkout (a
    gitignored local file), so a corrective task's V1 passes while the final tree stays
    RED; its stop policy names no number so `thresholds.goal_finish_rounds: 1` is the cap.
    GDT-8's milestones are no longer stubbed (review H3): round 1's graduation is a
    milestone gap, and the diagnosis task it adds (its scripted implementer writes the
    cause file) dispatches and settles before the cap question; the lane allows 24 polls.
    The lanes spy on `goal_completion._tell`: the lane harness installs its notify
    doubles after the test body runs.

Review fixes (`~/maestro-evidence/gdt-evidence/gdt10-review.md`, H1–H3, M1–M5, L1–L9) are folded into
the sections above. Known limits: legacy is verdict-only (no questions or rounds); Dan's
uncommitted edits block `complete`; one run per HEAD, so every commit (a landed ROADMAP, a
graduation) reruns the contract at the next exhaustion; a held exhaustion poll still
reads the store, `goal_state.progress` and `git status` and re-judges the stored run
(trust is cached per FIN row; the report and journal line change only with the verdict
or its limitations, M3, follow-up L-a, so the loop rewrites a "verdict only" report
`maestro goals finish` wrote at the same FIN); an answer to an older FIN's question is
read through the channel's read-only `question_answer`, never by polling it again, so
no wait is put back (L9, follow-up L-b; every poll still scans the `dq-finish-*` records,
which grow with the FINs asked); a `kind: dan` fallback check (an unbound criterion's
direct task, an unguarded operator fix) asks Dan to confirm at the gate; `maestro goals
tasks` (GDT-4's setup print) prints each block with its `goal_roadmap.verifications`
(follow-up M-A: nothing merges these blocks, and a pasted block without a check would
be refused by INV-04); a lost store re-asks nothing already answered (sign-offs and
decisions are restored from the question records) but reruns the criteria.

GDT-10 follow-ups (`~/maestro-evidence/gdt-evidence/gdt10-verify.md`, L-c to L-g), further known
limits:
- L-c: a bound test that writes a tracked file makes every run dirty (M2): Dan cleans the
  tree, the full contract reruns and dirties it again. Not a loop (each cycle needs Dan),
  and the checkout item names the file, but each cycle costs a full rerun; the item does
  not yet say "dirtied by the run".
- L-d: `python -m maestro.acceptance` run in a linked worktree reads the criterion from
  its main worktree's current `docs/GOALS.md` (as `git rev-parse --git-common-dir` names
  it) and runs it with that checkout's interpreter and `.venv` PATH; the tests run are the
  worktree's. For a task worktree, whose controller is the main worktree, that is what the
  finish run does. A project that is itself a linked worktree cannot be told apart from a
  task worktree, so its gate check is judged against the main worktree's contract and
  `.venv`, not its own, while its finish run uses its own: on a branch whose `GOALS.md`
  differs, the two can judge different criteria, or the check exits 2 (GDT-10 follow-up
  review F1; known limit). Outside git, and when the main worktree has no
  `docs/GOALS.md`, the check uses the directory's own contract; a criterion only the
  worktree's contract names exits 2 (pinned in `tests/test_goal_completion.py`).
- L-e: `maestro.acceptance` checks and pytest bindings carry the controller's absolute
  interpreter path in the ROADMAP (deviation 4). A moved venv or another clone makes
  each such check exit 127, so the task fails its gate with a message that does not name
  the missing interpreter.
- L-f: an enabling task's `--credible` check passes once its criterion is credibly RED or
  GREEN. That is the goal of a harness task on a BLOCKED criterion, but a milestone-gap or
  "missing tasks" enabling task on a criterion that is already credibly RED passes it with
  no work and graduates on its review alone (not a wedge: the next finish run still sees
  the criterion RED).
- L-g: a diagnosis task's `test -s <cause file>` accepts any non-empty file; it proves the
  cause was written, not that it is right. The next round's notes name the file. (Since
  GDT-12 the check requires the `Cause: <token>` first line, §19.8; still not that it is
  right: the reviewer judges that.)

### 19.11 Grill surfaces (GDT-13)

R01, R10, R12, C1.1, C1.2, `01` §12 bullet 2: a `docs/GOALS.md` contract is written in
three places, and all three carry the same steps, fields, defaults and rules.

**Where each surface lives.**
- ChatGPT's grill: the Drive folder `ChatGPT/grill-me-framework` (`SKILL.md`,
  `GOALS-FORMAT.md`, `PLAN-FORMAT.md` and the other format docs, native Google Docs whose
  bodies are markdown). `GLOBAL-INSTRUCTION.md` is the paste-in text and points ChatGPT at
  the folder, so the folder is the ChatGPT paste-ready path too.
- Claude/Codex: `~/agent-skills/skills/grill-me` (`SKILL.md`, `GOALS-FORMAT.md`,
  `PLAN-FORMAT.md`).
- The fallback: `~/agent-skills/skills/maestro-setup` Step 2c, which runs `maestro goals
  check docs/GOALS.md --json`, writes the contract when there is none, shows `maestro goals
  migrate` for format 1, and asks Dan only the `"ask": true` issues of a format-2 file.

Both grills open with `## Contract first`: twelve bold steps (Purpose, Users, Goals, Final
product, Scenarios, Acceptance criteria, Operator checks, Non-goals and invariants,
Trade-offs, Priority, Authority, Approval) before the first design section, drafted from
the brief and the repo and confirmed in batches, never inventing a target or oracle. Both
have "Conflicts while grilling" (`01` §8.2's five parts; a bounded `TO-` plus a history row;
priority never decides; R47's grill stage) and a GOALS-FORMAT with the format-2 template,
the Fields table, the rules (3–7 goals is guidance, R03), a Good/Bad "Behaviour, not
design" pair (R15) and a worked example. The examples are one parity contract
(`tests/fixtures/goals/parity/`): skill style in grill-me, ChatGPT style (numbered
headings, `—`, `*`, `AC1`, `·`) in Drive; both parse to the same contract. Both
PLAN-FORMATs give a phase `Serves`, `Criteria` and `Contribution`, and say a phase's
`Serves` is a suggestion for its tasks, not proof (R26). Setup 5a gives every task `serves`,
`contribution`, `criteria`, `tests` and `evidence`, with an enabling child in its example,
and runs `maestro goals baseline` and `maestro goals tasks` after the roadmap (R20).

**The Fields table.** `goals.CONTRACT_FIELDS` is every format-2 field as `Field(part, label,
required, default)`: `required` is the worst its absence does, `gate` (a gate problem on
the ids it names), `draft` or `no`. A section counts with its entries, so Goals, Final
product, Acceptance criteria, Operator checks, Hard invariants and Change history are
`gate` (their ids are linked, or the revision loses its approval row), and Format is
`draft` (without it the file is read as format 1). The table on each surface is generated
from it (`scripts/grill_surfaces.py fields`), and `tests/test_grill_surfaces.py` deletes
each field from the parity contract and requires the worst new problem to equal the row
(review M1). The parser also strips a leading BOM now (a Google-Docs export has one, which
hid the title).

**The revision rule.** `goals._REVISION_FIELDS` (renamed from `_CONTRACT_FIELDS`, which
read like the Fields table) lists the entry fields `goals.changes` compares; once a
`maestro goals baseline` or a task gate recorded the contract, a change to one at the same
revision is a gate problem that fails `maestro doctor`. Every surface states it the same
way: of an entry's fields only its name, Rationale, Kind, Layer, Evidence, Fixtures,
Environment, Data, Test, Sign-off and Decision may change at the same revision; setup 2c's
mechanical fixes after the first baseline go in a revision Dan approves. A test derives
that list from the code (and changes each field on the parity contract) and compares it to
every surface (review M2).

**`ask`.** `goals.Problem.ask` is `True` when only Dan can answer the problem (a missing
section, target, baseline, oracle, Given/When/Then, verification, operator-check part,
trade-off bound or decision, authority line, open question, unapproved revision, priority
rank, an orphan goal, scenario or criterion, a definitional conflict) and `False` when an
agent fixes it from what the contract says (id or heading syntax, the other side of a
named link, a kind/layer/evidence spelling, a typo'd id, an unknown format number, an
Autonomous line naming a reserved action). Notes are never `ask`. `maestro goals check
--json` carries `"ask"` per issue; a missing `docs/GOALS.md` is one `ask: true` gate issue
(no longer one per section an empty parse lacks).

**Vendoring.** Tests never read Drive or `~/agent-skills` (the full suite is every
project's release gate). They read snapshots: `tests/fixtures/grill_surfaces/{drive,
grill-me}/` and the setup mirror `maestro/skills/claude/SKILL.md` (byte for byte from
agent-skills, so `install-skills` on a fresh machine installs current text; the Codex prompt
is its header plus the same body). `SOURCES.json` records each file's source, agent-skills
commit or Drive id and time, and sha256. `scripts/grill_surfaces.py refresh` rewrites them
(Drive through `rclone --drive-export-formats txt`, normalised: no BOM, `\n`, no trailing
spaces, one blank line per run; `--drive-from DIR` reads a revised text before it is
pushed); `check` recomputes and exits 1 naming each drifted file, writing nothing. Any later
surface edit runs `refresh` in the same change.

**Drive edits** are text round trips: `rclone copyto --drive-import-formats txt
--drive-export-formats txt <name>.md.txt gdrive:ChatGPT/grill-me-framework/<name>.md.txt`
updates the native doc in place (same id) or creates it; a re-export normalises to the
pushed text exactly (trial in `ChatGPT/server-exchange/gdt13-trial`).

**install-skills guard.** `maestro install-skills` leaves a destination reached through a
symlink (`~/.claude/skills/maestro-setup` links into `~/agent-skills`) unchanged and says
so; it used to `copy2` the repo mirror through it, reverting the canonical skill.

### 19.12 The demonstrator (GDT-14)

R64, `01` §12 bullet 6, `02` §C. "Recipe Box" (`tests/fixtures/goals/demo/`) is one sample
project taken from the grill's contract to `complete`. Its `project/` is the parity
`skill_GOALS.md` plus setup's same-revision `Test:` binding of AC-2, and a stdlib project whose
seed baseline holds all five states: AC-1 RED (a working bench measures 0), AC-2 GREEN, AC-3
BLOCKED (the harness defect: the missing `cookbook500` helper, so `goals tasks` proposes an
enabling task), AC-4 unknown (optional, unbound), OP-1 Dan's. `overlays/` are the commits a
scripted implementer makes, one per task round (`01-test/` before `02-code/` for inner TDD).
`tests/conftest.py`'s `collect_ignore_glob` keeps the fixture out of collection. AC-4 is a
deterministic cost model (0.2 s per parser label), not a timer. No product feature was added
for it; it surfaced four product fixes: an "at or above" oracle is inclusive and a negated one
("not less than") is its opposite inclusive bound, never a false GREEN (`acceptance._direction`,
`16b3516`, review M3/L1); a finish observation's label names its FIN run
(`goal_state._label`, `7133261`); the scope check and the goals guard take a task's change set
from the fork point (`validation.changed_paths(since_fork=True)`, review M2b); and `goals
tasks` marks each block that has no `scope:` (`goal_roadmap.SCOPE_HINT`, review M2a).

**Shape.** Root `tests/test_goals_demo.py` drives setup through the real CLI. Three graph lanes
(`tests/graph_engineering/test_goals_demo_lanes.py`) drive the real loop with fakes only at
the agent CLI, Telegram and ADR-agent boundaries. Lane A goes from the RED baseline to
`complete`. Lane B binds AC-4 at the same revision and runs a `serves: []` TIDY beside AC-1's
direct task: TIDY's break of AC-2 reworks it, and AC-1's G1-vs-G3 latency conflict goes to Dan
while TIDY runs to its merge. Both tasks declare `scope:`: a proposal cannot know the files its
task writes (its bound test is the one file it must not edit), so `goals tasks` ends each block
without one with a `# scope:` comment and setup step 4 has the planner fill it; a task without
`scope:` runs alone (D20), and would wait whenever a gate question waits on Dan. The lanes'
subprocess double leaves each run's `code_base_sha` empty, so their gates take `main` as the
base while other tasks merge there: the scope check and the goals guard diff from the fork
point (`base...HEAD`), as `risky_files_touched` does, so another task's merge is never charged
to this one. Root tests prove on Recipe Box what the lanes do not: C23 (one change advances G1
and breaks INV-1: `hard_invariant_regression`, no question), C13's guard alone (a loosened test
that makes the check pass), and `02` §D bullet 9 (Q1 modified-B: two direct G1 misses raise one
replan, an enabling miss never counts; the bounded final repeat gate: one corrective round,
then Dan at the cap). `goals_demo.C_MAP` maps C1–C33 to test nodes or limits (TRACEABILITY §5:
demo, reuse, partial, limit; a check keeps the two equal).

**Provenance** (`maestro-goals-demo-provenance/1`, `goals_demo.provenance`) is assembled in
test code only from Maestro's own outputs (the stored baseline, the outcome report, the goals
store, control-store attempts) and git. It is written to `.orchestrator/demo_provenance.json`,
and also to `$MAESTRO_DEMO_EVIDENCE` when that is set. Volatile fields, dropped for the
reproducibility diff: `generated_at`, `ran_at`, `at`, `commit`, `introduced`, `merge_commit`,
`finish_id`, `question_id`. `fixture` records its committed tree and whether it is clean (lane A
checks the hash against the tree read from git objects); `maestro` records this checkout and,
under `gate`, the Maestro the plan's acceptance checks import through their interpreter;
`decisions` carries Dan's goal decisions; `independence` is false when nothing is met. Only
lane A writes a bundle: lane B ends before the completion gate, so it has no outcome report to
build one from (its decision and fresh-process status are asserted in the lane); lane C (GDT-11)
ends blocked at the cap and asserts its stages, rounds and question in the lane.

**W7 on the demo (GDT-11).** Lane A's three reviews raise 5 follow-ups (4 explicit, plus AC-1's
"G1 impact unknown" one); at exhaustion, before the completion gate, triage (overlay
`triage-1`) merges 2 duplicates and declines the G1 item as moot, and exactly one polish task,
`polish-1-a`, runs: its round 1 (`polish-break`) also drops the not-a-recipe check, the gate
reruns the protected AC-2 beside the area's AC-1 and AC-3 and sends it back as a plain
`acceptance_regression` (C29, C18), and round 2 (`polish-1`) does both items. The bundle gains
`backlog` (id, source task, state, of, reason, task). Lane C (C31, R55) runs TIDY alone with
`thresholds.goal_finish_rounds: 1` (its contract's stop policy names no number): TIDY's
follow-up goes straight to `polish-1-a`, the finish adds AC-1's corrective round, which passes
its gate in its worktree while a gitignored stale parser cache keeps AC-1 RED on the
controller (`import-miss`; GDT-8 then adds a missing task, `import-miss-2`, and a diagnosis
task), its follow-up opens `polish-2-a`, and at the cap Dan's Keep going keeps it blocked.

**W12 on the demo (GDT-12).** Three D-tests on Recipe Box with a real ControlStore ledger
(`tests/improvement_fixture.py`) prove what were W12's limits here: C20
(`test_w12_attribution_analysis_and_a_rollback_leave_every_accepted_verdict_unchanged_on_the_demo`:
after a real `goals finish`, attribution, an analysis, Dan's approval and his rollback leave every
run status, the registry, ROADMAP, goals store, outcome report and HEAD byte-identical, and a
second finish gives the same verdict and fingerprint), C32
(`test_a_historical_no_progress_row_reads_unattributed_and_a_declined_proposal_returns_only_on_new_evidence_on_the_demo`)
and C33 (`test_an_approved_lever_s_effect_is_measured_before_and_after_on_the_demo`, cheaper vs
cheaper with an escaped defect). Lane A runs its one project-end analysis right after
exhaustion (the analyst scripted to propose nothing): no question, the outcome report
byte-identical around it, and the bundle gains `improvement` (each run's kind, status, tasks and
proposals). C1–C33: 29 demo, 4 reuse, 0 partial, 0 limit.

**Limits.** A GDT-8 diagnosis task (the replan for misses, a milestone gap or a contradiction) declares
its cause file as its `scope:` and runs beside independent work (2026-10-24). The other tasks a
replan appends carry no `scope:` and run alone, and that is accepted (2026-10-25), not a
limit left open. A declared scope is enforced (`scope_gate` refuses out-of-scope work), so a
scope must cover every file the task will write. No sound source does that here: a
feasibility replan's missing tasks serve criteria that no open task names, so nothing names
their files yet. The scopes of earlier tasks for the same goal, plus the bound test file, are
a guess that would block a task that has to touch new product code. The completion gate's
corrective tasks run when the queue is already empty. Their only peers are each other, and
they fix the same still-red goal, often in the same code, so serializing them keeps merges
trivial. Running alone costs throughput, never correctness. Pinned by
`test_goal_replan.py::test_every_task_a_replan_proposes_carries_a_check_the_graph_runs`
and `test_goal_completion.py` (no `scope` on either kind). The live-LLM grill is not automatable, so the parity
contract stands in for it. Gate checks run `python -m maestro.acceptance` in a subprocess. In a
linked worktree, the venv's editable install resolves that to the root checkout's `maestro`,
so a gate check never exercises a worktree-only change to `maestro.acceptance`.

### 19.13 The polish stage (GDT-11 / W7, `maestro/backlog.py`)

R52, the polish side of R28/R41, R55's loop-back, C29, C31; P13 §W7; `01` §9 bullets 1-2 and 5.
Before, each accepted task queued one `<id>-followups` micro-task that a format-2 gate held
(no `serves:`), duplicates were never merged, and no dependent waited for a follow-up it would
build on. Now review follow-ups wait for one end-of-project polish stage. Graph loop only (the
legacy loop never collected follow-ups); no second scheduler: triage and polish are ordinary
ROADMAP tasks the existing dispatch runs. Built in three slices: S1 `7580013`/`be5574d`, S2
`5f2d3e6`/`4ee94d5`, S3 `b0ef887`/`f4402ae`.

**Store and item.** `.orchestrator/backlog.json` `{"version": 1, "items": [...]}`, written
through goal_store's extracted locked, atomic writer (`goal_store.update_file`/`read_file`: a
corrupt file is moved aside, unknown keys kept). A separate file, because W7 serves every graph
project, with or without `docs/GOALS.md`. An item: `id` (`BL-0001`, sequential, never reused),
`text`, `source {task, round, node, commit}` (round 0: a goal checkpoint), `origin`, `scope`
(the raising task's declared write set), `checks` (its `kind: auto` checks without `await:`),
`criteria` (its `criteria:` ∪ `tests:`), `state` (`open` → `triage` → `merged` | `moot` |
`assigned` → `done` | `declined` | `deferred`), `stage`, `task`, `of`, `reason`, `at`,
`settled_at`. Keyed by (raising task, normalised text): a retried graduation adds nothing twice.
A project with no follow-ups gets no store file and an exhaustion byte-for-byte as before.

**Routing** (`followups.record`, one function): an ordinary task's follow-ups (incl. GDT-10
corrective and GDT-8 replan tasks, and GDT-8 checkpoint follow-ups, so R42 now runs in goal
projects) go to the backlog `open`; a `-followups` task's own `open` (no chain); a triage or
polish task's own are born `deferred` ("raised by a polish/triage review; polish never
chains"). Already-queued `-followups` tasks run unchanged. `before_dependents`: the reviewer may
mark a follow-up whose defect a dependent would build on (an interface, name or schema); with an
open dependent it runs as `<id>-followups` (`serves: []`) and each open dependent gains it in
`deps` (`taskgraph.add_document_deps`, the twin of `reduce_document_deps`: minimal, parse-back
verified, CAS registered; a drifted or uneditable block is journaled
`followups_dependents_not_wired`). With no open dependent it is a backlog item.

**Ordering and sprint.** A current active W5 sprint can stage eligible source follow-ups
before exhaustion through this same path. Source holds and requirements are checked again
at run opening and admission, including merged duplicates. Without an active sprint,
the ordinary exhausted branch runs before the completion gate:
`_graph_at_exhaustion` calls `_graph_polish_stage()` (module-level, never raises:
`polish_stage_failed`, then the finish runs). `backlog.stage` reopens items held by a stage
task Dan deleted, or still held by one that graduated (its `apply_triage` or `settle` failed),
then numbers the stage above every stage number in the store, the ROADMAP and the completion
registry (a lost or corrupt store reuses no graduated id), then: 2+ open items → one triage task `polish-triage-<n>`; one → its polish
task directly; none → the completion gate (GDT-10). The blocks are committed by
`roadmap.add_tasks` (the generalised `add_replan_tasks`), journal `polish_stage_opened`, one
Telegram message. Outside sprint a stage opens at queue exhaustion, the loop's own "nothing left to run":
parked or held main tasks do not delay it.

**Triage.** An implementer writes `docs/polish/triage-<n>.json` (`keep`, `duplicate` with `of`,
`moot` with a reason, every item once), under the triage bar and the `triage` goals excerpt
(every non-goal kept; `runner.py` passes the role for a triage implementer node); its reviewer
checks every moot reason at HEAD (a wrong decline is blocking). Its gate check is `python -m
maestro.backlog check-triage <file> --items …` (the ids ride in argv); `scope:` is the file.
At its graduation `apply_triage` re-validates the merged file (unreadable or invalid keeps
every item: `polish_triage_unreadable`), marks `merged`/`moot`, groups the kept items by area
and hands their polish blocks to the graduation commit (`mark_roadmap_complete(...,
polish_tasks=)`); `polish_triage_applied` and one message name the merges, every decline reason
and the polish tasks.

**Area and polish task.** An item's area is its raising task's declared scope; groups are the
connected components of overlapping scopes, and an undeclared scope joins all (D20), so distinct
polish tasks have disjoint scopes and merge trivially. A polish task `polish-<n>-<letters>`
(a..z, aa, ab, …; `apply_triage` refuses to leave a kept item unassigned):
`serves: []`, `polish_items`, the union scope plus its report (omitted when any scope is
undeclared), `tests:` = the items' criteria (never `criteria:`, which would add contract holds),
one acceptance criterion per kept item (a merged item's provenance and text appended to its
line; the outcome lists a duplicate of an item left there with its text), the
items' checks deduplicated and renumbered, then the report check last (INV-04; D23's "do or
decline each" made mechanical). The writer and reviewer bars (D23) forbid new scope and widening
an item. At its graduation `settle` marks each item `done` or `declined` from
`docs/polish/<id>.json`; one the report misses is `deferred` (`polish_items_settled`).

**Goal-driven rules (S3).** R28: the goals guard reruns every protected GREEN criterion
(hard-invariant checkers included) on every task and the criteria a task's `tests:` names, so a
polish task reruns its area's product acceptance tests and never escapes the invariants. R41: a
task serving no goal (`serves: []`) advances none (`goal_guard.policy` `named: []`), so a
regression it causes is GDT-6's blocking finding, never weighed as a GDT-7 trade and never a
question to Dan; tasks with `serves: [G…]` are unaffected. R55: the corrective rounds' follow-ups
are ordinary backlog items, so the next exhaustion opens the next stage before the finish runs
again; the loop is bounded by GDT-10's round cap and by polish follow-ups being born
`deferred`. R56/`01` §9: items left `open`, `triage`, `assigned` or `deferred` are listed in the
outcome as `deferred` ("review follow-up, nonblocking (…)"), never a verdict input; the
fingerprint is unchanged.

**Messages and journal.** Telegram: the stage opened and the triage result. Journal:
`backlog_recorded`, `polish_stage_opened`, `polish_stage_failed`, `polish_tasks_added`,
`polish_triage_applied`, `polish_triage_unreadable`, `polish_items_settled`,
`followups_dependents_not_wired`. No new command, telemetry field or Context Gate change.

**Choices (spec-internal, no Dan decision).** (1) A separate backlog file through goal_store's
writer. (2) `before_dependents` reuses `<id>-followups` and wires open dependents minimally; no
open dependent → backlog. (3) A `-followups`, triage or polish task never queues another task;
triage and polish follow-ups are born `deferred`. (4) Triage is an ordinary task with a
deterministic check and a deterministic apply, only for 2+ open items; when unsure, keep; an
invalid file keeps everything. (5) Area = declared scope; groups = overlap components; unknown
joins all. (6) Polish: `serves: []`, the items' auto checks, the report check, `tests:` = the
area's criteria. (7) A task serving no goal advances none in GDT-7's judgement. (8) The stage
opens at queue exhaustion. (9) Polish before the completion gate; corrective follow-ups loop back
through the next stage. (10) Leftover items are `deferred` in the outcome. (11) Graph loop only.

**Builder deviations** (the design file `~/maestro-evidence/gdt-evidence/gdt11-design.md`, builder notes
S1-S3): `followups.record` takes a `journal` hook; `Recorded` has `deferred`; the loop seam is
`_graph_at_exhaustion(channel) -> (polishing, finish step)`; `add_tasks(..., key=)` keeps
`add_replan_tasks`' events byte-identical; stage-task graduation is
`_graph_polish_graduation`; grouping uses kept items only; a merged item adds its provenance
and W5 source eligibility/resource requirements. In S3 the demo's polish round 1 reworks on `acceptance_regression: AC-2`, not
`hard_invariant_regression` (that wording is for a change that advanced another goal, C23,
and R41 means a polish task advances none); lane C runs TIDY rather than AC-1's planned task
(a planned direct task whose criterion stays RED on the merged tree is GDT-8's gap at its own
graduation, before any exhaustion), on the demo contract with AC-4 unbound (lane B's binding
turns the parser into a G1-vs-G3 conflict) and a stop policy naming no number; AC-1 stays RED
on the controller through a gitignored stale parser cache, as GDT-10's cap lane does.

**Limits.** Live change on release: every adopted graph project stops getting immediate
`-followups` tasks (Dan's "review follow-ups become a task, until post-P12: moves to an
end-of-project polish stage"). A project whose queue never empties waits for an eligible
W5 sprint to polish early; otherwise its backlog remains. A wrong "moot" drops a fix (mitigated: keep when unsure, the reviewer checks every moot
reason at HEAD, Dan sees every reason). No item cap: undeclared scopes make one large polish
task. Already-queued `-followups` tasks in a format-2 project stay held by the goals gate (no
`serves:`) and are listed as deferred by the finish. Without GOALS.md and with no auto checks on
the raising tasks, a polish task's only gate check is its report check (the reviewer is its
regression guard there). In a linked worktree the gate's `python -m maestro.backlog` imports
the venv's editable install (the root checkout), so the polish lanes set `PYTHONPATH` to this
checkout; harmless after the root merge.

### 19.14 Improvement proposals (GDT-12 / W12, `maestro/improvement*.py`)

R57-R60, R68, and the W12 parts of R38, R58, R62, R65, R70; C20, C32, C33; P13 §W12. Before,
W2 recorded a blame ledger nobody analysed, and W11a's goal misses were `unattributed`
observations with no way to attribute them. Now Maestro analyses its own ledger weekly and at
each project end, proposes at most three changes to Dan, applies the ones he approves, measures
their effect and asks before undoing a worse one. Graph loop only; no new role, scheduler,
dispatch path or slash command; the Context Gate untouched. Built in four slices: S1
`abdb432`/`ff4c396`, S2 `eb181cd`/`1329e14`, S3 `f425c95`/`3f70eab`, S4a
`1fc5cb0`/`7e6cb03`/`883af53`, S4b `d789f6e`/`7199ba6`.

**Dependencies and seams.** W2's ledger (`ExtraStepObserved`, `GoalImpactObserved`,
`maestro report usage`) is the only input besides the goals store, the stored baseline and the
global config. W10's digest is not built: W12 reports in its own per-run message, and
`python -m maestro.improvement report|evidence` prints read-only JSON whose numbers equal the
usage report, the seam W10 embeds later.

**Attribution and amendments** (`improvement_evidence.attribute`). A real miss is a
`no_progress_unexpected` episode on a goal the task served `direct`, not operator-only
(enabling, maintenance and `serves: []` never form one, R70). Rules over recorded facts, the
first that holds wins: A1 the run's own accepted gate rerun shows a named criterion GREEN that
was not GREEN at the baseline → `reviewer`; A2 every named criterion unmeasured → `none`; A3 Dan
revised the goal's terms since → `none`; A4 a reviewed diagnosis task's cause file at HEAD
starts with `Cause: <token>` → that token's fault (`review_origins.DIAGNOSIS_FAULT`); otherwise
`pending_evidence` → `unattributed`, never `planner` by default. The fact is appended to the
events table as an amendment of the episode's observation row (same ledger id), only when it
differs from the latest; the first row stays as the observation and its provenance (R68). The
usage report's `goal_impacts` shows the latest attribution beside the captured one.

**Mapping version 2** (R62). `CAUSE_FAULT["task_did_not_serve_goal"]` is `unattributed`; a
stored W11a row (mapping version 1, `planner`) reads `unattributed` with `captured_fault:
planner` kept. No database migration.

**Patterns and the pack.** The window is the tasks with a succeeded run finished in
`(since, until]`. Deterministic patterns, ranked, each with its spend per pool: `extra_step`
(fault, cause, role; 3 rows in ≥ 2 tasks), `goal_miss` (goal, attribution, cause; the replan
miss threshold in ≥ 2 tasks), `evidence_gap` (`unknown` impacts, 3), `false_alarm` (replans
settled `keep_going`, 2). The pack (window, goal section, at most five evidence ids per
pattern, route statistics, the earlier proposals and Dan's answers) fits `BRIEF_LIMIT` and never
cuts a pattern.

**The analyst.** One bounded call (`selfheal.diagnose._judge_complete` on the diagnoser route,
timeout 300 s), only when a pattern exists; no pattern is an `empty` run and no call. The answer
is checked against the full patterns: a cited pattern that was shown, evidence a subset of its
rows, the fault's side (a role lever names the fault's role, `planner_rule` needs a planner
fault, an unattributed or fault-less pattern takes only `gate`, `human_step`, `graph_order` or
`context_skill`), a saving between 0 and the pool's spend (null only "unmeasured"), the required
fields, the metric vocabulary. Up to 2 retries with the problems, then the run is `failed` with
no proposal and the window stays; there is no weaker fallback.

**Proposals, decline memory and the question.** Ranked by expected saving, then pattern rank;
at most 3 asked per run, the rest `deferred` and candidates again. A proposal under way
suppresses a new one with its key (lever, target, change); a declined, noted or rolled-back one
returns only with ≥ 3 pattern rows recorded after Dan's decision, and its question says what is
new. A free-text answer is a note: never applied, counted as declined, one revised resubmission
allowed. The question `dq-improve-IP-n` holds nothing (`holds: False`: no wait is marked), names
every field and offers Approve / Decline.

**Apply, rollback, intake and landing.** A config lever may only raise
`roles.<role>.strength`, in the global config, and never one a `project.yaml` masks:
`apply_config` backs the file up beside it (`backups/config.yaml.<IP>.<at>`) and edits only that
leaf (`config.set_global_leaf`: block style only, re-parsed, atomic under a lock). Every other
lever is a code lever: `queue_intake` writes the intake report `I-<project_key>-IP-n` (kind
`change`) into Maestro's checkout (`$MAESTRO_IMPROVEMENT_INTAKE`, else the running checkout's
main repository), a mailbox file and not a commit, so never a release; `landed` marks it applied
at the first adopted release whose history names the id. `rollback` runs only on Dan's tap: the
exact backup bytes while the file is as applied, else only the leaf while it holds the applied
value, else refused ("changed since applied", naming the leaf and both values); a code lever's
rollback queues `I-<project_key>-revert-IP-n`.

**Measurement and effects** (`improvement_metrics`). Ten lower-is-better metrics (cost units
and tokens per task per pool, wall time, gate attempts, review rounds, extra steps and their
fault/cause breakdown, false alarms, false-hold hours, time to outcome, quality defects); none
counts green criteria. At every run, before the analysis, each applied proposal is measured on
paired windows of N accepted tasks before and after `applied.at` (N =
`thresholds.improvement_min_tasks`, 5); too few after is a line ("IP-n: 2 of 5 accepted tasks
since applied"). The quality guard: a saving never outweighs a quality drop, so cheaper with an
escaped defect is `worse`. A `worse` effect asks `dq-improve-rollback-IP-n` (holds nothing, Roll
back recommended, Keep); it never acts by itself.

**The digest message.** One per run, only with an effect line, a new proposal or a
suppression, sent before the questions: `📈 Maestro improvement — <project>, <kind>, <span>: <n>
accepted tasks.`, the effects, new and deferred proposals, what was not re-sent and why, and the
analyst's calls and characters. No green count.

**Wiring.** `orchestrator._graph_improvement(store, artifacts, channel, kind)` runs a due
analysis (`improvement.due`): `project_end` once per HEAD, right after `queue_exhausted`;
`weekly` (`_graph_improvement_weekly`, throttled by `IMPROVEMENT_CHECK_S`) in the three idle
branches beside `_self_update_maybe` (finish-hold, exhausted, no runners), only when nothing is
runnable either (review M1: runnable work that admission or a quota pause holds, or that the
just-settled last runner freed, is not idle, and the skipped tick does not spend the
throttle), when no run of the
project is younger than `thresholds.improvement_interval_days` (7; 0 turns weekly off). Nothing
is in flight at either moment, so the call stalls no work. `_graph_improvement_poll` (after the
finish poll) reads Dan's answers and applies, queues or rolls back; a throttled landing check
follows. None of them raises (`improvement_failed`).

**Window.** `since` is the `until` of the latest run that is neither `failed` nor `empty`. A
failed run keeps the window so the next due run reads its tasks again; an empty one (no pattern,
no call) keeps it too (S4b): on a fresh project the first idle tick's weekly run can come after
one task, and that task's rows must still count with the later tasks' toward a pattern. The
weekly interval counts every run.

**Telemetry and journal.** `goal_attribution_recorded`, `goal_attribution_unlinked`,
`improvement_run_empty`, `improvement_run_done`, `improvement_analysis_failed`,
`improvement_failed`, `improvement_proposal_asked`, `improvement_proposal_suppressed`,
`improvement_decided`, `improvement_applied`, `improvement_queued`, `improvement_landed`,
`improvement_measured`, `improvement_rollback_asked`, `improvement_rollback_decided`,
`improvement_rolled_back`, `improvement_rollback_refused`, `improvement_apply_failed`,
`improvement_poll_failed`, and since the review fixes `improvement_apply_refused`,
`improvement_rollback_failed`, `improvement_question_superseded`. Telegram: up to 3 proposal questions per run, one digest, one
confirmation per applied or queued proposal, a rollback question on a worse effect.

**Choices (spec-internal, no Dan decision).** (1) W12 core is built here; W10's digest is not:
W12's per-run message and read-only JSON are its seam. (2) The analyst is the diagnoser route
through `agentcall`; no new role, policy publication or graph node. (3) It runs only when a
pattern exists, weekly at an idle tick and at project end; up to 2 retries with its problems,
then the run fails; no weaker fallback. (4) Attribution is deterministic rules over recorded
facts, a reviewed diagnosis `Cause:` line the only text input, `unattributed` the default,
persisted as append-only amendments. (5) `task_did_not_serve_goal` maps to `unattributed`
(mapping v2), captured `planner` kept, no database migration. (6) The store is per project
(`.orchestrator/improvement.json`); decline memory keys on lever, target and change; new
evidence is ≥ 3 pattern rows recorded after the decision; a free-text answer is a note. (7) A
config lever only raises `roles.<role>.strength` in the global config; every other lever is a
code lever queued through operator intake in Maestro's checkout and measured from the first
adopted release naming its id. (8) Effects use paired windows of N accepted tasks in the origin
project, with a quality guard and no green-count metric; a worse effect asks Dan, never acts.
(9) Improvement questions hold nothing; at most 3 per run. (10) Graph loop only. (11) The
backup sits beside the global config (`<dir>/backups/`), the same as `~/.maestro/backups` in
production, so a tmp config keeps test backups in tmp. (12) A rollback whose file changed
elsewhere puts back only the leaf, and refuses when the leaf changed too. (13) Landing matches the
whole intake id (`I-<project_key>-IP-n`, `project_key` = slug plus 8 hex of the sha256 of the
resolved repo path, so two projects of one directory name never share an id; a commit naming a
longer id that starts with it does not count), since two projects can both have an `IP-0001`. (14)
Effects are measured at the start of every run, `empty` and `failed` ones included. (15) An
empty run does not move the window (above).

**Demonstrator and lanes.** Recipe Box (§19.12) proves C20, C32 and C33 with a real ledger
(`tests/improvement_fixture.py`) and real acceptance runs; demo lane A runs its one project-end
analysis after exhaustion with nothing to propose, so it asks and changes nothing and the
outcome report is byte-identical, and its provenance bundle gains `improvement`. The
improvement lanes (`tests/graph_engineering/test_improvement_lanes.py`) take a seeded reviewer
pattern to Dan's approval and a changed global config, and a declined proposal past the next
weekly run without a second question. They seed an empty weekly run from the day before, since
in a live loop the weekly run usually comes at the first idle tick, which can be the poll where
the last task settles. A `holds: False` question has no `QUESTIONS_STATE_KEY` entry, so the
lanes tap through `_telegram._handle_danreq_callback`.

**Review fixes (2026-10-27; choices, no Dan decision).** (M2) `rollback` never raises: a
failure (a missing backup, a failed revert capture, an undo that raises after Dan's Roll back,
whose decision is already stored) is `state: rollback_failed`, `rollback_failed = {at, error}`,
journaled and told with what is still in place and how to undo it by hand; it counts as under
way, so the same change is never proposed again while it may still be in effect. (M3) An
approval never lowers quality: `set_global_leaf(..., check=)` runs the caller's check on the
text read under the config lock, and `apply_config`'s check reads the leaf's effective strength
again; unless it is still strictly weaker than the approved value (a stale approval: the leaf
was raised after the question; an unreadable value is refused too) the approval becomes
`apply_refused = {at, leaf, value, now}`, journaled and told ("a lever never lowers quality"),
nothing written and the backup removed. It is neither under way nor decided against, so the
same lever may return if the leaf is lowered again. (L2) `config.global_config_lock` is that
lock: the rollback's sha check and byte restore hold it, and its leaf path re-checks under it
that the leaf still holds the applied value. (M4) A question that holds nothing used to stay
pending until answered. Now still-unanswered proposals rank with each run's candidates and at
most `MAX_ASKED` stay open (kept, never re-sent); when a run sends a new question, every open one
it outranks (or whose pattern is not in the window) is withdrawn: `state: deferred`,
`superseded = {at, by, question_id}`, the question record `status: superseded` (no plain-text
answer; a tap on its message gets "No longer open"), "Withdrawn unanswered" in the digest. Asked
again later it gets a fresh question id `dq-improve-<IP>-<IR>`, so a stale answer is never read.
A rollback question is never withdrawn (the change is in effect until Dan answers). And a
`holds: False` request is never the target of unthreaded text, on the per-project bot
(`_route_freetext_answer`) and the shared bot (`address_update`): it is answered by its button
or a reply to its message, so it neither blocks plain answers to a real holding question nor
turns stray text into a decline note. (L1) The diagnosis gate reads only the first line
(`head -n 1 <file> | grep -qE '^Cause: …$'`), the line attribution reads.

**Limits.** Live change on release: in every adopted graph project the first idle tick runs a
weekly analysis over the whole history and each queue exhaustion a project-end one; with a
pattern that is one strong-route call and up to 3 proposals; an approval edits
`~/.maestro/config.yaml` (reversible) or writes an intake report into Maestro's checkout. The
analyst can block the poll loop for up to 3 × 300 s, at an idle moment only. Nothing processes
Maestro's intake automatically: a queued code lever waits for a Maestro session, and its
measurement starts only when a release names it. Effects are measured in the origin project
only, while a config lever applies to every project (rollback refuses if the leaf changed
since). Five-task samples are noisy: `insufficient_data` and `no_change` are explicit, and no
verdict acts by itself. A1 needs the accepted run's gate artifact; without it the miss stays
`unattributed`, which is safe.

Recorded with the GDT-12 review fixes, not fixed here (Codex's W4 area; each needs its own
reviewed product change): (i) `maestro/backends/codex_runtime.py` `_omission_inventory` (lines
43-50) walks the masked `~/.claude`/`~/.codex` surfaces recursively and raises at 20,000 entries
and on "directory changed" during the walk; today about 8,000 entries and growing, so every
Codex prepare will eventually fail, and each prepare spends 3-9 s on this walk. Production keeps
walking the real-home roots (they are exactly the masked surfaces the record exists for); the
suggested fix is a names-only or depth-limited record. (ii) The 60 s queued-reservation deadline
in production still counts worker interpreter startup, seen at 80-130 s at load 25.
