"""Command-line workflow; expected blocked actions return exit status 2."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "sla_guard"

from .engine import ReleaseGuard, atomic_text
from .validation import read_csv
from support import ROOT, prepare_inputs, safe_text


def reset_owned_workspace(root: Path, workspace: Path):
    root, workspace = root.resolve(), workspace.resolve()
    if workspace == root or root.is_relative_to(workspace):
        raise ValueError("Refusing to reset the project or an ancestor directory")
    if not workspace.exists():
        return
    if not any(workspace.iterdir()):
        return
    marker = workspace / ".sla-guard-workspace"
    if not marker.is_file() or marker.read_text() != "sla-semantic-release-guard\n":
        raise ValueError("Reset is limited to a workspace created by this application")
    allowed = {".sla-guard-workspace", "sla_guard.sqlite", "sla_guard.sqlite-wal", "sla_guard.sqlite-shm", "candidates", "evaluations", "reports", "demo_summary.json"}
    unexpected = [p.name for p in workspace.iterdir() if p.name not in allowed or p.is_symlink()]
    if unexpected:
        raise ValueError(f"Refusing reset: unrelated entries present: {unexpected}")
    for path in workspace.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def run_demo(guard: ReleaseGuard) -> dict:
    if guard.snapshot()["runs"]:
        raise ValueError("Workspace already contains an investigation; use a new --workspace or demo --reset")
    baseline = guard.evaluate("baseline")
    first = guard.publish(baseline["run_id"])
    if first["status"] != "PUBLISHED":
        raise ValueError(f"Baseline was not published: {first}")
    before = guard.snapshot()
    changed = guard.evaluate("changed")
    denied = guard.publish(changed["run_id"])
    after = guard.snapshot()
    unchanged = before["active_release"] == after["active_release"] and before["active_rows"] == after["active_rows"]
    if not changed["structural_pass"] or denied["status"] != "BLOCKED" or not unchanged:
        raise ValueError("Expected semantic block and active-snapshot preservation were not demonstrated")
    corrected = guard.evaluate("corrected")
    expected = {r["order_id"]: r for r in read_csv(guard.project_root / "data/expected_orders.csv")}
    from decimal import Decimal
    oracle_match = len(corrected["candidate_rows"]) == len(expected) and all(
        r["order_id"] in expected and Decimal(r["processing_duration"]) == Decimal(expected[r["order_id"]]["processing_duration"])
        and r["sla_met"] == (expected[r["order_id"]]["sla_met"] == "1") for r in corrected["candidate_rows"])
    if not oracle_match:
        raise ValueError("Corrected output disagrees with the independent hand-calculated fixture")
    final = guard.publish(corrected["run_id"])
    if final["status"] != "PUBLISHED":
        raise ValueError(f"Corrected candidate was not published: {final}")
    reports = guard.export_report()
    summary = {"status": "DEMO_PASSED", "baseline_run": baseline["run_id"], "changed_run": changed["run_id"],
               "corrected_run": corrected["run_id"], "baseline_publication": first, "changed_publication": denied,
               "corrected_publication": final, "blocked_attempt_preserved_active_snapshot": unchanged,
               "independent_order_oracle_matched": oracle_match, "metrics": corrected["metrics"],
               "db_path": str(guard.db_path), "reports": reports}
    atomic_text(guard.workspace / "demo_summary.json", json.dumps(summary, indent=2))
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Investigate semantic data drift and gate local SLA releases.")
    parser.add_argument("--workspace", type=Path, help="Local investigation directory (default: .runtime/sessions/cli_demo)")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run and verify the complete three-scenario demonstration")
    demo.add_argument("--reset", action="store_true", help="Clear only an application-owned investigation workspace")
    evaluate = commands.add_parser("evaluate", help="Create a validation run; does not publish")
    evaluate.add_argument("scenario", choices=["baseline", "changed", "corrected"])
    publish = commands.add_parser("publish", help="Publish a passing, unchanged, non-stale candidate")
    publish.add_argument("run_id")
    rollback = commands.add_parser("rollback", help="Restore an intact previously published snapshot")
    rollback.add_argument("release_id")
    rollback.add_argument("--reason", required=True)
    commands.add_parser("status", help="Show active KPI and investigation history")
    commands.add_parser("report", help="Export the current investigation evidence")
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    workspace = (args.workspace or ROOT / ".runtime/sessions/cli_demo").resolve()
    try:
        root = prepare_inputs()
        if args.command == "demo" and args.reset:
            reset_owned_workspace(ROOT, workspace)
        guard = ReleaseGuard(root, workspace)
        if args.command == "demo":
            result = run_demo(guard)
        elif args.command == "evaluate":
            run = guard.evaluate(args.scenario)
            result = {k: run[k] for k in ("run_id", "scenario", "status", "structural_pass", "metrics")}
            result["failed_checks"] = [c for c in run["checks"] if c["status"] != "PASS"]
        elif args.command == "publish":
            result = guard.publish(args.run_id)
        elif args.command == "rollback":
            result = guard.rollback(args.release_id, args.reason)
        elif args.command == "report":
            result = guard.export_report()
        else:
            snap = guard.snapshot()
            result = {"db_path": snap["db_path"], "active_release": snap["active_release"], "releases": snap["releases"],
                      "runs": [{k: r[k] for k in ("run_id", "scenario", "status")} for r in snap["runs"]]}
        print(safe_text(json.dumps(result, indent=2)))
        return 2 if result.get("status") == "BLOCKED" else 0
    except (ValueError, OSError) as exc:
        print(safe_text(json.dumps({"status": "ERROR", "reason": str(exc)}, indent=2)))
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
