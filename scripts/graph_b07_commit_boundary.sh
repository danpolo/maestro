#!/usr/bin/env bash
# B07 — the confinement commit boundary.
#
# The 2026-09-08 local audit proved an unprivileged bwrap profile can confine an implementer
# to one linked worktree, and recorded that `git commit` then FAILED at the shared
# `index.lock`. That left the plan with an unanswered question: an implementer that cannot
# commit cannot produce its output, so either enforced confinement is unusable or the profile
# needs widening — and widening it naively hands the agent every branch in the repository.
#
# This script answers it. It walks the failure ladder one grant at a time and then checks, under
# the resulting profile, that the three boundaries the architecture requires still hold.
#
# Result (bubblewrap 0.8.0, git worktree, this host, 2026-09-08): a commit needs exactly three
# writable paths beyond the worktree — the worktree's own git dir, the shared object store, and
# *only this branch's ref namespace* (plus its reflog). With those, the commit succeeds and is
# reachable on the host, while moving a protected branch, writing outside the worktree and
# reading a masked credential directory all still fail.
#
# The load-bearing consequence: enforcement granularity is a DIRECTORY, so the implementer's
# branch must live in its own namespace (refs/heads/impl/<task>) rather than flat in
# refs/heads/. Maestro currently names them flat (orchestrator.py `impl-<task>-<ts>`), which
# would force refs/heads/ to be writable and hand the agent the integration target.
#
# Run: bash scripts/graph_b07_commit_boundary.sh   (needs bwrap; touches only a scratch dir)
#
# Note: the scratch repo lives OUTSIDE /tmp on purpose — the profile tmpfs-masks /tmp, and a
# repo under a masked parent makes bwrap synthesise WRITABLE tmpfs intermediates, so forbidden
# writes silently succeed and vanish instead of failing. That false pass is its own hazard.
set -u
BASE=$(mktemp -d /home/dan/.cache/maestro-b07/XXXXXX)
MAIN="$BASE/repo"; WT="$BASE/wt"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
git init -q -b main "$MAIN"; cd "$MAIN"
git -c user.email=a@b -c user.name=t commit -q --allow-empty -m base
git branch agent/task-1
git worktree add -q "$WT" agent/task-1
GITDIR="$MAIN/.git/worktrees/wt"
mkdir -p "$MAIN/.git/logs/refs/heads/agent" "$MAIN/.git/refs/heads/agent"
MAIN_BEFORE=$(git rev-parse main)

run() { local label="$1"; shift; local -a extra=()
  while [ "$1" != "--" ]; do extra+=("$1"); shift; done; shift
  local out rc
  out=$(bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp \
        --bind "$WT" "$WT" "${extra[@]}" \
        --setenv GIT_CONFIG_GLOBAL /dev/null --setenv GIT_CONFIG_SYSTEM /dev/null \
        --setenv HOME "$WT" --chdir "$WT" /bin/bash -c "$1" 2>&1); rc=$?
  printf '%-36s rc=%-3s %s\n' "$label" "$rc" "$(echo "$out" | tr '\n' ' ' | cut -c1-140)"; }

COMMIT='echo x > f.txt && git add f.txt && git -c user.email=a@b -c user.name=t commit -q -m inside && git rev-parse --short HEAD'
P_GITDIR=(--bind "$GITDIR" "$GITDIR")
P_OBJ=(--bind "$MAIN/.git/objects" "$MAIN/.git/objects")
P_REF=(--bind "$MAIN/.git/refs/heads/agent" "$MAIN/.git/refs/heads/agent"
       --bind "$MAIN/.git/logs/refs/heads/agent" "$MAIN/.git/logs/refs/heads/agent")

echo "== incremental profiles =="
run "1 worktree rw only"        -- "$COMMIT"
run "2 + worktrees/wt"          "${P_GITDIR[@]}" -- "$COMMIT"
run "3 + objects"               "${P_GITDIR[@]}" "${P_OBJ[@]}" -- "$COMMIT"
run "4 + refs/heads/agent only" "${P_GITDIR[@]}" "${P_OBJ[@]}" "${P_REF[@]}" -- "$COMMIT"

echo "== under profile 4, what must still be refused =="
run "move protected branch main" "${P_GITDIR[@]}" "${P_OBJ[@]}" "${P_REF[@]}" -- 'git update-ref refs/heads/main HEAD; echo rc=$?'
run "write outside the worktree" "${P_GITDIR[@]}" "${P_OBJ[@]}" "${P_REF[@]}" -- "echo pwned > $MAIN/escaped.txt; echo rc=\$?"
run "read a masked secret dir"   "${P_GITDIR[@]}" "${P_OBJ[@]}" "${P_REF[@]}" --tmpfs "$HOME/.claude" -- "cat $HOME/.claude/settings.json; echo rc=\$?"

echo "== host-side truth =="
cd "$MAIN"
echo "main   before=$(echo $MAIN_BEFORE | cut -c1-7)  after=$(git rev-parse --short main)"
echo "agent  after =$(git rev-parse --short agent/task-1)"
echo "escaped.txt on host: $([ -f "$MAIN/escaped.txt" ] && echo PRESENT || echo absent)"
echo "commit reachable on host: $(git log --oneline agent/task-1 | head -2 | tr '\n' ' ')"
echo "BASE=$BASE"
