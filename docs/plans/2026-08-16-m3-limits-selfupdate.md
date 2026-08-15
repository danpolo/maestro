# M3 — Model limits and self-update

Spec: `docs/DESIGN.md` §8–§9. Process: `docs/EXECUTION.md`'s M3 stage entry and per-stage loop.

## Already done this session, before the workflow

The ceiling-table extraction (§8, first bullet) is complete and verified — done directly, not via
subagent, per the invariant "Claude side: edit directly, Codex side: invoke the Codex CLI":

- `~/.claude/model_context_limits.md` created; `~/.claude/CLAUDE.md`'s table replaced with a
  one-line pointer. Edited directly.
- `~/.codex/model_context_limits.md` created; `~/.codex/AGENTS.md`'s table replaced with a
  one-line pointer. Done via `codex exec -C ~/.codex --skip-git-repo-check -s workspace-write`
  with an exact-text prompt (create-file + replace-exact-block, no paraphrasing asked for or done).
  Both files read back and verified byte-for-byte against the request.

**Finding, not yet in `docs/FOUND_BUGS.md` (nothing to fix — it's a planning correction, not a
reference-code bug):** DESIGN.md §8 says `limits.py` "replaces the hardcoded 110K/120K constants
carried over in M1." A full-repo search (`grep -rn "110_000\|120_000\|110000\|120000\|110k\|120k"`)
found **zero** such constants anywhere in `maestro/`. The M1 extraction never carried any
context-ceiling numbers over from the reference implementation — `orchestrator_run.py`'s own
`main()` loop has no notion of *its own* context budget; the 110K/120K figures were this build's
own session-lifecycle guidance (now the table above), not reference behaviour. There is nothing to
replace. `limits.py` below still gets built as designed — it is genuinely useful, parsing the two
tables above for any future consumer (e.g. `doctor`, or a driver that wants to warn an operator) —
but the "replaces hardcoded constants" clause in DESIGN.md §8 does not apply to this codebase and
should be read as historical, not a remaining task.

## Scope for the workflow

Two independent library modules, each with its own tests, plus one small wiring point into
`orchestrator.py` for self-update's "at an idle moment" trigger. No characterization tests — this
is new code, not extracted from the reference, so ordinary TDD-style tests against the new modules
are correct here (unlike M0–M2).

### Component A — `maestro/limits.py`

- `parse_limits_table(text: str) -> dict[str, ModelLimits]` — parses the shared 4-column markdown
  table shape (`| Model | Prepare handoff | Normally start fresh | Exception ceiling |`) that both
  `model_context_limits.md` files already use. Tolerate the minor formatting differences already
  observed between the two real files (alignment colons, `**bold**` around one cell in the Claude
  original before extraction, `K`/`k` suffix, en-dash ranges like `90–100K`). A `ModelLimits`
  dataclass/namedtuple: `model: str`, `prepare_handoff_low: int`, `prepare_handoff_high: int`,
  `normally_fresh_low: int`, `normally_fresh_high: int`, `exception_ceiling: int` (all in raw token
  counts, i.e. `90–100K` → `90_000, 100_000`).
- `default_table_paths() -> list[Path]` — `~/.claude/model_context_limits.md`,
  `~/.codex/model_context_limits.md`. Overridable via `project.yaml`'s `model_limits_paths: [...]`
  (read through `maestro.config._load_project_yaml`, same pattern as other config reads in this
  repo — check `config.py` before inventing a second config-loading path).
- `load_limits(paths=None) -> LimitsResult` — reads each path (missing file → skip, not an error);
  parses successfully-read ones; merges into one `{model: ModelLimits}` map (later path wins on a
  duplicate model name — should not happen in practice since the tables are disjoint, but define the
  behaviour). Caches the merged result to `.orchestrator/model_limits.json` via
  `maestro.paths.Paths` (add a `model_limits` path property there, same pattern as `.state`/
  `.usage`) — cache write only, always re-parses from source on `load_limits()` (source files are
  small and local; caching is for other consumers to read without importing `limits.py`, not a
  performance optimization for `load_limits()` itself). `LimitsResult` carries `models: dict`,
  `warnings: list[str]` (e.g. "no table found at either default path", "file X has an unparseable
  row", "file X missing entirely — using shipped defaults") — **never raises** for a missing or
  unparseable file, per DESIGN.md §8 ("never a hard failure on a machine that lacks these files").
  Shipped fallback defaults (a small constant dict) are used only when zero real entries were parsed
  from any path.
- `resolve(model: str, paths=None) -> ModelLimits | None` — the `doctor`-style per-model lookup
  DESIGN.md describes; `None` plus a warning in the result when the model is not in any parsed
  table and there's no shipped default for it.
- `resolve_all(model_names: list[str], paths=None) -> dict[str, ModelLimits | None]` — resolves a
  batch; this is what the Done-when check (below) drives directly, standing in for the `doctor` CLI
  command that M4 has not built yet. Say so in a docstring so nobody mistakes it for `doctor` itself.

### Component B — `maestro/selfupdate.py`

DESIGN.md §9, single channel, git worktrees:

- Layout: `~/.maestro/versions/<sha>/` (git worktrees of this maestro repo), `~/.maestro/current`
  (a file or symlink — pick one, document the choice, a project imports maestro *through* this).
  Add a small `SelfUpdatePaths` (or extend `maestro.paths.Paths` if it fits the existing shape
  better — use judgement, but don't invent a second unrelated global-path convention).
- `candidate_version(repo: Path) -> str | None` — current maestro HEAD sha, compared against
  `state["maestro_version"]` (read via `maestro.state.read_state`). `None` when already current or
  `state` has no such key yet (first run — adopt silently, no test-and-adopt ceremony needed for
  "no version recorded yet").
- `materialize_worktree(repo: Path, sha: str) -> Path` — `git worktree add` under
  `~/.maestro/versions/<sha>/` if not already present (idempotent — a second call for the same sha
  is a no-op, not an error).
- `self_test(worktree: Path) -> SelfTestResult` — runs maestro's own suite (`python3 -m pytest`, or
  whatever `pyproject.toml` declares — read it, don't hardcode `-q` again given the known `-qq`
  footgun already on record in `docs/PROGRESS.md`) inside the candidate worktree. `SelfTestResult`:
  `passed: bool`, `summary: str` (the raw pass/fail line), `output_tail: str` (bounded, for the
  alert on red).
- `adopt(worktree_sha: str) -> None` — repoint `current`, `state.write_state` records the new
  `maestro_version`, `append_journal("self_update_adopted", ...)`. **Never called mid-task** — the
  caller's job, not this function's, to have checked that; document the precondition loudly in the
  docstring rather than trying to enforce it here (this module has no visibility into `in_flight`).
- `maybe_self_update(repo: Path | None = None) -> str` — the one entry point orchestrator.py calls.
  Returns a short status string (`"up_to_date"`, `"adopted:<sha>"`, `"held_red:<sha>"`,
  `"error:<msg>"` — never raises out of this function; a self-update failure must never crash the
  orchestrator loop). On red, journal `self_update_held` with the failing summary and leave
  `current` untouched (the "rollback" *is* not having moved it — no separate rollback code path is
  needed for the red case; write one explicit test that proves a deliberately-broken candidate
  worktree — e.g. one with a syntactically-failing test file added — is refused and `current` still
  points at the pre-existing sha afterward, since that is M3's literal Done-when).
- Idle-moment wiring: **one small, additive call in `orchestrator.py`**, mirroring how M2 added
  `_launch_backend`/`_entry_backend` without touching `main()`'s control flow otherwise. The natural
  hook is next to `_idle_heartbeat_maybe` (`orchestrator.py:885`) — call `maybe_self_update()` from
  the same idle branch that calls `_idle_heartbeat_maybe`, throttled the same way (do not run it on
  every poll; reuse or mirror the `IDLE_HEARTBEAT_S` throttle constant rather than inventing a
  second one), and **only when `in_flight` is empty** (idle *and* no task running — the literal
  "never mid-task" reading of DESIGN.md §9, stricter than "no launchable task" which still permits a
  gated-but-in-flight entry). Keep the diff to `orchestrator.py` minimal and additive; do not
  refactor `main()`.

## Definition of done

Run these yourself after the workflow, in this order, and record every number in `docs/PROGRESS.md`
— do not report a number you have not seen:

1. `python3 -m pytest` — full suite, must stay **zero skipped, zero failed** (whatever the passed
   count lands on; record it).
2. A test (in `tests/test_limits.py`) that calls `resolve_all()` against the two real files now at
   `~/.claude/model_context_limits.md` and `~/.codex/model_context_limits.md` and asserts every one
   of the 5 real model names (`Claude Sonnet 5`, `Claude Opus 5`, `GPT-5.6 Luna`, `GPT-5.6 Terra`,
   `GPT-5.6 Sol`) resolves to a non-`None` `ModelLimits`. This stands in for "`doctor` resolves
   every model in both tables" until M4 builds `doctor` itself — note that substitution explicitly
   in the test's docstring and in `docs/PROGRESS.md`.
3. A test (in `tests/test_selfupdate.py`) that builds a scratch git repo with a passing test, calls
   `maybe_self_update`, confirms adoption; then adds a candidate commit with a deliberately failing
   test, confirms `maybe_self_update` returns `held_red:...`, `current` is unchanged, and no
   exception propagates. This is the "deliberately-red candidate version is refused and `current`
   stays put" Done-when, run for real, not asserted from reading the code.
4. AbuAliArchive read-only check: HEAD, `git status --porcelain`, `.orchestrator/state.json`
   size/mtime, `.orchestrator/HALT` presence — must match the baseline in `docs/PROGRESS.md`
   exactly (verified once already this session before any file was touched; re-verify after, since
   nothing in this stage should touch it, but the invariant says check every stage regardless).

## Explicitly out of scope for this stage

- Wiring `resolve()` results into any live warning/pause behaviour (e.g. auto-triggering a
  human-facing handoff when a real Claude/Codex session crosses a ceiling) — DESIGN.md §8 describes
  `limits.py` as a lookup library; nothing in §8 or §9 asks for the orchestrator to *act* on a
  ceiling crossing yet, and there's no in-repo consumer of "this implementer session is near its own
  context limit" data (the orchestrator supervises *task* sessions, not its own context, and the
  supervised sessions' own context usage isn't sampled anywhere in this codebase). Flag as an open
  question for `docs/DESIGN.md` if the operator wants it later.
- `maestro doctor` itself, and the `~/.maestro/versions/` cleanup/GC policy for old worktrees —
  neither is in DESIGN.md §8–§9; both are natural M4/hardening follow-ups, not blockers here.
- Any change to `AbuAliArchive`.
