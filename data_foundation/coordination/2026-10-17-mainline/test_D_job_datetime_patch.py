"""Pure-function regression for a proposed D patch, without importing D app."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest


def candidate(tmp_path):
    folder = Path(__file__).resolve().parent
    target = tmp_path / "app/api/v1/jobs.py"
    target.parent.mkdir(parents=True)
    target.write_bytes((folder / "D-30-original-jobs.py").read_bytes())
    subprocess.run(["git", "apply", str(folder / "D-30-job-readback.patch")], cwd=tmp_path, check=True)
    module = ast.parse(target.read_text())
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "_to_cn_tz")
    tree = ast.Module(body=[function], type_ignores=[])
    namespace = {"datetime": datetime, "timezone": timezone, "_CN_TZ": timezone(timedelta(hours=8))}
    exec(compile(tree, "D-proposed-datetime-helper", "exec"), namespace)
    return namespace["_to_cn_tz"]


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None),
    (datetime(2026, 10, 17, 16, tzinfo=timezone.utc), "2026-10-18T00:00:00+08:00"),
    (datetime(2026, 10, 17, 16), "2026-10-18T00:00:00+08:00"),
    ("2026-10-17T16:00:00+00:00", "2026-10-18T00:00:00+08:00"),
    ("2026-10-17T16:00:00Z", "2026-10-18T00:00:00+08:00"),
    ("2026-10-18T00:00:00+08:00", "2026-10-18T00:00:00+08:00"),
])
def test_D_timestamp_helper_accepts_C_datetimes_and_old_ISO_values(value, expected, tmp_path):
    assert candidate(tmp_path)(value) == expected
