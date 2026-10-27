"""Shared paths and safe display helpers; run to check the prepared workspace."""
from __future__ import annotations
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def prepare_inputs():
    spec = importlib.util.spec_from_file_location("synthetic_inputs", ROOT / "02_inputs_and_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_inputs(ROOT / ".runtime" / "inputs")

def workspace(session="take1"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", session):
        raise ValueError("Session names use 1-48 letters, digits, underscores or hyphens.")
    return ROOT / ".runtime" / "sessions" / session

def get_guard(session="take1"):
    from sla_guard.engine import ReleaseGuard
    return ReleaseGuard(prepare_inputs(), workspace(session))

def safe_text(text):
    return str(text).replace(str(ROOT), "PROJECT_ROOT").replace(str(Path.home()), "HOME")

def show(value):
    print(safe_text(json.dumps(value, indent=2, default=str)))

if __name__ == "__main__":
    prepare_inputs()
    print("Synthetic inputs ready. Session data is kept under .runtime/sessions.")
