"""Exercise the CLI boundary with synthetic subprocess results only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from margin_ta_runner import MarginTARunner


@pytest.fixture
def runner(monkeypatch, tmp_path):
    python = tmp_path / '.venv/bin/python3'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv('MARGIN_TA_HOME', str(tmp_path))
    monkeypatch.setenv('KINVEST_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.delenv('TOSS_TOKEN_CACHE', raising=False)
    return MarginTARunner()


def completed(stdout, returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def invoke(runner, command):
    if command == 'analyze':
        return runner.analyze('TEST')
    return getattr(runner, command)()


@pytest.mark.parametrize('command,timeout,script,flags', [
    ('analyze', 120, 'margin_ta.py', ['TEST', '--json', '--quiet', '--no-tv', '--no-session-quote', '--no-options', '--no-market']),
    ('market_risk', 180, 'market_risk.py', ['--json']),
    ('scan_top_stocks', 1800, 'scan_nightly.py', ['--json', '--top', '5']),
])
def test_cli_contract(runner, monkeypatch, command, timeout, script, flags):
    payload = {'symbol': 'TEST', 'warnings': [], 'score': 42}
    run = Mock(return_value=completed(json.dumps(payload)))
    monkeypatch.setattr(subprocess, 'run', run)
    assert invoke(runner, command) == payload
    args, kwargs = run.call_args
    assert args[0] == [str(runner._python), str(runner._analyze_script.parent / script), *flags]
    assert kwargs['timeout'] == timeout
    assert kwargs['capture_output'] is True
    assert kwargs['text'] is True
    assert kwargs['env']['TOSS_TOKEN_CACHE'] == str(runner._toss_cache)
    assert run.call_count == 1


def test_optional_arguments_and_explicit_cache_are_preserved(runner, monkeypatch, tmp_path):
    custom_cache = tmp_path / 'explicit-cache.json'
    monkeypatch.setenv('TOSS_TOKEN_CACHE', str(custom_cache))
    run = Mock(return_value=completed('{}'))
    monkeypatch.setattr(subprocess, 'run', run)
    runner.analyze('TEST', market='us', no_market=False, extra_args=['--fixture'])
    assert run.call_args.args[0][-3:] == ['--market', 'us', '--fixture']
    assert '--no-market' not in run.call_args.args[0]
    assert run.call_args.kwargs['env']['TOSS_TOKEN_CACHE'] == str(custom_cache)
    runner.market_risk(include_kr=False)
    assert run.call_args.args[0][-2:] == ['--json', '--no-kr']
    runner.scan_top_stocks(top_n=7, min_score=65)
    assert run.call_args.args[0][-4:] == ['--top', '7', '--min-score', '65']


@pytest.mark.parametrize('command,label', [
    ('analyze', 'margin-ta'), ('market_risk', 'market_risk'), ('scan_top_stocks', 'Scan'),
])
def test_process_and_parse_errors_keep_existing_envelopes(runner, monkeypatch, command, label):
    run = Mock(return_value=completed('ignored', returncode=2, stderr='  failure\n'))
    monkeypatch.setattr(subprocess, 'run', run)
    assert invoke(runner, command) == {
        'error': True, 'message': f'{label} failed with return code 2', 'stderr': 'failure',
    }
    run.return_value = completed('x' * 2100 + 'END\n')
    assert invoke(runner, command) == {
        'error': True, 'message': f'Failed to parse {label.lower()} JSON output',
        'stdout': '…(truncated)…\n' + 'x' * 1997 + 'END',
    }


@pytest.mark.parametrize('command', ['analyze', 'market_risk', 'scan_top_stocks'])
@pytest.mark.parametrize('stdout', ['null', '[]', '"text"'])
def test_non_object_json_is_a_parse_error(runner, monkeypatch, command, stdout):
    monkeypatch.setattr(subprocess, 'run', Mock(return_value=completed(stdout)))
    result = invoke(runner, command)
    assert result['error'] is True
    assert result['message'].startswith('Failed to parse ')
    assert result['stdout'] == stdout


@pytest.mark.parametrize('retry', [
    completed('{broken'), completed('[]'), completed('', 1, 'failed'),
])
def test_unsuccessful_retry_keeps_original_analysis(runner, monkeypatch, retry):
    payload = {'symbol': 'TEST', 'warnings': ['토스 candles 401'], 'current_price': 100}
    runner._toss_cache.parent.mkdir()
    runner._toss_cache.write_text('synthetic expired cache')
    run = Mock(side_effect=[completed(json.dumps(payload)), retry])
    monkeypatch.setattr(subprocess, 'run', run)
    assert runner.analyze('TEST') == payload
    assert run.call_count == 2
    assert run.call_args_list[0] == run.call_args_list[1]
    assert not runner._toss_cache.exists()


def test_retry_is_bounded_even_if_warning_remains(runner, monkeypatch):
    payload = {'symbol': 'TEST', 'warnings': ['토스 candles 401'], 'current_price': 100}
    recovered = {**payload, 'current_price': 101}
    run = Mock(side_effect=[completed(json.dumps(payload)), completed(json.dumps(recovered))])
    monkeypatch.setattr(subprocess, 'run', run)
    assert runner.analyze('TEST') == recovered
    assert run.call_count == 2


@pytest.mark.parametrize('command', ['analyze', 'market_risk', 'scan_top_stocks'])
def test_timeout_still_propagates(runner, monkeypatch, command):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=subprocess.TimeoutExpired('fixture', 1)))
    with pytest.raises(subprocess.TimeoutExpired):
        invoke(runner, command)
