# Local Graph Blind-Spots Implementation Plan

> **For agentic workers:** Execute this plan inline. Keep all probes inside disposable scratch directories, preserve exact backend tuples, and do not use external research or the deferred free/open-model track.

**Goal:** Turn Maestro-resolvable graph-engineering blind spots into reproducible local evidence, narrowly scoped fixes, and explicit remaining limitations.

**Architecture:** Add a standard-library audit harness and source-controlled experiment catalog rather than graph-runtime infrastructure. Reuse Maestro's existing init, backend, state, adapter, self-update, and characterization seams; change production behavior only for directly demonstrated, low-risk defects.

**Tech Stack:** Python 3.11 standard library, pytest, existing Maestro package and installed CLIs.

**Spec:** `GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md` section “Actions / experiments in Maestro,” plus local-only installed-backend observations requested on 2026-09-08.

## Global Constraints

- No external/deep research and no free/open-model work.
- No live project, credential, provider, service, or privileged mutation from automated fixtures.
- Paid-agent calls must perform useful engineering work, pin backend/model/effort, and capture observable 5-hour/weekly signals without inventing attribution.
- Preserve unrelated dirty watchdog and handoff changes.
- Do not introduce the proposed graph runtime, control database, scheduler, or repository index in this pass.

---

### Task 1: Evidence schema and fixed evaluation catalog

**Files:**
- Create: `maestro/graph_evidence.py`
- Create: `tests/graph_engineering/fixtures/cases.json`
- Create: `tests/graph_engineering/test_evidence.py`

**Interfaces:**
- Produce validated experiment records that distinguish `observed`, `inferred`, and `unknown` fields.
- Produce task, acceptance-criterion, dependency-mutation, and backend-tuple records usable by the audit runner.

- [x] Write tests for schema validation, exact backend tuple preservation, weighted outcomes, raw paired results, and small-sample uncertainty.
- [x] Run the tests and confirm they fail because the module is absent.
- [x] Implement the minimal module and fixture catalog.
- [x] Run the focused tests.

### Task 2: Project-policy and validation-dependency baselines

**Files:**
- Create: `tests/graph_engineering/test_project_policy.py`
- Create: `tests/graph_engineering/test_validation_dependencies.py`
- Modify if the tests prove the defect: `maestro/cli.py`, `maestro/templates/adapters/test`, `tests/test_cli.py`

**Interfaces:**
- Compare deterministic `init` facts against Python, Node, Go, mixed/monorepo, missing-test, generated-asset, and service-dependency ground truth.
- Exercise behavior-preserving signatures, dynamic imports, data/config, generated files, test infrastructure, and environment dependencies against complete acceptance suites.

- [x] Add fixture-first tests that expose current false/missed project-policy facts and signature-only validation misses.
- [x] Run them red and record the exact misses.
- [x] Apply only a small init/template correction that is unambiguous from the evidence; retain broader policy questions as measurements.
- [x] Run focused and existing init/template tests.

### Task 3: Confinement, process recovery, action reconciliation, and storage faults

**Files:**
- Create: `tests/graph_engineering/test_local_faults.py`
- Create: `tests/graph_engineering/test_external_actions.py`
- Create: `tests/graph_engineering/test_storage_recovery.py`
- Create: `scripts/graph_blind_spot_audit.py`

**Interfaces:**
- Probe installed enforcement locally without model inference where possible.
- Exercise child/grandchild ownership, exclusive-writer locks, launch-acknowledgment loss, idempotent operation receipts, blind replay, atomic state failure, SQLite online backup/restore, missing artifacts, schema upgrade, and simulated ENOSPC.

- [x] Write deterministic tests for each fixture and run them red where harness support is absent.
- [x] Implement the smallest reusable runner and fixture support.
- [x] Run the focused fault suites and store raw JSON evidence under `artifacts/graph-engineering/`.
- [x] Leave any failure requiring a new runtime contract unresolved with a recommendation rather than changing architecture.

### Task 4: Concurrency/resource and self-update/service ownership

**Files:**
- Create: `tests/graph_engineering/test_concurrency.py`
- Create: `tests/graph_engineering/test_service_ownership.py`
- Modify if directly required: `maestro/templates/systemd/maestro-watchdog.service.tmpl`, `tests/test_templates.py`

**Interfaces:**
- Measure queue delay, elapsed time, peak RSS, SQLite writer contention, duplicate claims, and overlap conflicts under a bounded local workload.
- Demonstrate current shared-version-pointer behavior across two scratch projects and validate the generated TMUX-owned `Type=forking` service contract.

- [x] Add and run failing tests for the required service template ownership contract.
- [x] Make the minimal template correction and run template/init tests.
- [x] Run bounded serial/concurrent and SQLite contention probes; record machine-qualified measurements.
- [x] Record the shared-pointer design consequence without silently changing version ownership.

### Task 5: Installed backend capability, routing, and useful quality observations

**Files:**
- Extend: `scripts/graph_blind_spot_audit.py`
- Create/update: `artifacts/graph-engineering/local-audit.json`

**Interfaces:**
- Record CLI path/version, locally observed model/effort/sandbox/resume controls, Maestro driver support, and normalized usage-window signals for Claude Code, Codex CLI, and Antigravity CLI.
- Run at most one small useful review/evaluation task per paid backend, only when it contributes engineering evidence, with exact tuple and outcome retained.

- [x] Capture no-inference CLI and usage observations first.
- [x] Select useful held-out review tasks from the fixture catalog and run only the calls whose quota cost is justified.
- [x] Record the manually mapped review scores as provisional because raw outputs needed for independent deterministic verification were not retained.
- [x] Do not generalize route rankings beyond observed task classes.

### Task 6: Evidence matrix, documentation, and final verification

**Files:**
- Modify: `GRAPH_ENGINEERING_RESEARCH_BLIND_SPOTS.md`
- Modify: `docs/PROGRESS.md`
- Update: `artifacts/graph-engineering/local-audit.json`

**Interfaces:**
- Give every in-scope blind spot a status, evidence command/artifact, verified result, and unresolved limitation.

- [x] Run the audit runner twice where repeatability matters and compare stable fields.
- [x] Run all graph-engineering tests, affected existing tests, and the full suite in the normal host environment.
- [x] Run `git diff --check` and inspect the final diff for unrelated changes.
- [x] Update the blind-spots document from actual outputs only; report any blocked backend run or unavailable signal as unknown.
