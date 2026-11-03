"""Nullable W2 evidence. Observers name causes; this module maps them to faults.

These facts never decide a verdict or a retry. Versions identify the rubric/mapping
used at capture; the report may show a revised mapping alongside the immutable fact.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
from types import MappingProxyType

from maestro.workflows.review_origins import CAUSE_FAULT, UNCLASSIFIED


#: 2 since GDT-12: `task_did_not_serve_goal` maps to `unattributed` (R58, R62).
MAPPING_VERSION = 2
RUBRIC_VERSION = 1
COMPLEXITY_RUBRIC = MappingProxyType({
    1: {'files': '1 file', 'reach': 'one local function',
        'new_behavior': 'small correction, no new contract', 'risk': 'low, local and reversible'},
    2: {'files': '2–3 files', 'reach': 'one module and its tests',
        'new_behavior': 'extension of an existing contract', 'risk': 'bounded edge cases'},
    3: {'files': '4–8 files', 'reach': 'several interacting modules',
        'new_behavior': 'new feature or public contract', 'risk': 'shared state or compatibility'},
    4: {'files': '9–15 files', 'reach': 'several subsystems or a workflow',
        'new_behavior': 'new workflow or protocol', 'risk': 'security, concurrency or migration'},
    5: {'files': 'more than 15 files', 'reach': 'project-wide architecture',
        'new_behavior': 'new subsystem or architectural contract', 'risk': 'system-wide invariants'},
})


def complexity_prompt() -> str:
    anchors = '\n'.join(f"{score}: " + '; '.join(f'{key}: {value}' for key, value in axes.items())
                        for score, axes in COMPLEXITY_RUBRIC.items())
    return ('After reviewing the actual frozen diff, add "complexity": 1–5 to review.json. '
            'Use the highest applicable anchor across files, cross-module reach, new behavior '
            'and risk (file count alone is only a cross-check). This is an after-the-fact '
            'score of the task, not a forecast and not a verdict. Fixed rubric v1:\n' + anchors)


def forecast_prompt(path: str) -> str:
    """W13: the planner's before-the-run forecast, on the reviewer's rubric."""
    anchors = '\n'.join(f"{score}: " + '; '.join(f'{key}: {value}' for key, value in axes.items())
                        for score, axes in COMPLEXITY_RUBRIC.items())
    return (f'Forecast this task\'s complexity before it runs: record {{"complexity": 1-5}} in '
            f'the file {path} in the WORKSPACE (your write_file tool, if that is your harness, '
            'lands there too). Use the highest applicable anchor across files, cross-module '
            f'reach, new behavior and risk. Fixed rubric v{RUBRIC_VERSION}:\n' + anchors)


def review_complexity(document) -> dict:
    value = document.get('complexity') if isinstance(document, dict) else None
    valid = type(value) is int and 1 <= value <= 5
    # The parser normalizes invalid scores to None; keep that distinction at recapture
    # and when reading the recorded fact. Only the fixed score flags are meaningful.
    prior = document.get('complexity_flags', document.get('flags', ())) if isinstance(document, dict) else ()
    invalid = value is None and isinstance(prior, (list, tuple)) and 'complexity_invalid' in prior
    return {'complexity': value if valid else None, 'rubric_version': RUBRIC_VERSION,
            'flags': [] if valid else ['complexity_missing' if value is None and not invalid else 'complexity_invalid']}


def extra_step(cause, *, role=None, rebriefed_role=None, gate_admitted=None) -> dict:
    observed = cause.strip().lower().replace('-', '_').replace(' ', '_') if isinstance(cause, str) else None
    mapped = CAUSE_FAULT.get(observed, UNCLASSIFIED)
    flags = []
    if mapped == '$role':
        mapped = role if isinstance(role, str) and role.strip() else UNCLASSIFIED
    elif mapped == '$rebriefed_role':
        mapped = rebriefed_role if isinstance(rebriefed_role, str) and rebriefed_role.strip() else UNCLASSIFIED
    elif mapped == '$gate':
        mapped = ('router' if gate_admitted else 'none') if type(gate_admitted) is bool else UNCLASSIFIED
    if observed not in CAUSE_FAULT or observed == UNCLASSIFIED:
        flags.append('cause_missing' if not observed else 'cause_unknown')
        observed = UNCLASSIFIED
    if mapped == UNCLASSIFIED and not flags:
        flags.append('fault_context_missing')
    return {'extra_step_cause': observed, 'fault': mapped, 'flags': flags}


def diff_stats(tree: str | Path | None, base: str | None, head: str | None) -> dict:
    facts = {'base_sha': None, 'head_sha': None, 'files_changed': None,
             'insertions': None, 'deletions': None, 'binary_files': None, 'flags': []}
    if not tree or not base or not head:
        facts['flags'] = ['diff_unavailable']
        return facts
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tree), *args], stderr=subprocess.DEVNULL,
                                       timeout=15)
    try:
        # Resolve both ends first: no current worktree contents or mutable ref name is stored.
        b = git('rev-parse', '--verify', f'{base}^{{commit}}').decode().strip()
        h = git('rev-parse', '--verify', f'{head}^{{commit}}').decode().strip()
        fields = git('diff', '--no-ext-diff', '--no-renames', '--numstat', '-z', b, h, '--').split(b'\0')
        added = removed = files = binary = 0
        for record in fields:
            if not record:
                continue
            plus, minus, _path = record.split(b'\t', 2)
            files += 1
            if plus == b'-' or minus == b'-':
                binary += 1
            else:
                added += int(plus)
                removed += int(minus)
        facts.update(base_sha=b, head_sha=h, files_changed=files, insertions=added,
                     deletions=removed, binary_files=binary)
    except (OSError, subprocess.SubprocessError, ValueError):
        facts['flags'] = ['diff_unavailable']
    return facts
