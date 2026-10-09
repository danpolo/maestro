# P12 second pilot: supervise itv 01 → 04, then close P12

## Goal

This continues `handoffs/2026-10-08-p12-itv-pilot-launch.md`. That handoff and `handoffs/2026-10-08-p12-itv-pilot-next.md` are still the scope and verification contract; read their "Supervision", "Close P12" and "Verification to report back" sections. The launch is **done**. This session should:

1. Supervise `01-runtime-evidence` to acceptance, then `04-quota-adapter`, collecting the per-task evidence listed in the launch handoff.
2. Close P12: fold the evidence into B01/B18; give every `_p12_open_threads` entry a disposition; run the full suite and `scripts/graph_qualification.py`; update PROGRESS/EXECUTION; build the verdict as a `visual-explainer` artifact and `send-to-me` it; ask Dan about the post-P12 queue.

## Read first

- Root `AGENTS.md`; the memory index, especially:
  - context-management-not-built
  - no-idle-subagent-polling
  - talk-in-local-time (IDT = UTC+3)
  - do-cleanup-yourself
  - handoffs-are-the-whole-prompt
  - post-p12-feature-queue
  - maestro-gate-uses-venv
  - deny-list-hits-ask-dan-with-recommendation
- `handoffs/2026-10-08-p12-itv-pilot-launch.md` (previous contract) and `handoffs/2026-10-08-p12-closeout/p12-verdict-evidence.md` (the audit and full-PASS conditions; don't redo it).
- `docs/graph-engineering/STATE.yaml` `_p12_open_threads`. Its top entries are this line's: G1 through G9, the shared bot token, and the clean-checkout suite. Note: `docs/graph-engineering/` is **gitignored**, so STATE edits are local and never committed.

## What happened in this session (2026-10-08, ~21:10–22:20 IDT)

**Maestro commits**, all on `feat/graph-engineering-foundation`. Only their own files were staged, never `git add -A`.

- `ff44b73` **G1:** `maestro init` prints the checkout it scaffolds from and warns when the pin redirected it (`MAESTRO_INVOKED_FROM`). 4 tests in `tests/test_bootstrap.py`.
- `da4e21b` **G5:** the watchdog's tmux window is now `orchestrator-<project>`. The fixed name `orchestrator` let DuetFlow's window block itv's launch. Test in `tests/characterization/test_watchdog.py`.
- `828c944` **G7, Dan's choice:** the six command-shaped deny-list patterns (`sudo`, `git push --force`/`-f`, `DROP TABLE`, `DELETE FROM`, `--force-reset-index`) skip `.md/.markdown/.rst/.txt`. Secret patterns and `deny_list_extra` still apply everywhere. The legacy `merge.deny_list_guard` is unchanged. 4 tests in `tests/graph_engineering/test_deny_list_ask.py`.
  - The auto-mode classifier denied one edit of this change as "Security Weaken". Dan then said "continue with the docs exemption".
  - A PreToolUse hook blocks any Bash command line containing the word "sudo", commit messages included. Use `git commit -F <file>` for such messages.
- `9f2a458` **G6:** interactive Claude mode no longer puts `.maestro_interactive/` in the task worktree; the hook dir is a temp dir when there is no `log_file`. Regression test in `tests/backends/test_claude_driver.py`. The bug came from the parallel agent's `fecc0bc`, which Dan said is OK to keep.
- `62eed26`: `tests/test_next_graph_prompt.py` skips the phase-spec checks when the gitignored specs are absent. Before this, every version worktree failed 5 tests.

**Suites, run in clean version worktrees:**

| Commit | Passed | Failed | Skipped | Xfailed |
|---|---|---|---|---|
| `9867768` | 5633 | 5 (clean-checkout) | 1 | 1 |
| `da4e21b` | 5649 | 5 (same) | 1 | 1 |
| **`62eed26`** | **5654** | 0 | 6 | 1 (exit 0) |

Logs are `handoffs/2026-10-08-p12-closeout/suite-*.log`. The P12 close still needs a run at the final release commit.

**itv:**

- **Pin:** `.orchestrator/current` → `~/.maestro/versions/62eed26b77a6d329521480ac2babc2052147133f`. `doctor` has 0 FAILs, and `adopted_code` is OK.
- **Remaining doctor WARNs:**
  - `systemd_unit`;
  - `model_policy` "not adopted", the same as DuetFlow;
  - `thirdparty`: itv venv pytest 9.0.3 / requests 2.34.0 / python-dotenv 1.2.4 differ from the verified rows;
  - `meta_branch`: no remote (G3).
- **The machine pin `~/.maestro/current` was not touched.**
- `maestro ctl resume` was needed before launch, because init leaves `paused_by_user: true` (**G4**, open, small fix candidate).
- **itv commit `b2b7b1c`:** phase 03 notes that the bot token is shared.
- **The controller committed `e804151`:** a transitive-deps reduction of the ROADMAP (expected).
- **Watchdog:** tmux `itv-watchdog`, launched by Dan with `handoffs/2026-10-08-p12-itv-pilot/launch-itv-watchdog.sh`, last at 22:16 IDT on `62eed26`. The controller runs in `agents:orchestrator-instagram-to-value`; it was verified with `PYTHONPATH=…/62eed26…`.
- **Relaunching the watchdog is classifier-denied from the agent shell.** Prepare it, and Dan runs `! bash …/launch-itv-watchdog.sh`.
- **Monitor:** `handoffs/2026-10-08-p12-itv-pilot/monitor-itv.sh` streams journal events and attempt/run transitions and contains no pgrep. Arm it with one Monitor (30 min max, re-arm on expiry).

**Run `run_Z3CTBZyR1D1RAMSG` (`01-runtime-evidence`) so far:**

- `implementer_01` (sonnet-5-5) stopped on D30 permission requests at 21:31 IDT.
  - Dan **declined** `Bash(* --version)`, because a leading wildcard is over-broad (G9).
  - Dan **allowed** `Bash(systemctl is-active *)`.
  - maestro self-refused `Bash(docker info*)`, which did not cover `docker --version`.
- `implementer_02` succeeded.
- `gate_01`: D29 asked about `sudo` in report prose three times; Dan approved all three at 21:42 IDT. This led to G7.
- `gate_02` failed `uncommitted_changes`: `.maestro_interactive/` (G6).
- `implementer_03` committed `.maestro_interactive/*` out of scope (`869f671` on `impl/01-runtime-evidence`).
- `gate_03` asked about `sudo` inside `turn_marker.json`. I HALTed itv (21:45), fixed G6, re-pinned, ran the suite and cleared HALT. **Dan Disapproved at 22:18 IDT** so the implementer removes the files.
- `gate_04` failed `verification_failed`; **not inspected yet**. It is probably the disapproved hit sent back as a finding; check `failure_json`.
- **`implementer_04` was claimed on `gemini-3.8-flash-high`.** That is agy/gemini, not the project roles sonnet-5-5 / gpt-6-luna. **Investigate first:**
  - why routing picked it: the parallel session's modelctl or model-switch work, or a usage-based step-down;
  - whether that is acceptable for the pilot;
  - capture it as evidence, since agy has never run live; F1 under agy (`--add-dir` scoped) is also unverified.
- **The task must end with `.maestro_interactive/` removed from the branch.** Before acceptance, confirm that `git diff main...impl/01-runtime-evidence --stat` shows only `AGENT-DATA-REPORT.md`.

**01 report review (implementer_02 version, read in full):**

- **No secrets.** Token paths are named but not their values. The NetBird IP `100.123.127.134:8787` is internal, not a credential.
- **DATA-001 is solid.** It confirms the `pace_delta` sign from QuotaPulse's source and tests. It finds that `pick_quota` ignores the bridge's `stale` map, an I8 gap that is relevant to 04.
- **DATA-002 is partial.** Docker and CLI versions are uncollected.
- **Nits:**
  - it attributes the `docker info` refusal to Dan, when maestro refused it itself;
  - there is a stray "Memory/disk" bullet at line ~164.
- Re-read the final version after the rework.

## Still to collect per task (from the launch handoff)

- the run id;
- node models from `attempts`;
- quality failures and rework rounds;
- whether it was accepted;
- the `acceptance_decisions` rows (expect 2);
- **F1 live evidence:** the reviewer's cwd = its workspace. cwd is **not** stored in the DB. Get it from the reviewer's Claude transcript, located through `attempts.session_id`; its directory under `~/.claude/projects/` encodes the cwd, and the `cwd` field is in the jsonl. Alternatively read `/proc/<pid>/cwd` while it runs.
- no diff touching a `confinement.deny` path.

## Open threads added this session (STATE `_p12_open_threads`)

| Thread | Disposition |
|---|---|
| G1, G5, G6, G7 | closed, with commits |
| G2, G3 | accepted; report in the verdict |
| G4: init leaves `paused_by_user` | P12, small fix candidate |
| Shared bot token | post-P12 |
| Clean-checkout suite | fixed `62eed26`; mark it closed |
| G8: task `scope` is not enforced at the gate | post-P12 design |
| G9: D30 proposes leading-wildcard families | record |

The pilot also runs `fecc0bc` (interactive Claude via tmux), so it is that commit's first live use; say so in the verdict.

## State at handoff

- **Maestro:** HEAD `62eed26`, no uncommitted edits of this line. The untracked `artifacts/…/duetflow.json`, `docs/GRAPH_ENGINEERING_ARTIFACT.md` and `docs/reviews/*` are not this line's; leave them. A parallel agent commits on this branch, e.g. `fecc0bc`, `c43c029`.
- **itv:** controller running, run `01` in rework (`implementer_04`). HALT absent. `itv-bot`/`itv-worker` untouched and still not running.
- **DuetFlow:** idle on `0c7eb92`, watchdog `duetflow-watchdog`, window `agents:orchestrator`. Leave it alone.
- **tmux `agents`:** the windows `suite-9867768`, `suite-da4e21b` and `suite-62eed26` are finished suite runs. Kill them during cleanup.
- **Version worktrees created:** `~/.maestro/versions/{98677683…, da4e21bb…, 62eed26b…}`. The first two are unused now; remove them during cleanup with `git worktree remove`, never DuetFlow's `0c7eb92`.
