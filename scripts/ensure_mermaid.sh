#!/usr/bin/env bash
# Ensures `mmdc` (mermaid-cli) is available for `maestro.docs.depmap.render_png()` to shell
# out to when it renders docs/dependency_map.md's mermaid block to dependency_map.png.
#
# Idempotent, check-then-install — same shape as `maestro doctor`'s other dependency checks
# (e.g. `_check_backends` in maestro/cli.py): look for what's already there before installing
# anything. Never reinstalls over a working `mmdc`, wherever it resolves from:
#   1. globally on $PATH (the normal case after this script has run once on a machine — mmdc
#      is not project-specific, so one global install serves every maestro-scaffolded repo)
#   2. a legacy per-repo local install at <repo>/.mermaid/node_modules/.bin/mmdc (the shape
#      the original reference `render_dependency_map.sh` used, still honoured by
#      `maestro.docs.depmap`'s own MMDC fallback for repos that already have one)
#
# Only installs (via `npm install -g @mermaid-js/mermaid-cli`) when neither is present.
#
# Usage:
#   scripts/ensure_mermaid.sh [repo_root]      # repo_root defaults to this script's own repo
#
# Called from `maestro init` (best-effort — see `_run_ensure_mermaid` in maestro/cli.py); never
# fails init over it. Safe to run by hand too.
set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCAL_MMDC="$REPO_ROOT/.mermaid/node_modules/.bin/mmdc"

if command -v mmdc >/dev/null 2>&1; then
    echo "[ensure_mermaid] already installed (global): $(command -v mmdc)"
    exit 0
fi

if [[ -x "$LOCAL_MMDC" ]]; then
    echo "[ensure_mermaid] already installed (local to this repo): $LOCAL_MMDC"
    exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "[ensure_mermaid] npm not found on PATH — cannot install mermaid-cli." >&2
    echo "[ensure_mermaid] install Node.js/npm, then re-run this script." >&2
    exit 1
fi

# Reuse a system Chromium/Chrome if one is already installed, instead of letting puppeteer
# download its own (~300MB, and often blocked/slow on a headless server). `maestro init`
# scaffolds .mermaid/puppeteer-config.json pointing mmdc at whichever binary is found here —
# see `_detect_chromium_path` in maestro/cli.py.
for candidate in /usr/bin/chromium /usr/bin/chromium-browser /usr/bin/google-chrome \
                 /usr/bin/google-chrome-stable; do
    if [[ -x "$candidate" ]]; then
        export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
        echo "[ensure_mermaid] found system Chromium at $candidate — skipping puppeteer's bundled download"
        break
    fi
done

echo "[ensure_mermaid] installing @mermaid-js/mermaid-cli globally via npm..."
npm install -g @mermaid-js/mermaid-cli
echo "[ensure_mermaid] installed: $(command -v mmdc)"
