# Post-extraction task — repair AbuAliArchive's `.orchestrator/state.json`

**Created:** 2026-08-09, by the session that paused the reference loop (see
`handoffs/2026-08-09_pause-reference-loop-and-harden-invariant.md`).
**Do not perform this until the Maestro extraction reports `PROGRAMME-STATUS: COMPLETE` or
`ABORTED` and the operator has explicitly said to proceed.**

---

## Why this is needed

During M0, a characterisation-harness bug (fixed in commit `1b04bb3`/`2d833d1` — see
`docs/PROGRESS.md` M0 findings) caused `write_state` to overwrite the **live**
`/home/dan/projects/AbuAliArchive/.orchestrator/state.json` twice, at 01:57:36 and 01:59:35 local on
2026-08-09, with a minimal placeholder document (`{"version": "1", "in_flight": []}`).

The running loop held the correct state in memory and rewrote the full document on its next cycle, so
the *live process* recovered immediately. But the loop has been idle since (`idle_gated`) and only
writes `state.json` on change, so **the on-disk file itself was never rewritten** and is still the
truncated 412-byte placeholder. It is not corrupt garbage — it's a coherent-but-wrong document that
will cause problems the moment something tries to read `waiting_on_dan` or `dan_id_counter` from disk.

The reference loop is currently **halted** (HALT sentinel present) precisely so nothing touches this
file until it's repaired.

## What's missing from the current on-disk `state.json`

Compared to the pre-incident baseline:

| Key | Pre-incident | Current (on disk) |
|---|---|---|
| `waiting_on_dan` | `{"7": {...P12C manual action...}}` | `{}` |
| `dan_id_counter` | ≥ 7 | `0` |
| `queue_ref` | present | **absent** |
| `blocked_on` | present | **absent** |
| `last_handoff` | present | **absent** |
| `paused_until` | present | **absent** |
| `paused_by_user` | present | **absent** |
| `halted` | present | **absent** |

`phase` legitimately advanced to `LAT1` during normal operation before the incident — that is **not**
damage. Leave it as-is; do not roll it back to whatever an older backup shows.

## Recovery data — sources that survived

This is a **restore**, not a reconstruction — the data needed to fix `waiting_on_dan` and
`dan_id_counter` still exists elsewhere:

- **`AbuAliArchive/.orchestrator/prepared_actions.json`** still contains entries for **`P12C`** and
  **`P10`**. Cross-reference against these when rebuilding `waiting_on_dan`.
- The lost `waiting_on_dan` entry, reconstructed from the pre-incident baseline:
  ```json
  {
    "7": {
      "task_id": "P12C",
      "session_id": "seed-b14-P12C",
      "kind": "manual-action",
      "parked_at": "2026-06-21T10:45:09Z",
      "branch": "",
      "worktree": "",
      "action": "open the Colab notebook, Runtime > Run all, report when Cell 5 prints the P12C COMPLETE line",
      "url": "https://colab.research.google.com/drive/1VuLw4nV5LDt-JNlcf6arjsVAuHfySAiw"
    }
  }
  ```
- `dan_id_counter` must be restored to **≥ 7** so a newly-generated Dan-request id cannot collide with
  id `7` above.
- There are also older backup files present that may help cross-check other fields (present at time of
  writing): `state.json.bak-20260616-125402`, `state.json.bak-eval2resolve`. Treat these as
  cross-reference only — they predate the loop's more recent legitimate state changes (e.g. the
  `LAT1` phase advance), so don't restore from them wholesale.

## What repair should NOT do

- Do not roll back `phase` — `LAT1` is legitimate, not damage.
- Do not restore `in_flight` from any backup blindly; check it against the loop's journal for
  whatever is actually running/parked at repair time (state may have moved on further during the
  pause).
- Do not touch anything else under `.orchestrator/` beyond `state.json`.

## Unparking

**TUNE2 and LAT1** both failed and are parked (`failed_escalated`, one with
`diagnose_skipped_usage_limit`) from 22:57–22:59Z on 2026-08-08, the same window the Maestro chain
started consuming the shared Claude account. Every journal entry since has been `idle_gated: all
gated: TUNE2,LAT1` — the loop has done nothing productive since. **Unparking these is part of this
repair task, not something to do now.**

## Suggested repair procedure (for whoever picks this up)

1. Confirm `docs/PROGRESS.md` in `~/projects/maestro` says `PROGRAMME-STATUS: COMPLETE` or `ABORTED`.
   If `ABORTED`, get explicit operator sign-off before touching AbuAliArchive at all — the abort may
   be unrelated to this repair being safe.
2. `cd /home/dan/projects/AbuAliArchive` — confirm `HEAD` is still `68056b5` and `git status
   --porcelain` shows only the known baseline entries (see the verification list in the pause
   handoff). If not, stop and investigate before touching state.
3. Back up the current `state.json` before editing it (e.g. `cp state.json
   state.json.bak-pre-repair-$(date +%Y%m%d)`).
4. Reconstruct `waiting_on_dan`, `dan_id_counter`, and the other absent keys per the data above,
   preserving `phase: LAT1` and whatever `in_flight`/`parked_tasks` the loop's journal shows as
   current at repair time.
5. Validate the resulting JSON against whatever schema/loader `orchestrator_run.py` uses for
   `state.json` before removing HALT.
6. Only then: unpark TUNE2 and LAT1, remove the HALT sentinel, confirm the orchestrator relaunches
   cleanly and the watchdog stops crash-looping.
7. Update `docs/FOUND_BUGS.md` / a fresh handoff noting the repair is done, with before/after
   `state.json` diffs as evidence.
