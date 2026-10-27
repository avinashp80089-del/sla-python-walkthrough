"""Isolated project fixtures: tests never mutate the deliverable's example data."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import prepare_inputs


@pytest.fixture
def project_copy(tmp_path: Path) -> Path:
    original = prepare_inputs()
    project = tmp_path / "project"
    project.mkdir()
    for directory in ("data", "contracts"):
        shutil.copytree(original / directory, project / directory)
    return project


@pytest.fixture
def new_guard(project_copy: Path, tmp_path: Path):
    """Instantiate after a test has finished editing its isolated input fixtures."""
    from sla_guard.engine import ReleaseGuard

    def create():
        return ReleaseGuard(project_root=project_copy, workspace=tmp_path / "workspace")

    return create


@pytest.fixture
def guard(new_guard):
    return new_guard()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    assert rows, "Use the original header explicitly for a deliberately empty CSV"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def refresh_manifest(project: Path, feed: str) -> None:
    """A valid transport checksum must not conceal semantically invalid contents."""
    manifest_path = project / "data" / f"{feed}.manifest.json"
    manifest = read_json(manifest_path)
    data_path = project / "data" / f"{feed}.csv"
    manifest["sha256"] = hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest["record_count"] = len(read_csv(data_path))
    write_json(manifest_path, manifest)


def assert_blocked(run: dict) -> None:
    assert run["status"] == "BLOCKED", run
    assert any(check["status"] == "FAIL" for check in run["checks"]), run



if __name__ == "__main__":
    print("Test fixtures are ready. Run python 04_verify_tests.py to execute the tests.")
