"""Explicit validation and release actions. Run --help; no action publishes implicitly."""
from __future__ import annotations
import argparse
import sys
from support import get_guard, show

def describe_run(run):
    return {"run_id": run["run_id"], "scenario": run["scenario"],
            "structural_pass": run["structural_pass"], "release_decision": run["status"],
            "metrics": run["metrics"],
            "checks": [{k: c[k] for k in ("check_id", "status", "message")} for c in run["checks"]],
            "candidate_rows": run["candidate_rows"]}

def latest_run(guard, scenario):
    runs = [run for run in guard.snapshot()["runs"] if run["scenario"] == scenario]
    if not runs:
        raise ValueError(f"Evaluate {scenario} first.")
    return runs[0]

def print_run(run):
    """Compact evidence for the recording; full details remain in the database."""
    print(f"Run: {run['run_id']} | Scenario: {run['scenario']}")
    print(f"Structural check: {'PASS' if run['structural_pass'] else 'FAIL'} | Release decision: {run['status']}")
    if run["metrics"]:
        m = run["metrics"]
        print(f"Orders: {m['order_count']} | Compliant: {m['compliant_count']} | Breached: {m['breach_count']}")
        print(f"Compliance: {m['compliance_rate']}% | Breach rate: {m['breach_rate']}%")
    else:
        print("No trustworthy canonical candidate is available for publication.")
    print("\nVALIDATION CHECKS")
    for check in run["checks"]:
        print(f"  {check['status']:4}  {check['check_id']}")
        if check["status"] != "PASS":
            print(f"        {check['message']}")
    if run["candidate_rows"]:
        print("\nORDER       SECONDS    DURATION_MS   SLA_MET   SOURCE_RECORDS")
        for row in run["candidate_rows"]:
            print(f"{row['order_id']:8} {row['processing_duration']:>10} {row['duration_ms']:>12} {str(row['sla_met']):>9} {row['source_record_count']:>16}")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default="take1", help="Use a new session for each take; old work is retained.")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("prepare", help="Require a fresh session before recording")
    for scenario in ("baseline", "changed", "corrected"):
        commands.add_parser(scenario, help=f"Evaluate {scenario}; do not publish")
    publish = commands.add_parser("publish")
    publish.add_argument("scenario", choices=("baseline", "changed", "corrected"))
    rollback = commands.add_parser("rollback")
    rollback.add_argument("scenario", choices=("baseline", "corrected"))
    rollback.add_argument("--reason", required=True)
    commands.add_parser("impact")
    commands.add_parser("status")
    commands.add_parser("export")
    if len(sys.argv) == 1:
        parser.print_help()
        return 0
    args = parser.parse_args()
    if args.action is None:
        parser.print_help()
        print("\nChoose an action, for example: python 03_workflow.py baseline")
        return 0
    guard = get_guard(args.session)
    if args.action == "prepare":
        snap = guard.snapshot()
        if snap["runs"] or snap["releases"]:
            raise ValueError("This session already contains work. Choose a new --session; nothing was reset.")
        show({"status": "READY", "session": args.session, "published_release": None, "validation_runs": 0})
    elif args.action in ("baseline", "changed", "corrected"):
        run = guard.evaluate(args.action)
        print_run(run)
        return 2 if run["status"] == "BLOCKED" else 0
    elif args.action == "publish":
        run = latest_run(guard, args.scenario)
        before = guard.snapshot()
        result = guard.publish(run["run_id"])
        after = guard.snapshot()
        result["selected_run_id"] = run["run_id"]
        if result["status"] == "BLOCKED":
            result["active_release_preserved"] = before["active_release"] == after["active_release"]
            result["published_rows_preserved"] = before["active_rows"] == after["active_rows"]
        show(result)
        return 2 if result["status"] == "BLOCKED" else 0
    elif args.action == "rollback":
        releases = [r for r in guard.snapshot()["releases"] if r["scenario"] == args.scenario]
        if not releases:
            raise ValueError(f"No published {args.scenario} snapshot exists.")
        result = guard.rollback(releases[0]["release_id"], args.reason)
        show(result)
        return 2 if result["status"] == "BLOCKED" else 0
    elif args.action == "impact":
        show({"purpose": "Hypothetical impact comparison; these are not publishable validation runs",
              "comparison": guard.snapshot()["comparison"]})
    elif args.action == "export":
        snap = guard.snapshot()
        if not snap["active_release"]:
            raise ValueError("Publish a validated release before exporting final evidence.")
        paths = guard.export_report()
        show({"active_scenario": snap["active_release"]["scenario"],
              "active_release_id": snap["active_release"]["release_id"], "reports": paths})
    else:
        snap = guard.snapshot()
        show({"active_release": snap["active_release"], "active_rows": snap["active_rows"],
              "releases": snap["releases"], "events": snap["events"]})
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        show({"status": "ERROR", "reason": str(exc)})
        raise SystemExit(1)
