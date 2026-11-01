"""Prepare a private agy filesystem boundary without changing shared native settings.

This module is not an activation switch. It gives an invocation a private permission
profile: other hooks, MCP servers and plugins are omitted; global and discovery-alias
rules and skills are retained in read-only snapshots. Direct AGENTS.md/GEMINI.md
remain LIVE in their native directory scope, editable under the task's normal write
grants. The override inventory is retained for review.
Auth stays at the CLI's original path, mounted read-only; Maestro never parses it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
from typing import Sequence

from maestro import confinement, permissions
from maestro.backends import agy_customizations, control_files, git_staging, native_ownership

_CUSTOMIZATION_ALIASES = ('.agents', '.agent', '_agents', '_agent')
_REPLACED_CONFIGS = ('agents', 'hooks.json', 'plugins', 'plugins.json', 'mcp_config.json',
                     'settings.json', 'config.json', 'import_manifest.json')
_RETAINED = agy_customizations.RETAINED
RUNTIME_DIR = '.agy-runtime'


def _omission_inventory(root: Path) -> dict:
    if not root.exists():
        return {'path': str(root), 'existed': False, 'omitted': [],
                'omitted_paths': [], 'omitted_mcp_servers': [],
                'omitted_plugin_entries': [], 'omitted_plugin_inherits': [],
                'omitted_hooks': [], 'retained_security_hooks': []}
    paths = []
    with control_files.DirectoryView(root) as view:
        entries = view.entries()
        omitted = [name for name in _REPLACED_CONFIGS if name in entries]
        def walk(parent, name, info, depth=0):
            if len(paths) >= 20000 or depth >= 64:
                raise ValueError('omission inventory entry/depth limit exceeded')
            path = parent.path / name
            paths.append(str(path.relative_to(root)))
            if stat.S_ISDIR(info.st_mode):
                with control_files.DirectoryView(path) as child:
                    if control_files.identity(child.info) != control_files.identity(info):
                        raise ValueError('omission inventory directory changed')
                    for leaf, value in child.entries().items():
                        walk(child, leaf, value, depth + 1)
        for name in omitted:
            walk(view, name, entries[name])
        def document(name):
            if name not in entries:
                return {}
            # Inventory names/paths only, never commands/env/credentials. Invalid
            # JSON has no parseable names; unsafe reads or races still refuse.
            try:
                value = json.loads(view.read(name))
                return value if isinstance(value, dict) else {}
            except (json.JSONDecodeError, UnicodeError):
                return {}
        mcp, plugins, hooks = (document(name) for name in
                               ('mcp_config.json', 'plugins.json', 'hooks.json'))
    servers = mcp.get('mcpServers', {})
    def configured_paths(key):
        entries = plugins.get(key, [])
        return sorted(row['path'] for row in entries if isinstance(row, dict)
                      and isinstance(row.get('path'), str)) if isinstance(entries, list) else []
    return {'path': str(root), 'existed': True, 'omitted': omitted,
            'omitted_paths': sorted(paths),
            'omitted_mcp_servers': sorted(servers) if isinstance(servers, dict) else [],
            'omitted_plugin_entries': configured_paths('entries'),
            'omitted_plugin_inherits': configured_paths('inherits'),
            'omitted_hooks': sorted(hooks), 'retained_security_hooks': []}


def _security_profile(native_config: Path, control: Path) -> tuple[list, dict]:
    """Freeze the approved existing security gate; inventory other global hooks."""
    hooks_path = control_files.file(native_config / 'hooks.json')
    hooks = json.loads(control_files.read(hooks_path)) if hooks_path.exists() else {}
    if not isinstance(hooks, dict):
        raise confinement.ConfinementError('invalid global agy hook configuration')
    name = 'skillspector-security-gate'
    gate = hooks.get(name)
    retained = []
    commands = []
    if gate is not None:
        if (not isinstance(gate, dict) or gate.get('enabled', True) is not True
                or set(gate) - {'enabled', 'PreToolUse'}):
            raise confinement.ConfinementError('unsupported existing security hook profile')
        groups = gate.get('PreToolUse')
        if not isinstance(groups, list) or len(groups) != 1:
            raise confinement.ConfinementError('unsupported existing security hook matchers')
        group = groups[0]
        if not isinstance(group, dict) or group.get('matcher') not in ('*', '.*'):
            raise confinement.ConfinementError('unsupported existing security hook matcher')
        entries = group.get('hooks')
        if not isinstance(entries, list) or len(entries) != 1:
            raise confinement.ConfinementError('unsupported existing security hook commands')
        entry = entries[0]
        script = control_files.file(native_config / 'scripts/skillspector_gate.py')
        expected = [str(script), '>>', '/tmp/hook_log.txt', '2>&1']
        if (not isinstance(entry, dict) or entry.get('type') != 'command'
                or not isinstance(entry.get('command'), str)
                or shlex.split(entry['command']) not in ([str(script)], expected)):
            raise confinement.ConfinementError('unsupported existing security hook command')
        timeout = entry.get('timeout', 30)
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise confinement.ConfinementError('unsupported existing security hook timeout')
        snapshot = control / 'skillspector_gate.py'
        control_files.SourceCopy().copy(script, snapshot)
        # Keep the exact scanner logic. Feed its JSON decision to the combined
        # guard rather than redirecting the decision away from the native hook.
        commands.append({'argv': [sys.executable, '-I', str(snapshot)], 'timeout': timeout})
        retained.append(name)
    inventory = _omission_inventory(native_config)
    inventory.update(omitted_hooks=sorted(set(hooks) - set(retained)),
                     retained_security_hooks=retained)
    return commands, inventory


@dataclass(frozen=True)
class PreparedRun:
    cwd: Path
    workspace: Path
    home: Path
    directory: Path
    control: Path
    state: Path
    config: Path
    scratch: Path
    write_roots: tuple[Path, ...]
    customizations: tuple[tuple[Path, Path], ...]
    protected_paths: tuple[Path, ...]
    trusted_source: Path
    created_mountpoints: tuple[tuple[str, int, int], ...]
    transaction: git_staging.Transaction | None = None
    reservation: native_ownership.Reservation | None = None
    prepared_directories: tuple = ()

    def cleanup(self) -> None:
        control_files.cleanup_resources(
            ('Git', lambda: self.transaction.cleanup() if self.transaction is not None else None),
            ('ownership', lambda: native_ownership.cleanup(self.reservation)),
            ('mountpoint', lambda: control_files.release_mountpoints(self.created_mountpoints)))

    def environment(self) -> dict[str, str]:
        return {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
                'LANG': 'C.UTF-8', 'TMPDIR': str(self.scratch),
                'AGY_CLI_DISABLE_AUTO_UPDATE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}

    def launcher(self, argv: Sequence[str], *, log_file: Path, mode: str) -> str:
        control_files.verify_directories(self.prepared_directories)
        package = self.control / 'maestro'
        control_files.mkdir(package / 'backends')
        control_files.write(package / 'backends/__init__.py', '')
        copier = control_files.SourceCopy()
        for name in ('agy_worker.py', 'control_files.py', 'git_staging.py', 'git_objects.py',
                     'native_ownership.py'):
            copier.copy(self.trusted_source / 'backends' / name, package / 'backends' / name)
        path = self.control / 'job.json'
        wrapped = self.wrap(argv)
        control_files.write(path, json.dumps({'argv': wrapped, 'mounts': wrapped.identities, 'cwd': str(self.cwd),
            'log_file': str(log_file), 'mode': mode, 'env': self.environment(),
            'created_mountpoints': self.created_mountpoints,
            'git_transaction': self.transaction.job if self.transaction else None,
            'reservation': self.reservation.job if self.reservation else None}))
        return ('import sys\n'
                f'sys.path.insert(0, {str(self.control)!r})\n'
                'from maestro.backends.agy_worker import main\n'
                f'main({str(path)!r})\n')

    @property
    def hook_argv(self) -> list[str]:
        # -I excludes cwd, PYTHONPATH, user site and startup customization. The
        # bootstrap adds only the frozen package, never the writer's source tree.
        return [sys.executable, '-I', str(self.control / 'guard.py'),
                'agy', str(self.control / 'policy.json')]

    def wrap(self, argv: Sequence[str]) -> list[str]:
        control_files.verify_directories(self.prepared_directories)
        with control_files.DirectoryView(self.home / '.gemini') as home_view, \
                control_files.DirectoryView(self.home / '.gemini/antigravity-cli') as state_view:
            wrapped = self._wrap(argv, home_view, state_view)
            control_files.verify_directories(self.prepared_directories)
            return wrapped

    def _wrap(self, argv, home_view, state_view):
        native_home = self.home / '.gemini'
        native_state = native_home / 'antigravity-cli'
        native_config = native_home / 'config'
        wrapped = confinement.bubblewrap_argv(
            argv, cwd=self.cwd, write_roots=(*self.write_roots, self.state,
                                           self.config / 'projects'),
            read_only_roots=(self.directory,), scratch=self.scratch)
        separator = len(wrapped) - len(argv) - 1
        roots = sorted(set((self.cwd, self.workspace, *self.write_roots)), key=lambda p: len(p.parts))
        def rebind(root):
            writable = any(root.is_relative_to(grant) for grant in self.write_roots)
            return ['--bind' if writable else '--ro-bind', str(root), str(root)]
        overlays = []
        for root in roots:
            if native_home.is_relative_to(root):
                overlays += rebind(root)
        overlays += ['--bind', str(self.state), str(self.state),
                    '--bind', str(self.config / 'projects'), str(self.config / 'projects'),
                    # The enclosing read-only runtime bind hides the base scratch
                    # mount. Reopen only this invocation's private tmpfs.
                    '--tmpfs', str(self.scratch),
                    '--tmpfs', str(native_home)]
        for name in home_view.entries():
            child = native_home / name
            if child.name not in ('antigravity-cli', 'config', *_REPLACED_CONFIGS):
                snapshot = self.control / 'native-home' / child.name
                if child.name not in _RETAINED:
                    overlays += ['--ro-bind', str(child), str(child)]
                elif snapshot.exists():
                    overlays += ['--ro-bind', str(snapshot), str(child)]
        overlays += ['--bind', str(self.state), str(native_state),
                     '--ro-bind', str(self.config), str(native_config),
                     '--bind', str(self.config / 'projects'), str(native_config / 'projects'),
                     '--ro-bind', str(self.control / 'settings.json'),
                     str(native_home / 'settings.json'),
                     '--ro-bind', str(self.control / 'settings.json'),
                     str(native_state / 'settings.json')]
        # Native auth/install files and builtins are read-only original mounts.
        # The native terminal rewrites bin/agentapi, so bin is a private copy.
        for name in state_view.entries():
            child = native_state / name
            if child.name in ('builtin', 'antigravity-oauth-token', 'installation_id',
                               'jetski_state.pbtxt'):
                overlays += ['--ro-bind', str(child), str(child)]
        overlays += ['--setenv', 'AGY_CLI_DISABLE_AUTO_UPDATE', '1']
        for root in roots:
            if not native_home.is_relative_to(root):
                overlays += rebind(root)
        overlays += ['--ro-bind', str(self.directory), str(self.directory),
                     '--bind', str(self.state), str(self.state),
                     '--bind', str(self.config / 'projects'), str(self.config / 'projects'),
                     '--bind', str(self.control.parent / 'bin'), str(self.state / 'bin'),
                     '--bind', str(self.control.parent / 'bin'), str(native_state / 'bin'),
                     '--ro-bind', str(self.control / 'settings.json'), str(self.state / 'settings.json'),
                     '--ro-bind', str(self.config / 'mcp_config.json'), str(self.state / 'mcp_config.json'),
                     '--ro-bind', str(self.config / 'mcp_config.json'), str(native_state / 'mcp_config.json'),
                     '--tmpfs', str(self.scratch),
                     '--ro-bind', str(self.trusted_source), str(self.trusted_source)]
        for path in self.protected_paths:
            overlays += ['--ro-bind', str(path), str(path)]
        for root in roots:
            git = root / '.git'
            if git.exists():
                overlays += ['--ro-bind', str(git), str(git)]
        for target, snapshot in self.customizations:
            control_files.directory(target)
            overlays += ['--ro-bind', str(snapshot), str(target)]
        if self.transaction is not None:
            overlays += self.transaction.mounts()
        if self.reservation is not None:
            overlays += self.reservation.mounts()
        return control_files.freeze_mounts([*wrapped[:separator], *overlays, *wrapped[separator:]],
            expected={**home_view.expected, **state_view.expected,
                      **{Path(p): value for p, value in self.prepared_directories}})


@native_ownership.prepare
@control_files.preparation
def prepare(*, cwd: Path, workspace: Path, write_roots: Sequence[Path],
            home: Path | None = None, allow: Sequence[str] | None = None,
            protected_paths: Sequence[Path] = ()) -> PreparedRun:
    """Freeze one invocation's policy/code and make its private writable state.

    The returned files are deliberately retained for evidence and resume work.
    Native driver integration must own that lifetime; nothing deletes a workspace.
    """
    cwd, workspace = control_files.directory(cwd), control_files.directory(workspace)
    home = control_files.directory(home if home is not None else Path.home())
    if not cwd.is_dir() or not workspace.is_dir():
        raise confinement.ConfinementError('agy needs existing cwd and report workspace')
    roots = tuple(dict.fromkeys(control_files.directory(path) for path in write_roots))
    content_roots = roots
    roots = git_staging.write_roots(cwd, roots)
    native_state = control_files.directory(home / '.gemini/antigravity-cli')
    native_config = control_files.directory(home / '.gemini/config')
    helper = control_files.directory(native_state / 'bin')
    if not helper.is_dir():
        raise confinement.ConfinementError('agy native helper installation is unavailable')
    directory = control_files.directory(workspace / RUNTIME_DIR)
    state = control_files.directory(directory / 'state')
    control_files.directory(state / 'bin')
    for name in ('settings.json', 'mcp_config.json'):
        control_files.file(state / name)
    locations = dict.fromkeys((cwd, *cwd.parents, workspace, *content_roots, home))
    targets = tuple(root / alias for root in locations for alias in _CUSTOMIZATION_ALIASES)
    for target in targets:
        control_files.directory(target)
    protected = tuple(dict.fromkeys((*(workspace / name for name in
        (*control_files.PARENT_CONTROLS, 'resume.py', 'launch_spec.json', 'launch_spec.tmp')),
        *map(Path, protected_paths))))
    for path in protected:
        control_files.file(path)
    source = control_files.directory(Path(__file__).absolute().parents[1])
    # Copy only native instruction surfaces. Audit directories and other data
    # under a customization root are not part of the approved worker profile.
    for target in targets:
        if target.exists():
            for name in ('skills.json', 'rules.json', 'AGENTS.md', 'GEMINI.md'):
                control_files.file(target / name)
    for path in protected:
        with control_files.open_file(path, 'a'):
            pass
    control_files.mkdir(directory, exist_ok=True)
    invocation = control_files.temporary_directory(directory)
    control, config, scratch = [invocation / name for name in ('control', 'config', 'temp')]
    control_files.mkdir(state, exist_ok=True)
    for path in (control, config, scratch, config / 'projects'):
        control_files.mkdir(path)
    control_files.mkdir(state / 'bin', exist_ok=True)
    # Native agentapi is rewritten at startup. Give each call a fresh writable
    # copy so a command in one call cannot install code for the next call.
    control_files.SourceCopy().copy(native_state / 'bin', invocation / 'bin')
    package = control / 'maestro'
    control_files.mkdir(package)
    control_files.write(package / '__init__.py', '')
    copier = control_files.SourceCopy()
    for name in ('permissions.py', 'tool_policy.py', 'tool_hooks.py'):
        copier.copy(source / name, package / name)
    control_files.write(control / 'guard.py',
        'import sys\nfrom pathlib import Path\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
        'from maestro.tool_hooks import main\nmain()\n')
    security_hooks, global_inventory = _security_profile(home / '.gemini/config', control)
    control_files.write(control / 'policy.json', json.dumps({
        'version': 1, 'cwd': str(cwd),
        'allow': list(permissions.allow_rules() if allow is None else allow),
        'write_roots': list(map(str, roots)), 'security_hooks': security_hooks,
        'deny': [f'Edit({directory}/**)', f'Edit({source}/**)',
                 f'Edit({workspace}/.native-git/**)',
                 *(f'Edit({path})' for path in protected),
                 *(f'Edit({path}/**)' for path in targets)]}))
    control_files.write(control / 'settings.json', json.dumps({
        'toolPermission': 'request-review',
        # This native gate is reached only after the shared matcher allows the
        # actual complete command. A prefix translation would silently widen it.
        'permissions': {'allow': ['command(*)', 'read_file(*)',
                                   *(f'write_file({root})' for root in roots)],
                        'deny': [], 'ask': []}}))
    with control_files.open_file(state / 'settings.json', 'a'):
        pass
    for parent in (state, config):
        control_files.write(parent / 'mcp_config.json', '{"mcpServers":{}}')
    native_home_snapshot = control / 'native-home'
    control_files.mkdir(native_home_snapshot)
    instructions = agy_customizations.Snapshot(control, home=home, cwd=cwd,
        private_roots=(directory, workspace / '.codex-runtime', workspace / '.native-git'))
    for original, snapshot in ((home / '.gemini', native_home_snapshot),
                               (native_config, config)):
        instructions.retained(original, snapshot)
    run = PreparedRun(cwd, workspace, home, directory, control, state, config, scratch,
                      roots, (), protected, source, ())
    hooks = json.dumps({'maestro-permissions': {
        'PreToolUse': [{'matcher': '*', 'hooks': [{
            'type': 'command', 'command': shlex.join(run.hook_argv), 'timeout': 35}]}]}})
    control_files.write(config / 'hooks.json', hooks)
    # Every supported workspace discovery alias gets the same guard, including
    # absent aliases: a writer cannot introduce a higher-priority disabled hook.
    customizations = []
    inventory = [global_inventory, _omission_inventory(native_state),
                 _omission_inventory(home / '.gemini')]
    snapshots = control / 'customizations'
    control_files.mkdir(snapshots)
    for root in locations:
        for alias in _CUSTOMIZATION_ALIASES:
            target = root / alias
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise confinement.ConfinementError(f'unsupported customization root: {target}')
            if not target.exists() and not any(root.is_relative_to(grant) for grant in roots):
                # Outside the write grants, the read-only parent already prevents
                # introduction of a new discovery directory. Do not create a host
                # mountpoint or mask the ancestor just to hide an absent path.
                continue
            snapshot = snapshots / str(len(customizations))
            control_files.mkdir(snapshot)
            if target.exists():
                # Shared skill libraries commonly use symlinks. Dereference the
                # retained content into the frozen snapshot, never a live alias.
                instructions.retained(target, snapshot)
            omitted = []
            for name in _REPLACED_CONFIGS:
                path = snapshot / name
                if path.is_symlink() or path.is_file():
                    omitted.append(name)
                    path.unlink()
                elif path.is_dir():
                    omitted.append(name)
                    shutil.rmtree(path)
            control_files.write(snapshot / 'hooks.json', hooks)
            control_files.write(snapshot / 'mcp_config.json', '{"mcpServers":{}}')
            customizations.append((target, snapshot))
            inventory.append(_omission_inventory(target))
    control_files.write(control / 'customizations.json', json.dumps(inventory, indent=2))
    control_files.write(control / 'instruction-sources.json', json.dumps(instructions.inventory, indent=2))
    created_mountpoints = []
    try:
        for target, _ in customizations:
            if not target.exists():
                control_files.mkdir(target)
                info = target.lstat()
                created_mountpoints.append((str(target), info.st_dev, info.st_ino))
    except BaseException:
        control_files.release_mountpoints(created_mountpoints)
        raise
    from dataclasses import replace
    transaction = None
    try:
        transaction = git_staging.Transaction.prepare(cwd, workspace) if cwd in roots else None
        if transaction is not None:
            transaction.exclude_generated(target / leaf
                for target, _ in customizations for leaf in ('hooks.json', 'mcp_config.json')
                if not (target / leaf).exists() and not (target / leaf).is_symlink())
    except BaseException as error:
        try:
            control_files.cleanup_resources(
                ('Git', lambda: transaction.cleanup() if transaction is not None else None),
                ('mountpoint', lambda: control_files.release_mountpoints(created_mountpoints)))
        except Exception as cleanup_error:
            error.add_note(f'agy Git discovery cleanup failed: {cleanup_error!r}')
        raise
    return replace(run, customizations=tuple(customizations),
                   created_mountpoints=tuple(created_mountpoints),
                   transaction=transaction)
