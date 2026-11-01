# P13 W9 safety and gate checks

W9 qualifies an isolated candidate. Integration, release and native policy activation
are separate actions. Context Gate remains unbuilt.

## Task contracts before dispatch

The routed graph runner reads the controller's canonical task spec before admission,
manifest assembly, routing or claim. It refuses absent or unreadable required checks,
including an automatic check with no `cmd`. Required `manual` reviewer judgments and
`dan` observations remain valid; they need no executable command. Unrouted synthetic
runners retain their low-level contract.

A refusal creates no attempt, worker or task-worktree lease. Its reason is in the tick
report, a deduplicated `PreValidationRefused` store event and the operator journal.
The node remains unclaimed; correct the task contract and restart/recompile the run.
This is a configuration refusal, not an implementer quality failure.

## Scope findings at the verification gate

The controller builds the scope contract from ROADMAP fields; worker reports cannot
grant an exception. For a task with a declared `scope`, the committed diff against the
run base must fit it. Bare directories (including a trailing slash) include descendants;
`*` stays in one directory and `**` crosses directories. Deleted paths and both sides
of a rename count. A failed or unavailable diff blocks with `scope_diff_unavailable`.
An omitted/empty scope preserves the historical unrestricted task contract.

The gate screens scope before a manual `post_action_cmd`, ordinary required checks or
deny-list questions, and checks again if the post-action command commits changes.
`out_of_scope` findings use the existing `verification_failed` rework path and are in
the next implementer brief; they never become a Dan deny-list question.

Dan approved two exceptions:

- The task author can declare `scope_exceptions: [{path: tests/support.txt, reason:
  supporting fixture for V1}]` in the ROADMAP. Every entry must have a path and a
  nonempty reason. Invalid entries refuse before launch.
- Blocking reviewer findings may widen scope for rework. Exact repository paths are
  allowed; a basename of an existing file resolves only if unique in the run-base
  snapshot, matching the manifest's rule. An ambiguous basename grants nothing.
  An explicit new file named by a blocking reviewer can also be created. Use backticks
  for a new bare extensionless filename (for example, `Dockerfile`) to distinguish it
  from prose. Invalid external references such as `../other/pkg.py` grant nothing.
  These grants
  match exact paths, not directory or wildcard prefixes.

Scope gates the final committed diff; it is not an OS boundary for reads or temporary
writes. The existing uncommitted-tree guard still applies after a manual finalizer.

## Command families and legacy Claude delivery

New leading-wildcard Bash proposals are refused by both screening and storage; use a
named executable/operation family. Historical seeded absolute-venv wildcard families
remain readable for compatibility. The permission brief forbids `Bash(* --version)`
and explains why `Bash(docker info*)` cannot cover `docker --version`.

The brief explains that a dontAsk denial is only for the attempted command family,
names `Bash(git mv *)` as an example, and requires the exact denied command/family in
reports. The vendor's denial string itself remains vendor-owned.
This explanatory brief applies to graph agent workers; legacy tmux implementer briefs
retain their historical wording. The legacy flags below still enforce the lists.

Claude tmux fresh/resume paths, both headless and interactive, now receive the global
allow/never-allow lists, tree/workspace Edit roots and `dontAsk`. Completion calls and
graph workers retain their existing explicit policies. Quoted permission families
survive generated launcher rendering. No live settings file is changed by W9.
Pre-existing launcher path literals still fail for workspace paths with apostrophes;
actual newlines in raw argument values are also unsupported. That separate quoting
cleanup is deferred; W9's quoted permission-family regression covers ordinary arguments.

## Staged Codex and agy lists

`permissions.harness_policy(harness, cwd=..., add_dirs=..., writable=...)` builds
inspectable native lists from the same global source. Every source rule appears in
`translated_allow`/`translated_deny` or `untranslated_allow`/`untranslated_deny`.
The result declares bounded `write_roots` and `activation_ready=False`.
`permissions.codex_rules(policy)` renders the supported Starlark token-prefix subset.
agy uses action targets (`command`, `read_file`, `write_file`); its literal recursive
file roots are different from Claude path globs. These exports do not write settings
or replace the live Codex/agy launch flags. agy is still inactive.

Offline installed Codex checks measured:

| Command tokens | Native subset result |
| --- | --- |
| `git status --short` | allow |
| `sudo whoami` | forbidden |
| `git push --force origin main` | no matching rule |
| `git push origin main --force` | no matching rule |
| `cat .env` | allow; secret denial is residual |
| `docker --version extra` | allow; exact source rule became a token prefix |

The native subset cannot enforce the entire source policy. W4/any future launch
activation must enforce residual deny rules, preserve allow semantics (including
exact-vs-prefix differences and shell decomposition), and confine writable roots
with the planned sandbox plus post-run audit. Headless agy asks can soft-deny with
exit 0, so sentinel/result validation remains necessary. No agy model call was used
for W9 qualification; its export shape has unit coverage, not live enforcement proof.

Current documentation consulted through Context7: Codex execpolicy README
(`/openai/codex`, `codex-rs/execpolicy/README.md`), and agy's
<https://antigravity.google/docs/cli/permissions> and
<https://antigravity.google/docs/cli/headless.md>. Local CLI help and the offline
native checker supplement the documentation; no model turn is required.

## Interpreter secret-read limit

Allow/deny command lists inspect tool names, command tokens and literal paths. An
allowed interpreter can construct a secret path in code without naming it in the
command. The fixture `python3 -c "print(__import__('pathlib').Path('.en' +
'v').read_text())"` reads a synthetic `.env` even though an explicit `Read(.env)`
is denied. This is a tested limitation, including Claude's existing lists. A command
list does not provide an OS read sandbox. Stronger read isolation is a separate
confinement design, not a promise added by W9.
