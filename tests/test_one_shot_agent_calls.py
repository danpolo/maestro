"""The one-shot agent call is not backend-agnostic. This pins how far the gap reaches.

`AgentBackend` describes a *session*: `launch` starts an agent in a tmux window, `resume`
continues it, `parse_exit` classifies how it died. It has no method for the other thing
maestro does constantly — ask a model one bounded question and read stdout. So every such
call site shells out to one CLI by name instead of going through the driver it was
configured to use.

That answers the M4 open question ("should `upcoming.py` explanation generation be
backend-agnostic or pinned to one role?"). Backend-agnostic: `upcoming.py`'s
`_opus_complete` is not a documentation quirk, it is one instance of a systemic gap, and
pinning it to a role would not help because the *role* already resolves to a backend that
the call then ignores. A project configured `fallback_chain: [codex]` has no working
`--refresh-explanations`, no `/redo`, no `/ask`, no self-fix and no failure diagnosis —
each one silently degrades to "" or a non-zero exit rather than failing loudly.

Closing it means adding a one-shot capability to the protocol — roughly

    def complete(self, prompt: str, *, model: str = "", timeout: int = 240) -> str

implemented by both drivers, declared through `capabilities()` so callers can ask rather
than assume, with each site below resolving its driver from its role. That is a
cross-cutting change to a live launch path and is deliberately not bundled into the commit
that added this test.

`tests/test_no_name_branching.py` does not catch these: a backend name in an argv list is
explicitly allowed there, and correctly so — the argv *is* how you spell a CLI invocation.
The defect is not the literal, it is that no driver was consulted before writing it.
"""
from __future__ import annotations

import ast
from pathlib import Path

from maestro.backends.registry import BACKENDS

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "maestro"

#: Every known agent CLI, taken from the registry so a backend added later is covered here
#: the moment it is registered.
BACKEND_NAMES = frozenset(BACKENDS)

#: Call sites that invoke an agent CLI directly instead of through a driver, as
#: `module: what it is for`. Every entry is a place a non-claude project loses a feature.
#: The list is meant to shrink; removing an entry is the acceptance test for routing that
#: site through the protocol's one-shot capability.
KNOWN_DIRECT_CALL_SITES = {
    "maestro/gates.py",              # sonnet_review_proofs — manual-verification proof review
    "maestro/docs/upcoming.py",      # _opus_complete — refresh_explanations (the M4 question)
    "maestro/selfheal/diagnose.py",  # _judge_complete — failure diagnosis, and merge.py via it
    "maestro/selfheal/redo.py",      # the /redo runner
    "maestro/selfheal/selffix.py",   # the self-fix runner
    "maestro/hitl/ask.py",           # the /ask handler
}


def _direct_agent_cli_calls(tree: ast.AST) -> list[int]:
    """Line numbers of list literals that are an agent-CLI argv.

    An argv is `[<backend-name>, <flag>, ...]` — the name first, followed by at least one
    more element. Matched wherever it appears rather than only in argument position,
    because these are as often built into a local (`cmd = ["claude", "-p", ...]`, then
    `subprocess.run(cmd)`) as passed inline.

    A bare `["claude"]` or `["claude", "codex"]` is not an argv — the first is a probe,
    the second a chain of names — so a second element that is itself a backend name does
    not count, and neither does a single-element list. A `BACKENDS = {"claude": ...}`
    table and a plain `name = "claude"` are untouched either way.
    """
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        head, second = node.elts[0], node.elts[1]
        if not (isinstance(head, ast.Constant) and head.value in BACKEND_NAMES):
            continue
        if isinstance(second, ast.Constant) and second.value in BACKEND_NAMES:
            continue
        hits.append(node.lineno)
    return hits


def _scan() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        lines = _direct_agent_cli_calls(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            found[path.relative_to(REPO).as_posix()] = lines
    return found


def test_no_new_module_shells_out_to_an_agent_cli_directly():
    """A new one is a new feature that silently does nothing on a non-claude project."""
    unexpected = set(_scan()) - KNOWN_DIRECT_CALL_SITES
    assert not unexpected, (
        "these modules invoke an agent CLI by name instead of through its driver: "
        + ", ".join(sorted(unexpected))
        + " — resolve the backend from the role and go through the driver, so the call "
          "works on whichever backend the project actually configured"
    )


def test_the_known_direct_call_sites_still_exist_where_recorded():
    """Keeps the list honest in the other direction: a site that got fixed (or moved)
    must be struck off, so this file never overstates the remaining work."""
    stale = KNOWN_DIRECT_CALL_SITES - set(_scan())
    assert not stale, (
        "recorded as shelling out to an agent CLI, but no longer does: "
        + ", ".join(sorted(stale))
        + " — remove it from KNOWN_DIRECT_CALL_SITES"
    )


def test_the_driver_protocol_has_no_one_shot_completion_method():
    """The root cause, asserted directly rather than described in a comment.

    When this fails, the capability has been added — which is the moment the call sites
    above can start being routed through it, and the moment this test should be replaced
    by one asserting every driver implements it.
    """
    from maestro.backends.base import AgentBackend

    surface = {name for name in vars(AgentBackend) if not name.startswith("_")}
    assert not (surface & {"complete", "one_shot", "prompt"}), (
        "AgentBackend now offers a one-shot completion — route "
        f"{len(KNOWN_DIRECT_CALL_SITES)} direct call sites through it and update this test"
    )
