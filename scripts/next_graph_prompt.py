#!/usr/bin/env python3
"""
Maestro Graph Engineering - Deterministic Agent Prompt Generator

Reads:
  - State file: docs/graph-engineering/STATE.yaml
  - Spec Index: docs/graph-engineering/specs/README.md
  - Phase Specs: docs/graph-engineering/specs/phases/P{XX}_*.md

Outputs:
  A deterministic, context-engineered prompt for the next AI implementation agent session.
  Supports fresh phase starts, mid-phase handoffs (what was done, what's left, context),
  and phase advancement.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Default paths relative to repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = REPO_ROOT / "docs" / "graph-engineering" / "STATE.yaml"
SPECS_DIR = REPO_ROOT / "docs" / "graph-engineering" / "specs"
PHASES_DIR = SPECS_DIR / "phases"

# Order phases are taken in: the core path to P11, then the enhancement branch
# (P08 -> P09 -> P10 -> P10B -> P07B), then P12A wires both into live dispatch, P12B handles
# failing models in that loop (D-FAIL-1/2), then P12.
# Phase IDs need not be `P\d\d`. P07B only depends on P06/P07, so `--phase P07B` can take it earlier.
PHASE_SEQUENCE = [
    "P00", "P01", "P02", "P03", "P04", "P05", "P06", "P07", "P11", "P08", "P09", "P10", "P10B", "P07B", "P12A", "P12B", "P12",
    "P13"
]
ALL_PHASES = [
    "P00", "P01", "P02", "P03", "P04", "P05", "P06", "P07", "P07B", "P08", "P09", "P10", "P10B", "P11", "P12A", "P12B", "P12",
    "P13"
]

# Open threads live in STATE.yaml under any `_<phase>_open_threads` key, one mapping each:
#   {owner: <phase>, thread: "..."}                           still open, that phase must act
#   {disposition: accepted|closed|needs-dan, thread, note}    no phase owns it
# A phase spec section that constrains the phase for downstream work it must not implement
# (docs/maestro-context-gate-integration.md, after P12); the directive points the agent at it.
FORWARD_COMPAT_HEADING = "FORWARD COMPATIBILITY: CONTEXT GATE"

OPEN_THREADS_SUFFIX = "_open_threads"
THREAD_DISPOSITIONS = ("accepted", "closed", "needs-dan")

# Mapping of phase to primary and secondary context documents from Context Routing Matrix
CONTEXT_ROUTING = {
    "P00": {
        "title": "Establish Migration Baseline",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P00_MIGRATION_BASELINE.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/graph_engineering/test_baseline_lanes.py"
    },
    "P01": {
        "title": "Introduce Transactional Control & Single Writer",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P01_TRANSACTIONAL_CONTROL.md",
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/control"
    },
    "P02": {
        "title": "Make Roadmap a Validated, Revisioned Dependency Model",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P02_ROADMAP_DEPENDENCY_MODEL.md",
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/test_taskgraph.py tests/characterization/test_roadmap.py"
    },
    "P03": {
        "title": "Execute Existing Lanes as Durable Workflow Nodes",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P03_WORKFLOW_NODES_AND_RUNNER.md",
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/workflows tests/control/test_recovery.py"
    },
    "P04": {
        "title": "Make Evidence and Staging Integration Authoritative",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P04_AUTHORITATIVE_EVIDENCE_STAGING.md",
            "docs/graph-engineering/specs/04_VALIDATION_STAGING_AND_ACCEPTANCE.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/test_validation_evidence.py tests/test_integration_acceptance.py"
    },
    "P05": {
        "title": "Construct Project Workflows and Distinct Agent Definitions",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P05_PROJECT_WORKFLOWS_AND_ROLES.md",
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/workflows/test_policy.py tests/workflows/test_compiler.py"
    },
    "P06": {
        "title": "Route Every AI Call by Capability, Model and Effort",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P06_CAPABILITY_ROUTING_CATALOG.md",
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/05_SCHEDULING_AND_RESOURCES.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/backends/test_catalog.py tests/test_routing.py"
    },
    "P07": {
        "title": "Deliver Free-Model and Hybrid Execution",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P07_FREE_MODEL_AND_HYBRID.md",
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/backends/test_model_runtime.py tests/workflows/test_mode_policies.py"
    },
    "P08": {
        "title": "Deepen Resource Admission and Scheduling",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P08_RESOURCE_ADMISSION_SCHEDULING.md",
            "docs/graph-engineering/specs/05_SCHEDULING_AND_RESOURCES.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/test_scheduling.py tests/control/test_resources.py"
    },
    "P09": {
        "title": "Add Repository Facts and Benchmark Structural Context",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P09_REPOSITORY_FACTS_STRUCTURAL.md",
            "docs/graph-engineering/specs/06_REPOSITORY_INTELLIGENCE.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/01_DATA_CONTRACTS_AND_SCHEMAS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/repository"
    },
    "P10": {
        "title": "Enable Qualified Affected Validation and Local Plan Repair",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P10_AFFECTED_VALIDATION_REPAIR.md",
            "docs/graph-engineering/specs/04_VALIDATION_STAGING_AND_ACCEPTANCE.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/test_affected_validation.py tests/workflows/test_plan_repair.py"
    },
    "P07B": {
        "title": "Integrate Antigravity (agy) as a Light / Balanced Backend",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P07B_ANTIGRAVITY_BACKEND.md",
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/05_SCHEDULING_AND_RESOURCES.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/backends/test_antigravity.py tests/test_routing.py tests/backends/test_catalog.py tests/test_thirdparty.py tests/test_limits.py"
    },
    "P10B": {
        "title": "Wire Host Resource Admission into the Launch Loop",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P10B_HOST_RESOURCE_ADMISSION.md",
            "docs/graph-engineering/specs/05_SCHEDULING_AND_RESOURCES.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/test_host_resource_admission.py tests/test_scheduling.py tests/control/test_resources.py"
    },
    "P11": {
        "title": "Harden Operator Views, Version Adoption and Service Lifecycle",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P11_OPERATOR_LIFECYCLE_BOOTSTRAP.md",
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/05_SCHEDULING_AND_RESOURCES.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/test_workflow_status.py tests/ops/test_graph_service_lifecycle.py"
    },
    "P12A": {
        "title": "Wire the Built Graph Components into Live Dispatch",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P12A_GRAPH_DISPATCH_WIRING.md",
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md",
            "docs/graph-engineering/specs/phases/MIGRATION_GUARDRAILS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/graph_engineering/test_dispatch_wiring.py tests/test_host_resource_admission.py tests/test_workflow_status.py tests/workflows"
    },
    "P12B": {
        "title": "Handle Failing Models Without Lowering Quality",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P12B_MODEL_FAILURE_HANDLING.md",
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/02_EXECUTION_ENGINE_AND_DURABILITY.md",
            "docs/graph-engineering/specs/phases/P12A_GRAPH_DISPATCH_WIRING.md",
            "docs/graph-engineering/specs/phases/MIGRATION_GUARDRAILS.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q tests/workflows/test_failures.py tests/graph_engineering/test_failure_ladder.py tests/graph_engineering/test_dispatch_wiring.py tests/backends tests/workflows"
    },
    "P12": {
        "title": "Qualify the Complete Evolution Before Live Adoption",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P12_QUALIFICATION_AND_RELEASE.md",
            "docs/graph-engineering/specs/phases/MIGRATION_GUARDRAILS.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/07_EXPERIMENTS_AMENDMENTS_AND_DISPOSITION.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q && .venv/bin/python scripts/graph_qualification.py --repo /tmp/maestro-graph-qualification --fixture-backends"
    },
    "P13": {
        "title": "Post-P12 Features",
        "primary": [
            "docs/graph-engineering/specs/README.md",
            "docs/graph-engineering/specs/phases/P13_POST_P12_FEATURES.md"
        ],
        "secondary": [
            "docs/graph-engineering/specs/03_BACKENDS_CATALOG_AND_ROUTING.md",
            "docs/graph-engineering/specs/05_SCHEDULING_AND_RESOURCES.md"
        ],
        "test_cmd": ".venv/bin/python -m pytest -q"
    }
}

# Repositories outside REPO_ROOT that a phase is authorized to create, modify or
# operate on. Nothing here is implied by the phase spec's Target Files, which only
# ever name paths inside the maestro repo -- without this block an agent has no
# grounds to touch another project and will (correctly) stop and ask.
EXTERNAL_WORKSPACES = {
    "P00": [
        {
            "path": "/home/dan/projects/duetflow",
            "role": "Authorized scratch project for the P00 setup rehearsal and first real coding task (first half of B01).",
            "authorized": "Create and modify freely: project.yaml, .orchestrator/, docs/ROADMAP.md, docs/PROJECT.md, "
                          "a root pyproject.toml, and the source/test files of the one real coding task. Commits in "
                          "this repo are expected; it is a scratch project, not a live one.",
            "entry_point": "Invoke the `maestro-setup` skill against this repo. Do NOT call `maestro init` directly -- "
                           "the skill owns the judgment inputs init cannot derive and runs the pre-flight checks below.",
            "state": "Already grilled: 3 commits on `master`, CONTEXT.md, docs/adr/, docs/PLAN.md, tests/test_smoke.py, "
                     ".venv. No project.yaml, no .orchestrator/, no root manifest. Do not re-create the skeleton.",
            "preflight": [
                "No root manifest (pyproject.toml / requirements.txt) means `maestro/cli.py::_derive_facts` derives an "
                "EMPTY test_command, and the rendered adapters/test fails closed with a TODO on every run -- the gate "
                "certifies nothing and every task parks at the merge gate. Resolve with Dan; record it as a rehearsal "
                "finding, not a chore.",
                "`git remote get-url origin` may be empty. maestro/merge.py runs `git push origin main` with its return "
                "code unchecked, so merges would land locally and silently never leave the machine. Report either way.",
            ],
            "instrumentation": "maestro-setup is interactive. Dan's answers are RECORDED HUMAN INTERVENTIONS for B01 -- "
                               "retain the questions, the answers and every correction verbatim. A summary does not "
                               "close B01, and setup success is not evidence of engineering quality.",
        }
    ],
    "P12": [
        {
            "path": "/home/dan/projects/duetflow",
            "role": "Greenfield half of the two-project qualification set (B01 second half).",
            "authorized": "Run the representative six-class task set; retain every raw outcome, rework and intervention.",
            "entry_point": "Should already be onboarded by P00. Verify project.yaml and .orchestrator/ before starting.",
            "state": "Depends on P00's rehearsal outcome.",
            "preflight": [],
            "instrumentation": "Fixed criterion weights and pinned source revisions BEFORE the run, not after.",
        },
        {
            "path": "/home/dan/projects/instagram-to-value",
            "role": "Brownfield half of the two-project qualification set (B01 second half).",
            "authorized": "Run the representative six-class task set under daemon/queue safeguards.",
            "entry_point": "Invoke the `maestro-setup` skill if not yet onboarded.",
            "state": "Live-ish project with a running daemon/queue -- NOT a scratch project. Confirm safeguards with "
                     "Dan before any autonomous task is dispatched.",
            "preflight": [
                "Daemon/queue must not be able to consume or race the qualification tasks. Establish the safeguard "
                "before the first dispatch, not after.",
            ],
            "instrumentation": "Same weights and catalog as the DuetFlow half, or the pairing proves nothing.",
        },
    ],
}


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {
            "schema_version": 1,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "current_phase": "P00",
            "status": "not_started",
            "completed_phases": [],
            "in_progress_details": None
        }
    with open(state_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_state(state_file: Path, state: dict):
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(state_file, "w", encoding="utf-8") as f:
        yaml.dump(state, f, sort_keys=False, default_flow_style=False)


def iter_open_threads(state: dict):
    """`(ref, entry)` for every open-thread entry, in file order; ref reads `_p10_open_threads #2`."""
    for key, entries in state.items():
        if isinstance(key, str) and key.endswith(OPEN_THREADS_SUFFIX) and isinstance(entries, list):
            for index, entry in enumerate(entries, 1):
                yield f"{key} #{index}", entry


def owned_threads(state: dict, phase_id: str) -> list[tuple[str, str]]:
    """The `(ref, thread text)` pairs whose `owner` is `phase_id`."""
    return [
        (ref, " ".join(str(entry.get("thread", "")).split()))
        for ref, entry in iter_open_threads(state)
        if isinstance(entry, dict) and entry.get("owner") == phase_id
    ]


def unstructured_threads(state: dict) -> list[tuple[str, object]]:
    """Entries that name neither exactly one `owner` nor one known `disposition`."""
    bad = []
    for ref, entry in iter_open_threads(state):
        if not isinstance(entry, dict) or not entry.get("thread"):
            bad.append((ref, entry))
            continue
        has_owner = bool(entry.get("owner"))
        has_disposition = entry.get("disposition") in THREAD_DISPOSITIONS
        if has_owner == has_disposition or ("disposition" in entry and not has_disposition):
            bad.append((ref, entry))
    return bad


def find_phase_spec_file(phase_id: str) -> Path | None:
    for p in PHASES_DIR.glob(f"{phase_id}_*.md"):
        return p
    return None


def extract_spec_summary(spec_file: Path) -> dict:
    """Extracts metadata, checklist, and criteria from phase spec markdown."""
    text = spec_file.read_text(encoding="utf-8")
    
    target_files = []
    checklist = []
    acceptance = []
    
    # Extract Target Files
    in_target_files = False
    in_checklist = False
    in_acceptance = False
    
    for line in text.splitlines():
        if "Target Files:" in line:
            in_target_files = True
            continue
        if in_target_files:
            if line.startswith("Verification:") or line.startswith("```") or line.startswith("---"):
                in_target_files = False
            elif line.strip().startswith("-"):
                target_files.append(line.strip())
                
        if "## 2. IMPLEMENTATION CHECKLIST" in line:
            in_checklist = True
            continue
        if in_checklist:
            if line.startswith("---") or line.startswith("## 3."):
                in_checklist = False
            elif line.strip().startswith("- [ ]") or line.strip().startswith("-"):
                checklist.append(line.strip())
                
        if "## 3. ACCEPTANCE CRITERIA" in line:
            in_acceptance = True
            continue
        if in_acceptance:
            if line.startswith("---") or line.startswith("## 4."):
                in_acceptance = False
            elif line.strip():
                acceptance.append(line.strip())
                
    return {
        "target_files": target_files,
        "checklist": checklist,
        "acceptance": acceptance
    }


def construct_prompt(state: dict, override_phase: str | None = None) -> str:
    phase_id = override_phase or state.get("current_phase", "P00")
    status = state.get("status", "not_started")
    in_progress = state.get("in_progress_details") or {}
    
    routing = CONTEXT_ROUTING.get(phase_id)
    if not routing:
        raise ValueError(f"Unknown phase ID: {phase_id}")
        
    spec_path = find_phase_spec_file(phase_id)
    spec_data = extract_spec_summary(spec_path) if spec_path else {}
    
    lines = []
    
    if status == "in_progress" and not override_phase:
        lines.append(f"# RESUME IMPLEMENTATION: PHASE {phase_id} ({routing['title']})\n")
        lines.append("> [!IMPORTANT]")
        lines.append(f"> You are RESUMING an in-progress session for **Phase {phase_id}**.")
        lines.append("> Review the handoff notes below to see what is already done and what remains.\n")
        
        lines.append("## 1. Handoff State & Context")
        lines.append(f"- **Active Git Branch**: `{in_progress.get('active_branch', 'feat/graph-engineering-foundation')}`")
        
        what_done = in_progress.get("what_was_done", [])
        if what_done:
            lines.append("\n### What Was Already Done in Previous Session:")
            for item in what_done:
                lines.append(f"- [x] {item}")
        else:
            lines.append("- Work was started but no specific items were marked completed.")
            
        whats_left = in_progress.get("whats_left", [])
        if whats_left:
            lines.append("\n### What is Left to Complete:")
            for item in whats_left:
                lines.append(f"- [ ] {item}")
                
        blockers = in_progress.get("blockers", [])
        if blockers:
            lines.append("\n### Known Blockers / Issues:")
            for b in blockers:
                lines.append(f"- ⚠️ {b}")
                
        ctx = in_progress.get("relevant_context", [])
        if isinstance(ctx, str):
            # A single note is written as a string; iterating it would print one bullet
            # per character.
            ctx = [ctx]
        if ctx:
            lines.append("\n### Relevant Context & Notes:")
            for c in ctx:
                lines.append(f"- ℹ️ {c}")
                
    else:
        lines.append(f"# IMPLEMENTATION DIRECTIVE: PHASE {phase_id} ({routing['title']})\n")
        lines.append(f"Execute **Phase {phase_id}** of the Maestro Graph Engineering evolution.")
        completed = [p.get("phase") if isinstance(p, dict) else p for p in state.get("completed_phases", [])]
        if completed:
            lines.append(f"Prior completed phases: `{', '.join(completed)}`.\n")

    lines.append("\n## 2. Minimal Required Context (Context Engineering)")
    lines.append("Before writing code, read ONLY the following specification files to preserve context window attention:")
    lines.append("**Primary Specifications**:")
    for f in routing["primary"]:
        lines.append(f"- [`{f}`](file://{REPO_ROOT / f})")
    lines.append("**Secondary Specifications** (consult only if interface details are needed):")
    for f in routing["secondary"]:
        lines.append(f"- [`{f}`](file://{REPO_ROOT / f})")
        
    lines.append("\n## 3. Scope & Target Deliverables")
    lines.append(f"- **Target Git Branch**: `{in_progress.get('active_branch', 'feat/graph-engineering-foundation')}`")
    lines.append(f"- Focus exclusively on **Phase {phase_id}**. Do NOT implement downstream phases.")
    if spec_data.get("target_files"):
        lines.append("Target files to create/modify:")
        for tf in spec_data["target_files"]:
            lines.append(f"  {tf}")

    if spec_path and FORWARD_COMPAT_HEADING in spec_path.read_text(encoding="utf-8"):
        lines.append(f"- **Forward compatibility**: the phase spec's `{FORWARD_COMPAT_HEADING}` section is binding. "
                     "Shape this phase's seams for the Context Gate that follows P12, but implement no part of the gate.")

    workspaces = EXTERNAL_WORKSPACES.get(phase_id, [])
    if workspaces:
        lines.append("\n### Authorized External Workspaces")
        lines.append("The Target Files above name only paths inside the maestro repo. This phase is ALSO authorized to")
        lines.append("work in the repositories below. Treat this section as standing authorization from Dan: do not stop")
        lines.append("to ask whether you may write there. Everything outside these paths and the maestro repo remains")
        lines.append("out of bounds, and destructive or irreversible actions still require confirmation.\n")
        for ws in workspaces:
            lines.append(f"**`{ws['path']}`** — {ws['role']}")
            lines.append(f"- **Current state**: {ws['state']}")
            lines.append(f"- **Entry point**: {ws['entry_point']}")
            lines.append(f"- **Authorized to change**: {ws['authorized']}")
            for pf in ws.get("preflight", []):
                lines.append(f"- ⚠️ **Pre-flight**: {pf}")
            lines.append(f"- 📋 **Instrumentation**: {ws['instrumentation']}")
            lines.append("")

    if spec_data.get("checklist") and status != "in_progress":
        lines.append("\n## 4. Implementation Checklist")
        for cl in spec_data["checklist"]:
            lines.append(cl)

    owned = owned_threads(state, phase_id)
    lines.append("\n## 4A. Open Threads Owned by This Phase (Mandatory)")
    if owned:
        lines.append("`STATE.yaml` assigns these open threads to this phase. Each one is in scope:")
        for ref, text in owned:
            lines.append(f"- [ ] **{ref}**: {text}")
    else:
        lines.append("No open threads are owned by this phase.")
    unstructured = unstructured_threads(state)
    if unstructured:
        lines.append("\n> [!WARNING]")
        lines.append("> UNSTRUCTURED OPEN THREADS: these entries name neither an `owner` nor a valid `disposition`.")
        lines.append("> Give each one a disposition before you close this session (format: STATE.yaml header).")
        for ref, _ in unstructured:
            lines.append(f"> - `{ref}`")

    lines.append("\n## 5. Verification & Acceptance Gate")
    lines.append("Run the focused test command inside the virtual environment:")
    lines.append(f"```bash\n{routing['test_cmd']}\n```")
    lines.append("Acceptance requires a 0 exit code with all assertions passing.")

    lines.append("\n## 6. End-of-Session State & Handoff Protocol (Mandatory)")
    lines.append("Before concluding your session, you MUST update `docs/graph-engineering/STATE.yaml`:")
    lines.append("1. **If Phase is Fully Completed and Verified**:")
    lines.append("   - Set `status: not_started`")
    lines.append(f"   - Add `{phase_id}` to `completed_phases` with completion timestamp and test result")
    # Determine next phase
    try:
        idx = PHASE_SEQUENCE.index(phase_id)
        next_phase = PHASE_SEQUENCE[idx + 1] if idx + 1 < len(PHASE_SEQUENCE) else "P12"
    except ValueError:
        next_phase = "P12"
    lines.append(f"   - Set `current_phase: {next_phase}`")
    lines.append("   - Set `in_progress_details: null`")
    lines.append("2. **If You Must Stop Mid-Phase (Incomplete / Handoff Needed)**:")
    lines.append("   - Set `status: in_progress`")
    lines.append(f"   - Set `current_phase: {phase_id}`")
    lines.append("   - Fill `in_progress_details`:")
    lines.append("     - `what_was_done`: list of completed checklist items / files")
    lines.append("     - `whats_left`: remaining checklist items / failing tests")
    lines.append("     - `relevant_context`: diagnostic notes, temporary state, blockers")
    lines.append("     - `active_branch`: `feat/graph-engineering-foundation`")
    lines.append("3. **Open Threads (both cases)**:")
    lines.append("   - Before marking the phase complete, close or re-own every thread listed in §4A: set "
                 "`disposition: closed` with a `note:` giving the evidence, or hand it on with `owner: <later phase>` "
                 "and a `note:` saying why.")
    lines.append(f"   - Record what this phase leaves behind under `_{phase_id.lower()}_open_threads`. Every entry "
                 "needs `owner: <phase>` or `disposition: accepted|closed|needs-dan` with a `note:`. Bare strings "
                 "are rejected.")
    lines.append("   - Edit STATE.yaml with targeted replacements only: another session may be writing it too.")

    lines.append("\n## 7. Design Drift & Escalation Invariant (Mandatory)")
    lines.append("> [!CAUTION]")
    lines.append("> If new evidence, unexpected architecture incompatibilities, or test observations arrive that would cause a material change to the original design (D01–D08) or architecture plan, you MUST STOP WORK IMMEDIATELY.")
    lines.append("> Do NOT unilaterally modify the architecture or compromise quality gates. Ask Dan for guidance using the `AskUserQuestion` tool.")

    lines.append("\n## 8. Session Close-Out Instruction (Mandatory)")
    lines.append("When all deliverables, verification checks, and `STATE.yaml` updates for this session are complete (or when stopping mid-phase), always invoke the `close-session` skill to review token budget, record open threads, update docs, and cleanly close out.")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate deterministic implementation prompts for Maestro Graph Engineering.")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="Path to STATE.yaml")
    parser.add_argument("--phase", type=str, default=None, help="Override phase (e.g. P00, P01)")
    parser.add_argument("--output", type=Path, default=None, help="Write prompt to file")
    parser.add_argument("--advance", action="store_true", help="Mark current phase complete and advance to next phase")
    parser.add_argument("--evidence", type=str, default="Verified by automated test suite", help="Evidence string for completion")
    args = parser.parse_args()

    state = load_state(args.state_file)

    if args.advance:
        curr = state.get("current_phase", "P00")
        try:
            curr_idx = PHASE_SEQUENCE.index(curr)
            next_p = PHASE_SEQUENCE[curr_idx + 1] if curr_idx + 1 < len(PHASE_SEQUENCE) else "COMPLETED"
        except ValueError:
            next_p = "P12"
            
        completed = state.get("completed_phases", [])
        completed.append({
            "phase": curr,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "evidence": args.evidence
        })
        state["completed_phases"] = completed
        state["current_phase"] = next_p
        state["status"] = "not_started"
        state["in_progress_details"] = None
        save_state(args.state_file, state)
        print(f"Advanced state: Phase {curr} marked complete -> Current phase is now {next_p}")
        return

    prompt = construct_prompt(state, args.phase)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(prompt, encoding="utf-8")
        print(f"Prompt written to {args.output}")
    else:
        print(prompt)


if __name__ == "__main__":
    main()
