# PHASE P10B: WIRE HOST RESOURCE ADMISSION INTO THE LAUNCH LOOP (AGENT EXECUTION SPEC)

```
Phase ID:        PHASE-P10B
Depends On:      P08 (AdmissionEngine, HostCapacity, scheduling.schedule, host ledger), P10
Output:          CPU / RAM / VRAM-aware task launch with starvation protection across projects
Target Files:
  - Extend:  maestro/taskgraph.py
  - Extend:  maestro/scheduling.py
  - Extend:  maestro/control/resources.py
  - Adapt:   maestro/orchestrator.py
  - Create:  tests/test_host_resource_admission.py
  - Extend:  tests/control/test_resources.py
Verification:    .venv/bin/python -m pytest -q tests/test_host_resource_admission.py tests/test_scheduling.py tests/control/test_resources.py
```

---

## 1. OBJECTIVE & DELIVERABLES
P08 built the admission engine but the legacy loop only consults `scheduling.rank` for ordering; the B8 `resource:` label mutex is still its only resource gate (`_p08_open_threads`). This phase gives ROADMAP tasks per-task resource requests and makes the orchestrator admit launches against the host's real CPU, RAM and VRAM — within one project and across every project sharing the host — so a heavy task waits instead of oversubscribing the machine.

---

## 2. IMPLEMENTATION CHECKLIST
- [ ] **ROADMAP resource requests**: A task may declare `resources: {cpu, ram_mb, vram_mb}`. `taskgraph` parses and validates it (non-negative numbers; unknown keys are a parse error, not ignored). A task without it requests nothing on those dimensions. The B8 `resource:` label mutex keeps its meaning and is not folded in.
- [ ] **Admission in the launch loop**: `orchestrator.main` builds `scheduling.AdmissionEngine(HostCapacity.detect(...))` each poll, takes the in-flight tasks' requests first, then runs `scheduling.schedule` over the ready candidates and launches only what it admits. A waiting task records its `Admission.reason` / `detail` so `maestro explain` / status can say why it has not started.
- [ ] **Counted dimensions in the host ledger**: Extend `maestro/control/resources.py`'s `HostReservationLedger` from pool slots to counted `cpu` / `ram_mb` / `vram_mb` amounts, reserved before launch, heartbeated, released on exit and reconciled on restart/process death exactly as pool reservations are. Other projects' live reservations are subtracted from `HostCapacity` before a poll admits anything.
- [ ] **Starvation protection**: Use `scheduling.schedule`'s protected-dimension hold so a large request that has aged past the policy threshold is not starved forever by a stream of small ones.
- [ ] **Agent slots stay separate**: Script / deterministic tasks never request agent-slot dimensions, so a saturated agent pool cannot delay them (P08's invariant, now under the live loop).

---

## 3. ACCEPTANCE CRITERIA & VERIFICATION
- **Acceptance Condition**:
  - With RAM capacity for one of two ready heavy tasks, exactly one launches and the other waits with a RAM reason; it launches once the first releases.
  - A ready script task launches in the same poll while every agent slot is taken.
  - Two projects on one host sharing the ledger never together reserve more `ram_mb` than `HostCapacity` reports, under a concurrent-claim stress test.
  - An aged heavy task is eventually admitted while small tasks keep arriving.
  - A dead holder's counted reservation is reclaimed on reconcile.
  - ROADMAPs with no `resources:` blocks launch in the same order as before this phase.
- **Execution Command**:
  ```bash
  .venv/bin/python -m pytest -q tests/test_host_resource_admission.py tests/test_scheduling.py tests/control/test_resources.py
  ```
