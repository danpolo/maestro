# PHASE P11: HARDEN OPERATOR VIEWS, VERSION ADOPTION & SERVICE LIFECYCLE (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P11
Depends On:      P01–P07
Output:          CLI/HTML workflow views, per-project bootstrap version pin, Type=forking TMUX service
Target Files:
  - Modify:  maestro/status.py
  - Modify:  maestro/cli.py
  - Modify:  maestro/hitl/commands.py
  - Modify:  maestro/hitl/telegram.py
  - Modify:  maestro/hitl/dan_request.py
  - Modify:  maestro/selfupdate.py
  - Modify:  maestro/bootstrap.py
  - Modify:  maestro/watchdog.py
  - Create:  tests/test_workflow_status.py
  - Create:  tests/test_graph_update_compatibility.py
  - Create:  tests/ops/test_graph_service_lifecycle.py
Verification:    .venv/bin/python -m pytest -q tests/test_workflow_status.py tests/test_graph_update_compatibility.py tests/ops/test_graph_service_lifecycle.py tests/test_selfupdate.py tests/test_bootstrap.py
```

---

## 1. OBJECTIVE & DELIVERABLES
Harden operator visibility tools (`workflow show --html`, `explain`). Implement per-project runtime version pinning in `bootstrap.py` to prevent cross-project update drift. Generate TMUX-isolated systemd service definitions.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **Operator Projections**:
  - Implement `maestro workflow show <task_id> --html` (self-contained static interactive graph/timeline).
  - Implement `maestro explain <task_id>` (causal trace of criteria -> binding -> evidence -> acceptance).
- [ ] **Per-Project Bootstrap Pin (`[INV-09]`, `[INV-A09]`)**:
  - Modify `bootstrap.reexec_into_adopted_version`: parse `--repo` / `$MAESTRO_REPO` directly from `argv` using standard library before importing any Maestro modules. Prefer per-project pin over machine-global `~/.maestro/current`.
- [ ] **TMUX Service Generation (`[INV-10]`)**:
  - Generate systemd service adhering to: `Type=forking` and `ExecStart=/usr/bin/tmux new-session -d -s maestro -n main 'cd /app && ./run.sh'`.

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - Adopting a new Maestro version in one project CANNOT alter another project's active runtime.
  - Service restart or controller crash never duplicates active workers.
  - HTML timeline export contains zero secret-bearing artifacts or credentials.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_workflow_status.py tests/test_graph_update_compatibility.py tests/ops/test_graph_service_lifecycle.py tests/test_selfupdate.py tests/test_bootstrap.py
  ```\n