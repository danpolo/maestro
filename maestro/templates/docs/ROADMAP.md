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

## Example (commented out — not a real task)

```yaml
# id: example-001
# status: open
# mode: autonomous
# deps: []
# self_modifying: false
# hold: false
# title: "One-line description of the deliverable"
# notes: >
#   Whatever an implementer needs that doesn't fit the fields above.
```
