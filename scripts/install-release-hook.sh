#!/usr/bin/env bash
# Installs the git hooks that make this maestro checkout announce each new commit.
#
# After a commit (post-commit) or a merge/pull (post-merge) in the main checkout, the hook
# runs `maestro release` in the background: it tests HEAD once and, if green, writes the sha
# to ~/.maestro/latest. Every project controller adopts that version at its next idle poll
# and restarts on it (maestro/selfupdate.py `release`, `maybe_self_update`). A red HEAD is
# held in ~/.maestro/latest.held and nothing moves. Output goes to ~/.maestro/release.log.
#
# Commits in linked worktrees (feature work, ~/.maestro/versions/*) are skipped: only the
# main checkout announces. Idempotent; refuses to overwrite a hook it did not write.
#
# Usage:
#   scripts/install-release-hook.sh [checkout]    # checkout defaults to this script's repo
set -euo pipefail

checkout="$(cd "${1:-$(dirname "$0")/..}" && git rev-parse --show-toplevel)"
hooks="$(cd "$checkout" && git rev-parse --path-format=absolute --git-common-dir)/hooks"
marker="# maestro-release-hook"

for name in post-commit post-merge; do
  target="$hooks/$name"
  if [ -e "$target" ] && ! grep -q "$marker" "$target"; then
    echo "[install-release-hook] $target exists and is not ours; leaving it alone" >&2
    exit 1
  fi
  cat > "$target" <<EOF
#!/usr/bin/env bash
$marker
# Only the main checkout announces; linked worktrees have their own git dir.
[ "\$(git rev-parse --git-dir)" = "\$(git rev-parse --git-common-dir)" ] || exit 0
sha="\$(git rev-parse --short HEAD)"
# Git exports GIT_INDEX_FILE (relative), GIT_AUTHOR_DATE, ... to hooks; they would leak into
# the release's \`git worktree add\` and every git command the suite runs.
for v in \$(env | grep -o '^GIT_[A-Za-z_]*'); do unset "\$v"; done
home="\${MAESTRO_HOME:-\$HOME/.maestro}"
mkdir -p "\$home"
echo "\$(date -Is) $name \$sha" >> "\$home/release.log"
MAESTRO_BOOTSTRAPPED=1 setsid nohup "$checkout/.venv/bin/python" -m maestro.cli release \\
  --checkout "$checkout" >> "\$home/release.log" 2>&1 < /dev/null &
exit 0
EOF
  chmod +x "$target"
  echo "[install-release-hook] installed $target"
done
