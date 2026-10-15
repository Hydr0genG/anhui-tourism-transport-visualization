"""Safety guards for the optional official E 0.5.1 / C MySQL acceptance tool."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


PATH = Path(__file__).resolve().parents[1] / "examples/verify_ce051_knowledge.py"
SPEC = importlib.util.spec_from_file_location("ce051_receiving", PATH)
receiving = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiving)


@pytest.mark.parametrize("url", [
    "sqlite:///demo_test", "postgresql://localhost/demo_test",
    "mysql://localhost/demo_test", "mysql+pymysql://localhost/business",
    "mysql+pymysql://localhost/demo_test_backup", "mysql+pymysql://localhost/",
])
def test_rejects_wrong_driver_or_business_schema_before_connect(url):
    with pytest.raises(ValueError, match="exclusive empty"):
        receiving.guard_url(url)


def test_exclusive_test_url_parsed_without_connecting():
    assert receiving.guard_url("mysql+pymysql://localhost/ce051_test").database == "ce051_test"


@pytest.mark.parametrize("name", ["", "/etc/passwd", "../out", "a/../out", "a//out", "a\\out", "C:/out", "a/./out", "a/\0out"])
def test_archive_path_guard(name):
    with pytest.raises(ValueError, match="Unsafe"):
        receiving.safe_member(name)


def test_archive_safe_relative_member():
    assert str(receiving.safe_member("docs/fixtures/index.json")) == "docs/fixtures/index.json"


def test_modified_delivery_fails_pinned_digest(tmp_path):
    wheel = tmp_path / "changed.whl"
    wheel.write_bytes(b"candidate replacement, not the release")
    with pytest.raises(AssertionError, match="c_wheel SHA256 mismatch"):
        receiving.check_pins({"c_wheel": wheel})


def test_cli_does_not_print_database_credentials(tmp_path):
    import os
    env = {**os.environ, "TEST_DATABASE_URL": "mysql+pymysql://user:DO_NOT_DISCLOSE@invalid/business"}
    result = subprocess.run([sys.executable, "-I", str(PATH), "--run-root", str(tmp_path / "new-run")],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert "ValueError" in result.stderr
    assert "DO_NOT_DISCLOSE" not in result.stderr + result.stdout
    assert not (tmp_path / "new-run").exists()
