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

import pathlib

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


@pytest.mark.parametrize("path, pattern, denied", [
    # A glob with a directory part denies what is under that directory, at any depth...
    ("secrets/key.txt", "secrets/*", True),
    ("config/secrets/key.txt", "secrets/*", True),
    ("config/secrets/deep/key.txt", "secrets/**", True),
    ("src/pay/charge.py", "src/pay/*.py", True),
    # ...and nothing else: its last segment alone is not the pattern.
    ("src/app.py", "secrets/*", False),
    ("src/app.py", "secrets/**", False),
    ("other/charge.py", "src/pay/*.py", False),
    # A name glob, bare or behind `**/`, still matches at the root and at any depth.
    ("key.pem", "**/*.pem", True),
    ("deploy/key.pem", "**/*.pem", True),
    ("deploy/key.pem", "*.pem", True),
    ("deploy/key.pem.txt", "**/*.pem", False),
    # `**/` is zero or more directories wherever it stands (W8 review F3).
    ("config/a.pem", "config/**/*.pem", True),
    ("config/tls/live/a.pem", "config/**/*.pem", True),
    ("src/secrets.yaml", "src/**/secrets.yaml", True),
    ("keys/a.pem", "**/keys/**/*.pem", True),
    ("vendor/keys/old/a.pem", "**/keys/**/*.pem", True),
    ("config/a.txt", "config/**/*.pem", False),
    ("other/a.pem", "config/**/*.pem", False),
    # A glob spelled from the root, or as a directory, names the same files.
    ("secrets/a", "/secrets/*", True),
    ("secrets/a", "./secrets/*", True),
    ("secrets/a", "secrets/*/", True),
    ("src/app.py", "/secrets/*", False),
    # `*/.env` is one directory down: a glob says what it says.
    ("deploy/.env", "*/.env", True),
    (".env", "*/.env", False),
])
def test_a_glob_entry_denies_what_it_names_and_nothing_else(path, pattern, denied):
    """P12 finding D10, edge 4: `secrets`, `prod_stores` and `risky_set` globs are globs.
    A directory glob used to be reduced to its last segment (`*`), which named every file."""
    assert c._denied_by(path, pattern) is denied


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


# ── rules_prose ──


def test_rules_prose_names_the_configured_allow_and_deny_surface():
    config = {"confinement": {"self_fix_allow": ["src/"]}, "secrets": [".env"]}
    prose = c.rules_prose(c.SELF_FIX, config)
    assert prose == (
        "A self-fix may only change files under: src/. "
        "It must NEVER change: .git/, .orchestrator/, .env."
    )


def test_rules_prose_names_the_actual_config_key_when_the_allow_list_is_empty():
    """`self_fix_allow: []` is a deliberate 'no self-fix at all' — the prose says so by
    kind label (`tests/characterization/test_selfheal.py` pins the substring "self-fix is
    disabled here" for this exact case) and, past that, names the real `confinement:` key
    an operator would edit to change it."""
    prose = c.rules_prose(c.SELF_FIX, {"confinement": {"self_fix_allow": []}})
    assert "self-fix is disabled here" in prose
    assert "confinement.self_fix_allow is empty in project.yaml" in prose


def test_rules_prose_uses_the_redo_label_and_key():
    prose = c.rules_prose(c.REDO, {"confinement": {"redo_allow": []}})
    assert "A /redo rewrite may only change files under:" in prose
    assert "/redo rewrite is disabled here" in prose
    assert "confinement.redo_allow is empty in project.yaml" in prose


def test_rules_prose_rejects_an_unrecognised_kind_loudly():
    """A typo'd `kind` should raise, the same way `_ALLOW_KEYS`'s own docstring wants a
    typo to be a `KeyError`/`AttributeError` rather than a prompt naming a raw string."""
    with pytest.raises(KeyError):
        c.rules_prose("not-a-real-kind", SCAFFOLD)


def test_rules_prose_reads_project_yaml_when_no_config_is_passed(monkeypatch):
    monkeypatch.setattr(c, "load_project_yaml",
                        lambda: {"confinement": {"redo_allow": ["exports/"]}})
    assert "exports/" in c.rules_prose(c.REDO)


# ── available ──


def test_available_true_for_an_existing_file(tmp_path):
    f = tmp_path / "check_notebook.py"
    f.write_text("", encoding="utf-8")
    assert c.available(f) is True


def test_available_false_for_a_missing_file(tmp_path):
    assert c.available(tmp_path / "nope.py") is False


def test_available_false_for_a_directory():
    """`.is_file()`, not `.exists()` — a directory shadowing the script's name must not
    read as "the script is here"."""
    assert c.available(pathlib.Path(__file__).parent) is False


# ── [P7] §4: branch namespacing and the git commit boundary ──────────────────────────
#
# A different question from everything above. `path_ok` asks which *files in the diff* an
# autonomous change may rewrite; these ask which *paths on the filesystem* a confined
# implementer process may write at all. The tests are together in one file because the
# module is one module, and deliberately separated by this banner because conflating the
# two questions is the mistake the second half exists to prevent.


def _boundary(tmp_path, **kwargs):
    return c.CommitBoundary.for_task(tmp_path / "repo", "T-001", **kwargs)


def test_every_namespace_produces_a_hierarchical_name():
    assert c.branch_name("impl", "T-001") == "impl/T-001"
    assert c.branch_name("selffix", "T-001", attempt_id="a7") == "selffix/T-001_a7"
    assert c.branch_name("redo", "T-001", attempt_id="a7") == "redo/T-001_a7"
    assert c.branch_name("impl", "T-001", candidate="c2") == "impl/T-001/c2"


def test_a_flat_branch_name_is_not_one_of_ours():
    """`[INV-A08]`'s negative half. A flat name puts a task's ref in the same directory as
    `refs/heads/main`, which collapses "may write its own ref" and "may write the target
    branch" into one filesystem permission."""
    for flat in ("task-01", "T-001", "main", "feature/x"):
        assert c.is_namespaced(flat) is False
    assert c.parse_branch("task-01") is None


def test_a_flat_branch_cannot_be_given_a_boundary_at_all(tmp_path):
    with pytest.raises(c.BranchNameError):
        c.CommitBoundary(repo=tmp_path, branch="task-01", worktree=tmp_path / "wt")


def test_a_selffix_branch_is_per_attempt_and_an_impl_branch_is_not():
    """Two attempts sharing a self-fix branch would make the second one's history the
    first one's."""
    with pytest.raises(c.BranchNameError):
        c.branch_name("selffix", "T-001")
    with pytest.raises(c.BranchNameError):
        c.branch_name("impl", "T-001", attempt_id="a7")


def test_a_task_id_that_could_escape_the_namespace_is_refused():
    for hostile in ("../main", "a/b", "-force", ".hidden", "x.lock"):
        with pytest.raises(c.BranchNameError):
            c.branch_name("impl", hostile)


def test_an_unknown_namespace_is_refused():
    with pytest.raises(c.BranchNameError):
        c.branch_name("wip", "T-001")


def test_parsing_round_trips_every_namespace():
    for name in ("impl/T-001", "impl/T-001/c2", "selffix/T-001_a7", "redo/T-001_a7"):
        assert str(c.parse_branch(name)) == name
        assert str(c.parse_branch(f"refs/heads/{name}")) == name


# ── §4.2: the four writable paths, and nothing else ──────────────────────────────────


def test_the_boundary_is_exactly_the_four_paths_the_spec_enumerates(tmp_path):
    boundary = _boundary(tmp_path)
    repo = tmp_path / "repo"
    assert boundary.writable_roots() == (
        repo / ".orchestrator" / "worktrees" / "T-001",
        repo / ".orchestrator" / "worktrees" / "T-001" / ".git",
        repo / ".git" / "objects",
        repo / ".git" / "refs" / "heads" / "impl" / "T-001",
    )


def test_the_worktree_its_git_dir_and_the_shared_object_store_are_writable(tmp_path):
    boundary = _boundary(tmp_path)
    repo = tmp_path / "repo"
    for path in (
        repo / ".orchestrator" / "worktrees" / "T-001" / "src" / "a.py",
        repo / ".orchestrator" / "worktrees" / "T-001" / ".git" / "index",
        repo / ".git" / "objects" / "ab" / "cdef",
    ):
        assert boundary.permits(path)[0] is True, path


def test_the_agent_may_advance_its_own_ref_and_its_own_reflog(tmp_path):
    boundary = _boundary(tmp_path)
    repo = tmp_path / "repo"
    assert boundary.permits(repo / ".git" / "refs" / "heads" / "impl" / "T-001")[0] is True
    assert boundary.permits(
        repo / ".git" / "logs" / "refs" / "heads" / "impl" / "T-001"
    )[0] is True


def test_the_target_branch_is_physically_blocked_and_says_which_rule_fired(tmp_path):
    ok, reason = _boundary(tmp_path).permits(
        tmp_path / "repo" / ".git" / "refs" / "heads" / "main"
    )
    assert ok is False
    assert "INV-05" in reason


def test_another_tasks_ref_is_blocked_even_though_it_is_namespaced(tmp_path):
    ok, reason = _boundary(tmp_path).permits(
        tmp_path / "repo" / ".git" / "refs" / "heads" / "impl" / "T-002"
    )
    assert ok is False
    assert "outside this task's namespace" in reason


def test_packed_refs_is_blocked_because_it_is_the_other_way_to_move_main(tmp_path):
    """A loose ref file is not the only writable representation of a branch head."""
    for name in ("packed-refs", "HEAD", "config", "hooks/pre-commit"):
        ok, reason = _boundary(tmp_path).permits(tmp_path / "repo" / ".git" / name)
        assert ok is False, name


def test_anything_outside_the_worktree_fails_closed(tmp_path):
    boundary = _boundary(tmp_path)
    for path in ("/etc/passwd", tmp_path / "repo" / "src" / "a.py", "../../elsewhere"):
        assert boundary.permits(path)[0] is False, path


def test_a_traversal_is_refused_rather_than_normalised(tmp_path):
    ok, reason = _boundary(tmp_path).permits(".orchestrator/worktrees/T-001/../../../etc/x")
    assert ok is False
    assert "escapes its sandbox" in reason


def test_candidates_get_sibling_directories_under_one_task_root(tmp_path):
    """`[INV-06]` as a filesystem fact: one bind mount of the task directory grants
    exactly that task's candidates and nothing else."""
    first = _boundary(tmp_path, candidate="c1")
    second = _boundary(tmp_path, candidate="c2")
    assert first.worktree != second.worktree
    assert first.worktree.parent == second.worktree.parent
    assert first.permits(second.worktree / "a.py")[0] is False
    assert first.permits(
        tmp_path / "repo" / ".git" / "refs" / "heads" / "impl" / "T-001" / "c2"
    )[0] is False


# ── §4.2: the mount hazard ───────────────────────────────────────────────────────────


def test_a_write_that_vanished_on_unmount_is_an_execution_failure(tmp_path):
    """Masking a parent while binding a child leaves a writable tmpfs between them: the
    write succeeds, the process sees its own file, and the bytes are gone at unmount.
    Indistinguishable from success at the time, so it is checked afterwards."""
    boundary = _boundary(tmp_path)
    boundary.worktree.mkdir(parents=True)
    survived = boundary.worktree / "kept.py"
    survived.write_text("x = 1\n")

    assert boundary.confirm_persisted([survived]) == (True, "ok")

    ok, reason = boundary.confirm_persisted([survived, boundary.worktree / "vanished.py"])
    assert ok is False
    assert "did not survive the sandbox" in reason


def test_confirming_a_write_outside_the_boundary_is_its_own_failure(tmp_path):
    ok, reason = _boundary(tmp_path).confirm_persisted(["/etc/passwd"])
    assert ok is False
    assert "outside the boundary" in reason


# ── P12B F6: restoring a retry's worktree to the SHA its previous attempt started from ─


def _worktree_repo(path):
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()
    return head


def _head(path):
    import subprocess

    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_a_retry_starts_from_the_sha_its_predecessor_claimed_at(tmp_path):
    import subprocess

    boundary = _boundary(tmp_path)
    start = _worktree_repo(boundary.worktree)
    (boundary.worktree / "junk.py").write_text("half an answer\n", encoding="utf-8")
    subprocess.run(["git", "add", "junk.py"], cwd=boundary.worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "junk"], cwd=boundary.worktree, check=True)
    (boundary.worktree / "stray.txt").write_text("untracked\n", encoding="utf-8")
    assert _head(boundary.worktree) != start

    boundary.restore(start)

    assert _head(boundary.worktree) == start
    assert not (boundary.worktree / "junk.py").exists()
    assert not (boundary.worktree / "stray.txt").exists()


def test_a_restore_refuses_anything_that_is_not_this_boundarys_own_worktree(tmp_path):
    import dataclasses

    boundary = _boundary(tmp_path)
    _worktree_repo(boundary.worktree)
    calls = []
    outside = dataclasses.replace(boundary, worktree=boundary.repo)
    with pytest.raises(c.ConfinementError, match="repository root"):
        outside.restore("HEAD", runner=calls.append)
    stray = dataclasses.replace(boundary, worktree=tmp_path / "elsewhere")
    with pytest.raises(c.ConfinementError, match="worktrees"):
        stray.restore("HEAD", runner=calls.append)
    with pytest.raises(c.ConfinementError, match="sha"):
        boundary.restore("", runner=calls.append)
    assert calls == [], "nothing is ever run before the target is confirmed"


def test_a_restore_that_git_refuses_is_raised_not_swallowed(tmp_path):
    boundary = _boundary(tmp_path)
    _worktree_repo(boundary.worktree)
    with pytest.raises(c.ConfinementError, match="deadbee"):
        boundary.restore("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
