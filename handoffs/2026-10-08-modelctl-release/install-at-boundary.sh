#!/usr/bin/env bash
set -euo pipefail
cd /tmp
exec /home/dan/projects/maestro/.venv/bin/python /home/dan/projects/maestro/handoffs/2026-10-08-modelctl-release/adopt-at-boundary.py
