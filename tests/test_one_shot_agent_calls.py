"""No module may reach an agent CLI by name. The gap this file tracked is closed.

`AgentBackend` describes a *session*: `launch` starts an agent in a tmux window, `resume`
continues it, `parse_exit` classifies how it died. It had no method for the other thing
maestro does constantly — ask a model one bounded question and read its output — so every
such call site shelled out to one CLI by name instead of going through the driver the
project had configured. Six of them did: `gates.sonnet_review_proofs`,
`docs/upcoming._opus_complete`, `selfheal/diagnose._judge_complete` (and `merge.py`
through it), the `/redo` runner, the self-fix runner, and the `/ask` handler. A project
configured `fallback_chain: [codex]` therefore had no working `--refresh-explanations`,
no `/redo`, no `/ask`, no self-fix and no failure diagnosis — each silently degrading to
`""` or a non-zero exit rather than failing loudly.

That answered the M4 open question ("should `upcoming.py` explanation generation be
backend-agnostic or pinned to one role?"). Backend-agnostic: `_opus_complete` was not a
documentation quirk but one instance of a systemic gap, and pinning it to a role would not
have helped, because the *role* already resolved to a backend that the call then ignored.

Both halves have landed. `AgentBackend.complete(CompletionSpec) -> Completion` arrived on
2026-08-26 and both drivers implement it; `maestro.agentcall.ask` routes a role to a driver;
and on 2026-08-30 the last of the six call sites was converted. `KNOWN_DIRECT_CALL_SITES`
is now empty and must stay that way — the assertion below is what keeps it closed, since
nothing else would notice a new hand-built argv until a non-claude project lost a feature.

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

#: Call sites that invoke an agent CLI directly instead of through a driver. Every entry
#: would be a place a non-claude project loses a feature. Empty since 2026-08-30, when the
#: last of the original six was converted — an entry added back here is a regression being
#: recorded, not a to-do being filed.
KNOWN_DIRECT_CALL_SITES: set = set()


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


def test_no_module_shells_out_to_an_agent_cli_directly():
    """A new one is a new feature that silently does nothing on a non-claude project."""
    unexpected = set(_scan()) - KNOWN_DIRECT_CALL_SITES
    assert not unexpected, (
        "these modules invoke an agent CLI by name instead of through its driver: "
        + ", ".join(sorted(unexpected))
        + " — ask the role through `maestro.agentcall`, so the call works on whichever "
          "backend the project actually configured"
    )


def test_the_known_direct_call_sites_still_exist_where_recorded():
    """Keeps the list honest in the other direction: a site that got fixed (or moved)
    must be struck off, so this file never overstates the remaining work. With the list
    empty this is a statement about the *file*, not about the package — it fails if
    someone records a site here without one actually being there."""
    stale = KNOWN_DIRECT_CALL_SITES - set(_scan())
    assert not stale, (
        "recorded as shelling out to an agent CLI, but no longer does: "
        + ", ".join(sorted(stale))
        + " — remove it from KNOWN_DIRECT_CALL_SITES"
    )


def test_every_ex_call_site_now_asks_a_role_instead():
    """The positive half: each converted module reaches an agent through `agentcall`.

    `test_no_module_shells_out_to_an_agent_cli_directly` only proves nobody spells an
    argv by hand — a module that dropped its agent call entirely would pass it just as
    happily. These six are the features that were silently dead on a non-claude project,
    so what matters is that they still make the call, through the seam.
    """
    converted = {
        "maestro/gates.py",
        "maestro/docs/upcoming.py",
        "maestro/selfheal/diagnose.py",
        "maestro/selfheal/redo.py",
        "maestro/selfheal/selffix.py",
        "maestro/hitl/ask.py",
    }
    for relative in sorted(converted):
        source = (REPO / relative).read_text(encoding="utf-8")
        assert "agentcall.ask(" in source, (
            f"{relative} no longer asks a role through maestro.agentcall — the feature it "
            "provides is dead on every backend if the call was dropped, and dead on all "
            "but one if it was hand-built again"
        )


def test_the_driver_protocol_offers_a_one_shot_completion():
    """The root cause, now closed: `complete()` landed on the protocol 2026-08-26.

    Asserted directly rather than described in a comment, because the call sites above can
    only be routed through a capability that exists. Being on the `Protocol` rather than a
    convention is what makes a driver that forgets it fail `isinstance` instead of failing
    a caller at runtime.
    """
    from maestro.backends.base import AgentBackend

    assert "complete" in vars(AgentBackend)


def test_every_registered_driver_implements_it():
    """A backend that cannot answer a bounded question cannot serve half of maestro."""
    from maestro.backends import registry
    from maestro.backends.base import AgentBackend

    for name in sorted(BACKENDS):
        driver = registry.get_backend(name)
        assert isinstance(driver, AgentBackend), f"{name} does not satisfy the protocol"
        assert callable(getattr(driver, "complete", None)), f"{name} has no complete()"
