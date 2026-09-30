#!/usr/bin/env python3
"""Disposable review probes: real CLI/limits/governor; fake provider I/O and inference."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/home/dan/projects/modelctl')
sys.path.insert(0, '/home/dan/projects/modelctl/tests')
import conftest as fixtures
from modelctl.cli import main
from modelctl.core.addflow import Report, run_add
from modelctl.core.maestro import StepError
from modelctl.core.scan import Candidate, ScanResult, scan_provider
from modelctl.core.state import State
from modelctl.core.inference import VERIFIED
from modelctl.providers.codex import CodexProvider
from modelctl.providers.claude import ClaudeProvider


def env_at(path):
    path.mkdir(parents=True)
    return fixtures.env.__wrapped__(path)


def probe():
    findings = []
    with tempfile.TemporaryDirectory(prefix='modelctl-maestro-review-') as raw:
        root = Path(raw)
        env = env_at(root / 'codex')
        env.maestro_repo.mkdir()
        config = env.maestro_repo / 'project.yaml'
        config.write_text('roles:\n  implementer: {backend: claude, models: {claude: claude-sonnet-5-5}}\n')
        cli = fixtures.FakeCodexCli(env)
        provider = CodexProvider(env, cli=cli, http_get=lambda u, h: {'models': cli.bundled})
        before = config.read_bytes()
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            code = main(['add', 'codex', '--model', 'gpt-6.2-sol', '--handoff', '180K-200K',
                         '--fresh', '240K-270K', '--ceiling', '350K', '--yes'],
                        env=env, providers={'codex': provider})
        assert code == 0 and config.read_bytes() == before
        assert json.loads(env.maestro_cache.read_text())['GPT-6.2 Sol']['exception_ceiling'] == 350000
        print(log.getvalue())
        findings.append({'probe': 'Codex add public CLI', 'result': 'PASS',
                         'real': 'table, session hook, Maestro resolver/cache',
                         'fake': 'discovery, daemon and inference',
                         'project_routing_configuration_updated': False})

        env = env_at(root / 'claude')
        provider = ClaudeProvider(env, http_get=lambda u, h: fixtures.claude_listing(
            'claude-haiku-4-5-20251001', 'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-sonnet-6'),
            cli=fixtures.FakeClaudeCli())
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            code = main(['add', 'claude', '--model', 'claude-sonnet-6', '--handoff', '160K-180K',
                         '--fresh', '220K-240K', '--ceiling', '320K', '--yes'],
                        env=env, providers={'claude': provider})
        assert code == 0
        assert 'Claude Sonnet 6' in json.loads(env.maestro_cache.read_text())
        print(log.getvalue())
        findings.append({'probe': 'Claude add public CLI', 'result': 'PASS',
                         'real': 'table, governor exact id/family/window, Maestro resolver/cache',
                         'fake': 'discovery and inference'})

        # A deterministic state-save exception occurs after the config transaction.
        env = env_at(root / 'state-failure')
        cli = fixtures.FakeCodexCli(env)
        provider = CodexProvider(env, cli=cli, http_get=lambda u, h: {'models': cli.bundled})
        from argparse import Namespace
        args = Namespace(model='gpt-6.2-sol', handoff='180K-200K', fresh='240K-270K', ceiling='350K',
                         window=None, update=False, no_restart=False)
        with contextlib.redirect_stdout(io.StringIO()):
            plan = provider.build_add(env, args)
        before = env.codex_limits.read_bytes()
        def fail_record(status):
            raise OSError('injected state-save failure')
        plan.record = fail_record
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                run_add(plan, env, Report('state-failure'), yes=True)
        except OSError:
            changed = env.codex_limits.read_bytes() != before
            assert changed
            findings.append({'probe': 'state-save failure after valid add',
                             'result': 'GAP REPRODUCED', 'exception_escaped': True,
                             'configuration_left_changed': changed, 'rollback_missing': True})
        else:
            raise AssertionError('expected injected exception')

        # A configured lifecycle row is labelled VERIFIED without an access check.
        class ConfiguredProvider:
            name = 'configured-probe'
            label = 'probe'
            add_command = 'probe'
            def discover(self):
                return [Candidate('probe-1', 'Probe 1', 'probe', (1,), 'fp')]
            def configured_keys(self):
                return {'probe-1'}
            def smoke(self, candidate):
                raise AssertionError('must not call inference in this probe')
            def sync_aliases(self, aliases):
                pass
        state = State(root / 'configured-state.json')
        result = ScanResult()
        scan_provider(ConfiguredProvider(), state, lambda msg: None, result)
        assert state.models('configured-probe')['probe-1']['access'] == VERIFIED
        assert result.smoke_calls == 0
        findings.append({'probe': 'first scan with an existing lifecycle row', 'result': 'GAP REPRODUCED',
                         'access_label': VERIFIED, 'actual_access_checks': 0})
    output = Path('/tmp/modelctl-maestro-review-smoke.json')
    output.write_text(json.dumps(findings, indent=2) + '\n')
    print(json.dumps(findings, indent=2))


if __name__ == '__main__':
    probe()
