# Native worker contracts

## Antigravity task instructions

Direct `AGENTS.md` and `GEMINI.md` files are **LIVE**. Stock native discovery keeps
their directory scope; edits are visible to later native reads. A writer can edit
them within its task write grants. A reader has no task-tree write grant. Maestro
does not replace these direct task files with frozen copies or flatten their scope.

Global instruction surfaces and the supported discovery aliases (`.agents`,
`.agent`, `_agents`, `_agent`) use immutable per-invocation snapshots. Shared-library
aliases and retained rule includes are rewritten into those snapshots. These frozen
execution-profile differences must not be committed as task edits; stage exact task
paths and inspect the staged diff. Ordinary task-file edits still face the parent
clean-tip and exact-ref publication audit.

This is Dan's approved live-rule contract (2026-10-18, choice 2a). Native discovery
evidence is retained separately from filesystem/editability and prompt-delivery
tests; passing those tests alone does not qualify every native discovery behavior.

## Discovery mount stability

Discovery directories are masked before a worker runs. Removing or replacing a
discovered mount target during preparation/launch can cause a safe refusal before
native execution. No unconfined retry or broader write grant follows that refusal.
Codex tool-sandbox startup can briefly create and remove `/tmp/.codex` and
`/tmp/.agents` on this host. Current qualification uses dedicated `/tmp` fixtures
and host-only execution while namespace gates run. `/var/tmp` placement remains
unqualified: retained measurements show substantially slower fsync/preparation.
The original failed discovery run's exact target and timing were not traced;
reproducing removal-after-pin refusal does not establish that original cause.

## Stock authorization and retained qualification limits

Codex command approvals unwrap only literal system shell paths under `/bin` or
`/usr/bin`. A relative shell or a task executable named `bash`, `sh`, `dash` or
`zsh` never inherits authorization from its script argument. Protocol regression
tests cover this rule; native custom-shell model reachability was not established.

Maestro Codex clients serialize the lifetime of the existing file login, including
namespace teardown. Unrelated stock clients do not participate in that mutex and
can still race, as accepted in Dan's choice1a. No alternate login is substituted.

On the qualified agy1.4.2 fixture, advertised `ask_permission` returned unknown-tool
before any guard call. An earlier escalation request was refused by the model
before any native tool call. These two enforcement paths remain **UNQUALIFIED**;
neither is exercised permission-guard proof. Actual outside-write, hook-failure,
nonempty-input and crash/restart evidence is retained separately with its limits.
Disposable native graph proofs are not a live pilot or an aggregate model acknowledgment.

## Pending native reports and controller expiry

Worker-authored DONE and FAILED reports remain provisional while native Git
publication is pending and the window or trusted transaction owner is alive.
Both reconciliation and the main completion loop enforce the controller ceiling
even when a worker has already written FAILED. A stop attempt alone does not
authorize retry, task-tree removal or settlement. Timeout failures retain their
controller reason and take the existing park path after the worker has stopped.
The timeout outcome is durably recorded in the sealed native Git call directory,
independent of worker-writable reports and subsequent promotion. A late promotion
cannot turn that attempt into success, including after controller restart. FAILED
report updates use the bounded, nonsymlink, single-link control-file boundary;
refusing an alias still stops the worker and parks from the trusted timeout reason.
Dead-owner recovery checks both journal and lease records across a torn write;
it never adopts an unpromoted result as success.
