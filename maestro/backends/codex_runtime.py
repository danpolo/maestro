"""Private stock Codex configuration and original-path native authentication."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tomllib
from typing import Sequence

from maestro import confinement, permissions
from maestro.tool_policy import ToolPolicy
from maestro.backends.codex_app_server import verify_worker_config
from maestro.backends import codex_auth, control_files, git_staging, native_ownership

RUNTIME_DIR = '.codex-runtime'
_PARENT_CONTROLS = control_files.PARENT_CONTROLS
_SEALED = ('config.toml', 'rules', 'skills', 'plugins', 'hooks.json',
           'AGENTS.md', 'AGENTS.override.md')
_DISCOVERY = ('.codex', '.agents', '.agent', '_agents', '_agent', '.claude')
_FEATURES = ('unified_exec_tty', 'multi_agent', 'apps', 'plugins', 'view_image',
             'image_generation', 'browser_use', 'computer_use', 'in_app_browser',
             'tool_suggest', 'hooks')
_MODEL_METADATA = ('model_context_window', 'model_auto_compact_token_limit', 'model_catalog_json')
_EXECUTION_SURFACES = (*_SEALED, 'agents', 'settings.json', 'settings.local.json',
                       'mcp.json', 'mcp_config.json', 'plugins.json', 'skills.json',
                       'rules.json', 'CLAUDE.md', 'GEMINI.md')


# The inventory is a written record, not an input to verification, so being partial never fails
# a prepare. A plugin's own files are not worth the walk: real homes had about 5,000 entries,
# and four levels name about 450 of them.
_INVENTORY_DEPTH = 3
_INVENTORY_ENTRIES = 20000


def _omission_inventory(root: Path) -> dict:
    """Name masked execution surfaces, never read their instructions or auth.

    Native caches/worktrees are deliberately not recursively enumerated. Links
    are recorded by name, without following them outside the masked discovery
    root. Match preparation's bounded, pinned directory-input contract. A record
    that stopped early carries `truncated: true`.
    """
    paths = []
    existed = root.exists()
    omitted = []
    truncated = False
    if existed:
        def walk(parent, name, info, depth=0):
            nonlocal truncated
            if len(paths) >= _INVENTORY_ENTRIES:
                truncated = True
                return
            path = parent.path / name
            paths.append(str(path.relative_to(root)))
            if not stat.S_ISDIR(info.st_mode):
                return
            if depth >= _INVENTORY_DEPTH:
                truncated = True
                return
            try:
                with control_files.DirectoryView(path) as child:
                    if control_files.identity(child.info) != control_files.identity(info):
                        raise ValueError('Codex omission inventory directory changed')
                    for leaf, value in child.entries().items():
                        walk(child, leaf, value, depth + 1)
            except (OSError, ValueError):
                truncated = True  # a live home can change mid-walk; the record says it is partial
        with control_files.DirectoryView(root) as view:
            entries = view.entries(only=_EXECUTION_SURFACES)
            omitted = sorted(name for name in _EXECUTION_SURFACES if name in entries)
            for name in omitted:
                walk(view, name, entries[name])
    record = {'path': str(root), 'existed': existed, 'omitted': omitted,
              'omitted_paths': sorted(paths)}
    if truncated:
        record['truncated'] = True
    return record


def _model_metadata(native_home: Path, control: Path, model: str | None) -> tuple[dict, dict]:
    """Snapshot model facts only; omit the user's execution/configuration policy."""
    config = {}
    invalid = False
    path = control_files.file(native_home / 'config.toml')
    if path.exists():
        try:
            config = tomllib.loads(control_files.read(path).decode('utf-8'))
        except tomllib.TOMLDecodeError:
            # Isolated workers deliberately tolerate a broken normal config.
            # It supplies no model overrides when it cannot be parsed.
            invalid = True
    metadata = {}
    for key in ('model_context_window', 'model_auto_compact_token_limit'):
        value = config.get(key)
        if value is not None:
            if type(value) is not int or value <= 0:
                raise ValueError(f'invalid native model metadata: {key}')
            metadata[key] = value
    catalog_path = config.get('model_catalog_json')
    if catalog_path is None:
        candidate = native_home / 'model_catalog.json'
        catalog_path = str(candidate) if candidate.exists() else None
    if catalog_path is not None and model is not None:
        if not isinstance(catalog_path, str) or not Path(catalog_path).is_absolute():
            raise ValueError('native model catalog needs an absolute path')
        catalog = json.loads(control_files.read(Path(catalog_path)))
        rows = catalog.get('models') if isinstance(catalog, dict) else None
        if not isinstance(rows, list):
            raise ValueError('invalid native model catalog')
        selected = [row for row in rows if isinstance(row, dict) and row.get('slug') == model]
        if len(selected) != 1:
            raise ValueError(f'native model catalog has no unique selected model: {model}')
        snapshot = control / 'model_catalog.json'
        control_files.write(snapshot, json.dumps({'models': selected}))
        metadata['model_catalog_json'] = str(snapshot)
    inventory = {'omitted_config_keys': sorted(set(config) - set(_MODEL_METADATA)),
                 'invalid_config': invalid}
    for key, field in (('mcp_servers', 'omitted_mcp_servers'), ('plugins', 'omitted_plugins')):
        section = config.get(key, {})
        inventory[field] = sorted(section) if isinstance(section, dict) else []
    return metadata, inventory


@dataclass(frozen=True)
class PreparedRun:
    cwd: Path
    workspace: Path
    native_home: Path
    host_home: Path
    directory: Path
    control: Path
    state: Path
    scratch: Path
    write_roots: tuple[Path, ...]
    policy: ToolPolicy
    model: str | None
    effort: str | None
    expected_config: str
    protected_paths: tuple[Path, ...]
    trusted_source: Path
    discovery_paths: tuple[Path, ...]
    created_mountpoints: tuple[tuple[str, int, int], ...]
    transaction: git_staging.Transaction | None
    reservation: native_ownership.Reservation | None = None
    prepared_directories: tuple = ()
    auth_identity: dict | None = None

    def verify_auth(self):
        if self.auth_identity is None:
            raise ValueError('Codex auth identity is missing')
        codex_auth.verify(self.auth_identity)

    def cleanup(self) -> None:
        control_files.cleanup_resources(
            ('Git', lambda: self.transaction.cleanup() if self.transaction is not None else None),
            ('ownership', lambda: native_ownership.cleanup(self.reservation)),
            ('mountpoint', lambda: control_files.release_mountpoints(self.created_mountpoints)))

    @property
    def config(self) -> Path:
        return self.control / 'config.toml'

    def environment(self) -> dict[str, str]:
        # Native auth is consumed only by stock Codex; never inherit API keys,
        # endpoint overrides, parent sessions, Python injection or proxy settings.
        return {'HOME': str(self.host_home), 'CODEX_HOME': str(self.native_home),
                'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
                'LANG': 'C.UTF-8', 'TMPDIR': str(self.scratch),
                'PYTHONDONTWRITEBYTECODE': '1'}

    def verify_config(self, response: dict) -> None:
        verify_worker_config(response, self.expected_config, self.effort)

    def launcher(self, *, binary: str, prompt: str, resume_id: str = '',
                 fallback_prompt: str | None = None, timeout: float = 3600,
                 mode: str = 'w') -> str:
        """Freeze the trusted parent controller and its per-call policy before launch."""
        control_files.verify_directories(self.prepared_directories)
        self.verify_auth()
        source = self.trusted_source
        package = self.control / 'maestro'
        control_files.mkdir(package / 'backends', parents=True)
        for relative in ('__init__.py', 'backends/__init__.py'):
            control_files.write(package / relative, '')
        copier = control_files.SourceCopy()
        for relative in ('permissions.py', 'tool_policy.py',
                         'backends/codex_app_server.py', 'backends/codex_worker.py',
                         'backends/codex_auth.py',
                         'backends/control_files.py', 'backends/git_staging.py',
                         'backends/git_objects.py',
                         'backends/native_ownership.py'):
            copier.copy(source / relative, package / relative)
        wrapped = self.wrap([binary, 'app-server'])
        job = {'argv': wrapped, 'mounts': wrapped.identities, 'cwd': str(self.cwd),
               'model': self.model, 'effort': self.effort, 'prompt': prompt,
               'resume_id': resume_id, 'fallback_prompt': fallback_prompt,
               'timeout': timeout, 'env': self.environment(),
               'expected_config': self.expected_config,
               'allow': list(self.policy.allow), 'deny': list(self.policy.deny),
               'write_roots': list(map(str, self.policy.write_roots)),
               'workspace': str(self.workspace), 'mode': mode,
               'created_mountpoints': self.created_mountpoints,
               'git_transaction': self.transaction.job if self.transaction else None,
               'reservation': self.reservation.job if self.reservation else None}
        job['auth_identity'] = self.auth_identity
        path = self.control / 'job.json'
        control_files.write(path, json.dumps(job))
        return ('import sys\n'
                f'sys.path.insert(0, {str(self.control)!r})\n'
                'from maestro.backends.codex_worker import main\n'
                f'main({str(path)!r})\n')

    def wrap(self, argv: Sequence[str]) -> list[str]:
        control_files.verify_directories(self.prepared_directories)
        self.verify_auth()
        # The source runtime itself is always sealed, including its parent, even
        # when the report directory is writable. Only native state is reopened.
        wrapped = confinement.bubblewrap_argv(argv, cwd=self.cwd,
            write_roots=self.write_roots, read_only_roots=(self.directory,),
            scratch=self.scratch)
        separator = len(wrapped) - len(argv) - 1
        roots = sorted(set((self.cwd, self.workspace, *self.write_roots)), key=lambda p: len(p.parts))
        def rebind(root):
            writable = any(root.is_relative_to(grant) for grant in self.write_roots)
            return ['--bind' if writable else '--ro-bind', str(root), str(root)]

        overlays = []
        # Ancestor views must land first; rebinding one after the private home
        # would expose normal configuration/auth/session discovery again.
        for root in roots:
            if self.native_home.is_relative_to(root):
                overlays += rebind(root)
        overlays += ['--bind', str(self.state), str(self.state),
                     '--tmpfs', str(self.scratch),
                     '--bind', str(self.state), str(self.native_home)]
        # Original path and original inode, no parent read/copy/symlink. Native
        # renewal may update this narrowly shared cache; workers cannot access it.
        overlays += ['--bind', str(self.native_home / 'auth.json'),
                     str(self.native_home / 'auth.json')]
        for name in _SEALED:
            overlays += ['--ro-bind', str(self.control / name), str(self.native_home / name)]
        # Cwd/workspace can themselves live under ~/.codex (managed worktrees).
        # Rebind their host views after masking native configuration.
        for root in roots:
            if not self.native_home.is_relative_to(root):
                overlays += rebind(root)
        overlays += ['--ro-bind', str(self.directory), str(self.directory),
                     '--bind', str(self.state), str(self.state), '--tmpfs', str(self.scratch)]
        for path in self.protected_paths:
            overlays += ['--ro-bind', str(path), str(path)]
        # A worker must not poison the package a later parent invocation copies
        # into its sealed controller. This may be inside an otherwise writable
        # checkout when Maestro develops itself.
        overlays += ['--ro-bind', str(self.trusted_source), str(self.trusted_source)]
        for root in set((self.cwd, self.workspace, *self.write_roots)):
            git = root / '.git'
            if git.exists():
                overlays += ['--ro-bind', str(git), str(git)]
        for target in self.discovery_paths:
            control_files.directory(target)
            if target.exists():
                overlays += ['--ro-bind', str(self.control / 'empty'), str(target)]
        if self.transaction is not None:
            overlays += self.transaction.mounts()
        if self.reservation is not None:
            overlays += self.reservation.mounts()
        expected = {Path(p): value for p, value in self.prepared_directories}
        expected[Path(self.auth_identity['path'])] = self.auth_identity['leaf']
        wrapped = control_files.freeze_mounts([*wrapped[:separator], *overlays, *wrapped[separator:]],
            expected=expected)
        control_files.verify_directories(self.prepared_directories)
        self.verify_auth()
        return wrapped


@native_ownership.prepare
@control_files.preparation
def prepare(*, cwd: Path, workspace: Path, model: str | None, effort: str | None,
            write_roots: Sequence[Path], native_home: Path | None = None,
            host_home: Path | None = None, allow: Sequence[str] | None = None,
            protected_paths: Sequence[Path] = ()) -> PreparedRun:
    """Prepare private state across calls and immutable controls for this call.

    Native system-skill installation may report EROFS against the sealed empty
    skills directory. It is deliberately sealed before startup; no user skills or
    hooks are inherited, and startup verification must pass before inference.
    """
    cwd = control_files.directory(cwd)
    workspace = control_files.workspace(workspace, protected_paths)
    host_home = control_files.directory(host_home if host_home is not None else Path.home())
    native_home = control_files.directory(native_home if native_home is not None else host_home / '.codex')
    if native_home.is_symlink() or not native_home.is_dir():
        raise ValueError('Codex native home must be an existing nonsymlink directory')
    auth = native_home / 'auth.json'
    control_files.file(auth)
    if auth.is_symlink() or not auth.is_file():
        raise ValueError('Codex existing file login is unavailable; refusing alternate authentication')
    auth_identity = codex_auth.capture(auth)
    if not cwd.is_dir() or not workspace.is_dir():
        raise ValueError('Codex needs an existing cwd and report directory')
    roots = tuple(dict.fromkeys(control_files.directory(p) for p in write_roots))
    content_roots = roots
    roots = git_staging.write_roots(cwd, roots)
    profile_roots = (*roots, *git_staging.private_roots(cwd, roots))
    trusted_source = control_files.directory(Path(__file__).absolute().parents[1])
    discovery_paths = tuple(dict.fromkeys(root / name
        for root in dict.fromkeys((cwd, *cwd.parents, workspace, *content_roots, host_home))
        for name in _DISCOVERY if root / name != native_home))
    for target in discovery_paths:
        control_files.directory(target)
    protected = tuple(dict.fromkeys((*(workspace / name for name in _PARENT_CONTROLS),
                                     *map(Path, protected_paths))))
    for path in protected:
        control_files.file(path)
        control_files.touch(path)
    if effort is not None and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('unsupported Codex reasoning effort')
    directory = workspace / RUNTIME_DIR
    if directory.is_symlink():
        raise ValueError('Codex runtime directory must not be a symlink')
    control_files.mkdir(directory, mode=0o700, exist_ok=True)
    state = directory / 'state'
    if state.is_symlink():
        raise ValueError('Codex state must not be a symlink')
    control_files.mkdir(state, mode=0o700, exist_ok=True)
    for name in ('sessions', 'archived_sessions'):
        target = state / name
        if target.is_symlink():
            raise ValueError(f'Codex state directory is a symlink: {target}')
        control_files.mkdir(target, exist_ok=True)
    control = control_files.temporary_directory(directory)
    scratch = control / 'tmp'
    for name in ('tmp', 'rules', 'skills', 'plugins', 'empty'):
        control_files.mkdir(control / name)
    for name in ('auth.json', *_SEALED):
        target = state / name
        if target.is_symlink():
            raise ValueError(f'Codex state mountpoint is a symlink: {target}')
        if name in ('rules', 'skills', 'plugins'):
            control_files.mkdir(target, exist_ok=True)
        else:
            control_files.touch(target)
    for name in ('AGENTS.md', 'AGENTS.override.md'):
        control_files.write(control / name, '')
    control_files.write(control / 'hooks.json', '{}')
    quoted = json.dumps
    # Account telemetry has no selected model and never starts a thread/turn.
    lines = ([f'model = {quoted(model)}'] if model is not None else []) + ['model_provider = "openai"',
             'cli_auth_credentials_store = "file"', 'approvals_reviewer = "user"',
             'sandbox_mode = "workspace-write"', 'web_search = "disabled"',
             'project_doc_max_bytes = 0', 'allow_login_shell = false']
    metadata, config_inventory = _model_metadata(native_home, control, model)
    lines += [f'{key} = {quoted(value)}' for key, value in metadata.items()]
    native_inventory = _omission_inventory(native_home)
    native_inventory.update(config_inventory)
    control_files.write(control / 'customizations.json', json.dumps([
        native_inventory, *(_omission_inventory(path) for path in discovery_paths)], indent=2))
    if effort is not None:
        lines.append(f'model_reasoning_effort = {quoted(effort)}')
    lines += ['[sandbox_workspace_write]', 'writable_roots = []', 'exclude_slash_tmp = true',
              'exclude_tmpdir_env_var = true', 'network_access = false', '[features]']
    lines += [f'{k} = false' for k in _FEATURES]
    lines += ['[agents]', 'enabled = false', '[skills]', 'include_instructions = false',
              '[skills.bundled]', 'enabled = false']
    for root in dict.fromkeys((cwd, *roots)):
        lines += [f'[projects.{quoted(str(root))}]', 'trust_level = "untrusted"']
    lines += ['[permissions.maestro.filesystem]', '"/" = "read"']
    # Name all roots explicitly; a reviewer receives no cwd write grant. Include
    # root-level and nested secret paths rather than research fixture-only globs.
    for root in profile_roots:
        if root == cwd:
            continue
        lines.append(f'{quoted(str(root))} = "write"')
    lines.append(f'{quoted(str(cwd))} = "' + ('write' if cwd in roots else 'read') + '"')
    for root in dict.fromkeys((cwd, *roots)):
        lines += [f'{quoted(str(root / pattern))} = "deny"'
                  for pattern in ('.env', '.env.*', '.credentials*', '**/.env',
                                  '**/.env.*', '**/.credentials*')]
    lines += [f'{quoted(str(directory))} = "read"']
    lines += [f'{quoted(str(path))} = "read"' for path in protected]
    # Wildcard descendants cover discovery paths introduced after startup without
    # asking the native sandbox to create an absent mountpoint on a read-only
    # ancestor. Existing directories are also masked in the outer namespace.
    lines += [f'{quoted(str(path / "**"))} = "deny"' for path in discovery_paths]
    blocked = [control, *(host_home / name for name in ('.ssh', '.aws', '.maestro')
                          if (host_home / name).exists())]
    for base in (native_home, state):
        blocked += [base / name for name in ('auth.json', 'config.toml', 'rules',
                    'skills', 'plugins', 'hooks.json', 'sessions', 'archived_sessions',
                    'history.jsonl', 'state_*.sqlite*', 'logs_*.sqlite*')]
    # Explicit absent paths on a read-only parent can make native sandbox mount
    # setup fail. Wildcards safely cover native files created later in the run.
    blocked = [p for p in blocked if '*' in p.name or p.exists()
               or (p.parent == native_home and (state / p.name).exists())]
    lines += [f'{quoted(str(p))} = "deny"' for p in dict.fromkeys(blocked)]
    lines += ['[permissions.maestro.network]', 'enabled = false']
    configuration = '\n'.join(lines) + '\n'
    control_files.write(control / 'config.toml', configuration)
    deny = (*permissions.NEVER_ALLOW, f'Edit({trusted_source}/**)', f'Edit({directory}/**)',
            f'Edit({workspace}/.native-git/**)',
            *(f'Edit({path}/**)' for path in discovery_paths),
            *(f'Edit({path})' for path in protected),
            f'Read({native_home}/auth.json)', f'Edit({native_home}/**)',
            f'Read({control}/**)', f'Read({state}/sessions/**)')
    policy = ToolPolicy(allow=tuple(permissions.allow_rules() if allow is None else allow),
                        write_roots=profile_roots, deny=deny)
    created_mountpoints = []
    try:
        for target in discovery_paths:
            # Native deny globs expand existing paths and miss a new root. Seal
            # an empty mountpoint before startup, retaining its cleanup identity.
            if not target.exists() and any(target.parent.is_relative_to(root) for root in roots):
                control_files.mkdir(target)
                info = target.lstat()
                created_mountpoints.append((str(target), info.st_dev, info.st_ino))
    except BaseException:
        control_files.release_mountpoints(created_mountpoints)
        raise
    transaction = git_staging.Transaction.prepare(cwd, workspace) if cwd in roots else None
    return PreparedRun(cwd, workspace, native_home, host_home, directory, control,
                       state, scratch, roots, policy, model, effort,
                       json.dumps(tomllib.loads(configuration)), protected, trusted_source,
                       discovery_paths, tuple(created_mountpoints), transaction,
                       auth_identity=auth_identity)
