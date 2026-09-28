# Handoff: D30. Agents run on allow/deny lists, not the auto-mode classifier; Dan approves new command families on Telegram

This file is the whole prompt. Dan designed this on 2026-09-28 (session s3y) after the Claude CLI auto-mode
classifier outage killed 06 run 5 (D4) and the final run of 06 run 7. **Your job:** build D30 (TDD, lanes, full
suite) in a maestro worktree. Adopt it into DuetFlow **only after run 8 has ended** (see §2).

## 0. Read first
- `docs/graph-engineering/STATE.yaml`:
  - `Pilot finding D30`: Dan's decision.
  - `Pilot finding D4`: the outage evidence.
  - `Pilot finding D26`: the ask-Dan machinery to reuse.
  - `Pilot finding D29`: the newest example of a Telegram request with buttons, a recommendation, and a hold that
    spends no budget. It is the closest template for this feature.
- The D29 code, to copy its shape:
  - `maestro/workflows/deny_list.py`
  - `maestro/hitl/verify.py`: `render_deny`, `poll_deny`, `deny_records`, `DENY_TYPE`
  - `WorkflowRunner._ask_deny` / `_poll_deny_rulings`
  - `tests/graph_engineering/test_deny_list_ask.py`
- Memory: `agent-permission-lists-not-classifier` (Dan's decision), `agents-ask-dan-on-spec-gaps`,
  `harness-auto-updates-must-not-break-gates`, `talk-in-local-time`, `do-cleanup-yourself`, `agent-scratch-not-in-tmp`.

## 1. Dan's decision (2026-09-28)
- **No classifier.** Maestro's Claude agents stop using auto mode. They run with `--permission-mode dontAsk`, which
  auto-denies anything not allowed and never calls the classifier, plus explicit lists: `--allowedTools` and
  `--disallowedTools` (`Bash(git *)` syntax).
  - Verify every flag against the installed `claude --help` (2.1.283 today) and the official docs; the CLI updates
    constantly. `--permission-prompts none|host` and `--permission-prompt-tool` also exist.
- **Escalation.** When a call is denied and the agent thinks it should be allowed, the agent proposes **the broadest
  command family that covers the call and that it believes should be allowed** (not the single command). Maestro
  sends Dan on Telegram:
  - the denied command;
  - the proposed family;
  - the agent's reason;
  - **Allow / Decline** buttons.

  On Allow, Maestro adds the family to the allow list and the agent continues. On Decline, the agent is told, and
  works around it or fails normally.
- **Scope decisions (Dan picked the recommended option on each):**
  - **Claude only for now.** Codex has a different sandbox and approval system, so it comes later; record that.
  - **The allow list is global, for all projects**, in one file under `~/.maestro/`, and seeded with the routine
    families agents already use: pytest, git add/commit/diff/status/log, file edits inside the worktree, and so on.
    Derive the seed from real `impl.log`s in `duetflow/.orchestrator/archive/*/attempts/`, not from guesses.
  - **A fixed never-allow list** is refused outright and never sent to Dan. It covers: sudo, force-push, reading
    token, `.env` or secret files, `rm -rf` outside the worktree, and over-broad families (`Bash(*)`, `Bash`, `*`).
    Keep it consistent with `merge._HARD_DENY_PATTERNS`.
  - Dan's expectation: "if this works I think option 3 is not needed". Option 3 was D4's retry-on-classifier-outage
    proposal. Once D30 works, mark that proposal superseded in D4. The D4 fix for "no sentinel → uncertain" is
    separate and still stands.

## 2. Coordination (another session is live)
- **Session s3z** (`handoffs/2026-09-28-graph-p12-s3z-run8-watch.md`) watches DuetFlow 06 **run 8**, which started
  15:18 IDT on c40a89d.
  - It owns DuetFlow and STATE's D28/D29 entries.
  - **Do not touch DuetFlow, and do not adopt, until run 8 has accepted or parked and DuetFlow is HALTED.** Ask Dan
    or check `.orchestrator/HALT` plus the journal.
  - In `docs/graph-engineering/STATE.yaml` (git-ignored, shared), edit only D30 and D4's proposal. Re-read the file
    immediately before each write.
- Work in a maestro worktree on persistent storage (e.g. `/home/dan/projects/maestro-wt/d30`), never /tmp.
  - `docs/graph-engineering` is git-ignored. Symlink it in and exclude it via `.git/info/exclude`, as s3x did.
  - `git pull`/rebase onto `feat/graph-engineering-foundation` before integrating, because s3z may commit archive
    exports.

## 3. In scope
1. **Find how agents are launched today.**
   - Look at `maestro/backends/claude.py` (its docstring discusses `--dangerously-skip-permissions`), the worker and
     routing argv, and where auto mode comes from. It is probably Dan's global `~/.claude/settings.json`
     `defaultMode`, not a Maestro flag.
   - Design the smallest change that makes Maestro's agents explicitly `dontAsk` + lists.
2. **Choose the escalation mechanism**; it is your design call.
   - (a) A `--permission-prompt-tool` MCP tool that blocks while Dan decides. That is live, but a long block
     inside an agent process can hit timeouts.
   - (b) The brief tells the agent to write a `PERMISSION_REQUEST` file (denied command, family, reason) and finish.
     Maestro holds the node like a D26/D29 ask, spending no budget, and on Allow re-runs or resumes the node with the
     updated list.

   Prefer whichever survives an agent timeout. D29's hold/resume is proven.
3. **Build it test-first:**
   - the family matcher;
   - the never-allow refusal, which is never sent;
   - the Telegram render with buttons;
   - Allow → list updated + journal/event + the agent continues;
   - Decline → the agent is told;
   - an over-broad proposal → refused;
   - lanes named as behaviours.

   Run the full suite from the main checkout: `.venv/bin/python -m pytest` (no `-q`). Baseline is **5127 passed + 1
   xfailed**.
4. **Integrate** (fast-forward, remove the worktree and branch yourself). Then amend the manifest (`code_under_test`,
   reason D30; the note says no criterion or weight change). **After run 8 ends and DuetFlow is halted**, adopt and
   run doctor (expect OK + 2 known WARNs).
5. **Prove it live.**
   - Probe: a real `claude -p --permission-mode dontAsk --allowedTools ...` call can run an allowed command, and it
     is denied on a non-allowed one without any classifier involvement.
   - On the next DuetFlow run, grep `impl.log`s for "classifier": expect 0 hits.
6. **STATE:** close D30 with the evidence, and mark D4's retry-on-outage proposal superseded if D30 holds.

## 4. Out of scope
- Codex and Antigravity permission lists (record them as follow-ups).
- The Context Gate (AGENTS.md).
- The D4 uncertain-node fix.

## 5. Report back
The suite count, the sha, the seed allow list (its size and source), the never-allow list, the mechanism chosen and
why, the live probe output, and the first live Telegram permission ask (text, Dan's tap, time to answer), if one
happened.
