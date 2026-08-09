# Handoff — Pause the reference project's loop, harden the invariant, let the extraction finish

**Date:** 2026-08-09
**Context:** The overnight Maestro extraction damaged the reference project (AbuAliArchive) and
starved it of Claude quota. The operator's decision: **stop AbuAliArchive entirely until the
extraction is finished**, and **make no changes to that project whatsoever**.

---

## Goal

1. **Pause AbuAliArchive's autonomous loop completely.** Zero Claude quota may be spent on it until
   the Maestro extraction is done. It is currently idle but still polling and still able to launch
   implementers.
2. **Harden Maestro's abort condition** so a repeat of last night's incident stops the build instead
   of being reasoned past.
3. **Let the extraction chain keep running.** It is healthy and mid-M1.
4. **Record — do not perform — the `state.json` repair**, so it can be done after the extraction.

## Read first

| Path | Why |
|---|---|
| `~/projects/maestro/docs/PROGRESS.md` | M0 Findings section documents the incident in the agent's own words. Read the two ⚠️ subsections before anything else. |
| `~/projects/maestro/docs/EXECUTION.md` | Process SoT. The "Abort conditions" block is what you are amending. |
| `~/projects/maestro/docs/DESIGN.md` | Design SoT. Background only; do not relitigate §2. |

Do **not** read `AbuAliArchive/scripts/orchestrator_run.py`. It is 4,567 lines and nothing here needs it.

---

## What happened (summary — detail is in `PROGRESS.md`)

The characterisation harness monkeypatched module globals that don't exist in the reference module
(`ORCH_DIR`/`STATE`/`USAGE` vs the real `STATE_JSON`/`USAGE_JSON`), with `raising=False`, so the
mismatch was silent. `write_state` kept resolving the real path and **overwrote the live
`.orchestrator/state.json` twice** (01:57:36 and 01:59:35 local).

The overnight agent detected this, fixed it structurally (reflection-based sandbox + a session-wide
write firewall, 11 tests), and flagged it — good behaviour. **But its conclusion that no data was lost
is wrong.** Two consequences it missed:

1. **`state.json` is still the truncated 412-byte document.** It was never rewritten, because the loop
   only writes on change and has been idle since. Lost relative to the pre-incident baseline:
   `waiting_on_dan` (was `{"7": P12C manual action}`, now `{}`), `dan_id_counter` (was ≥7, now `0`),
   and the keys `queue_ref`, `blocked_on`, `last_handoff`, `paused_until`, `paused_by_user`, `halted`
   are absent entirely.
2. **TUNE2 and LAT1 both failed and are parked.** At 22:57–22:59Z both launched, died within ~33s with
   `window gone`, and hit `failed_escalated`; one logged `diagnose_skipped_usage_limit`. The Maestro
   chain started at 22:55Z on the same Claude account. Every journal line since is
   `idle_gated: all gated: TUNE2,LAT1` — seven hours of nothing.

The abort condition didn't fire because it is worded as "files appearing in `git status`", and
`.orchestrator/` is gitignored. That gap is task 2 below.

---

## In scope

**1. Halt the reference loop**
- Create the HALT sentinel: `/home/dan/projects/AbuAliArchive/.orchestrator/HALT`.
  This is the sanctioned, no-sudo pause mechanism.
- Confirm the orchestrator process exits and that **no `claude` process attributable to
  AbuAliArchive remains**.
- The watchdog crash-looping while HALTed is **expected and normal** — do not try to fix it, and do
  not stop the systemd unit.
- Note in `PROGRESS.md` that the reference loop is paused, with the timestamp.

**2. Harden the Maestro abort condition**
- In `~/projects/maestro/docs/EXECUTION.md`, amend the abort conditions so the check is not limited
  to `git status`. It must also cover the reference project's **runtime state**: `.orchestrator/`
  contents, in particular `state.json` size and mtime.
- Add an explicit rule: **any write to the reference project's runtime state is an immediate abort**,
  regardless of whether git notices, and regardless of whether damage appears reversible.
- Also amend the "loop-owned files" carve-out the overnight agent added (`docs/UPCOMING.md`,
  `docs/dependency_map.md`, `docs/dependency_map.png`). That reasoning was correct at the time, but
  with the loop halted those files can no longer change on their own — so from now on **any**
  modification there is an abort.

**3. Record the repair as a post-extraction task**
- Create `~/projects/maestro/handoffs/POST-EXTRACTION-repair-reference-state.md` capturing the
  recovery data below. **Do not perform the repair.**

**4. Monitor the chain**
- Confirm it is still progressing. If `PROGRAMME-STATUS` is `ABORTED`, report why and stop.

### Recovery data to preserve (for the post-extraction repair)

Sources that survived and make the repair a restore rather than a reconstruction:
- `AbuAliArchive/.orchestrator/prepared_actions.json` still contains **`P12C` and `P10`**.
- The lost `waiting_on_dan` entry, from the pre-incident baseline:
  ```
  id 7 → task_id: P12C, session_id: seed-b14-P12C, kind: manual-action,
         parked_at: 2026-06-21T10:45:09Z, branch: "", worktree: "",
         action: open the Colab notebook, Runtime > Run all, report when Cell 5
                 prints the P12C COMPLETE line
         url: https://colab.research.google.com/drive/1VuLw4nV5LDt-JNlcf6arjsVAuHfySAiw
  ```
- `dan_id_counter` must be restored to **≥ 7** so new Dan-request ids don't collide.
- `phase` legitimately advanced to `LAT1` during normal operation — that is **not** damage; leave it.
- TUNE2 and LAT1 need unparking as part of the repair, not now.

## Out of scope — do not do these

- **Any change to AbuAliArchive's tracked files.** No code, docs, config, or commits. `HEAD` must
  remain `68056b5` and `git status --porcelain` must not gain entries. The HALT sentinel is the one
  permitted filesystem write, and it is gitignored.
- **Repairing `state.json` now.** Deferred until after the extraction, by operator decision.
- **Resuming the loop, unparking TUNE2/LAT1, or removing the HALT sentinel.**
- **Touching the extraction work itself** — the chain owns `maestro/`, `tests/`, and the plan.
  Editing files it is mid-flight on will corrupt its session.
- Stopping or reconfiguring the `abuali-watchdog` systemd unit.
- Any `sudo`.

## Conflict to resolve on the way in

`scripts/run_overnight.sh` ends with a safety net that **deletes a HALT sentinel** it finds and warns
if the reference orchestrator isn't running. That was correct when the loop was meant to stay up; it
now directly contradicts this handoff. **Remove or invert that block before the chain's current
session ends**, or the driver will silently un-pause the loop on exit.

---

## Verification — report these exact numbers

1. `ls -la /home/dan/projects/AbuAliArchive/.orchestrator/HALT` → exists
2. `pgrep -f orchestrator_run.py` → **no output** (loop stopped)
3. `pgrep -af "claude" | grep -i abuali` → **no output** (zero quota being spent)
4. `cd /home/dan/projects/AbuAliArchive && git log --oneline -1` → still `68056b5`
5. `cd /home/dan/projects/AbuAliArchive && git status --porcelain` → the 5 known untracked files, plus
   at most the 3 loop-owned doc files; **nothing else**
6. `stat -c '%s' /home/dan/projects/AbuAliArchive/.orchestrator/state.json` → still `412`
   (unchanged — proof you did not repair it)
7. `grep -m1 PROGRAMME-STATUS ~/projects/maestro/docs/PROGRESS.md` → value, and current stage
8. `cd ~/projects/maestro && .venv/bin/python -m pytest -q 2>&1 | tail -2` → passed/skipped counts
9. `grep -n "HALT" ~/projects/maestro/scripts/run_overnight.sh` → shows the safety-net block is
   removed or inverted

## Suggested skills

- `superpowers:verification-before-completion` — this handoff turns on evidence, not assertion. The
  overnight failure was precisely a confident "no data was lost" that wasn't checked.
- `lean-ctx` — use `ctx_*` tools throughout; native Grep/Glob are denied by policy here.
