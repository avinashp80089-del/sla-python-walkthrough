"""Validated immutable snapshots and a transactional active-release pointer.


"""
from __future__ import annotations

import csv
import html
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "sla_guard"

from . import __version__
from .validation import canonical_json, comparison, payload_hash, read_csv, sha256, validate


SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_runs (
 run_id TEXT PRIMARY KEY, scenario TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, observed_active_release_id TEXT,
 payload_json TEXT NOT NULL, payload_sha TEXT NOT NULL, candidate_sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_checks (
 run_id TEXT NOT NULL REFERENCES evaluation_runs(run_id), check_id TEXT NOT NULL,
 category TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
 expected_json TEXT, actual_json TEXT, PRIMARY KEY(run_id, check_id)
);
CREATE TABLE IF NOT EXISTS candidate_rows (
 run_id TEXT NOT NULL REFERENCES evaluation_runs(run_id), order_id TEXT NOT NULL,
 processing_duration TEXT NOT NULL, duration_ms INTEGER NOT NULL CHECK(duration_ms>=0),
 sla_ms INTEGER NOT NULL CHECK(sla_ms>=0), sla_met INTEGER NOT NULL CHECK(sla_met IN (0,1)),
 source_record_count INTEGER NOT NULL CHECK(source_record_count>0), PRIMARY KEY(run_id,order_id)
);
CREATE TABLE IF NOT EXISTS releases (
 release_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES evaluation_runs(run_id),
 scenario TEXT NOT NULL, created_at TEXT NOT NULL, metrics_json TEXT NOT NULL, rows_sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_rows (
 release_id TEXT NOT NULL REFERENCES releases(release_id), order_id TEXT NOT NULL,
 processing_duration TEXT NOT NULL, duration_ms INTEGER NOT NULL CHECK(duration_ms>=0),
 sla_ms INTEGER NOT NULL CHECK(sla_ms>=0), sla_met INTEGER NOT NULL CHECK(sla_met IN (0,1)),
 source_record_count INTEGER NOT NULL CHECK(source_record_count>0), PRIMARY KEY(release_id,order_id)
);
CREATE TABLE IF NOT EXISTS active_release (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), release_id TEXT REFERENCES releases(release_id),
 generation INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO active_release(singleton,release_id,generation) VALUES(1,NULL,0);
CREATE TABLE IF NOT EXISTS release_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
 action TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT, release_id TEXT,
 previous_release_id TEXT, reason TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS published_order_sla AS
 SELECT r.* FROM release_rows r JOIN active_release a ON r.release_id=a.release_id WHERE a.singleton=1;
CREATE VIEW IF NOT EXISTS published_kpi AS
 SELECT release_id,COUNT(*) AS order_count,SUM(sla_met) AS compliant_count,
 COUNT(*)-SUM(sla_met) AS breach_count,100.0*SUM(sla_met)/COUNT(*) AS compliance_rate,
 100.0*(COUNT(*)-SUM(sla_met))/COUNT(*) AS breach_rate,
 AVG(duration_ms)/1000.0 AS mean_duration_seconds
 FROM published_order_sla GROUP BY release_id;
"""
ROW_FIELDS = ["order_id", "processing_duration", "duration_ms", "sla_ms", "sla_met", "source_record_count"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temp.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def atomic_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class ReleaseGuard:
    def __init__(self, project_root: Path | None = None, workspace: Path | None = None):
        self.project_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
        self.workspace = (workspace or self.project_root / "state/demo").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / ".sla-guard-workspace").write_text("sla-semantic-release-guard\n")
        self.db_path = self.workspace / "sla_guard.sqlite"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        self._load_inspection_tables()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def _load_inspection_tables(self):
        """Convenience source views for DBeaver; source files remain evidence."""
        import pandas as pd
        with self.connection() as db:
            for name in ("orders", "packages", "feed_v1", "feed_v2"):
                path = self.project_root / "data" / f"{name}.csv"
                if path.is_file():
                    try:
                        frame = pd.read_csv(path, dtype="string", keep_default_na=False)
                        frame.to_sql("source_" + name, db, if_exists="replace", index=False)
                    except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
                        pass  # evaluate records failures; inspection convenience must not bypass it.

    @staticmethod
    def _active(db):
        return dict(db.execute("SELECT release_id,generation FROM active_release WHERE singleton=1").fetchone())

    @staticmethod
    def _event(db, action, status, reason, run_id=None, release_id=None, previous=None):
        db.execute("INSERT INTO release_events(created_at,action,status,run_id,release_id,previous_release_id,reason) VALUES(?,?,?,?,?,?,?)",
                   (now(), action, status, run_id, release_id, previous, reason))

    @staticmethod
    def _rows(db, table, key, value):
        # Table and key arguments only come from literals in this module.
        result = [dict(r) for r in db.execute(f"SELECT {','.join(ROW_FIELDS)} FROM {table} WHERE {key}=? ORDER BY order_id", (value,))]
        for row in result:
            row["sla_met"] = bool(row["sla_met"])
        return result

    def evaluate(self, scenario: str) -> dict:
        with self.connection() as db:
            observed = self._active(db)
        result = validate(self.project_root, scenario)
        result.update(run_id="run-" + uuid.uuid4().hex[:16], created_at=now(),
                      observed_active_release_id=observed["release_id"], observed_generation=observed["generation"],
                      engine_version=__version__)
        result["implementation_sha"] = payload_hash({name: sha256(Path(__file__).with_name(name)) for name in ("engine.py", "validation.py")})
        candidate_file = self.workspace / "candidates" / (result["run_id"] + "-candidate.csv")
        if result["candidate_rows"]:
            write_csv(candidate_file, result["candidate_rows"], ROW_FIELDS)
            result["artifacts"].append({"path": str(candidate_file), "sha256": sha256(candidate_file)})
        result["candidate_sha"] = payload_hash(result["candidate_rows"])
        encoded = canonical_json(result)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO evaluation_runs VALUES(?,?,?,?,?,?,?,?)", (result["run_id"], scenario, result["status"], result["created_at"], observed["release_id"], encoded, payload_hash(result), result["candidate_sha"]))
            db.executemany("INSERT INTO validation_checks VALUES(?,?,?,?,?,?,?)",
                           [(result["run_id"], c["check_id"], c["category"], c["status"], c["message"], canonical_json(c["expected"]), canonical_json(c["actual"])) for c in result["checks"]])
            db.executemany("INSERT INTO candidate_rows VALUES(?,?,?,?,?,?,?)",
                           [(result["run_id"], *(r[f] for f in ROW_FIELDS)) for r in result["candidate_rows"]])
            self._event(db, "EVALUATE", result["status"], "Validation completed; publication is a separate operation.", result["run_id"], previous=observed["release_id"])
            db.commit()
        atomic_text(self.workspace / "evaluations" / f"{result['run_id']}.json", json.dumps(result, indent=2))
        return result

    def _blocked(self, action: str, reason: str, run_id=None, target=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._active(db)["release_id"]
            self._event(db, action, "BLOCKED", reason, run_id, target, current)
            db.commit()
        return {"status": "BLOCKED", "reason": reason, "active_release_id": current}

    def publish(self, run_id: str) -> dict:
        try:
            with self.connection() as db:
                # Serialize publishers before checking the active pointer and inserting rows.
                db.execute("BEGIN IMMEDIATE")
                active = self._active(db)
                stored = db.execute("SELECT * FROM evaluation_runs WHERE run_id=?", (run_id,)).fetchone()
                if stored is None:
                    raise ValueError("Unknown evaluation run")
                result = json.loads(stored["payload_json"])
                if payload_hash(result) != stored["payload_sha"]:
                    raise ValueError("Evaluation record integrity check failed")
                if stored["status"] != "PASS" or result["status"] != "PASS":
                    raise ValueError("Evaluation is BLOCKED; structural success alone is insufficient")
                existing = db.execute("SELECT release_id FROM releases WHERE run_id=?", (run_id,)).fetchone()
                if existing:
                    if existing["release_id"] != active["release_id"]:
                        raise ValueError("This run was already released; use explicit rollback to restore historical data")
                    release = db.execute("SELECT rows_sha FROM releases WHERE release_id=?", (active["release_id"],)).fetchone()
                    if payload_hash(self._rows(db, "release_rows", "release_id", active["release_id"])) != release["rows_sha"]:
                        raise ValueError("Active release snapshot integrity check failed")
                    self._event(db, "PUBLISH", "NO_OP", "This validated run is already active.", run_id, active["release_id"], active["release_id"])
                    db.commit()
                    return {"status": "NO_OP", "reason": "This run is already active.", "active_release_id": active["release_id"], "release_id": active["release_id"]}
                if result["observed_active_release_id"] != active["release_id"] or result["observed_generation"] != active["generation"]:
                    raise ValueError("Stale evaluation: active release changed; evaluate again before publishing")
                implementation = payload_hash({name: sha256(Path(__file__).with_name(name)) for name in ("engine.py", "validation.py")})
                if result["implementation_sha"] != implementation:
                    raise ValueError("Validation implementation changed; evaluate again")
                for artifact in result["artifacts"]:
                    path = Path(artifact["path"])
                    if not path.is_file() or sha256(path) != artifact["sha256"]:
                        raise ValueError(f"Evidence changed after evaluation: {path.name}")
                rows = self._rows(db, "candidate_rows", "run_id", run_id)
                if not rows or payload_hash(rows) != stored["candidate_sha"] or rows != result["candidate_rows"]:
                    raise ValueError("Candidate snapshot integrity check failed")
                failed_checks = db.execute("SELECT COUNT(*) FROM validation_checks WHERE run_id=? AND status!='PASS'", (run_id,)).fetchone()[0]
                if failed_checks or not all(c["status"] == "PASS" for c in result["checks"]):
                    raise ValueError("At least one required validation check failed")
                release_id = "release-" + uuid.uuid4().hex[:16]
                db.execute("INSERT INTO releases VALUES(?,?,?,?,?,?)", (release_id, run_id, result["scenario"], now(), canonical_json(result["metrics"]), payload_hash(rows)))
                db.executemany("INSERT INTO release_rows VALUES(?,?,?,?,?,?,?)", [(release_id, *(r[f] for f in ROW_FIELDS)) for r in rows])
                db.execute("UPDATE active_release SET release_id=?,generation=generation+1 WHERE singleton=1", (release_id,))
                self._event(db, "PUBLISH", "PUBLISHED", "All checks and evidence hashes passed; snapshot and pointer committed together.", run_id, release_id, active["release_id"])
                db.commit()
                return {"status": "PUBLISHED", "reason": "Validated snapshot published atomically.", "active_release_id": release_id, "release_id": release_id}
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
            return self._blocked("PUBLISH", str(exc), run_id)

    def rollback(self, release_id: str, reason: str) -> dict:
        try:
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("A meaningful rollback reason is required")
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                active = self._active(db)
                target = db.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
                if target is None:
                    raise ValueError("Unknown historical release")
                rows = self._rows(db, "release_rows", "release_id", release_id)
                if not rows or payload_hash(rows) != target["rows_sha"]:
                    raise ValueError("Historical release snapshot integrity check failed")
                from .validation import metrics
                if metrics(rows) != json.loads(target["metrics_json"]):
                    raise ValueError("Historical KPI integrity check failed")
                status = "NO_OP" if active["release_id"] == release_id else "ROLLED_BACK"
                if status == "ROLLED_BACK":
                    db.execute("UPDATE active_release SET release_id=?,generation=generation+1 WHERE singleton=1", (release_id,))
                self._event(db, "ROLLBACK", status, reason.strip(), target["run_id"], release_id, active["release_id"])
                db.commit()
                return {"status": status, "reason": reason.strip(), "release_id": release_id, "active_release_id": release_id}
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
            return self._blocked("ROLLBACK", str(exc), target=release_id)

    def snapshot(self) -> dict:
        with self.connection() as db:
            db.execute("BEGIN")  # One consistent read across pointer, rows and history.
            active_id = self._active(db)["release_id"]
            releases = [dict(r) for r in db.execute("SELECT * FROM releases ORDER BY created_at DESC,release_id")]
            for release in releases:
                release["metrics"] = json.loads(release.pop("metrics_json"))
            runs = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM evaluation_runs ORDER BY created_at DESC,run_id")]
            active_rows = self._rows(db, "release_rows", "release_id", active_id) if active_id else []
            events = [dict(r) for r in db.execute("SELECT * FROM release_events ORDER BY event_id DESC")]
            db.commit()
        return {"active_release": next((r for r in releases if r["release_id"] == active_id), None),
                "active_rows": active_rows, "runs": runs, "releases": releases, "events": events,
                "comparison": comparison(self.project_root), "db_path": str(self.db_path)}

    def export_report(self) -> dict:
        snap = self.snapshot()
        folder = self.workspace / "reports"
        paths = {"summary_json": folder / "impact_report.json", "impact_csv": folder / "kpi_comparison.csv",
                 "html": folder / "impact_report.html", "active_csv": folder / "published_order_sla.csv"}
        # Export shareable evidence without the operator's personal home-directory path.
        def portable(value):
            if isinstance(value, dict):
                return {k: portable(v) for k, v in value.items()}
            if isinstance(value, list):
                return [portable(v) for v in value]
            if isinstance(value, str):
                return value.replace(str(self.project_root), "PROJECT_ROOT").replace(str(self.workspace), "WORKSPACE")
            return value
        atomic_text(paths["summary_json"], json.dumps(portable(snap), indent=2))
        write_csv(paths["impact_csv"], snap["comparison"], ["scenario", "label", "row_count", "compliant_count", "compliance_rate", "decision"])
        write_csv(paths["active_csv"], snap["active_rows"], ROW_FIELDS)
        esc = lambda value: html.escape(str(value))
        comparison_html = "".join(f"<tr><td>{esc(r['label'])}</td><td>{r['row_count']}</td><td>{r['compliant_count']}</td><td>{r['compliance_rate']:.2f}%</td><td>{esc(r['decision'])}</td></tr>" for r in snap["comparison"])
        checks_html = "".join(f"<tr><td>{esc(r['scenario'])}</td><td>{esc(c['check_id'])}</td><td class='{c['status']}'>{c['status']}</td><td>{esc(c['message'])}</td><td><code>{esc(c['actual'])}</code></td></tr>" for r in reversed(snap["runs"]) for c in r["checks"])
        active = snap["active_release"]
        active_label = f"{active['metrics']['compliance_rate']:.2f}% · {active['metrics']['order_count']} orders · {active['scenario']}" if active else "No published release"
        doc = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SLA semantic release audit</title>
<style>body{{font:16px/1.6 system-ui;color:#172d3b;background:#f4f7fa;margin:0}}main{{max-width:1180px;margin:auto;padding:40px 24px}}h1{{font-size:38px;line-height:1.15}}h2{{margin-top:36px}}.tag{{color:#087e8b;font-weight:700;letter-spacing:.08em}}.hero{{background:#fff;border-left:5px solid #087e8b;border-radius:12px;padding:24px;margin:20px 0}}table{{border-collapse:collapse;width:100%;font-size:14px;background:white}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dce4eb;vertical-align:top}}th{{background:#e6edf4}}code{{white-space:pre-wrap;word-break:break-word}}.PASS{{color:#08734a;font-weight:bold}}.FAIL{{color:#b42318;font-weight:bold}}.scroll{{overflow-x:auto}}small{{color:#596a78}}</style>
<main><p class="tag">SYNTHETIC CASE STUDY · VALIDATION EVIDENCE</p><h1>Same schema.<br>Different meaning.</h1><p>Order SLA release investigation. Units and entity grain are validated before publication.</p>
<div class="hero"><small>ACTIVE VALIDATED SNAPSHOT</small><h2>{esc(active_label)}</h2><p>{esc(active['release_id'] if active else 'Run the demonstration to establish a baseline.')}</p></div>
<h2>Why two fixes are necessary</h2><p>The two incorrect calculations below are hypothetical diagnostics. They are never published. The expected result is independently reconstructed from package completion timestamps.</p>
<div class="scroll"><table><tr><th>Calculation</th><th>Rows</th><th>Compliant</th><th>Rate</th><th>Interpretation</th></tr>{comparison_html}</table></div>
<h2>Validation trail</h2><div class="scroll"><table><tr><th>Scenario</th><th>Check</th><th>Result</th><th>Rule</th><th>Observed</th></tr>{checks_html}</table></div>
<h2>Publication authority</h2><p>The SQLite active-release pointer is authoritative. These files are derived reports. A blocked attempt does not alter the last accepted dataset. Snapshots and pointer changes are committed in a single transaction.</p>
<p><small>Generated {esc(now())}. Synthetic approval references describe this fixture only, not client approval. See the separate narration and setup guide for scope, assumptions, and reproduction steps.</small></p></main></html>"""
        atomic_text(paths["html"], doc)
        return {k: str(v) for k, v in paths.items()}


if __name__ == "__main__":
    print("Release engine: evaluate records evidence; publish rechecks it in a transaction; rollback restores a saved snapshot.")
    print("Run python 03_workflow.py --help for the controlled demonstration.")
