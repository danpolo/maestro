#!/usr/bin/env bash
# Owner (modelctl) suite against a Maestro source, with the session13 gate env.
# Usage: run-owner-gate.sh <maestro-source> [pytest args...]
set -u
SRC=${1:?maestro source}; shift
PY=/home/dan/projects/maestro/.venv/bin/python
RT=$(mktemp -d /home/dan/projects/maestro/.scratch/owner-gate-XXXXXX)
mkdir -p "$RT/bin" "$RT/project/.orchestrator"
NOTICES=/home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/owner-gate-notices.jsonl
"$PY" -c 'import sys,pathlib; pathlib.Path(sys.argv[1]).write_text("#!"+sys.argv[2]+"\nimport json,sys\nwith open("+repr(sys.argv[3])+",\"a\") as f: f.write(json.dumps(sys.argv[1:])+\"\\n\")\n")' "$RT/bin/send-to-me" "$PY" "$NOTICES"
chmod +x "$RT/bin/send-to-me"
cd /home/dan/projects/modelctl
PATH="$RT/bin:$PATH" MAESTRO_REPO="$RT/project" MAESTRO_HOME="$RT/home" MAESTRO_MODELS_HOME="$RT/models" \
MAESTRO_BOOTSTRAPPED=1 PYTHONDONTWRITEBYTECODE=1 \
MODELCTL_TEST_MAESTRO_SOURCE="$SRC" MODELCTL_MAESTRO_TEST_SOURCE="$SRC" MAESTRO_SOURCE="$SRC" \
"$PY" -m pytest -o addopts= -rs -p no:cacheprovider "${@:-tests}"
code=$?
rm -rf "$RT"
exit $code
