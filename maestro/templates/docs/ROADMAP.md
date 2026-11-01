# Roadmap — `{{ project_name }}`

This is the **machine-readable** task queue. `maestro.docs.roadmap` parses it by
regex-extracting every fenced ` ```yaml ` code block in this file — nothing else
here is parsed, so everything outside a ```yaml fence (like this paragraph) is free
for humans to write however they like.

A fresh `maestro init` scaffold starts with **zero tasks** on purpose (see the
"empty roadmap" no-op supervised loop check in `docs/plans/2026-08-16-m4-setup.md`).
The example below is commented out — every line starts with `#`, so it parses as a
YAML comment (`yaml.safe_load` returns `None` for it) and the parser skips it. Copy
it, strip the leading `# `, and fill in a real task to add your first one.

## Task block schema

Each task is one ```yaml fenced block with these keys (`maestro/docs/roadmap.py`
is the source of truth — this table mirrors what it actually reads):

| Key | Meaning |
|---|---|
| `id` | Required. Stable string/number id; everything else references it by this. |
| `status` | `complete` removes it from consideration and marks its id done for `deps` purposes elsewhere. Omit while a task is still open. |
| `mode` | `autonomous` (orchestrator launches it directly) or `needs-dan` (auto-normalised to `dispatch: manual` if `dispatch` isn't set explicitly — see below). |
| `dispatch` | `manual` marks a task Dan must perform himself; it is never auto-launched, but *is* auto-prepared (`parse_prep_tasks`) once its deps are satisfied. |
| `self_modifying` | `true` excludes it from auto-launch entirely (never run unattended against maestro's own code or this project's orchestration setup). |
| `hold` | `true` is a human hold — excluded from auto-launch until cleared, independent of deps. |
| `deps` | A list (or single string) of other task `id`s that must all have `status: complete` first. |
| `scope` | Every file, directory or glob the task will **write**, tests included. Tasks whose scopes overlap run one after another, never in parallel (parallel tasks must merge trivially). Omitting `scope` serialises the task against everything; `scope: []` declares it writes nothing. |
| `verifications` | What "done" means, as a list of checks (below). A task with none cannot be accepted. |
| `questions` | Setup prerequisites asked through the existing question channel before dispatch, including for manual tasks. Answers appear in the prep brief. |

### Verifications

Each entry has an `id` and a `kind`:

- `kind: auto` — `cmd` is run in the task's tree; it passes on exit 0, or when its last stdout
  line equals `expect` if one is given. The default stage is `pre_merge` (gate and staging).
  Set `stage: post_merge` for commands requiring deployed code: Maestro runs them on the
  integrated project checkout and retains their evidence. Failure automatically queues
  a repair task, without an operator ask or replaying the original merge.
- `kind: dan` — a check only Dan can confirm. `check` says what must hold (third person: it is
  the record of what was verified). When Dan must *do* something, give `steps:` — imperative
  lines addressed to him, each optionally with the command to `run:`; the request numbers them
  under "Dan action needed". Free-form `instructions` (one block) still work when there are no
  `steps`. `{worktree}` becomes the integrated project's path. Under
  `engineering.runner: graph` observations follow merge and use Pass / Fail on Telegram
  (`/verify <id> pass` or `/verify <id> fail <reason>`). Failure queues a repair task.
  Actual merge-review approval remains a separate Approve / Reject step before merge.
- `kind: manual` — a reviewer-judged check (legacy).

### Manual prep result

The prep brief includes each exact verification command and declared path. A shell
action declares `step_command`, `step_check_cmd` (an unprivileged result check), and
`expected_result`. Maestro writes the wrapper outside the task tree and shows its
invocation, underlying command, and expected result. Done means only “I ran it”:
the gate checks captured output, exit status, attempt/source binding, then the step check.
Legacy shell `dan_action` values use the same wrapper and require the check/expected fields.
For a URL or other non-shell action, set `step_kind: external` and declare the same
check/expected fields; the check must validate the resulting artifact or external state.
The gate retains that check's actual output and exit, without inventing a shell receipt
for the GUI action. A finalizer in `post_action_cmd` also retains output and exit evidence.
Integration can finish while deferred checks remain; final acceptance requires all of them.

## Example (commented out — not a real task)

```yaml
# id: example-001
# status: open
# mode: autonomous
# deps: []
# scope: [src/example.py, tests/test_example.py]
# self_modifying: false
# hold: false
# title: "One-line description of the deliverable"
# notes: >
#   Whatever an implementer needs that doesn't fit the fields above.
# verifications:
#   - id: V1
#     kind: auto
#     cmd: ".venv/bin/python -m pytest -q tests/test_example.py"
#   - id: V2
#     kind: dan
#     check: "The settings page shows the new toggle and it persists after reload"
#     steps:
#       - do: Start the app
#         run: cd {worktree} && make serve
#       - Open http://localhost:8000/settings, flip the toggle and reload
#       - Check the toggle kept its new position
```
