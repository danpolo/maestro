"""Unit tests for the two bindings in ``maestro.pending``.

``deferred()`` is the load-bearing one: it is what lets a module bind a name owned by a
module that comes *later* in the extraction order without creating an import cycle. Its
contract — nothing imported until the first call, resolution cached afterwards, loud
failure naming both the name and the owner, and still an ordinary module attribute a
test can monkeypatch — is exercised here directly, because every call site that uses it
is monkeypatched by its own characterisation tests and would therefore never notice.
"""
import importlib
import sys
import types

import pytest

from maestro.pending import deferred, pending


def _write_owner(tmp_path, monkeypatch, modname, body):
    """Create an importable module on disk that is not yet in ``sys.modules``."""
    (tmp_path / f"{modname}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, modname, raising=False)
    importlib.invalidate_caches()
    return modname


# --- pending() is unchanged --------------------------------------------------------


def test_pending_raises_when_called():
    placeholder = pending("do_thing", "maestro.nowhere")
    with pytest.raises(NotImplementedError) as excinfo:
        placeholder()
    assert "do_thing" in str(excinfo.value)
    assert "maestro.nowhere" in str(excinfo.value)


def test_pending_keeps_its_markers():
    placeholder = pending("do_thing", "maestro.nowhere")
    assert placeholder.__name__ == "do_thing"
    assert placeholder.__qualname__ == "do_thing"
    assert placeholder.pending_owner == "maestro.nowhere"


# --- deferred(): markers and late binding -------------------------------------------


def test_deferred_sets_introspectable_markers():
    bound = deferred("park_regression", "maestro.parking")
    assert bound.__name__ == "park_regression"
    assert bound.__qualname__ == "park_regression"
    assert bound.deferred_owner == "maestro.parking"


def test_deferred_imports_nothing_until_first_call(tmp_path, monkeypatch):
    modname = "maestro_deferred_late_owner"
    _write_owner(tmp_path, monkeypatch, modname, "def greet():\n    return 'hi'\n")

    bound = deferred("greet", modname)
    # Building the binding must not touch the owner at all.
    assert modname not in sys.modules

    assert bound() == "hi"
    assert modname in sys.modules


# --- deferred(): delegation ---------------------------------------------------------


def test_deferred_passes_through_args_kwargs_and_return(tmp_path, monkeypatch):
    modname = "maestro_deferred_passthrough_owner"
    _write_owner(
        tmp_path,
        monkeypatch,
        modname,
        "def echo(*args, **kwargs):\n    return (args, kwargs)\n",
    )

    bound = deferred("echo", modname)
    assert bound(1, 2, three=3, four=None) == ((1, 2), {"three": 3, "four": None})


def test_deferred_propagates_the_targets_own_exception(tmp_path, monkeypatch):
    modname = "maestro_deferred_raising_owner"
    _write_owner(
        tmp_path,
        monkeypatch,
        modname,
        "def boom():\n    raise ValueError('from the target')\n",
    )

    bound = deferred("boom", modname)
    with pytest.raises(ValueError, match="from the target"):
        bound()


# --- deferred(): caching ------------------------------------------------------------


def test_deferred_resolves_once_and_caches(tmp_path, monkeypatch):
    modname = "maestro_deferred_cached_owner"
    _write_owner(
        tmp_path,
        monkeypatch,
        modname,
        "calls = []\n\n\ndef target():\n    calls.append(1)\n    return 'original'\n",
    )

    bound = deferred("target", modname)
    assert bound() == "original"

    # Repoint the attribute *and* evict the module. A binding that re-resolved per call
    # would now pick up the replacement, or re-import from disk; a cached one cannot.
    owner = sys.modules[modname]
    owner.target = lambda: "replacement"
    monkeypatch.delitem(sys.modules, modname, raising=False)
    (tmp_path / f"{modname}.py").unlink()

    assert bound() == "original"
    assert bound() == "original"
    assert modname not in sys.modules


# --- deferred(): still an ordinary module attribute ----------------------------------


def test_deferred_binding_is_monkeypatchable_as_a_module_attribute(monkeypatch):
    host = types.ModuleType("maestro_deferred_host")
    host.park_regression = deferred("park_regression", "maestro.parking")
    original = host.park_regression

    calls = []
    monkeypatch.setattr(host, "park_regression", lambda *a, **k: calls.append((a, k)))
    host.park_regression("T-1", reason="flake")
    assert calls == [(("T-1",), {"reason": "flake"})]

    monkeypatch.undo()
    assert host.park_regression is original


# --- deferred(): loud failure -------------------------------------------------------


def test_deferred_raises_naming_the_owner_when_the_module_is_bogus():
    bound = deferred("_judge_complete", "maestro.judge")
    with pytest.raises(ImportError) as excinfo:
        bound()
    message = str(excinfo.value)
    assert "_judge_complete" in message
    assert "maestro.judge" in message
    # The underlying import failure stays attached for the traceback.
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_deferred_raises_naming_name_and_owner_when_the_attribute_is_missing(
    tmp_path, monkeypatch
):
    modname = "maestro_deferred_empty_owner"
    _write_owner(tmp_path, monkeypatch, modname, "OTHER = 1\n")

    bound = deferred("no_such_helper", modname)
    with pytest.raises(ImportError) as excinfo:
        bound()
    message = str(excinfo.value)
    assert "no_such_helper" in message
    assert modname in message
    assert isinstance(excinfo.value.__cause__, AttributeError)


def test_deferred_failure_is_not_an_attributeerror():
    """AttributeError would be swallowed by ``getattr(..., default)``/``hasattr``
    guards at a call site. ImportError is not, which is the whole point."""
    bound = deferred("whatever", "maestro.definitely_not_a_module")
    with pytest.raises(ImportError) as excinfo:
        bound()
    assert not isinstance(excinfo.value, AttributeError)


def test_deferred_does_not_cache_a_failure(tmp_path, monkeypatch):
    """A failed resolution must not poison the binding: if the owner becomes
    importable later (it is created on disk, or a test repairs it), the next call
    resolves normally rather than replaying the first error."""
    modname = "maestro_deferred_repaired_owner"
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, modname, raising=False)

    bound = deferred("later", modname)
    with pytest.raises(ImportError):
        bound()

    (tmp_path / f"{modname}.py").write_text("def later():\n    return 42\n")
    importlib.invalidate_caches()
    assert bound() == 42
