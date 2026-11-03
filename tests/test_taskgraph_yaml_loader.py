"""The ROADMAP reader uses libyaml when it is there (test audit, 2026-11-02): it parses every
block of a ROADMAP on every read. Its results must be the pure-Python loader's, block for block."""
from pathlib import Path
import re

import pytest
import yaml

from maestro import taskgraph

_BLOCK = re.compile(r"```ya?ml\n(.*?)```", re.S)
_ROOT = Path(__file__).resolve().parent.parent
_ROADMAPS = [_ROOT / "maestro/templates/docs/ROADMAP.md", _ROOT / "docs/P13_ROADMAP.md",
             *sorted((_ROOT / "tests").rglob("*ROADMAP*.md"))]
_EDGES = [
    "", "# only a comment\n", "id: T\nnotes: |\n  a: b\n  - c\n", "id: 'T: x'\ndeps: [a, '10', 10]\n",
    "id: T\nnotes: \"\\u05d0 unicode\"\n", "id: T\ntitle: שלום\n", "id: 2026-11-02\nwhen: 12:30\n",
    "id: T\nflag: yes\nnone: ~\nnum: 1e3\noct: 010\n", "a: &x {b: 1}\nc: *x\nd: {<<: *x, e: 2}\n",
    "- just\n- a list\n", "scalar\n", "id: T\r\nrisk: low\r\n",
    "id: [unclosed\n", "id: T\n\tbad: tab\n", "id: T\nid: duplicate\n", "a: b: c\n", "!!python/object:os.system {}\n",
]


def _both(text):
    outcomes = []
    for loader in (yaml.SafeLoader, taskgraph._YAML_LOADER):
        try:
            outcomes.append(("value", yaml.load(text, Loader=loader)))
        except yaml.YAMLError:
            outcomes.append(("refused", None))
    return outcomes


@pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")
def test_the_reader_uses_libyaml_when_it_is_available():
    assert taskgraph._YAML_LOADER is yaml.CSafeLoader


@pytest.mark.parametrize("text", _EDGES)
def test_loaders_agree_on_edge_cases(text):
    python, used = _both(text)
    assert python == used


def test_loaders_agree_on_every_real_roadmap_block():
    blocks = [b for path in _ROADMAPS if path.is_file()
              for b in _BLOCK.findall(path.read_text(encoding="utf-8"))]
    assert blocks
    for block in blocks:
        python, used = _both(block)
        assert python == used, block


def test_a_refused_block_still_names_its_line():
    """`_yaml_issue` reports the mark, so both loaders must provide one."""
    with pytest.raises(yaml.MarkedYAMLError) as raised:
        yaml.load("id: T\nfoo: [unclosed\n", Loader=taskgraph._YAML_LOADER)
    assert raised.value.problem_mark is not None
