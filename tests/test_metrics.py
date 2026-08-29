"""`maestro.metrics` — naming a project's numbers instead of one project's numbers.

Every display site in the loop used to read `smoke["metrics"]["recall_at_5"]` and print
`Recall@5=`. On the reference project that was right; on any other it named a metric the
project does not measure and reported it as *missing* — `Recall@5=?` beside a result that
was fine. These tests pin the replacement: the name comes from `project.yaml`'s
`gate.primary_metric`, the rest of the line comes from whatever the adapter reported, and
nothing here has an opinion about what a metric is called.

They are also the reason `gate.primary_metric` came off `test_templates.py`'s known-dead
list: a knob with a reader has tests, and a knob with neither should not be in the
template.
"""
from __future__ import annotations

import pytest

from maestro import metrics

WITH_PRIMARY = {"gate": {"primary_metric": "recall_at_5"}}
NO_PRIMARY = {"gate": {"primary_metric": None}}
SMOKE = {"pass": True, "metrics": {"latency_p95_ms": 140, "recall_at_5": 0.82}}


# ── primary_name ──


def test_primary_name_reads_the_configured_metric():
    assert metrics.primary_name(WITH_PRIMARY) == "recall_at_5"


@pytest.mark.parametrize("document", [
    {},                                   # no gate block at all
    {"gate": None},                       # gate present but null
    {"gate": {}},                         # gate without the key
    NO_PRIMARY,                           # the scaffold default
    {"gate": {"primary_metric": "   "}},  # whitespace is not a name
    {"gate": {"primary_metric": 5}},      # not a string
    {"gate": "not-a-mapping"},
    "not-a-mapping",
], ids=["absent", "null-gate", "empty-gate", "null", "blank", "not-a-string",
        "gate-not-a-mapping", "document-not-a-mapping"])
def test_primary_name_is_empty_when_none_is_nominated(document):
    """A fresh scaffold ships `primary_metric: null` on purpose — a project that has not
    chosen a headline metric is the normal starting state (D8), never an error."""
    assert metrics.primary_name(document) == ""


# ── metrics_of ──


@pytest.mark.parametrize("smoke", [
    None, "text", 42, [], {"metrics": None}, {"metrics": ["recall"]}, {},
], ids=["none", "str", "int", "list", "null-metrics", "list-metrics", "no-key"])
def test_metrics_of_treats_anything_unusable_as_no_metrics(smoke):
    """These are display paths that run *after* a merge has landed. A malformed smoke
    result must not be the reason the operator hears nothing about work that shipped —
    which is exactly what it used to be (`test_telegram.py`'s ex-`AttributeError` pin)."""
    assert metrics.metrics_of(smoke) == {}


def test_metrics_of_copies_so_a_caller_cannot_mutate_the_result():
    smoke = {"metrics": {"a": 1}}
    metrics.metrics_of(smoke)["a"] = 2
    assert smoke["metrics"]["a"] == 1


# ── primary_value ──


def test_primary_value_is_the_configured_metric_as_text():
    assert metrics.primary_value(SMOKE, WITH_PRIMARY) == "0.82"


def test_primary_value_is_a_placeholder_when_no_metric_is_nominated():
    assert metrics.primary_value(SMOKE, NO_PRIMARY) == "?"


def test_primary_value_is_a_placeholder_when_the_adapter_did_not_report_it():
    assert metrics.primary_value({"metrics": {"other": 1}}, WITH_PRIMARY) == "?"


# ── summary ──


def test_summary_puts_the_primary_metric_first():
    """Adapter order otherwise, so a project that cares about it gets it by choosing the
    order it writes them in."""
    assert metrics.summary(SMOKE, WITH_PRIMARY) == "recall_at_5=0.82 · latency_p95_ms=140"


def test_summary_keeps_adapter_order_when_no_primary_is_nominated():
    assert metrics.summary(SMOKE, NO_PRIMARY) == "latency_p95_ms=140 · recall_at_5=0.82"


def test_summary_shows_a_metric_the_project_did_not_nominate():
    """The old line printed `?` twice here, having looked for two fixed keys. Whatever the
    adapter measured is what an operator is shown."""
    assert metrics.summary({"metrics": {"other": 1}}, WITH_PRIMARY) == "other=1"


@pytest.mark.parametrize("smoke", [None, {}, {"metrics": {}}, {"metrics": "bad"}],
                         ids=["none", "empty", "empty-metrics", "bad-metrics"])
def test_summary_says_no_metrics_rather_than_nothing(smoke):
    """A caller drops this straight into a sentence, so it is never an empty string. It is
    also not an error: a skipped smoke and a project with no eval harness both land here,
    and both are ordinary states (DESIGN.md D8)."""
    assert metrics.summary(smoke, NO_PRIMARY) == metrics.NO_METRICS


def test_summary_reads_project_yaml_when_no_config_is_passed(monkeypatch):
    """The injectable `config` is for callers that already hold the document; the default
    path is the real loader, and it must be the real loader — a helper that silently
    skipped configuration would reintroduce a hardcoded answer by another route."""
    monkeypatch.setattr(metrics, "load_project_yaml", lambda: WITH_PRIMARY)
    assert metrics.summary(SMOKE) == "recall_at_5=0.82 · latency_p95_ms=140"
