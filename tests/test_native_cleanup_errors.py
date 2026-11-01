"""All cleanup is attempted without losing the original failure or later errors."""
import pytest

from maestro.backends import agy_runtime, codex_runtime, control_files, native_ownership
from tests.test_git_staging import linked


@pytest.mark.parametrize('backend', ['codex', 'agy'])
@pytest.mark.parametrize('primary', [False, True])
def test_real_prepared_cleanup_preserves_every_failure(linked, tmp_path, monkeypatch, backend, primary):
    _, tree, reports, _ = linked
    home = tmp_path / 'home'
    native = home / ('.codex' if backend == 'codex' else '.gemini/antigravity-cli')
    (native / 'bin').mkdir(parents=True)
    if backend == 'codex':
        (native / 'auth.json').write_text('synthetic')
        run = codex_runtime.prepare(cwd=tree, workspace=reports, native_home=native,
            host_home=home, model='fixture', effort='low', write_roots=(tree,))
    else:
        (home / '.gemini/config').mkdir()
        (native / 'bin/agentapi').write_text('synthetic helper')
        run = agy_runtime.prepare(cwd=tree, workspace=reports, home=home, write_roots=(tree,))
    attempted = []
    def failure(label, original):
        def call(*args):
            attempted.append(label)
            original(*args)
            raise ValueError(label)
        return call
    monkeypatch.setattr(run.transaction, 'cleanup', failure('git-cleanup', run.transaction.cleanup))
    monkeypatch.setattr(native_ownership, 'cleanup', failure('owner-cleanup', native_ownership.cleanup))
    monkeypatch.setattr(control_files, 'release_mountpoints',
                        failure('mount-cleanup', control_files.release_mountpoints))
    with pytest.raises(ValueError, match='operation-failed' if primary else 'git-cleanup') as caught:
        if primary:
            try:
                raise ValueError('operation-failed')
            finally:
                run.cleanup()
        else:
            run.cleanup()
    assert attempted == ['git-cleanup', 'owner-cleanup', 'mount-cleanup']
    notes = '\n'.join(getattr(caught.value, '__notes__', ()))
    for label in (attempted if primary else attempted[1:]):
        assert label in notes

