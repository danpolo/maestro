"""Pinned behaviour of the project-config loader.

Characterisation, not specification. The reference implementation is three lines::

    def _load_project_yaml() -> dict:
        try:
            return yaml.safe_load(PROJECT_YAML.read_text()) or {}
        except Exception:
            return {}

so almost everything interesting about it is what it *hides*: a missing file, a syntax
error, a directory, an unreadable file and a legitimately empty document all produce the
same empty dict, and the ``-> dict`` annotation is not enforced. Every test below pins
what actually happens, including where that is surprising.
"""
import codecs
import locale
import os

import pytest

pytestmark = pytest.mark.maestro_module("config")


def _loader(subject):
    """Prefer the public alias; fall back to the reference's private name.

    The extracted module exposes both spellings; the reference only has the private one.
    """
    fn = getattr(subject, "load_project_yaml", None)
    if fn is None:
        fn = getattr(subject, "_load_project_yaml", None)
    assert fn is not None, "subject exposes neither load_project_yaml nor _load_project_yaml"
    return fn


def _write(sandbox, text: str) -> None:
    sandbox.repo.mkdir(parents=True, exist_ok=True)
    (sandbox.repo / "project.yaml").write_text(text, encoding="utf-8")


# --- naming ------------------------------------------------------------------------


def test_private_name_is_always_available(subject):
    assert callable(subject._load_project_yaml)


def test_public_alias_is_an_alias_never_a_second_implementation(subject):
    """The reference has only the private spelling; the extracted module adds a public
    alias. Either is fine — what must never happen is a *second implementation* under the
    public name, so the alias, when it exists, must be the identical function object.
    Written without a skip so both subjects execute the assertion (M1 requires zero skips).
    """
    public = getattr(subject, "load_project_yaml", None)
    assert public is None or public is subject._load_project_yaml


def test_every_available_spelling_returns_the_same_document(subject, sandbox):
    """Whatever spellings a subject exposes, they all read the same file and agree."""
    _write(sandbox, "name: demo\n")
    spellings = [
        getattr(subject, name)
        for name in ("load_project_yaml", "_load_project_yaml")
        if getattr(subject, name, None) is not None
    ]
    assert spellings, "subject exposes no loader at all"
    assert all(fn() == {"name": "demo"} for fn in spellings)


# --- the global it reads -----------------------------------------------------------


def test_project_yaml_global_is_project_yaml_at_the_repo_root(subject):
    assert subject.PROJECT_YAML.name == "project.yaml"
    assert subject.PROJECT_YAML.parent == subject.REPO


def test_reads_the_module_global_not_the_working_directory(subject, sandbox, tmp_path, monkeypatch):
    """A decoy project.yaml in the cwd is ignored; only PROJECT_YAML is consulted."""
    decoy = tmp_path / "cwd"
    decoy.mkdir()
    (decoy / "project.yaml").write_text("name: decoy\n", encoding="utf-8")
    monkeypatch.chdir(decoy)
    _write(sandbox, "name: real\n")
    assert _loader(subject)() == {"name": "real"}


def test_takes_no_arguments(subject, sandbox):
    _write(sandbox, "name: demo\n")
    with pytest.raises(TypeError):
        _loader(subject)(sandbox.repo / "project.yaml")


# --- the happy path ----------------------------------------------------------------


def test_valid_mapping_with_several_list_keys(subject, sandbox):
    _write(
        sandbox,
        "name: demo\n"
        "self_modifying_paths:\n"
        "  - scripts/orchestrator_run.py\n"
        "  - adapters/smoke.py\n"
        "protected_paths:\n"
        "  - .env\n"
        "  - secrets/\n"
        "  - deploy/prod.yaml\n"
        "smoke:\n"
        "  cmd: pytest -q\n"
        "  timeout: 300\n",
    )
    assert _loader(subject)() == {
        "name": "demo",
        "self_modifying_paths": ["scripts/orchestrator_run.py", "adapters/smoke.py"],
        "protected_paths": [".env", "secrets/", "deploy/prod.yaml"],
        "smoke": {"cmd": "pytest -q", "timeout": 300},
    }


def test_scalars_keep_their_yaml_types(subject, sandbox):
    """No coercion or validation: YAML's own typing wins, including `no` -> False."""
    _write(sandbox, "count: 3\nratio: 1.5\nenabled: true\nlegacy: no\nmissing: null\n")
    assert _loader(subject)() == {
        "count": 3,
        "ratio": 1.5,
        "enabled": True,
        "legacy": False,
        "missing": None,
    }


def test_nothing_is_validated(subject, sandbox):
    """Unknown keys and wrong-typed known keys pass straight through."""
    _write(sandbox, "self_modifying_paths: not-a-list\ntotally_unknown_key: 7\n")
    assert _loader(subject)() == {
        "self_modifying_paths": "not-a-list",
        "totally_unknown_key": 7,
    }


def test_duplicate_keys_silently_keep_the_last_value(subject, sandbox):
    _write(sandbox, "name: first\nname: second\n")
    assert _loader(subject)() == {"name": "second"}


def test_anchors_and_aliases_are_expanded(subject, sandbox):
    _write(sandbox, "base: &base\n  a: 1\ncopy: *base\n")
    assert _loader(subject)() == {"base": {"a": 1}, "copy": {"a": 1}}


def test_yaml_escapes_produce_non_ascii_strings(subject, sandbox):
    """Locale-independent: the file bytes are pure ASCII, the value is not."""
    _write(sandbox, 'name: "caf\\u00e9 \\u2705"\n')
    assert _loader(subject)() == {"name": "café ✅"}


def test_non_ascii_bytes_decode_under_a_utf8_locale(subject, sandbox):
    """`read_text()` passes no encoding, so decoding follows the process locale."""
    if codecs.lookup(locale.getpreferredencoding(False)).name != "utf-8":
        pytest.skip("ambient default encoding is not UTF-8")
    sandbox.repo.mkdir(parents=True, exist_ok=True)
    (sandbox.repo / "project.yaml").write_bytes("name: café — ✅\n".encode("utf-8"))
    assert _loader(subject)() == {"name": "café — ✅"}


# --- everything that fails looks identical -----------------------------------------


def test_missing_file_returns_empty_dict(subject, sandbox):
    assert not (sandbox.repo / "project.yaml").exists()
    assert _loader(subject)() == {}


def test_missing_parent_directory_returns_empty_dict(subject, sandbox, monkeypatch):
    target = sandbox.repo / "no-such-dir" / "project.yaml"
    assert not target.parent.exists()
    monkeypatch.setattr(subject, "PROJECT_YAML", target)
    assert _loader(subject)() == {}


def test_malformed_yaml_returns_empty_dict(subject, sandbox):
    """A syntax error is indistinguishable from an absent config."""
    _write(sandbox, "name: [unclosed\n  bad: : :\n")
    assert _loader(subject)() == {}


def test_tab_indentation_returns_empty_dict(subject, sandbox):
    _write(sandbox, "root:\n\tchild: 1\n")
    assert _loader(subject)() == {}


def test_python_object_tag_is_refused_and_returns_empty_dict(subject, sandbox):
    """safe_load refuses arbitrary tags, and the bare except turns that into `{}`."""
    _write(sandbox, "evil: !!python/object/apply:os.system ['echo pwned']\n")
    assert _loader(subject)() == {}


def test_empty_file_returns_empty_dict(subject, sandbox):
    _write(sandbox, "")
    assert _loader(subject)() == {}


def test_comments_only_document_returns_empty_dict(subject, sandbox):
    _write(sandbox, "# just a comment\n")
    assert _loader(subject)() == {}


def test_document_that_parses_to_none_returns_empty_dict(subject, sandbox):
    _write(sandbox, "--- null\n")
    assert _loader(subject)() == {}


def test_directory_in_place_of_the_file_returns_empty_dict(subject, sandbox):
    (sandbox.repo / "project.yaml").mkdir(parents=True)
    assert _loader(subject)() == {}


def test_broken_symlink_returns_empty_dict(subject, sandbox):
    sandbox.repo.mkdir(parents=True, exist_ok=True)
    (sandbox.repo / "project.yaml").symlink_to(sandbox.repo / "does-not-exist.yaml")
    assert _loader(subject)() == {}


def test_undecodable_bytes_return_empty_dict(subject, sandbox):
    """read_text() uses the locale encoding, so a bad byte is swallowed like a syntax error."""
    sandbox.repo.mkdir(parents=True, exist_ok=True)
    (sandbox.repo / "project.yaml").write_bytes(b"name: \xff\xfe\x00bad\n")
    assert _loader(subject)() == {}


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)
def test_unreadable_file_returns_empty_dict(subject, sandbox):
    path = sandbox.repo / "project.yaml"
    _write(sandbox, "name: demo\n")
    path.chmod(0o000)
    try:
        assert _loader(subject)() == {}
    finally:
        path.chmod(0o600)


# --- the return type is not a dict -------------------------------------------------


def test_top_level_list_is_returned_as_a_list(subject, sandbox):
    """FOUND_BUGS: annotated `-> dict`, but a sequence document comes back as a list."""
    _write(sandbox, "- one\n- two\n")
    result = _loader(subject)()
    assert result == ["one", "two"]
    assert isinstance(result, list)


def test_empty_top_level_list_collapses_to_empty_dict(subject, sandbox):
    """`or {}` is falsiness, not a type check: `[]` becomes `{}`."""
    _write(sandbox, "[]\n")
    assert _loader(subject)() == {}


def test_top_level_string_is_returned_as_a_string(subject, sandbox):
    _write(sandbox, "just a string\n")
    result = _loader(subject)()
    assert result == "just a string"
    assert isinstance(result, str)


def test_top_level_zero_collapses_to_empty_dict(subject, sandbox):
    """Any falsy document — 0, false, '', [] — is erased by `or {}`."""
    _write(sandbox, "0\n")
    assert _loader(subject)() == {}


def test_top_level_false_collapses_to_empty_dict(subject, sandbox):
    _write(sandbox, "false\n")
    assert _loader(subject)() == {}


def test_multi_document_stream_returns_empty_dict(subject, sandbox):
    """safe_load (not safe_load_all) raises on a second document, so `{}` comes back."""
    _write(sandbox, "a: 1\n---\nb: 2\n")
    assert _loader(subject)() == {}


# --- no caching --------------------------------------------------------------------


def test_second_call_sees_an_edit_between_calls(subject, sandbox):
    load = _loader(subject)
    _write(sandbox, "name: before\n")
    assert load() == {"name": "before"}
    _write(sandbox, "name: after\n")
    assert load() == {"name": "after"}


def test_second_call_sees_a_deletion_between_calls(subject, sandbox):
    load = _loader(subject)
    _write(sandbox, "name: before\n")
    assert load() == {"name": "before"}
    (sandbox.repo / "project.yaml").unlink()
    assert load() == {}


def test_returned_mapping_is_a_fresh_object_each_call(subject, sandbox):
    """No shared cache: mutating one result cannot leak into the next."""
    load = _loader(subject)
    _write(sandbox, "paths:\n  - a\n")
    first = load()
    first["paths"].append("injected")
    assert load() == {"paths": ["a"]}
