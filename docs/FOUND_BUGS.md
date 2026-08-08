# Found bugs

Behavioural surprises discovered in the reference implementation while characterising it.

**Nothing here is fixed.** Through M1 the extraction is a pure refactor: every surprise below is
copied across verbatim and pinned by a characterisation test, so the extracted code fails in exactly
the same way. Fixing any of them is separate, later, deliberate work.

Format: one entry per surprise, with the test that pins it.

---

## M0 — state and journal (`state.py`)

### 1. `read_json` and `read_state` have opposite failure contracts

`read_json` wraps everything in `except Exception: return {}` — a missing file, a corrupt file, a
directory and a permission error are all indistinguishable from a legitimately empty document.
`read_state`, three lines below it, has no error handling at all: a missing `state.json` raises
`FileNotFoundError` and a corrupt one raises `JSONDecodeError`, uncaught, into the poll loop.

Two adjacent loaders in the same module, opposite contracts, no comment explaining why.

Pinned by `test_read_json_missing_returns_empty_dict`, `test_read_json_directory_returns_empty_dict`,
`test_read_state_raises_when_the_file_is_missing`, `test_read_state_raises_on_malformed_json`.

### 2. `read_state` defaults six keys but not `version` or `in_flight`

The six bookkeeping keys (`hitl_mode`, `waiting_on_dan`, `dan_id_counter`, `retry_counts`,
`parked_tasks`, `launch_times`) get `setdefault`s. The two keys every consumer actually depends on
do not, so a truncated or half-initialised state document fails with a `KeyError` deep inside a
consumer rather than at the loader.

Pinned by `test_read_state_injects_six_defaults`,
`test_read_state_does_not_default_version_or_in_flight`.

### 3. `write_state` mutates its argument

`state["updated_at"] = now_iso()` is applied to the caller's dict before any I/O, and stays applied
even if the write then raises. A function named `write_*` reads as a pure sink.

Pinned by `test_write_state_mutates_the_callers_dict`.

### 4. `write_state` does not create its parent directory

`tmp.write_text(...)` raises `FileNotFoundError` if `.orchestrator/` does not exist. There is no
`mkdir(parents=True, exist_ok=True)`, so the state layer cannot initialise itself on a fresh
checkout — something else must have created the directory first.

Pinned by `test_write_state_requires_an_existing_parent_directory`.

### 5. `write_state`'s temp file is not collision-free, and is not fsynced

The temp path is `STATE_JSON.with_suffix(".json.tmp")` — derived from the target name, with no pid
or uuid component. Two concurrent `write_state` calls therefore share one temp path and can
interleave. Neither the file nor the directory is fsynced before or after the rename, so the
tmp+rename pattern signals a durability guarantee it does not provide, and a crash between the write
and the rename leaves an orphan `state.json.tmp`. The reference project's `.orchestrator/` currently
contains four orphaned `usage.json.tmp<pid>` files, so this does happen in practice.

Partially pinned by `test_write_state_leaves_no_temp_file_behind` (the happy path).

### 6. `_persist_launch_time` is an unlocked read-modify-write of the whole document

It re-reads `state.json` from disk, sets one key, and writes the entire document back. Any unsaved
in-memory edits the caller was holding are silently discarded, and any concurrent writer's changes
made between its read and its write are clobbered.

Pinned by `test_persist_launch_time_rereads_from_disk_and_discards_unsaved_edits`.

### 7. `write_state` writes in the platform default encoding

`Path.write_text()` is called with no `encoding=` argument, while `append_journal` ten lines later
explicitly passes `encoding="utf-8"`. Both pass `ensure_ascii=False`, so non-ASCII content in
`state.json` round-trips correctly only for as long as the process locale is UTF-8. The journal is
hardened against this; the state file is not.

Not separately pinned — asserting it would require running the suite under a non-UTF-8 locale.

### 8. `append_journal` grows without bound and hardcodes the agent name

No rotation, no size cap; the live journal is already ~7.3 MB, and two call sites read it back by
scanning the whole file, so the cost of those lookups grows without limit. The `agent` field is a
hardcoded string literal naming a specific phase of the consuming project — a purity problem for
M2+, since it is copied verbatim into maestro at M1.

Pinned by `test_append_journal_stamps_a_constant_agent`, which asserts the field is constant and
non-empty without importing the literal into maestro.

### 9. `read_json` is annotated `-> dict` but returns whatever JSON contained

A file holding a top-level list, string or number is returned unchanged with that type.

Pinned by `test_read_json_returns_non_dict_json_unchanged`.

---

## M0 — project.yaml (`config.py`)

Characterised but not yet pinned by tests — `test_config.py` lands with the `config` extraction.

### 10. A corrupt `project.yaml` silently disarms the merge safety gates

`_load_project_yaml` is `try: ... except Exception: return {}`. A missing file, a YAML syntax error,
a permission error and an intentionally empty config all produce the same `{}` with no exception, no
log line, no journal entry and no print.

The callers are security gates. With `{}`: the deny-list guard loses every `deny_list_extra` pattern,
the risky-file reviewer short-circuits with `return True, "no risky_set defined"` and skips its
safety review entirely, and the resumable-merge hard-stop loses its risky list. A one-character YAML
typo downgrades the merge gates to permissive with no signal that config loading failed.

### 11. `_load_project_yaml` does not cache, so one decision can read several configs

Four call sites each re-open and re-parse the file. Within a single merge decision the config is
loaded independently at each gate; if `project.yaml` is edited — or is mid-write and truncated —
between those reads, different gates in the same merge evaluate against different configurations.

### 12. Config defaults live at the call sites and disagree with each other

`secrets` defaults to `[".env", ".env.local"]` at one call site while `risky_set` defaults to `[]` at
another, so a missing config still blocks secret-file edits but fully disables the risky-file review.
`bot_files` is defaulted independently in two places, duplicating the value with no single source of
truth.

---

## M0 — test harness (not reference code)

### 13. The planned monkeypatch list named globals that do not exist

`docs/plans/2026-08-09-m0-m1-core-extraction.md` specified repointing `ORCH_DIR`, `STATE` and
`USAGE`. The reference module has none of those names: the globals are `STATE_JSON`, `USAGE_JSON`,
and there is no directory-level global at all. Combined with `monkeypatch.setattr(..., raising=False)`
the mismatch was silent — three unused attributes were created, `write_state` kept resolving the real
`STATE_JSON`, and the harness test overwrote the **live** `state.json` of the running reference
project. The running loop's own read-modify-write restored it within seconds and no data was lost
(its journal shows a full `in_flight` immediately afterwards), but the harness was one unlucky
interleaving away from destroying production state.

Not a reference-code bug — a plan bug, recorded here because it is the most dangerous thing found so
far. Fixed at source: `sandbox` now discovers path globals by reflection instead of from a list, and
`tests/reference_repo.py` installs a session-wide firewall that makes any mutating filesystem call
inside the reference repo raise. See `tests/test_reference_repo_firewall.py`.
