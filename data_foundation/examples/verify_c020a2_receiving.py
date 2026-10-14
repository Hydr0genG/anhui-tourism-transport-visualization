"""Verify immutable C 0.2.0a2 delivery and its installed wheel, never a patched copy.

Runs only isolated SQLite storage checks. Windows CI is supplementary evidence,
not E's Windows receipt, E JSON validation, or D/MySQL/HTTP joint acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

VERSION = '0.2.0a2'
SOURCE_COMMIT = '06d192f068817aa9ffd24ad988e92d9737592aff'
PREFIX = 'C-index-artifacts-0.2.0a2-2026-10-14'
WHEEL = 'anhui_data_foundation-0.2.0a2-py3-none-any.whl'
DIGESTS = {
    WHEEL: '4cf48f5c8c4303e2cb73a4169d62dd6111dba03df266256eced48ec565590fbe',
    PREFIX + '.zip': '7fcbbc0031a13520b5d02aa9ebc8f9066d7b5a237609b0b2c6b0a87a96e54ad5',
    PREFIX + '.contents.json': 'a554fd38650cf8942958cc1224b2c278c5746306e9ddc4759e07e34d8ff93b0d',
}
REQUIRED_TESTS = {
    'test_native_persist_file_closes_before_atomic_rename',
    'test_windows_directory_permission_error_is_invalid_argument',
    'test_publish_and_complete_same_transaction_read_rechecks',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_member(name):
    path = PurePosixPath(name)
    if (not name or not path.parts or '\\' in name or '\0' in name or path.is_absolute()
            or path.as_posix() != name or any(p in {'.', '..'} or ':' in p for p in path.parts)):
        raise ValueError('Unsafe archive member name')
    return path


def check_delivery(delivery_root):
    release = delivery_root / 'release'
    for name, expected in DIGESTS.items():
        if digest((release / name).read_bytes()) != expected:
            raise ValueError('Published delivery SHA256 differs: ' + name)
    listing = json.loads((release / (PREFIX + '.contents.json')).read_text(encoding='utf-8'))
    if listing['package_version'] != VERSION or listing['migration_head'] != '0006':
        raise ValueError('Unexpected delivery version or migration')
    with ZipFile(release / (PREFIX + '.zip')) as zipped:
        names = zipped.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive member')
        for info in zipped.infolist():
            safe_member(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('Symlink archive member')
        manifest_name = 'data_foundation/release/' + PREFIX + '.contents.json'
        if set(names) != set(listing['files']) | {manifest_name}:
            raise ValueError('Archive members differ from manifest')
        if zipped.read(manifest_name) != (release / (PREFIX + '.contents.json')).read_bytes():
            raise ValueError('Embedded manifest differs')
        for name, expected in listing['files'].items():
            if digest(zipped.read(name)) != expected:
                raise ValueError('Archive member SHA256 differs: ' + name)
        if zipped.read('data_foundation/release/' + WHEEL) != (release / WHEEL).read_bytes():
            raise ValueError('Embedded wheel differs')
    return release


def copy_external_harness(archive, work_dir):
    if work_dir.exists() and any(work_dir.iterdir()):
        raise ValueError('Receiving work directory must be empty; nothing is removed')
    work_dir.mkdir(parents=True, exist_ok=True)
    prefixes = ('data_foundation/tests/', 'data_foundation/examples/', 'data_foundation/alembic/',
                'data_foundation/assets/', 'data_foundation/real_data/')
    with ZipFile(archive) as zipped:
        for info in zipped.infolist():
            if info.filename == 'data_foundation/alembic.ini' or info.filename.startswith(prefixes):
                path = safe_member(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError('Symlink archive member')
                target = work_dir.joinpath(*path.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zipped.read(info))
    if (work_dir / 'data_foundation').exists():
        raise ValueError('Package source must not shadow the installed wheel')


def clean_environment():
    env = dict(os.environ)
    # Even if the parent shell has business credentials, this receipt uses no DB.
    for name in ('COVERAGE_MYSQL_ADMIN_URL', 'TEST_DATABASE_URL', 'DATABASE_URL', 'DATA_ROOT', 'PYTHONPATH'):
        env.pop(name, None)
    return env


def run_python(work_dir, args):
    result = subprocess.run([sys.executable, '-I', '-X', 'utf8', *args], cwd=work_dir,
                            env=clean_environment(), capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=300)
    return result


def installed_identity(release, work_dir, report_dir=None):
    result = run_python(work_dir, ['-c',
        'import data_foundation as c, json; from importlib.metadata import version; '
        'print(json.dumps({"version":c.__version__, "distribution_version":version("anhui-data-foundation"),'
        '"module_origin":c.__file__}))'])
    if report_dir is not None:
        (report_dir / 'installed-origin.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('Installed C package could not be imported')
    identity = json.loads(result.stdout)
    if identity['version'] != VERSION or identity['distribution_version'] != VERSION:
        raise ValueError('Install the exact C 0.2.0a2 wheel, not an older or candidate runtime')
    package_root = Path(identity['module_origin']).resolve().parent
    if package_root.is_relative_to(work_dir):
        raise ValueError('Source checkout shadows installed wheel')
    count = 0
    with ZipFile(release / WHEEL) as zipped:
        for name in zipped.namelist():
            if name.startswith('data_foundation/') and name.endswith(('.py', '.json')):
                relative = safe_member(name).parts[1:]
                if package_root.joinpath(*relative).read_bytes() != zipped.read(name):
                    raise ValueError('Installed package differs from formal wheel: ' + name)
                count += 1
    identity['source_files_verified'] = count
    return identity


def junit_results(path, expected, allow_fifo_skip):
    cases = list(ET.parse(path).getroot().iter('testcase'))
    failed = [c.attrib.get('name') for c in cases if c.find('failure') is not None or c.find('error') is not None]
    skips = [{'name': c.attrib.get('name'), 'reason': c.find('skipped').attrib.get('message', '')}
             for c in cases if c.find('skipped') is not None]
    if len(cases) != expected or failed:
        raise ValueError(f'Unexpected regression result: total={len(cases)}, failures={failed}')
    for skipped in skips:
        if (not allow_fifo_skip or skipped['name'] != 'test_file_failure_does_not_publish[fifo]'
                or 'os.mkfifo' not in skipped['reason']):
            raise ValueError('Unapproved test skip: ' + skipped['name'])
    passed = {c.attrib.get('name') for c in cases if c.find('failure') is None
              and c.find('error') is None and c.find('skipped') is None}
    if not REQUIRED_TESTS <= passed:
        raise ValueError('Native persistence, directory rejection, and activation/read tests must pass')
    return {'total': len(cases), 'passed': len(cases) - len(skips), 'skipped': skips, 'failed': failed}


def verify(delivery_root, work_dir, report_dir, suite, require_windows):
    report = {
        'status': 'failed', 'scope': 'supplementary_installed_C_sqlite_storage_regression',
        'package_version': VERSION, 'source_commit': SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'windows_native_run': os.name == 'nt', 'suite': suite, 'delivery_sha256': DIGESTS,
        'Windows_official_C_receiving': 'pending', 'formal_DE_knowledge_chain': 'pending',
        'MySQL_verified_in_this_run': False, 'E_JSON_binding_verified': False,
        'HTTP_and_worker_restart_verified': False, 'crawler_step2': 'paused',
        'github_run_id': os.getenv('GITHUB_RUN_ID'), 'github_sha': os.getenv('GITHUB_SHA'),
        'verified_at': datetime.now(timezone.utc).isoformat(),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        if require_windows and os.name != 'nt':
            raise ValueError('--require-windows refuses simulated/non-Windows evidence')
        release = check_delivery(delivery_root)
        copy_external_harness(release / (PREFIX + '.zip'), work_dir)
        report['installed_package'] = installed_identity(release, work_dir, report_dir)
        targets = ['tests'] if suite == 'all' else [
            'tests/test_index_artifacts.py', 'tests/test_artifact_windows_compat.py']
        result = run_python(work_dir, ['-m', 'pytest', *targets, '-q', '-ra',
                                     '--junitxml', str(report_dir / 'pytest.xml')])
        (report_dir / 'pytest.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError('Pytest failed; see pytest.txt and pytest.xml')
        report['regression'] = junit_results(report_dir / 'pytest.xml', 311 if suite == 'all' else 69,
                                            os.name == 'nt' and not hasattr(os, 'mkfifo'))
        # Isolated Python omits the script directory. Add only the already
        # verified external examples directory, never C package source.
        smoke = run_python(work_dir, ['-c',
            'import runpy, sys; sys.path.insert(0, "examples"); '
            'runpy.run_path("examples/index_artifact_smoke.py", run_name="__main__")'])
        (report_dir / 'artifact-smoke.txt').write_text(smoke.stdout + smoke.stderr, encoding='utf-8')
        if smoke.returncode:
            raise RuntimeError('Native publication/readback smoke failed')
        report['smoke'] = json.loads(smoke.stdout)
        if report['smoke']['status'] != 'passed' or len(report['smoke']['checks']) != 3:
            raise ValueError('Unexpected smoke result')
        report['dependencies'] = {name: metadata.version(name) for name in
                                  ('SQLAlchemy', 'alembic', 'PyMySQL', 'pytest')}
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc)
    (report_dir / 'receiving.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                               encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery-root', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--suite', choices=['files', 'all'], default='files')
    parser.add_argument('--require-windows', action='store_true')
    args = parser.parse_args()
    report = verify(args.delivery_root.resolve(), args.work_dir.resolve(), args.report_dir.resolve(),
                    args.suite, args.require_windows)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
