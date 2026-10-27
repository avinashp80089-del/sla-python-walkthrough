"""Independent business oracles and fail-closed validation scenarios."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from conftest import (
    assert_blocked,
    read_csv,
    read_json,
    refresh_manifest,
    write_csv,
    write_json,
)


# These values are hand specified from the scenario, not calculated by engine helpers.
EXPECTED_SECONDS = {
    "O1001": 1800, "O1002": 4200, "O1003": 3600, "O1004": 3900,
    "O1005": 600, "O1006": 3601, "O1007": 3300, "O1008": 7200,
    "O1009": 5400, "O1010": 900, "O1011": 3600, "O1012": 3601,
}
EXPECTED_MET = {"O1001", "O1003", "O1005", "O1007", "O1010", "O1011"}
PACKAGE_COUNTS = dict(zip(EXPECTED_SECONDS, [2, 2, 2, 3, 1, 2, 3, 1, 3, 2, 2, 1]))


@pytest.mark.parametrize("scenario", ["baseline", "corrected"])
def test_valid_candidates_match_every_hand_calculated_order(guard, scenario):
    run = guard.evaluate(scenario)
    assert run["status"] == "PASS", run
    assert run["structural_pass"] is True
    assert not [check for check in run["checks"] if check["status"] == "FAIL"]
    rows = run["candidate_rows"]
    assert len(rows) == 12
    assert {row["order_id"] for row in rows} == set(EXPECTED_SECONDS)
    for row in rows:
        oid = row["order_id"]
        assert Decimal(row["processing_duration"]) == Decimal(EXPECTED_SECONDS[oid])
        assert row["duration_ms"] == EXPECTED_SECONDS[oid] * 1000
        assert row["sla_ms"] == 3_600_000
        assert row["sla_met"] is (oid in EXPECTED_MET)
        if scenario == "corrected":
            assert row["source_record_count"] == PACKAGE_COUNTS[oid]
    metrics = run["metrics"]
    assert metrics["order_count"] == 12
    assert metrics["compliant_count"] == 6
    assert metrics["breach_count"] == 6
    assert metrics["compliance_rate"] == pytest.approx(50.0)
    assert metrics["breach_rate"] == pytest.approx(50.0)
    assert metrics["mean_duration_seconds"] == pytest.approx(41702 / 12)


def test_structurally_valid_change_is_blocked_without_touching_published_result(guard):
    baseline = guard.evaluate("baseline")
    assert guard.publish(baseline["run_id"])["status"] == "PUBLISHED"
    before = guard.snapshot()
    changed = guard.evaluate("changed")
    assert changed["structural_pass"] is True
    assert_blocked(changed)
    assert guard.publish(changed["run_id"])["status"] == "BLOCKED"
    after = guard.snapshot()
    assert after["active_release"] == before["active_release"]
    assert after["active_rows"] == before["active_rows"]
    assert len(after["releases"]) == len(before["releases"])


def test_impact_comparison_exposes_both_errors_and_correct_business_denominator(guard):
    comparison = {row["scenario"]: row for row in guard.snapshot()["comparison"]}
    expected = {
        "baseline": (12, 6, 50),
        "changed": (24, 0, 0),
        "units_only": (24, 18, 75),
        "corrected": (12, 6, 50),
    }
    for scenario, (count, met, rate) in expected.items():
        assert comparison[scenario]["row_count"] == count
        assert comparison[scenario]["compliant_count"] == met
        assert comparison[scenario]["compliance_rate"] == pytest.approx(rate)
    assert guard.snapshot()["active_release"] is None
    assert guard.snapshot()["releases"] == []


@pytest.mark.parametrize("duration,compliant", [("3600", True), ("3600.001", False)])
@pytest.mark.parametrize("scenario", ["baseline", "corrected"])
def test_exact_sla_boundary_uses_integer_milliseconds(
    project_copy, new_guard, duration, compliant, scenario
):
    # O1005 has one package, so the expected duration is unambiguous.
    seconds = Decimal(duration)
    orders = read_csv(project_copy / "data/orders.csv")
    accepted = next(row["accepted_at"] for row in orders if row["order_id"] == "O1005")
    start = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
    completion = start + timedelta(microseconds=int(seconds * 1_000_000))
    packages = read_csv(project_copy / "data/packages.csv")
    for row in packages:
        if row["order_id"] == "O1005":
            row["completed_at"] = completion.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    write_csv(project_copy / "data/packages.csv", packages)
    for feed, value in (("feed_v1", duration), ("feed_v2", str(int(seconds * 1000)))):
        rows = read_csv(project_copy / "data" / f"{feed}.csv")
        for row in rows:
            if row["order_id"] == "O1005":
                row["processing_duration"] = value
        write_csv(project_copy / "data" / f"{feed}.csv", rows)
        refresh_manifest(project_copy, feed)
    reference = read_csv(project_copy / "data/expected_orders.csv")
    for row in reference:
        if row["order_id"] == "O1005":
            row["processing_duration"] = duration
            row["sla_met"] = "1" if compliant else "0"
    write_csv(project_copy / "data/expected_orders.csv", reference)
    summary_path = project_copy / "data/expected_summary.json"
    summary = read_json(summary_path)
    summary.update(
        compliant_count=6 if compliant else 5,
        breach_count=6 if compliant else 7,
        compliance_rate=50.0 if compliant else 100 * 5 / 12,
        package_compliant_count=18 if compliant else 17,
    )
    write_json(summary_path, summary)
    run = new_guard().evaluate(scenario)
    assert run["status"] == "PASS", run
    candidate = next(row for row in run["candidate_rows"] if row["order_id"] == "O1005")
    assert candidate["duration_ms"] == int(seconds * 1000)
    assert candidate["sla_met"] is compliant
    assert run["metrics"]["compliant_count"] == (6 if compliant else 5)


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "-1", "not-a-number", "0.0001"])
def test_invalid_or_unrepresentable_duration_cannot_reach_publication(
    project_copy, new_guard, value
):
    path = project_copy / "data/feed_v2.csv"
    rows = read_csv(path)
    rows[0]["processing_duration"] = value
    write_csv(path, rows)
    refresh_manifest(project_copy, "feed_v2")
    guard = new_guard()
    run = guard.evaluate("corrected")
    assert_blocked(run)
    assert guard.publish(run["run_id"])["status"] == "BLOCKED"
    assert guard.snapshot()["active_release"] is None


def test_renamed_required_column_fails_structural_validation(project_copy, new_guard):
    path = project_copy / "data/feed_v2.csv"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("processing_duration", "duration", 1), encoding="utf-8")
    refresh_manifest(project_copy, "feed_v2")
    run = new_guard().evaluate("corrected")
    assert_blocked(run)
    assert run["structural_pass"] is False


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "wrong_order"])
def test_feed_must_cover_the_exact_authoritative_package_roster(
    project_copy, new_guard, fault
):
    path = project_copy / "data/feed_v2.csv"
    rows = read_csv(path)
    if fault == "missing":
        rows.pop()
    elif fault == "extra":
        rows.append({**rows[0], "record_id": "P-UNREGISTERED"})
    elif fault == "duplicate":
        rows.append(dict(rows[0]))
    else:
        rows[0]["order_id"] = "O1012"
    write_csv(path, rows)
    refresh_manifest(project_copy, "feed_v2")
    assert_blocked(new_guard().evaluate("corrected"))


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "wrong_order", "before_acceptance"])
def test_corrupt_reference_packages_fail_closed(project_copy, new_guard, fault):
    path = project_copy / "data/packages.csv"
    rows = read_csv(path)
    if fault == "missing":
        rows.pop()
    elif fault == "extra":
        rows.append({**rows[0], "package_id": "P-EXTRA"})
    elif fault == "duplicate":
        rows.append(dict(rows[0]))
    elif fault == "wrong_order":
        rows[0]["order_id"] = "O1012"
    else:
        rows[0]["completed_at"] = "2026-10-01T08:00:00Z"
    write_csv(path, rows)
    assert_blocked(new_guard().evaluate("corrected"))


@pytest.mark.parametrize("changes", [
    {"duration_unit": "minutes"},
    {"duration_unit": "seconds", "row_grain": "order", "semantics_version": "1.0"},
    {"duration_origin": "package.started_at"},
    {"cohort": "2026-10-02"},
    {"record_count": 999},
    {"sha256": "0" * 64},
])
def test_manifest_claims_are_verified_not_trusted_blindly(project_copy, new_guard, changes):
    path = project_copy / "data/feed_v2.manifest.json"
    manifest = read_json(path)
    manifest.update(changes)
    write_json(path, manifest)
    assert_blocked(new_guard().evaluate("corrected"))


@pytest.mark.parametrize("field,value", [
    ("approved_by", ""), ("approval_reference", ""),
    ("aggregation", "sum"), ("duration_origin", "package.started_at"),
    ("input_unit", "seconds"), ("output_grain", "package"),
    ("from_semantics_version", "999.0"),
])
def test_adapter_must_match_its_documented_semantics_and_approval(
    project_copy, new_guard, field, value
):
    path = project_copy / "contracts/adapter_v2.json"
    adapter = read_json(path)
    adapter[field] = value
    write_json(path, adapter)
    assert_blocked(new_guard().evaluate("corrected"))



if __name__ == "__main__":
    from pathlib import Path
    raise SystemExit(pytest.main([str(Path(__file__)), "-q", "--tb=short"]))
