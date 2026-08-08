import pytest


@pytest.mark.maestro_module("state")
def test_subject_module_is_importable(subject):
    assert hasattr(subject, "now_iso")


@pytest.mark.maestro_module("state")
def test_sandbox_isolates_state(subject, sandbox):
    subject.write_state({"version": "1", "in_flight": []})
    assert (sandbox.orch_dir / "state.json").exists()
    assert subject.read_state()["version"] == "1"


@pytest.mark.maestro_module("state")
def test_no_live_credentials_leak_into_the_test_process(subject):
    """Importing the reference module must not leave a usable token in os.environ."""
    import os
    import re

    for key, value in os.environ.items():
        if key.endswith(("_TOKEN", "_KEY")) and re.fullmatch(r"\d{9,}:[A-Za-z0-9_-]{30,}", value):
            raise AssertionError(f"live bot token leaked via {key}")
