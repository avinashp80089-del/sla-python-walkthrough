"""State transitions, evidence integrity, rollback, and transaction failure tests."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


def published_state(guard):
    snapshot = guard.snapshot()
    return snapshot["active_release"], snapshot["active_rows"], snapshot["releases"]


def publish_baseline(guard):
    run = guard.evaluate("baseline")
    result = guard.publish(run["run_id"])
    assert result["status"] == "PUBLISHED", result
    return run, result


def test_initial_state_has_no_published_data(guard):
    snapshot = guard.snapshot()
    assert snapshot["active_release"] is None
    assert snapshot["active_rows"] == []
    with sqlite3.connect(guard.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM published_order_sla").fetchone()[0] == 0


def test_repeated_publication_is_idempotent(guard):
    run, first = publish_baseline(guard)
    before = published_state(guard)
    second = guard.publish(run["run_id"])
    assert second["status"] == "NO_OP", second
    assert second["active_release_id"] == first["active_release_id"]
    assert published_state(guard) == before


def test_an_unknown_run_cannot_create_a_release(guard):
    before = published_state(guard)
    assert guard.publish("RUN-DOES-NOT-EXIST")["status"] == "BLOCKED"
    assert published_state(guard) == before


@pytest.mark.parametrize("relative_path", [
    "data/feed_v2.csv", "data/feed_v2.manifest.json",
    "data/orders.csv", "data/packages.csv", "contracts/sla_contract.json",
    "contracts/adapter_v2.json",
])
def test_changed_evidence_after_evaluation_blocks_release(
    project_copy, guard, relative_path
):
    publish_baseline(guard)
    run = guard.evaluate("corrected")
    assert run["status"] == "PASS"
    before = published_state(guard)
    path = project_copy / relative_path
    path.write_bytes(path.read_bytes() + b"\n")
    result = guard.publish(run["run_id"])
    assert result["status"] == "BLOCKED", result
    assert published_state(guard) == before
    assert guard.snapshot()["events"]


def test_checked_candidate_artifact_is_protected_from_post_validation_edit(guard):
    publish_baseline(guard)
    run = guard.evaluate("corrected")
    candidates = [a for a in run["artifacts"] if "candidate" in Path(a["path"]).name.lower()]
    assert candidates, "PASS run must identify its persisted candidate artifact"
    artifact = candidates[0]
    path = Path(artifact["path"])
    assert path.is_absolute(), "Artifact references must be unambiguous absolute paths"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    before = published_state(guard)
    path.write_bytes(path.read_bytes() + b"\nTAMPERED\n")
    result = guard.publish(run["run_id"])
    assert result["status"] == "BLOCKED", result
    assert published_state(guard) == before


def test_evaluations_cannot_overwrite_a_release_they_did_not_observe(guard):
    publish_baseline(guard)
    old_candidate = guard.evaluate("corrected")
    new_candidate = guard.evaluate("corrected")
    assert old_candidate["observed_active_release_id"] == new_candidate["observed_active_release_id"]
    assert guard.publish(new_candidate["run_id"])["status"] == "PUBLISHED"
    before = published_state(guard)
    result = guard.publish(old_candidate["run_id"])
    assert result["status"] == "BLOCKED", result
    assert published_state(guard) == before


def test_two_publishers_of_distinct_candidates_cannot_both_win(guard, new_guard):
    publish_baseline(guard)
    second_guard = new_guard()
    left = guard.evaluate("corrected")
    right = second_guard.evaluate("corrected")
    assert left["observed_active_release_id"] == right["observed_active_release_id"]
    before_count = len(guard.snapshot()["releases"])
    barrier = Barrier(2, timeout=10)

    def publish_together(instance, run_id):
        barrier.wait()
        return instance.publish(run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(publish_together, guard, left["run_id"]),
            pool.submit(publish_together, second_guard, right["run_id"]),
        ]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(result["status"] for result in results) == ["BLOCKED", "PUBLISHED"], results
    snapshot = guard.snapshot()
    assert len(snapshot["releases"]) == before_count + 1
    assert len(snapshot["active_rows"]) == 12
    assert snapshot["active_release"]["run_id"] in {left["run_id"], right["run_id"]}


def test_rollback_requires_reason_and_preserves_an_intact_historical_snapshot(guard):
    baseline_run, baseline = publish_baseline(guard)
    baseline_state = guard.snapshot()
    corrected_run = guard.evaluate("corrected")
    assert guard.publish(corrected_run["run_id"])["status"] == "PUBLISHED"
    before = published_state(guard)
    release_id = baseline["active_release_id"]
    assert guard.rollback(release_id, "   ")["status"] == "BLOCKED"
    assert published_state(guard) == before
    rollback = guard.rollback(release_id, "Synthetic demonstration of a verified rollback")
    assert rollback["status"] == "ROLLED_BACK", rollback
    restored = guard.snapshot()
    assert restored["active_release"]["release_id"] == release_id
    assert restored["active_rows"] == baseline_state["active_rows"]
    assert restored["active_release"]["run_id"] == baseline_run["run_id"]
    # A publication command must not silently undo an explicit rollback.
    after_rollback = published_state(guard)
    assert guard.publish(corrected_run["run_id"])["status"] == "BLOCKED"
    assert published_state(guard) == after_rollback
    assert guard.rollback(release_id, "Already active")["status"] == "NO_OP"


def test_unknown_rollback_target_does_not_change_active_data(guard):
    publish_baseline(guard)
    before = published_state(guard)
    assert guard.rollback("RELEASE-DOES-NOT-EXIST", "Testing missing target")["status"] == "BLOCKED"
    assert published_state(guard) == before


def test_failed_sql_write_leaves_no_partial_release_or_pointer_change(guard):
    publish_baseline(guard)
    run = guard.evaluate("corrected")
    before = published_state(guard)
    with sqlite3.connect(guard.db_path) as connection:
        connection.execute(
            "CREATE TRIGGER test_release_write_failure BEFORE INSERT ON release_rows "
            "BEGIN SELECT RAISE(ABORT, 'injected test storage failure'); END"
        )
    try:
        result = guard.publish(run["run_id"])
        assert result["status"] == "BLOCKED", result
    finally:
        with sqlite3.connect(guard.db_path) as connection:
            connection.execute("DROP TRIGGER test_release_write_failure")
    assert published_state(guard) == before
    with sqlite3.connect(guard.db_path) as connection:
        orphan_count = connection.execute(
            "SELECT COUNT(*) FROM release_rows AS rr "
            "LEFT JOIN releases AS r ON rr.release_id = r.release_id "
            "WHERE r.release_id IS NULL"
        ).fetchone()[0]
        assert orphan_count == 0
    # A transient storage failure must not poison the evaluated candidate.
    retry = guard.publish(run["run_id"])
    assert retry["status"] == "PUBLISHED", retry


def test_exports_describe_the_active_snapshot_without_changing_it(guard):
    publish_baseline(guard)
    before = published_state(guard)
    paths = guard.export_report()
    assert {"summary_json", "impact_csv", "html", "active_csv"} <= paths.keys()
    for name in ("summary_json", "impact_csv", "html", "active_csv"):
        path = Path(paths[name])
        assert path.is_file() and path.stat().st_size > 0
    assert published_state(guard) == before



if __name__ == "__main__":
    from pathlib import Path
    raise SystemExit(pytest.main([str(Path(__file__)), "-q", "--tb=short"]))
