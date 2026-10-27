from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from sla_guard.cli import reset_owned_workspace


def test_reset_refuses_unrelated_files_even_in_a_marked_workspace(guard):
    unrelated = guard.workspace / "my-notes.txt"
    unrelated.write_text("Keep this file")
    with pytest.raises(ValueError, match="unrelated"):
        reset_owned_workspace(guard.project_root, guard.workspace)
    assert unrelated.read_text() == "Keep this file"
    assert guard.db_path.exists()


def test_reset_never_accepts_project_or_ancestor(guard):
    with pytest.raises(ValueError, match="project or an ancestor"):
        reset_owned_workspace(guard.project_root, guard.project_root)


def test_shared_report_omits_personal_absolute_paths(guard):
    guard.publish(guard.evaluate("baseline")["run_id"])
    paths = guard.export_report()
    content = Path(paths["summary_json"]).read_text()
    assert str(guard.project_root) not in content
    assert str(guard.workspace) not in content
    assert "PROJECT_ROOT" in content


def test_module_cli_runs_from_source_only_package(tmp_path):
    """The alternate entry point must create packaged fixtures, not seek legacy data/."""
    import json
    import os
    import shutil
    import subprocess
    source = Path(__file__).resolve().parents[1]
    package = tmp_path / "source_only"
    package.mkdir()
    shutil.copytree(source / "sla_guard", package / "sla_guard", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("support.py", "02_inputs_and_contract.py"):
        shutil.copy2(source / name, package / name)
    assert not (package / "data").exists()
    result = subprocess.run(
        [sys.executable, "-m", "sla_guard", "demo"], cwd=package,
        env=dict(os.environ, PYTHONPATH=str(package)), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "DEMO_PASSED"
    assert evidence["blocked_attempt_preserved_active_snapshot"] is True
    assert evidence["independent_order_oracle_matched"] is True
    assert evidence["metrics"]["compliance_rate"] == 50.0
    assert (package / ".runtime/inputs/data/feed_v1.csv").is_file()
    assert (package / ".runtime/sessions/cli_demo/sla_guard.sqlite").is_file()


if __name__ == "__main__":
    from pathlib import Path
    raise SystemExit(pytest.main([str(Path(__file__)), "-q", "--tb=short"]))
