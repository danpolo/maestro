# Prepared design — filesystem confinement for implementer sessions

**Status: PREPARED, NOT IMPLEMENTED.** Written 2026-09-02 at the operator's direction, so that the
fix exists in worked-out form the day it is needed rather than being designed under pressure.

**Decision on record:** `Capabilities.sandbox` stays as it is — declared honestly by both drivers,
read by nothing in core, and tracked by the strict `xfail` in
`tests/test_no_dead_backend_surface.py`. That marker fails loudly the day anything reads the
capability, which is what forces this document to be revisited rather than forgotten.

## Why it is deferred rather than built

Implementers already run in per-task **git worktrees**, and maestro carries separate confinement
machinery (`maestro/confinement.py`) guarding the operations that can actually cause damage. Adding
filesystem confinement now would be a second belt over an existing one, and the design question it
raises — *what exactly should an autonomous implementer be prevented from touching?* — has no
pressing answer today.

**The trigger to build it:** the first incident where an agent modifies something outside its
worktree that it had no business touching, or the first time maestro runs unattended against a
repository where that would be expensive.

## Scope decision: files yes, network no

**The network is deliberately NOT confined.** An agent that wants to read documentation, look up an
API, or fetch context to do its job better should be allowed to. Restricting it buys little and
degrades the work. This is a standing decision, not an omission — anyone implementing this should not
"complete" the design by adding network restrictions.

**The filesystem is the part worth confining**, and only that part.

## Mechanism (operator-supplied, for the claude backend)

The claude driver reports `Capabilities.sandbox = False` because the CLI cannot fence itself the way
codex can. Confinement can still be imposed *around* it, by constructing a workspace that physically
contains only the files the agent may see, and bind-mounting the real files into it.

Sketch, as supplied by the operator:

    mkdir -p ~/claude-box
    touch ~/claude-box/app.py ~/claude-box/config.py

    mount --bind ~/myproject/app.py ~/claude-box/app.py      # privileged; see below
    mount --bind ~/myproject/config.py ~/claude-box/config.py

    cd ~/claude-box && claude

with the CLI's own sandbox enabled and escape disabled:

    {
      "sandbox": {
        "enabled": true,
        "allowUnsandboxedCommands": false,
        "failIfUnavailable": true,
        "autoAllowBashIfSandboxed": true
      }
    }

The agent sees only `app.py` and `config.py`; edits land on the real files; everything else in the
project is not merely denied but **absent**. Unmount when finished.

For repeated use this becomes a small wrapper — `claude-safe app.py config.py` — that builds the box,
mounts the requested files, launches, and tears down.

**Verify before implementing:** the settings block above is the operator's, and the CLI's sandbox
schema may have moved. Confirm the key names and semantics against the current Claude Code settings
documentation rather than transcribing this file. Getting this wrong fails *open* — an agent that
believes it is fenced and is not — so it must be checked, not assumed.

## The hard part, and it is not the mounting

The technique above is sound for an **interactive** session where a human already knows which two
files are in play. An **autonomous implementer** is a different problem:

- it does not know its file set in advance — discovering which files a task touches is much of the task;
- it must run the test suite, which needs the whole tree;
- it must run `git` commands, which need `.git`;
- it creates new files, which by definition were not mounted.

So a per-file allow-list is the wrong shape here. The natural rule for maestro is **mount the task's
worktree and nothing above it** — the agent gets full freedom inside the worktree it is supposed to
be working in, and the rest of the filesystem (other projects, `~/.ssh`, the operator's home, maestro's
own source) is absent rather than merely forbidden. That preserves everything an implementer needs to
do while removing everything it should never reach.

This is a smaller change than the per-file sketch and fits the existing model, since implementers are
already confined to a worktree *by convention*. It would make that convention physical.

## How it lands on the existing plumbing

The protocol already carries the concepts; nothing new is needed in the interfaces:

- `Capabilities.sandbox` (`maestro/backends/base.py:95`) — the driver's honest answer about whether it
  can fence itself.
- `LaunchSpec.sandbox` (`:109`) — the per-launch *request*, currently never set by core.
- `CompletionSpec.writable` — the same idea for one-shot calls; its docstring already states that
  drivers which can enforce confinement must, and those that cannot are free to ignore it.

So the work is: have core decide the request and set `LaunchSpec.sandbox`; have the codex driver honour
it natively; and for claude, wrap the launch in the box described above. Once core reads the
capability, the strict `xfail` flips to a hard failure and must be deleted in the same commit — that is
the designed forcing function, not a surprise.

## Privileged commands

`mount --bind` and `umount` require root. Per the operator's standing rule, no agent runs privileged
commands directly. Any implementation must package the full privileged sequence into a single script —
`scripts/` in this repo for a reusable workflow — make it executable, and stop so the operator can run
it themselves. Design the wrapper so the privileged part is one script invocation, not a series of
prompts, and so an unmount always happens even when a run fails.

## Open questions for whoever builds this

1. Does the codex driver's native sandbox and this bind-mount box need to look the same to core, or is
   `LaunchSpec.sandbox` allowed to mean "best effort, per driver"? The docstring currently says the
   latter; confirm that is still wanted.
2. What happens when the box cannot be created — refuse to launch, or launch unconfined with a loud
   breadcrumb? Failing open silently is the one outcome to design out.
3. Does a mid-task backend switch or a D4 session rotation reuse the same box? Both reuse the worktree,
   so the box should follow the worktree, not the session.
4. Cleanup on crash: a killed run must not leave mounts behind. A stale bind-mount is worse than none,
   because the next run inherits it silently.
