"""`maestro.confinement` — what an autonomous change may touch in *this* project.

The two self-heal gates each carried a tuple written for the reference project's
directory layout. After the extraction that made the self-fix gate reject everything
(nothing lives under `scripts/` or `orchestrator/` in a scaffolded project) and the deny
half match nothing (`main_bot.py`, `data/`, `models/` are one project's files) — so the
feature was dead on every project but one, and unguarded on the rest. These tests pin the
replacement, including the two holes that were previously pinned as bugs: the
`lstrip("./")` character-set strip, and `prod_stores` having no reader at all.

Nothing here touches disk or configuration on disk: every case passes `config` explicitly,
which is the same seam `maestro.metrics` uses and the reason both are testable at all.
"""
from __future__ import annotations

import pytest

from maestro import confinement as c

SCAFFOLD: dict = {}                       # a fresh `init`, nothing declared yet


# ── allow_prefixes ──


def test_the_defaults_are_the_scaffolding_init_creates():
    """The only surface maestro can assume exists in a project it was just pointed at."""
    assert c.allow_prefixes(c.SELF_FIX, SCAFFOLD) == ("adapters/", "profiles/", "docs/")
    assert c.allow_prefixes(c.REDO, SCAFFOLD) == ("docs/",)


def test_a_configured_surface_replaces_the_default():
    config = {"confinement": {"self_fix_allow": ["src/"], "redo_allow": ["colab/"]}}
    assert c.allow_prefixes(c.SELF_FIX, config) == ("src/",)
    assert c.allow_prefixes(c.REDO, config) == ("colab/",)


def test_an_explicitly_empty_list_is_not_the_same_as_an_absent_one():
    """`self_fix_allow: []` is a project saying it permits no unattended self-fix. That
    is a reasonable thing to say, and silently substituting the default would widen what
    an autonomous change may touch — the one direction a config error must never go."""
    config = {"confinement": {"self_fix_allow": []}}
    assert c.allow_prefixes(c.SELF_FIX, config) == ()
    ok, reason = c.path_ok(["docs/a.md"], kind=c.SELF_FIX, config=config)
    assert ok is False and "no path is permitted" in reason


def test_a_scalar_is_read_as_a_one_element_list():
    """`redo_allow: docs/` is the mistake a hand-edited YAML file makes."""
    assert c.allow_prefixes(c.REDO, {"confinement": {"redo_allow": "docs/"}}) == ("docs/",)


@pytest.mark.parametrize("document", [
    {"confinement": None}, {"confinement": "nope"}, {"confinement": []}, "not-a-mapping",
], ids=["null", "string", "list", "document-not-a-mapping"])
def test_a_malformed_confinement_block_falls_back_to_the_defaults(document):
    assert c.allow_prefixes(c.REDO, document) == ("docs/",)


def test_an_unknown_kind_permits_nothing():
    """A typo is an empty surface, not an unrestricted one."""
    assert c.allow_prefixes("made_up", SCAFFOLD) == ()
    assert c.path_ok(["docs/a.md"], kind="made_up", config=SCAFFOLD)[0] is False


# ── deny_fragments ──


def test_git_and_the_orchestrator_state_are_always_denied():
    """Not in `secrets` and never should be: an autonomous rewrite of the repository's
    own object store is not a change, it is a loss."""
    assert ".git/" in c.deny_fragments(SCAFFOLD)
    assert ".orchestrator/" in c.deny_fragments(SCAFFOLD)


def test_secrets_and_prod_stores_are_deny_surfaces():
    config = {"secrets": [".env"], "prod_stores": ["db/prod.sqlite"],
              "confinement": {"deny": ["vendor/"]}}
    fragments = c.deny_fragments(config)
    assert {".env", "db/prod.sqlite", "vendor/"} <= set(fragments)


def test_deny_list_extra_is_deliberately_not_a_path_surface():
    """Those are regexes matched against diff *text* (`merge._HARD_DENY_PATTERNS` and
    friends). Treating one as a path fragment would match nothing or everything."""
    assert r"git\s+push\s+.*--force" not in c.deny_fragments(
        {"deny_list_extra": [r"git\s+push\s+.*--force"]})


def test_fragments_are_deduplicated_in_order():
    config = {"secrets": [".env", ".env"], "confinement": {"deny": [".env"]}}
    fragments = c.deny_fragments(config)
    assert fragments.count(".env") == 1


# ── path_ok: the deny half ──


def test_a_secret_inside_an_allowed_directory_is_still_refused():
    config = {"secrets": [".env"], "confinement": {"self_fix_allow": ["docs/"]}}
    assert c.path_ok(["docs/.env"], kind=c.SELF_FIX, config=config) == (
        False, "denied path: docs/.env")


def test_a_glob_secret_matches_by_basename():
    """The template ships `secrets: ["**/*.pem", "**/*credentials*.json"]`, and
    `merge.deny_list_guard` reduces those to `*.pem` *substrings* — which match no real
    filename at all. A pattern with glob metacharacters is matched as a glob here."""
    config = {"secrets": ["**/*.pem", "**/*credentials*.json"],
              "confinement": {"self_fix_allow": ["docs/"]}}
    assert c.path_ok(["docs/deploy.pem"], kind=c.SELF_FIX, config=config)[0] is False
    assert c.path_ok(["docs/svc-credentials.json"], kind=c.SELF_FIX, config=config)[0] is False
    assert c.path_ok(["docs/notes.md"], kind=c.SELF_FIX, config=config)[0] is True


def test_a_pattern_without_metacharacters_is_a_plain_substring():
    """`.env` denies `config/.env`; `data/prod.db` denies itself. This is the behaviour
    every caller already expected of a deny entry."""
    config = {"secrets": [".env"], "confinement": {"self_fix_allow": ["config/"]}}
    assert c.path_ok(["config/.env"], kind=c.SELF_FIX, config=config)[0] is False


def test_deny_is_checked_before_allow():
    """A denied path is reported as denied, not as out-of-scope — the operator is told the
    stronger reason."""
    config = {"secrets": ["secret.txt"], "confinement": {"self_fix_allow": []}}
    assert c.path_ok(["elsewhere/secret.txt"], kind=c.SELF_FIX, config=config) == (
        False, "denied path: elsewhere/secret.txt")


# ── path_ok: normalisation ──


@pytest.mark.parametrize("raw", ["docs/a.md", "./docs/a.md", "  ./docs/a.md  ",
                                 "/docs/a.md", "./docs/./a.md"],
                         ids=["plain", "dot-slash", "padded", "leading-slash", "inner-dot"])
def test_equivalent_spellings_of_an_allowed_path_all_pass(raw):
    """A leading slash is a repo-relative path spelled with one — that is how a diff
    sometimes arrives — and `.` components mean nothing. Both are normalised."""
    assert c.path_ok([raw], kind=c.REDO, config=SCAFFOLD) == (True, "ok")


@pytest.mark.parametrize("raw", ["../docs/a.md", "docs/../../etc/passwd", "../../etc/passwd"],
                         ids=["parent", "escape-via-inner", "double-parent"])
def test_a_parent_traversal_is_refused_not_stripped(raw):
    """The bug both gates shared, previously pinned rather than fixed: `lstrip("./")`
    strips a *character set*, so `../etc/passwd` arrived at the prefix check as
    `etc/passwd` — a path outside the repository laundered into one that looks inside it.
    An unattended agent chooses these paths, which is why this one was worth fixing."""
    ok, reason = c.path_ok([raw], kind=c.REDO, config=SCAFFOLD)
    assert ok is False
    assert reason.startswith("path escapes the repository:")


def test_a_bare_string_is_one_path_not_a_sequence_of_characters():
    """Also previously pinned as a bug: a string `target_files` was iterated character by
    character, so an allowed path was rejected on its own first letter."""
    assert c.path_ok("docs/a.md", kind=c.REDO, config=SCAFFOLD) == (True, "ok")


@pytest.mark.parametrize("files", [[], None, ["", "   "], 7, {"a": 1}],
                         ids=["empty", "none", "blanks", "int", "mapping"])
def test_nothing_to_check_is_a_refusal(files):
    """Matching both gates: a self-fix with no target files has nothing to confine, and a
    `/redo` that changed nothing has produced nothing to ship."""
    assert c.path_ok(files, kind=c.REDO, config=SCAFFOLD) == (False, "no files identified")


def test_one_bad_path_vetoes_the_whole_set():
    assert c.path_ok(["docs/a.md", "src/b.py"], kind=c.REDO, config=SCAFFOLD) == (
        False, "out-of-scope path: src/b.py")


def test_a_non_string_entry_is_stringified_rather_than_crashing():
    ok, reason = c.path_ok([7], kind=c.REDO, config=SCAFFOLD)
    assert ok is False and reason == "out-of-scope path: 7"


def test_path_ok_reads_project_yaml_when_no_config_is_passed(monkeypatch):
    monkeypatch.setattr(c, "load_project_yaml",
                        lambda: {"confinement": {"redo_allow": ["exports/"]}})
    assert c.path_ok(["exports/a.csv"], kind=c.REDO) == (True, "ok")
    assert c.path_ok(["docs/a.md"], kind=c.REDO)[0] is False
