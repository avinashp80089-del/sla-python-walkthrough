"""Fail-closed semantic validation; numeric decisions use exact integer milliseconds.

The producer manifest is evidence, not sufficient proof. Durations and population
are also reconciled to a complete order/package ledger with independent timestamps.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a duration")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid number: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Duration must be finite and nonnegative")
    if result > Decimal("315360000000"):
        raise ValueError("Duration exceeds supported numeric range")
    return result


def to_ms(value: Any, unit: str) -> int:
    factor = {"seconds": 1000, "milliseconds": 1}.get(unit)
    if factor is None:
        raise ValueError(f"Unsupported unit: {unit}")
    result = number(value) * factor
    if result != result.to_integral_value():
        raise ValueError("Duration exceeds millisecond precision")
    return int(result)


def seconds_text(milliseconds: int) -> str:
    return format(Decimal(milliseconds) / Decimal(1000), "f")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    if result.microsecond % 1000:
        raise ValueError("Timestamp exceeds millisecond precision")
    return result.astimezone(timezone.utc)


def elapsed_ms(start: str, end: str) -> int:
    delta = timestamp(end) - timestamp(start)
    result = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    if result < 0:
        raise ValueError("Completion precedes acceptance")
    return result


def read_csv(path: Path) -> list[dict]:
    # Retain numeric lexemes: float inference must not round SLA boundaries.
    frame = pd.read_csv(path, dtype="string", keep_default_na=False)
    return frame.to_dict("records")


def metrics(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    total = len(rows)
    passed = sum(bool(r["sla_met"]) for r in rows)
    return {
        "order_count": total,
        "compliant_count": passed,
        "breach_count": total - passed,
        "compliance_rate": round(100 * passed / total, 6),
        "breach_rate": round(100 * (total - passed) / total, 6),
        "mean_duration_seconds": float(Decimal(sum(r["duration_ms"] for r in rows)) / total / 1000),
    }


def validate(root: Path, scenario: str) -> dict:
    if scenario not in {"baseline", "changed", "corrected"}:
        raise ValueError("Scenario must be baseline, changed, or corrected")
    feed_name = "feed_v1" if scenario == "baseline" else "feed_v2"
    paths = [root / "data" / f"{feed_name}.csv", root / "data" / f"{feed_name}.manifest.json",
             root / "data/orders.csv", root / "data/packages.csv", root / "contracts/sla_contract.json"]
    if scenario == "corrected":
        paths.append(root / "contracts/adapter_v2.json")
    checks: list[dict] = []

    def check(key: str, category: str, ok: bool, message: str, expected: Any, actual: Any):
        checks.append({"check_id": key, "category": category, "status": "PASS" if ok else "FAIL",
                       "message": message, "expected": expected, "actual": actual})

    artifacts = [{"path": str(p.resolve()), "sha256": sha256(p)} for p in paths if p.is_file()]
    rows: list[dict] = []
    try:
        check("inputs.present", "integrity", all(p.is_file() for p in paths),
              "Every required evidence file must exist.", len(paths), len(artifacts))
        if len(artifacts) != len(paths):
            raise ValueError("Missing required input")
        contract = json.loads(paths[4].read_text())
        manifest = json.loads(paths[1].read_text())
        if not isinstance(contract, dict) or not isinstance(manifest, dict):
            raise ValueError("Contract and manifest must be JSON objects")
        feed = read_csv(paths[0])
        orders_raw, packages_raw = read_csv(paths[2]), read_csv(paths[3])
        contract_columns = [{"name": "record_id", "type": "string"},
                            {"name": "order_id", "type": "string"},
                            {"name": "processing_duration", "type": "number"}]
        supported = {
            "duration_unit": "seconds", "row_grain": "order", "duration_origin": "order.accepted_at",
            "sla_inclusive": True, "precision": "milliseconds", "aggregation": "max",
        }
        contract_ok = all(contract.get(k) == v for k, v in supported.items()) and contract.get("columns") == contract_columns
        check("contract.supported", "contract", contract_ok,
              "Only the explicitly implemented SLA contract may be evaluated.", supported,
              {k: contract.get(k) for k in supported})
        with paths[0].open(newline="") as file:
            header = next(csv.reader(file), [])
        expected_columns = [c["name"] for c in contract_columns]
        columns_ok = header == expected_columns
        check("structure.columns", "structure", columns_ok,
              "Names, order and number of columns must match; extra fields are not silently dropped.", expected_columns, header)
        type_errors = []
        if columns_ok:
            for index, item in enumerate(feed, 2):
                try:
                    if not item["record_id"].strip() or not item["order_id"].strip():
                        raise ValueError("Empty identifier")
                    number(item["processing_duration"])
                except (KeyError, ValueError) as exc:
                    type_errors.append(f"row {index}: {exc}")
        else:
            type_errors.append("Header mismatch prevents type validation")
        check("structure.types", "structure", bool(feed) and not type_errors,
              "Identifiers are nonempty strings; duration is a finite nonnegative number.", "nonempty, typed records", type_errors or len(feed))
        check("manifest.binding", "integrity", manifest.get("sha256") == sha256(paths[0]) and manifest.get("record_count") == len(feed),
              "Producer metadata must refer to these exact bytes and row count.",
              {"sha256": sha256(paths[0]), "record_count": len(feed)},
              {"sha256": manifest.get("sha256"), "record_count": manifest.get("record_count")})
        check("manifest.schema", "structure", manifest.get("schema_version") == "1.0",
              "Both producer versions intentionally retain schema version 1.0.", "1.0", manifest.get("schema_version"))
        check("manifest.producer", "semantics", manifest.get("producer") == "synthetic-fulfillment",
              "The source must be the contracted synthetic producer.", "synthetic-fulfillment", manifest.get("producer"))
        check("manifest.origin", "semantics", manifest.get("duration_origin") == "order.accepted_at",
              "All package durations share the parent order's acceptance instant.", "order.accepted_at", manifest.get("duration_origin"))
        check("manifest.cohort", "population", manifest.get("cohort") == contract.get("cohort"),
              "The candidate and contract must describe the same closed cohort.", contract.get("cohort"), manifest.get("cohort"))
        if not contract_ok or not columns_ok or type_errors or not feed:
            raise ValueError("Structural or contract failure prevents trustworthy calculation")

        orders, packages, reference_errors = {}, {}, []
        for item in orders_raw:
            try:
                oid = item["order_id"]
                if not oid or oid in orders:
                    raise ValueError("Empty or duplicate order key")
                accepted = timestamp(item["accepted_at"])
                if accepted.date().isoformat() != contract["cohort"]:
                    raise ValueError("Order outside contracted acceptance cohort")
                expected_count = int(item["expected_package_count"])
                if expected_count < 1 or to_ms(item["sla_seconds"], "seconds") != to_ms(contract["sla_seconds"], "seconds"):
                    raise ValueError("Invalid package count or inconsistent SLA threshold")
                orders[oid] = {**item, "expected_count": expected_count, "sla_ms": to_ms(item["sla_seconds"], "seconds")}
            except (KeyError, ValueError, TypeError) as exc:
                reference_errors.append(f"order {item.get('order_id')}: {exc}")
        for item in packages_raw:
            try:
                pid, oid = item["package_id"], item["order_id"]
                if not pid or pid in packages or oid not in orders:
                    raise ValueError("Invalid, duplicate, or orphan package key")
                packages[pid] = {**item, "duration_ms": elapsed_ms(orders[oid]["accepted_at"], item["completed_at"])}
            except (KeyError, ValueError, TypeError) as exc:
                reference_errors.append(f"package {item.get('package_id')}: {exc}")
        counts = Counter(p["order_id"] for p in packages.values())
        for oid, order in orders.items():
            if counts[oid] != order["expected_count"]:
                reference_errors.append(f"{oid}: expected {order['expected_count']} required packages, found {counts[oid]}")
        check("reference.complete", "population", bool(orders) and not reference_errors,
              "Authoritative order/package evidence must be unique, complete and temporally valid.", "all reference checks pass", reference_errors or {"orders": len(orders), "packages": len(packages)})
        if reference_errors or not orders:
            raise ValueError("Reference evidence is incomplete or invalid")

        ids = [r["record_id"] for r in feed]
        check("records.unique", "population", len(ids) == len(set(ids)),
              "Duplicate source records are rejected, not silently deduplicated.", len(ids), len(set(ids)))
        input_unit, input_grain = manifest.get("duration_unit"), manifest.get("row_grain")
        check("manifest.recognized", "semantics", input_unit in {"seconds", "milliseconds"} and input_grain in {"order", "package"},
              "Unrecognized semantics require a new reviewed mapping.", "seconds/order or milliseconds/package", f"{input_unit}/{input_grain}")
        if scenario == "corrected":
            adapter = json.loads(paths[5].read_text())
            if not isinstance(adapter, dict):
                raise ValueError("Adapter must be a JSON object")
            adapter_expected = {"adapter_id": "package-ms-to-order-seconds-v1", "producer": "synthetic-fulfillment",
                                "from_semantics_version": "2.0", "input_unit": "milliseconds", "input_grain": "package",
                                "output_unit": "seconds", "output_grain": "order", "duration_origin": "order.accepted_at",
                                "aggregation": "max", "approved_by": "Synthetic Data Owner",
                                "approval_reference": "SYNTHETIC-ADR-001", "synthetic": True}
            adapter_ok = all(adapter.get(k) == v for k, v in adapter_expected.items())
            adapter_ok = adapter_ok and manifest.get("semantics_version") == adapter.get("from_semantics_version")
            adapter_ok = adapter_ok and input_unit == adapter.get("input_unit") and input_grain == adapter.get("input_grain")
            check("adapter.authorized", "semantics", adapter_ok,
                  "Correction is permitted only by the declared synthetic mapping decision; it is not inferred from value distributions.", adapter_expected, adapter)
            if not adapter_ok:
                raise ValueError("Unrecognized or unapproved adapter mapping")
        else:
            check("semantics.unit", "semantics", input_unit == contract["duration_unit"],
                  "Raw producer units must match the consumer contract.", contract["duration_unit"], input_unit)
            check("semantics.grain", "semantics", input_grain == contract["row_grain"],
                  "The SLA denominator is orders, never packages.", contract["row_grain"], input_grain)
            check("semantics.version", "semantics", manifest.get("semantics_version") == "1.0",
                  "A changed producer semantic version needs an explicit adapter.", "1.0", manifest.get("semantics_version"))

        expect_packages = scenario == "corrected"
        expected_ids = set(packages) if expect_packages else set(orders)
        mapping_errors = []
        if set(ids) != expected_ids:
            mapping_errors.append({"missing": sorted(expected_ids - set(ids)), "unexpected": sorted(set(ids) - expected_ids)})
        for item in feed:
            expected_order = packages.get(item["record_id"], {}).get("order_id") if expect_packages else item["record_id"]
            if item["order_id"] not in orders or item["order_id"] != expected_order:
                mapping_errors.append(f"{item['record_id']}: invalid order association")
        check("population.keys", "population", not mapping_errors and len(ids) == len(expected_ids),
              "Every contracted entity must appear exactly once with its correct parent key.", len(expected_ids), mapping_errors or len(ids))

        reference_order_ms = {oid: max(p["duration_ms"] for p in packages.values() if p["order_id"] == oid) for oid in orders}
        timing_errors = []
        for item in feed:
            try:
                observed = to_ms(item["processing_duration"], input_unit)
                if input_grain == "package":
                    anchor = packages[item["record_id"]]
                    expected_ms = anchor["duration_ms"]
                    if anchor["order_id"] != item["order_id"]:
                        raise ValueError("Wrong parent order")
                else:
                    expected_ms = reference_order_ms[item["order_id"]]
                if observed != expected_ms:
                    raise ValueError(f"declared duration {observed}ms != timestamp evidence {expected_ms}ms")
            except (KeyError, ValueError) as exc:
                timing_errors.append(f"{item['record_id']}: {exc}")
        check("semantics.timestamp_evidence", "semantics", not timing_errors,
              "Declared units are independently reconciled against acceptance/completion timestamps.", "exact millisecond agreement", timing_errors or "all source durations match")

        if mapping_errors or len(ids) != len(expected_ids) or timing_errors:
            raise ValueError("Source keys or durations cannot support a canonical candidate")
        if expect_packages:
            frame = pd.DataFrame([{"order_id": item["order_id"], "duration_ms": to_ms(item["processing_duration"], "milliseconds")} for item in feed])
            aggregated = frame.groupby("order_id", sort=True)["duration_ms"].agg(["max", "count"])
            normalized = [(str(oid), int(item["max"]), int(item["count"])) for oid, item in aggregated.iterrows()]
        else:
            normalized = [(item["order_id"], to_ms(item["processing_duration"], "seconds"), 1) for item in feed]
        rows = [{"order_id": oid, "processing_duration": seconds_text(ms), "duration_ms": ms,
                 "sla_ms": orders[oid]["sla_ms"], "sla_met": ms <= orders[oid]["sla_ms"], "source_record_count": count}
                for oid, ms, count in sorted(normalized)]
        check("canonical.population", "population", len(rows) == len(orders) and {r["order_id"] for r in rows} == set(orders),
              "The corrected denominator must equal the complete eligible order population.", len(orders), len(rows))
        canonical_errors = [r["order_id"] for r in rows if r["duration_ms"] != reference_order_ms[r["order_id"]]]
        check("canonical.duration", "business", not canonical_errors,
              "Order completion uses the latest required package measured from a common order start.", "max required-package elapsed time", canonical_errors or "all orders match")
        check("canonical.sla", "business", all(r["sla_met"] == (reference_order_ms[r["order_id"]] <= orders[r["order_id"]]["sla_ms"]) for r in rows),
              "Exactly-at-threshold orders comply; values above it breach.", "duration_ms <= sla_ms", "exact integer comparison")
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        check("calculation.available", "business", False,
              "Publication is blocked when the candidate cannot be calculated reliably.", "complete, valid evidence", str(exc))
    structural = [c for c in checks if c["category"] == "structure"]
    passed = bool(checks) and all(c["status"] == "PASS" for c in checks) and bool(rows)
    # Capture drift during evaluation as well as between evaluation and publication.
    stable = all(Path(a["path"]).is_file() and sha256(Path(a["path"])) == a["sha256"] for a in artifacts)
    check("inputs.stable", "integrity", stable, "Evidence must not change during validation.", "same hashes", stable)
    return {"scenario": scenario, "status": "PASS" if passed and stable else "BLOCKED",
            "structural_pass": bool(structural) and all(c["status"] == "PASS" for c in structural),
            "checks": checks, "metrics": metrics(rows), "candidate_rows": rows, "artifacts": artifacts}


def comparison(root: Path) -> list[dict]:
    """Descriptive counterfactuals; not candidates and never accepted by publish()."""
    try:
        orders = {r["order_id"]: r for r in read_csv(root / "data/orders.csv")}
        v1, v2 = read_csv(root / "data/feed_v1.csv"), read_csv(root / "data/feed_v2.csv")
        if not orders or not v1 or not v2:
            return []
        package_rows = read_csv(root / "data/packages.csv")
        reference = []
        for oid, order in orders.items():
            durations = [elapsed_ms(order["accepted_at"], p["completed_at"]) for p in package_rows if p["order_id"] == oid]
            if len(durations) != int(order["expected_package_count"]):
                raise ValueError("Incomplete reference population")
            reference.append({"order_id": oid, "processing_duration": seconds_text(max(durations))})
        result = []
        for code, label, records, unit, decision in [
            ("baseline", "Original order-level feed", v1, "seconds", "Reference preview"),
            ("changed", "Wrong units + wrong denominator", v2, "seconds", "Blocked: hypothetical calculation"),
            ("units_only", "Units fixed, package denominator retained", v2, "milliseconds", "Not publishable: wrong grain"),
            ("corrected", "Expected order-level result", reference, "seconds", "Reference target; candidate must pass validation"),
        ]:
            passed = sum(to_ms(r["processing_duration"], unit) <= to_ms(orders[r["order_id"]]["sla_seconds"], "seconds") for r in records)
            result.append({"scenario": code, "label": label, "row_count": len(records), "compliant_count": passed,
                           "compliance_rate": round(100 * passed / len(records), 6), "decision": decision})
        return result
    except (ValueError, KeyError, OSError, TypeError, pd.errors.ParserError, pd.errors.EmptyDataError):
        return []


if __name__ == "__main__":
    for value in ("3600", "3600.001"):
        milliseconds = to_ms(value, "seconds")
        print(f"{value} seconds -> {milliseconds} ms; SLA met: {milliseconds <= 3_600_000}")
