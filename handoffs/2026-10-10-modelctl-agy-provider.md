# Handoff: modelctl Antigravity (`agy`) provider

Written 2026-10-10 ~04:00 IDT. Previous session stopped at its context threshold after the
investigation and design; **no provider code is written yet**.

## Goal

Dan (2026-10-10): "make sure agy is supported in modelctl and if not add support". It is not
supported (`~/projects/modelctl` has only `codex` and `claude` providers). Build an `antigravity`
provider at parity with Claude/Codex **inside modelctl**, and report agy's real auto-compaction
limits so Dan can choose the lifecycle limits.

Dan's answers (binding):
1. Scope: full parity with Claude/Codex, **but only what P13 does not plan**. P13 W4 owns the Maestro
   side. When done, update the docs/plans to match.
2. Classes: family line with effort folded (`gemini-flash`, `gemini-pro`, `claude-opus`,
   `claude-sonnet`, `gpt-oss`); one row per version, no effort suffix; a new version is a successor
   that inherits the predecessor's limits; older rows stay.
3. First scan: measure agy's compaction limit, report it, notify; **Dan supplies the limits**. Rule he
   gave: if the compaction limit is high enough, use the same limits as Claude Code; if not, shrink all
   three limit categories to fit under the enforced compaction limit.

## Read first

- `~/projects/modelctl/docs/SPEC.md` (whole file, incl. the two revisions at the end)
- `~/projects/modelctl/modelctl/providers/claude.py` (the template), `core/scan.py`,
  `core/transitions.py`, `core/outbox.py`, `core/table.py`, `core/addflow.py`, `cli.py`, `paths.py`,
  `tests/conftest.py`
- `~/.gemini/model_context_limits.md` (the live agy table: `agy/` row prefix, a second prose
  "Boundary basis" table)
- Maestro: `maestro/limits.py` lines ~200-225 and ~405-445 (`agy/` prefix, variant fold),
  `maestro/model_detection.py` `consume` (lines 44-90), `maestro/backends/registry.py` (`modelctl_provider`)
- `docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md` W1 and W4, decisions B4 and B8
- Probe: `~/projects/maestro-wt/agy-compaction-probe/` (`probe.py`, `chain.log`, `chain2.log`, `*.jsonl`)

## Facts established

- `agy models` (agy 1.3.2) prints `id<TAB>Display (Effort)`, metadata only. Current list: Gemini 3.8 /
  3.7 / 3.6 Flash (high/medium/low), Gemini 3.1 Pro (high/low), **Claude Opus 5.5 and Sonnet 5.5**
  (low/medium/high), GPT-OSS 120B (medium). The table and Maestro's `_agy_profiles` still carry Claude
  **4.6** rows, which agy no longer lists.
- Smoke call: `agy --output-format json --model <variant> -p "Reply exactly: OK"` returns
  `{"status":"SUCCESS","response":"OK\n",...}`.
- `~/.local/bin/modelctl` runs `~/projects/modelctl` **directly**, and `modelctl-scan.timer` runs daily
  (~05:50 IDT). Any change in that checkout is live at once. Work only in the worktree
  `~/projects/maestro-wt/modelctl-agy` (branch `feat/antigravity-provider`, created from master
  `7b4d0e6`, no changes yet).
- The live modelctl checkout has **uncommitted changes that are not ours** (dated 2026-10-08:
  `_bin()` binary resolution in `providers/claude.py`, `providers/codex.py` and their tests). Leave them.
- Maestro's `consume` raises `PolicyError` for any outbox event whose provider is not a
  `modelctl_provider` in the registry, which aborts the consume for Claude and Codex too. So modelctl
  must **not** put antigravity events in the outbox until Maestro declares it.
- Maestro's `core/maestro.py` verify probe calls `limits.resolve(q)` without a backend, so agy queries
  must be spelled with the prefix (`agy/Gemini 3.8 Flash`, `agy/gemini-3.8-flash-high`).
- agy's compaction thresholds are server-delivered (proto fields `compaction_threshold`,
  `max_context_tokens`); logs, binary and changelog state no number. They must be measured.

## Compaction measurements (agy 1.3.2, 2026-10-10)

Method: one headless conversation grown by ~24K tokens per turn (Gemini tokenizer; the same filler is
~44K tokens per turn on Claude); live context = last `step_update` usage `input_tokens +
cache_read_tokens`. A `checkpoint` step fires **before** the reply when the incoming context would
pass the threshold.

| Model | Largest context that survived | Compacted when next turn would reach | After compaction |
|---|---:|---:|---:|
| Gemini 3.8 Flash (fine, ~3K steps) | 176.4K | 179.5K | ~20K |
| Gemini 3.1 Pro (fine, ~3K steps) | 88.8K | 92.2K | ~20K |
| Claude Sonnet 5.5 via agy (coarse, ~44K steps) | 288K | ~332K | ~31K |
| GPT-OSS 120B (coarse) | 39K | ~62K | ~17K |

So the boundaries are about **178K** (Flash), **90K** (Pro), **288K-332K** (Sonnet 5.5) and
**39K-62K** (GPT-OSS). Compaction is harsh: the context falls to ~20K and even the turn that
triggered it is summarised. The old table's 140K (Flash), 128K (Pro) and 80K (GPT-OSS) boundaries are
not what was observed; Pro and GPT-OSS compact **below** their current table ceilings (123K, 75K).

All probes have finished; nothing is running. Results: `chain.log`, `fine.log`, `*.jsonl`. Not yet
measured: Gemini 3.7 / 3.6 Flash (assumed like 3.8), Claude Opus 5.5 via agy (third-party pool is
small; ask before spending it), and a fine pass for Sonnet and GPT-OSS
(`python3 probe.py <model> <cap> 100 <fine-from> 12`).

## Design decided in the previous session (not yet built)

- Provider name `antigravity`, label `Antigravity`, `add_command` `modelctl add antigravity`;
  `Env.antigravity_dir` = `~/.gemini`, `Env.antigravity_limits` = its `model_context_limits.md`.
- Class id = the agy id lower-cased, separators folded to `-`, trailing effort variant
  (`high|medium|low|thinking`) removed: `gemini-3-8-flash`, `claude-opus-5-5`, `gpt-oss-120b`.
  Family = the word tokens (`gemini-flash`, `claude-opus`, `gpt-oss`), version = the numeric tokens.
  Row name `agy/<display without the "(Effort)">`. The row key the core compares must be
  `fold(class id)`, not `fold(row name)`; `configured_models`, `inherited_plan` and `prune_model` in
  `core/transitions.py` need a small per-provider adapter for that.
- Variant ids (what `--model` accepts) are kept in a provider-owned state file and returned as
  `aliases`, so Maestro can map `gemini-3.8-flash-high` to its class (P13 B4 routes are per effort).
- Enforced window: agy's observed compaction boundary, stored in a machine-parseable
  `| Model | Enforced window |` table in the agy file (as Codex has), replacing the prose "Boundary
  basis" table; `add antigravity --window`; validation `ceiling < window`; a successor inherits the
  predecessor's window. Maestro's table parser ignores 2-cell rows, so this is safe for it.
- Smoke uses the cheapest variant (low, then medium, high, thinking).
- Outbox gate: `outbox.maestro_consumes(env, provider)`: true for `claude`/`codex`; for any other
  provider only when `maestro/backends/registry.py` (read by AST) has
  `modelctl_provider="<provider>"`. Gate `record_detection`, `record_set`, `_maestro_asks` and the
  switch-notice check on it, so until W4 flips that one line modelctl sends its own notices and
  writes no antigravity outbox events. Refuse `prune_model` while the provider is not consumed.
- No auto-compact pin or catalog to regenerate for agy; `sync_aliases` only refreshes the variants file.

## In scope

- The provider, CLI wiring (`add antigravity`, status, scan), core adapters, the outbox gate, tests.
- Finishing the compaction measurements and reporting them to Dan.
- After Dan's limits: migrate `~/.gemini/model_context_limits.md` (window table, 5.5 rows), run the
  adds, merge the branch into the live modelctl checkout, verify a scan.
- Docs: modelctl `docs/SPEC.md` (new "Antigravity provider" revision); P13 spec W4 first step and
  STATE.yaml's "agy rows in modelctl" note (both gitignored, edited live by the P13 session, so make
  small targeted edits); `maestro/thirdparty.json` / `p07b` compaction notes only if W4 does not own them.

## Out of scope

- Maestro code: registry `modelctl_provider`, `modelctl_bridge.py`, catalog rows, canonical identity,
  permission lists, confinement. All P13 W4/W1. No Maestro commit (commits auto-release to every project).
- The Context Gate (AGENTS.md standing rule).
- The foreign uncommitted changes in the live modelctl checkout.

## Verification to report

- `cd ~/projects/maestro-wt/modelctl-agy && python3 -m pytest -q`: pass count before and after (baseline on
  master first), zero failures.
- New tests: id/class parsing for every current `agy models` line; scan (configured class quiet; new
  class notifies once; successor inherits limits and window); add (writes both tables, rollback on
  FAILED smoke); no antigravity outbox event while the registry does not declare it, and events once a
  fixture registry does; Claude/Codex outbox behaviour unchanged.
- Against the real home, read-only: `modelctl status` from the worktree lists the Antigravity rows.
- After go-live: one `modelctl scan` shows `events=0` for antigravity and no error note, and
  Maestro's `limits.resolve("gemini-3.8-flash-high", backend="antigravity")` returns the new row.
- Maestro suite untouched; if anything in Maestro is read by tests, run with `.venv/bin/python -m pytest`.

## Suggested skills

`test-driven-development`, `verification-before-completion`, `using-git-worktrees` (worktree already
exists), `close-session` at the end.

## Blocked on Dan

The next session's **first reply must give Dan these items** and wait for his answer before the
dependent work. Building and testing the provider in the worktree does not depend on them.

1. **The agy lifecycle limits** (prepare handoff / start fresh / exception ceiling, and the recorded
   compaction boundary) for each class, after seeing the final measurements. Blocks: table migration,
   `modelctl add antigravity`, go-live. Confirm by `modelctl status` showing his values.
2. **Whether to spend third-party agy quota** to measure Claude Opus 5.5 (and a fine pass for Sonnet),
   or assume Opus matches Sonnet.
3. **What to do with the agy rows for Gemini 3.7 / 3.6 Flash and the Claude 4.6 rows** (4.6 is no
   longer listed by agy): keep, re-limit, or remove.
4. **Go-live**: merging into `~/projects/modelctl` makes the daily scan include agy. Ask once, after
   the limits are in.

## Coordination

- A parallel session is running P13 (W0 done; stage 1 = W1/W2/W8/W9 next) in `~/projects/maestro` and
  edits `docs/graph-engineering/STATE.yaml` and the P13 spec. Do not touch Maestro source; keep doc edits small.
- Scratch lives in `~/projects/maestro-wt/` (persistent), never the session scratchpad.
- Remove the probe directory and the worktree yourself when the work is merged.
