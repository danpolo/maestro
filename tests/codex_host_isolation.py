"""Keep Codex preparation tests off the developer's real home.

`codex_runtime.prepare` records an omission inventory of the discovery
directories (`.codex`, `.claude`, `.agents`, ...) in the call's cwd, every cwd
ancestor and the host home. Test trees live on persistent disk under the
developer's home, so those ancestors include the real `~/.claude` and
`~/.codex`: thousands of entries (about 8,000 measured) that take 3-9 s to walk
under a parallel suite, change while other sessions run, and can approach the
inventory's entry limit. Their contents are unrelated to every fixture, yet the
walk spends the same deadline the tests assert on, so an unrelated home decided
whether the asserted branch was reached.

`skip_real_home_inventory` reports only discovery roots that sit directly in
the account's real home or one of its ancestors as absent. Every other root,
including each fixture's own tree and the host home a fixture passes in, is
still inventoried by the real walk. The inventory is a written record, never an
input to verification, and the walk itself keeps its own tests
(`tests/test_codex_omission_inventory.py`, `tests/test_native_directory_inputs.py`).
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path

from maestro.backends import codex_runtime


def _real_home_ancestry() -> frozenset[Path]:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).absolute()
    return frozenset((home, *home.parents))


def skip_real_home_inventory(monkeypatch=None):
    """Patch `codex_runtime._omission_inventory`; use monkeypatch when given."""
    unrelated = _real_home_ancestry()
    real = codex_runtime._omission_inventory

    def inventory(root):
        if Path(root).absolute().parent in unrelated:
            return {'path': str(root), 'existed': False, 'omitted': [], 'omitted_paths': []}
        return real(root)

    if monkeypatch is None:
        codex_runtime._omission_inventory = inventory
    else:
        monkeypatch.setattr(codex_runtime, '_omission_inventory', inventory)
    return inventory
