"""Unit checks for receiving evidence; not E/Windows/MySQL joint acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from zipfile import ZipFile

import pytest

spec = importlib.util.spec_from_file_location('c_receiving',
    Path(__file__).resolve().parents[1] / 'examples/verify_c020a2_receiving.py')
receiving = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiving)


@pytest.mark.parametrize('name', ['', '.', '../escape', '/absolute', 'C:/file',
                                 'x\\y', 'a//b', 'a/./b', 'a/../b', 'a/\0b'])
def test_receiving_rejects_unsafe_archive_names(name):
    with pytest.raises(ValueError):
        receiving.safe_member(name)


def test_receiving_allows_normal_unicode_path():
    assert receiving.safe_member('data_foundation/原件/notice.txt').parts[-1] == 'notice.txt'


def test_receiving_never_propagates_business_database_environment(monkeypatch):
    for name in ['DATABASE_URL', 'TEST_DATABASE_URL', 'COVERAGE_MYSQL_ADMIN_URL', 'DATA_ROOT', 'PYTHONPATH']:
        monkeypatch.setenv(name, 'unit-test-only')
    env = receiving.clean_environment()
    assert not {'DATABASE_URL', 'TEST_DATABASE_URL', 'COVERAGE_MYSQL_ADMIN_URL', 'DATA_ROOT', 'PYTHONPATH'} & env.keys()
    assert os.environ['DATABASE_URL'] == 'unit-test-only'


def test_receiving_child_python_is_isolated_and_utf8(tmp_path, monkeypatch):
    seen = {}
    def run(command, **kwargs):
        seen.update(command=command, options=kwargs)
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setenv('DATABASE_URL', 'unit-test-only')
    monkeypatch.setattr(receiving.subprocess, 'run', run)
    receiving.run_python(tmp_path, ['-c', 'pass'])
    assert seen['command'][1:4] == ['-I', '-X', 'utf8']
    assert 'DATABASE_URL' not in seen['options']['env']


def test_receiving_rejects_nonempty_work_directory_without_removal(tmp_path):
    marker = tmp_path / 'owned.txt'
    marker.write_text('keep')
    with pytest.raises(ValueError, match='must be empty'):
        receiving.copy_external_harness(tmp_path / 'missing.zip', tmp_path)
    assert marker.read_text() == 'keep'


def test_receiving_external_harness_does_not_copy_package_source(tmp_path):
    archive = tmp_path / 'test.zip'
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('data_foundation/tests/test_sample.py', '# external test')
        zipped.writestr('data_foundation/examples/sample.py', '# external example')
        zipped.writestr('data_foundation/data_foundation/__init__.py', '# must not be extracted')
    work = tmp_path / 'work'
    receiving.copy_external_harness(archive, work)
    assert (work / 'tests/test_sample.py').exists()
    assert not (work / 'data_foundation').exists()


def test_receiving_rejects_wrong_delivery_sha_before_extracting(tmp_path):
    release = tmp_path / 'release'
    release.mkdir()
    (release / receiving.WHEEL).write_bytes(b'not-the-formal-wheel')
    with pytest.raises(ValueError, match='Published delivery SHA256 differs'):
        receiving.check_delivery(tmp_path)


@pytest.mark.parametrize('version', ['0.1.9', '0.2.0a1'])
def test_receiving_rejects_old_installed_distribution(tmp_path, monkeypatch, version):
    value = {'version': version, 'distribution_version': version, 'module_origin': 'irrelevant.py'}
    monkeypatch.setattr(receiving, 'run_python', lambda *args: subprocess.CompletedProcess([], 0, json.dumps(value), ''))
    with pytest.raises(ValueError, match='exact C 0.2.0a2'):
        receiving.installed_identity(tmp_path, tmp_path / 'work')


def test_receiving_rejects_patched_candidate_even_with_same_version(tmp_path, monkeypatch):
    package = tmp_path / 'installed/data_foundation'
    package.mkdir(parents=True)
    (package / '__init__.py').write_bytes(b'patched-copy')
    with ZipFile(tmp_path / receiving.WHEEL, 'w') as wheel:
        wheel.writestr('data_foundation/__init__.py', b'formal-copy')
    value = {'version': '0.2.0a2', 'distribution_version': '0.2.0a2',
             'module_origin': str(package / '__init__.py')}
    monkeypatch.setattr(receiving, 'run_python', lambda *args: subprocess.CompletedProcess([], 0, json.dumps(value), ''))
    with pytest.raises(ValueError, match='differs from formal wheel'):
        receiving.installed_identity(tmp_path, tmp_path / 'work')


def junit(tmp_path, extra=''):
    path = tmp_path / 'pytest.xml'
    native = ''.join(f'<testcase name="{name}"/>' for name in receiving.REQUIRED_TESTS)
    path.write_text(f'<testsuites><testsuite>{native}{extra}</testsuite></testsuites>')
    return path


def test_receiving_accepts_only_documented_fifo_skip(tmp_path):
    xml = junit(tmp_path, '<testcase name="test_file_failure_does_not_publish[fifo]">'
                         '<skipped message="平台不提供 os.mkfifo"/></testcase>')
    result = receiving.junit_results(xml, 4, True)
    assert result['passed'] == 3
    assert result['skipped'][0]['reason'] == '平台不提供 os.mkfifo'


@pytest.mark.parametrize('extra,expected,fifo', [
    ('<testcase name="arbitrary"><skipped message="not allowed"/></testcase>', 4, True),
    ('<testcase name="bad"><failure/></testcase>', 4, False),
    ('<testcase name="bad"><error/></testcase>', 4, False),
    ('', 99, False),
])
def test_receiving_cannot_mark_missing_or_failed_tests_passed(tmp_path, extra, expected, fifo):
    with pytest.raises(ValueError):
        receiving.junit_results(junit(tmp_path, extra), expected, fifo)


def test_receiving_required_native_tests_cannot_be_skipped(tmp_path):
    path = tmp_path / 'pytest.xml'
    path.write_text('<testsuite><testcase name="unrelated"/></testsuite>')
    with pytest.raises(ValueError, match='must pass'):
        receiving.junit_results(path, 1, False)


def test_receiving_require_windows_never_accepts_local_simulation(tmp_path, monkeypatch):
    monkeypatch.setattr(receiving.os, 'name', 'posix')
    report = receiving.verify(tmp_path, tmp_path / 'work', tmp_path / 'report', 'files', True)
    assert report['status'] == 'failed'
    assert report['windows_native_run'] is False
    assert report['Windows_official_C_receiving'] == 'pending'
    assert 'refuses simulated/non-Windows' in report['error']
    assert json.loads((tmp_path / 'report/receiving.json').read_text())['status'] == 'failed'
