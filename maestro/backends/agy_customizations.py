"""Freeze instruction content and manifest references for a restricted agy call.

Keep native filters and inheritance intact; rewrite only paths to immutable
per-call copies. Native discovery must never follow a retained manifest back to
live project or shared-library content after preparation. Direct task-directory
AGENTS.md/GEMINI.md are outside this snapshot mechanism: they stay LIVE, scoped
and editable under the task's ordinary permissions.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from maestro.backends import control_files

RETAINED = ('rules', 'rules.json', 'skills', 'skills.json', 'AGENTS.md', 'GEMINI.md')
_INCLUDE = re.compile(rb'@\[([^\]]*)\]\(([^)]+)\)')


class Snapshot:
    def __init__(self, control: Path, *, home: Path, cwd: Path, private_roots=()):
        self.home = home
        self.project = next((p for p in (cwd, *cwd.parents) if (p / '.git').exists()), cwd)
        self.references = control / 'instructions'
        control_files.mkdir(self.references)
        self.count = 0
        self.mappings = []
        self.reads = set()
        self.copier = control_files.SourceCopy(dereference=True,
                                              validate_source=self._check_source)
        private = (self.home / '.gemini/antigravity-cli',
                   self.home / '.claude/.credentials.json',
                   self.home / '.claude/projects',
                   *(self.home / '.codex' / name for name in
                     ('auth.json', 'sessions', 'archived_sessions', 'history.jsonl')))
        private = (*private, *private_roots)
        codex = self.home / '.codex'
        self.codex_roots = (codex, codex.resolve())
        if codex.exists():
            # Record only names/targets, never database or credential bytes.
            # Resolution of an include must not erase a private leaf's name.
            with control_files.DirectoryView(self.codex_roots[1]) as native:
                private = (*private, *(native.path / name for name in native.entries()
                                       if name.startswith(('state_', 'logs_'))))
        self.private = (*private, *(path.resolve() for path in private))

    def _check_source(self, source: Path):
        # Instructions may alias shared rule/skill libraries, but may not turn
        # native login or conversation state into prompt text. This path-only
        # check runs before every snapshot read, including recursive aliases.
        if (any(source.is_relative_to(path) for path in self.private) or
                (source.parent in self.codex_roots and source.name.startswith(('state_', 'logs_')))):
            raise ValueError('customization references private native credentials or state')
        self.reads.add(str(source))

    @property
    def inventory(self):
        """Paths actually prepared; never instruction bodies or native auth."""
        return {'version': 1, 'mappings': self.mappings, 'reads': sorted(self.reads)}

    def content(self, source: Path, target: Path, ancestors=(), *, rules=False):
        """Dereference shared-library aliases, refusing cycles/special files."""
        self.copier.copy(source, target)
        if rules:
            self._rule_content(source, target, ancestors)
        self.mappings.append({'source': str(source), 'snapshot': str(target),
                              'surface': 'rules' if rules else 'skills'})

    def _rule_content(self, source: Path, target: Path, ancestors):
        if target.is_dir():
            for child in sorted(target.iterdir()):
                self._rule_content(source / child.name, child, ancestors)
            return
        # Rule directories contain Markdown rules. Included text is processed
        # regardless of extension, just as the native include reader does.
        if target.suffix.lower() != '.md' and not ancestors:
            return
        original = source.resolve(strict=True)
        if original in ancestors or len(ancestors) >= 32:
            raise ValueError('cyclic or overdeep rule include')
        text = control_files.read(target)

        def include(match):
            spelling = match.group(2).decode('utf-8')
            if '\x00' in spelling or spelling.startswith('~'):
                raise ValueError('unsupported rule include path')
            path = Path(spelling)
            if not path.is_absolute():
                path = source.parent / path
            try:
                path = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError('unavailable rule include') from exc
            if not path.is_file():
                raise ValueError('rule include must name a regular file')
            self.count += 1
            if self.count > 512:
                raise ValueError('customization reference limit exceeded')
            frozen = self.references / f'include-{self.count}'
            self.content(path, frozen, (*ancestors, original), rules=True)
            return b'@[' + match.group(1) + b'](' + str(frozen).encode('utf-8') + b')'

        replaced = _INCLUDE.sub(include, text)
        if replaced != text:
            control_files.write_bytes(target, replaced)

    def _resolve(self, spelling: str, *, base: Path) -> Path:
        if not isinstance(spelling, str) or not spelling or '\x00' in spelling:
            raise ValueError('invalid customization manifest path')
        if spelling.startswith('~/'):
            path = self.home / spelling[2:]
        elif spelling.startswith('~'):
            raise ValueError('unsupported customization manifest home path')
        else:
            path = Path(spelling)
            if not path.is_absolute():
                local = base / path
                path = local if local.exists() else self.project / path
        try:
            return path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f'unavailable customization manifest path: {spelling}') from exc

    def manifest(self, source: Path, target: Path, *, base: Path, ancestors=(), rules=False):
        source = source.resolve(strict=True)
        self._check_source(source)
        if source in ancestors or len(ancestors) >= 32:
            raise ValueError('cyclic or overdeep customization manifest')
        try:
            document = json.loads(control_files.read(source))
        except (OSError, ValueError) as exc:
            raise ValueError(f'invalid customization manifest: {source}') from exc
        if not isinstance(document, dict) or set(document) - {'entries', 'inherits', 'exclude'}:
            raise ValueError(f'unsupported customization manifest fields: {source}')
        self._filters(document)
        for name in ('entries', 'inherits'):
            rows = document.get(name, [])
            if not isinstance(rows, list):
                raise ValueError('customization manifest entries must be a list')
            for row in rows:
                if not isinstance(row, dict) or set(row) - {'path', 'include_only', 'exclude'}:
                    raise ValueError('unsupported customization manifest entry')
                self._filters(row)
                original = self._resolve(row.get('path'), base=base)
                self.count += 1
                if self.count > 512:
                    raise ValueError('customization manifest reference limit exceeded')
                frozen = self.references / str(self.count)
                if name == 'inherits':
                    self.manifest(original, frozen, base=original.parent,
                        ancestors=(*ancestors, source), rules=rules)
                else:
                    if not original.is_dir():
                        raise ValueError('customization manifest entry must name a directory')
                    self.content(original, frozen, rules=rules)
                row['path'] = str(frozen)
        control_files.write(target, json.dumps(document))
        self.mappings.append({'source': str(source), 'snapshot': str(target),
                              'surface': 'rules-manifest' if rules else 'skills-manifest'})

    @staticmethod
    def _filters(row):
        for name in ('include_only', 'exclude'):
            if name in row and (not isinstance(row[name], list) or
                    any(not isinstance(value, str) for value in row[name])):
                raise ValueError('customization manifest filters must be string lists')

    def retained(self, source: Path, target: Path):
        for name in RETAINED:
            original = source / name
            if original.exists() or original.is_symlink():
                if name in ('skills.json', 'rules.json'):
                    self.manifest(original, target / name, base=source.parent,
                                  rules=name == 'rules.json')
                else:
                    self.content(original, target / name, rules=name != 'skills')
