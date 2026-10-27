import json
import sqlite3

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import assert_blocked, refresh_manifest


@pytest.mark.parametrize("file", ["contracts/sla_contract.json", "contracts/adapter_v2.json", "data/feed_v2.manifest.json"])
def test_non_object_configuration_is_blocked(project_copy, new_guard, file):
    (project_copy / file).write_text("[]")
    assert_blocked(new_guard().evaluate("corrected"))


def test_empty_feed_is_blocked_and_dashboard_comparison_is_unavailable(project_copy, new_guard):
    (project_copy / "data/feed_v2.csv").write_text("record_id,order_id,processing_duration\n")
    refresh_manifest(project_copy, "feed_v2")
    guard = new_guard()
    assert_blocked(guard.evaluate("corrected"))
    assert guard.snapshot()["comparison"] == []


def test_database_candidate_edit_is_detected(guard):
    run = guard.evaluate("corrected")
    with sqlite3.connect(guard.db_path) as db:
        db.execute("UPDATE candidate_rows SET duration_ms=1 WHERE run_id=? AND order_id='O1001'", (run["run_id"],))
    assert guard.publish(run["run_id"])["status"] == "BLOCKED"
    assert guard.snapshot()["active_release"] is None


def test_corrupt_historical_snapshot_cannot_be_restored(guard):
    baseline = guard.publish(guard.evaluate("baseline")["run_id"])
    corrected = guard.publish(guard.evaluate("corrected")["run_id"])
    with sqlite3.connect(guard.db_path) as db:
        db.execute("UPDATE release_rows SET duration_ms=1 WHERE release_id=? AND order_id='O1001'", (baseline["release_id"],))
    assert guard.rollback(baseline["release_id"], "Integrity test")["status"] == "BLOCKED"
    assert guard.snapshot()["active_release"]["release_id"] == corrected["release_id"]


def test_rollback_uses_saved_snapshot_not_mutated_current_inputs(project_copy, guard):
    first = guard.publish(guard.evaluate("baseline")["run_id"])
    assert guard.publish(guard.evaluate("corrected")["run_id"])["status"] == "PUBLISHED"
    (project_copy / "data/feed_v1.csv").write_text("This file is no longer a valid feed")
    assert guard.rollback(first["release_id"], "Restore the independently saved snapshot")["status"] == "ROLLED_BACK"


def test_release_generation_prevents_aba_stale_overwrite(guard):
    baseline = guard.publish(guard.evaluate("baseline")["run_id"])
    stale = guard.evaluate("corrected")
    assert guard.publish(guard.evaluate("corrected")["run_id"])["status"] == "PUBLISHED"
    assert guard.rollback(baseline["release_id"], "Return to original pointer")["status"] == "ROLLED_BACK"
    assert guard.publish(stale["run_id"])["status"] == "BLOCKED"


if __name__ == "__main__":
    from pathlib import Path
    raise SystemExit(pytest.main([str(Path(__file__)), "-q", "--tb=short"]))
