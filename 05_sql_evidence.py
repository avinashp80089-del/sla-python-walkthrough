"""Inspect saved evidence with read-only SQL. This cannot publish or edit releases."""
from __future__ import annotations
import argparse
import sqlite3
import sys
from support import workspace, show

QUERIES = {
    "population": """SELECT COUNT(*) AS packages, COUNT(DISTINCT package_id) AS distinct_packages,
        COUNT(DISTINCT order_id) AS represented_orders FROM source_packages""",
    "orders": "SELECT COUNT(*) AS eligible_orders FROM source_orders",
    "expected": """WITH scored_orders AS (
        SELECT f.order_id, CAST(f.processing_duration AS INTEGER) AS duration_seconds,
            CAST(o.sla_seconds AS INTEGER) AS sla_seconds
        FROM source_feed_v1 f JOIN source_orders o ON o.order_id=f.order_id
    )
    SELECT COUNT(*) AS order_count,
        SUM(CASE WHEN duration_seconds <= sla_seconds THEN 1 ELSE 0 END) AS compliant_count,
        SUM(CASE WHEN duration_seconds > sla_seconds THEN 1 ELSE 0 END) AS breach_count,
        100.0 * SUM(CASE WHEN duration_seconds <= sla_seconds THEN 1 ELSE 0 END) / COUNT(*) AS compliance_rate,
        100.0 * SUM(CASE WHEN duration_seconds > sla_seconds THEN 1 ELSE 0 END) / COUNT(*) AS breach_rate
    FROM scored_orders""",
    "trace": """SELECT p.order_id, p.package_id, o.accepted_at, p.completed_at
        FROM source_packages p JOIN source_orders o ON o.order_id=p.order_id
        WHERE p.order_id IN ('O1002','O1009') ORDER BY p.order_id,p.package_id""",
    "kpi": "SELECT * FROM published_kpi",
    "rows": "SELECT order_id, processing_duration, duration_ms, sla_met, source_record_count FROM published_order_sla ORDER BY order_id",
    "active": "SELECT release_id, generation FROM active_release WHERE singleton=1",
    "history": "SELECT action,status,release_id,previous_release_id,reason FROM release_events ORDER BY event_id",
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", choices=(*QUERIES, "summary"), default="summary")
    parser.add_argument("--session", default="take1")
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()
    path = workspace(args.session) / "sla_guard.sqlite"
    if not path.is_file():
        raise SystemExit("Prepare the recording session first with 03_workflow.py prepare.")
    queries = ("orders", "population") if args.query == "summary" else (args.query,)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for query in queries:
            print(f"{query}: {QUERIES[query]}")
            show([dict(row) for row in db.execute(QUERIES[query])])

if __name__ == "__main__":
    main()
